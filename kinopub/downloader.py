"""Download manager: queue, workers, resumable HTTP transfers, HLS via ffmpeg."""
import hashlib
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import http.client
import socket
import urllib.error
import urllib.parse
import urllib.request

from . import config, hls_mirror, store

CHUNK = 1024 * 256
PROGRESS_INTERVAL = 0.7
UA = "KinoPubOffline/1.0"
# a flaky hotel/airport wifi should not lose a download (tests shorten this)
MAX_ATTEMPTS = int(os.environ.get("KP_MAX_ATTEMPTS") or 8)   # ~3 min of backoff in total
BACKOFF_CAP = 60
DISK_MARGIN = 500 * 1024 * 1024   # keep the boot volume breathing room

class TransferTruncated(Exception):
    """The connection ended before Content-Length bytes arrived."""


class NotEnoughSpace(Exception):
    """The file will not fit on the library volume."""


_controls = {}          # entry_id -> {"stop": Event, "reason": str}
_children = set()       # ffmpeg Popen objects we started
_children_lock = threading.Lock()
HLS_STALL_SECONDS = 90  # no growth of the part file for this long = hung on a dead host


def _register(proc):
    with _children_lock:
        _children.add(proc)


def _unregister(proc):
    with _children_lock:
        _children.discard(proc)


def kill_children():
    """Stop every ffmpeg we started. Called on shutdown so none outlive the app."""
    with _children_lock:
        procs = list(_children)
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.terminate()
        except OSError:
            pass
    for proc in procs:
        try:
            proc.wait(timeout=5)
        except Exception:                      # noqa: BLE001 - be firm
            try:
                proc.kill()
            except OSError:
                pass


def sweep_orphans():
    """Kill ffmpeg processes from a previous run that are still writing into the library.

    A crashed or replaced server leaves its ffmpeg children behind; they keep
    downloading into .part files nobody tracks and eat the bandwidth the live
    downloads need. Anything writing a .part.mp4 under our library is ours.
    """
    library = os.path.realpath(config.get("library_dir"))
    killed = 0
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
    except Exception:                          # noqa: BLE001
        return 0
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2 or "ffmpeg" not in parts[1] or ".part.mp4" not in parts[1]:
            continue
        if library not in parts[1] and config.get("library_dir") not in parts[1]:
            continue
        try:
            pid = int(parts[0])
            if pid == os.getpid():
                continue
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except (ValueError, OSError):
            pass
    return killed
_controls_lock = threading.Lock()
_wake = threading.Event()
_started = False


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def safe_name(value, limit=120):
    value = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", str(value or "")).strip()
    value = re.sub(r"\s{2,}", " ", value)
    return (value[:limit] or "untitled").rstrip(". ")


def which_ffmpeg():
    for candidate in (shutil.which("ffmpeg"), os.path.expanduser("~/bin/ffmpeg"), "/opt/homebrew/bin/ffmpeg"):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def human_path(entry):
    """Absolute path of the media file for an entry."""
    return os.path.join(config.get("library_dir"), entry["rel_path"])


def free_space(path=None):
    """Bytes free on the volume that holds the library (walks up to a real dir)."""
    target = path or config.get("library_dir")
    while target and not os.path.isdir(target):
        parent = os.path.dirname(target)
        if parent == target:
            break
        target = parent
    try:
        return shutil.disk_usage(target or "/").free
    except OSError:
        return 0


def _check_space(needed, path=None):
    if not needed:
        return
    available = free_space(path)
    if available and needed + DISK_MARGIN > available:
        raise NotEnoughSpace("needs %.1f GB, %.1f GB free" % (
            needed / 1024 ** 3, available / 1024 ** 3))


def _open(url, offset=0):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if offset:
        req.add_header("Range", "bytes=%d-" % offset)
    return urllib.request.urlopen(req, timeout=60)


def cache_poster(url):
    """Fetch a poster into the local cache so the library works offline."""
    if not url:
        return None
    name = hashlib.sha1(url.encode("utf-8")).hexdigest() + ".jpg"
    path = os.path.join(config.POSTER_CACHE, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return name
    try:
        os.makedirs(config.POSTER_CACHE, exist_ok=True)
        with _open(url) as resp, open(path, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        return name
    except Exception:
        return None


def _srt_to_vtt(text):
    text = text.replace("\r\n", "\n")
    text = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text)
    return "WEBVTT\n\n" + text


def download_subtitles(entry, subs):
    saved = []
    base_dir = os.path.dirname(human_path(entry))
    stem = os.path.splitext(os.path.basename(entry["rel_path"]))[0]
    for index, sub in enumerate(subs or []):
        url = sub.get("url")
        if not url:
            continue
        lang = safe_name(sub.get("lang") or "sub", 20)
        try:
            with _open(url) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except Exception:
            continue
        if not raw.lstrip().upper().startswith("WEBVTT"):
            raw = _srt_to_vtt(raw)
        name = "%s.%s%s.vtt" % (stem, lang, "" if index == 0 else str(index))
        try:
            os.makedirs(base_dir, exist_ok=True)
            with open(os.path.join(base_dir, name), "w", encoding="utf-8") as fh:
                fh.write(raw)
        except OSError:
            continue
        saved.append({"lang": sub.get("lang") or lang, "file": name})
    return saved


# --------------------------------------------------------------------------- #
# transfer strategies
# --------------------------------------------------------------------------- #
def _report(entry_id, done, total, started_at, base):
    elapsed = max(time.time() - started_at, 0.001)
    speed = (done - base) / elapsed
    store.update(entry_id, {
        "downloaded_bytes": done,
        "total_bytes": total,
        "progress": round(done / total, 4) if total else 0.0,
        "speed": int(speed),
        "eta": int((total - done) / speed) if speed > 0 and total else 0,
    }, flush=False)


def _http_download(entry, stop):
    path = human_path(entry)
    part = path + ".part"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    offset = os.path.getsize(part) if os.path.exists(part) else 0

    try:
        resp = _open(entry["source_url"], offset)
    except urllib.error.HTTPError as exc:
        if exc.code in (416, 400) and offset:      # stale partial file
            os.remove(part)
            offset = 0
            resp = _open(entry["source_url"], 0)
        else:
            raise

    with resp:
        # connection is up again: drop any stale "reconnecting" notice
        store.update(entry["id"], {"error": None, "attempt": 0}, flush=False)
        if offset and resp.status != 206:          # server ignored Range
            offset = 0
        total = int(resp.headers.get("Content-Length") or 0) + offset
        _check_space(total - offset, os.path.dirname(path))
        mode = "ab" if offset else "wb"
        done = offset
        started_at = time.time()
        last = 0.0
        own = entry_limiter(entry)
        shared = global_limiter()
        with open(part, mode) as fh:
            while True:
                if stop.is_set():
                    fh.flush()
                    return False
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                own.take(len(chunk), stop)
                shared.take(len(chunk), stop)
                fh.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last > PROGRESS_INTERVAL:
                    _report(entry["id"], done, total, started_at, offset)
                    last = now
    if total and done < total:
        # EOF before the whole file arrived: never publish a truncated video.
        raise TransferTruncated("received %d of %d bytes" % (done, total))
    os.replace(part, path)
    store.update(entry["id"], {"downloaded_bytes": done, "total_bytes": done, "progress": 1.0, "speed": 0})
    return True


def _readrate_for(entry):
    """Translate a bytes-per-second cap into ffmpeg's realtime multiplier."""
    limit = effective_limit_bytes(entry)
    if not limit:
        return None
    seconds = float(entry.get("duration") or 0)
    size = float(entry.get("total_bytes") or 0)
    if not size:
        size = _probe_size(entry)
    if not seconds or not size:
        return None
    stream_rate = size / seconds                 # bytes per second of video
    if stream_rate <= 0:
        return None
    return max(limit / stream_rate, 0.5)


def _probe_size(entry):
    """Ask the direct file how big it is, to estimate the stream's bitrate."""
    url = (entry.get("urls") or {}).get("http")
    if not url:
        return 0
    try:
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=15) as response:
            return int(response.headers.get("Content-Length") or 0)
    except Exception:                            # noqa: BLE001 - only an estimate
        return 0


def _probe_variant(playlist_url, timeout, sample=64 * 1024):
    """Time the first bytes of a variant's first segment; None if the host is dead."""
    started = time.time()
    try:
        request = urllib.request.Request(playlist_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            playlist = response.read().decode("utf-8", "replace")
        first = next((l.strip() for l in playlist.splitlines() if l.strip() and not l.startswith("#")), None)
        if not first:
            return None
        segment = urllib.parse.urljoin(playlist_url, first)
        request = urllib.request.Request(segment, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            got = len(response.read(sample))
        if not got:
            return None
        return got / max(time.time() - started, 0.05)
    except Exception:                          # noqa: BLE001 - a dead host is the finding
        return None


def choose_hls_master(master_url, attempts=3, probe_timeout=8, fetch=None):
    """Return the text of a master playlist trimmed to variants whose host answers.

    Each quality is served from a different CDN host and any of them can be
    dead; ffmpeg would sit on it. Hosts are drawn afresh on every fetch of the
    master, so when every host is dead we draw again. None means give up and
    use the original.
    """
    fetch = fetch or (lambda u: urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent": UA}), timeout=20).read().decode("utf-8", "replace"))
    for _ in range(attempts):
        try:
            text = fetch(master_url)
        except Exception:                      # noqa: BLE001 - try again
            continue
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        header, media, variants = [], [], []
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("#EXT-X-STREAM-INF"):
                attrs = line[len("#EXT-X-STREAM-INF:"):]
                uri = urllib.parse.urljoin(master_url, lines[i + 1] if i + 1 < len(lines) else "")
                bandwidth = int((re.search(r"BANDWIDTH=(\d+)", attrs) or [None, 0])[1] or 0)
                group = (re.search(r'AUDIO="([^"]+)"', attrs) or [None, None])[1]
                variants.append({"attrs": attrs, "uri": uri, "bandwidth": bandwidth, "group": group})
                i += 2
                continue
            if line.startswith("#EXT-X-MEDIA:"):
                group = (re.search(r'GROUP-ID="([^"]+)"', line) or [None, None])[1]
                absolute = re.sub(r'URI="([^"]+)"',
                                  lambda m: 'URI="%s"' % urllib.parse.urljoin(master_url, m.group(1)), line)
                media.append((group, absolute))
            elif line.startswith("#"):
                header.append(line)
            i += 1
        if len(variants) < 2:
            return None
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(variants)) as pool:
            speeds = list(pool.map(lambda v: _probe_variant(v["uri"], probe_timeout), variants))
        healthy = [(v, sp) for v, sp in zip(variants, speeds) if sp]
        if not healthy:
            continue
        sustains = lambda v, sp: sp >= (v["bandwidth"] / 8.0) * 1.2  # noqa: E731
        healthy.sort(key=lambda x: (0 if sustains(*x) else 1,
                                    -x[0]["bandwidth"] if sustains(*x) else -x[1]))
        keep = [v for v, _ in healthy]
        groups = {v["group"] for v in keep if v["group"]}
        out = list(header)
        out += [line for group, line in media if not group or group in groups]
        for v in keep:
            out.append("#EXT-X-STREAM-INF:" + v["attrs"])
            out.append(v["uri"])
        return "\n".join(out) + "\n"
    return None


def hls_command(ffmpeg, source, part, readrate=None, local=False):
    """The ffmpeg argv for an HLS download.

    With a *local* trimmed playlist as the input, ffmpeg rejects the http-only
    options outright ("Option user_agent not found", likewise the reconnect
    flags) - only -rw_timeout is accepted there. Verified against ffmpeg 6.0.
    """
    cmd = [ffmpeg, "-y", "-loglevel", "error",
           "-rw_timeout", "20000000"]          # 20s: a socket that goes quiet errors out
    if local:
        cmd += ["-protocol_whitelist", "file,http,https,tcp,tls,crypto"]
    else:
        cmd += ["-user_agent", UA,
                "-reconnect_on_network_error", "1", "-reconnect_delay_max", "10"]
    if readrate:
        # ffmpeg paces by playback speed, so a byte cap becomes a multiplier
        cmd += ["-readrate", "%.2f" % readrate]
    cmd += ["-i", source, "-c", "copy", "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", part]
    return cmd


def _hls_download(entry, stop):
    ffmpeg = which_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found — install it (brew install ffmpeg) or switch stream type to http")
    path = human_path(entry)
    part = os.path.splitext(path)[0] + ".part.mp4"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    duration = float(entry.get("duration") or 0)
    # Only retry at connect time. `-reconnect`/`-reconnect_streamed` resume a
    # dropped segment mid-way with a byte range, and this CDN answers those
    # unreliably - the result was files with garbage spliced into the stream.
    # pick hosts that answer before ffmpeg commits to one
    source = entry["source_url"]
    trimmed = choose_hls_master(source)
    local = False
    if trimmed:
        source = os.path.splitext(path)[0] + ".master.m3u8"
        with open(source, "w", encoding="utf-8") as handle:
            handle.write(trimmed)
        local = True
    cmd = hls_command(ffmpeg, source, part, _readrate_for(entry), local)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _register(proc)
    store.update(entry["id"], {"error": None, "attempt": 0}, flush=False)
    started_at = time.time()

    # ffmpeg blocked on a dead host prints nothing, so the stdout loop below
    # would wait forever; a side thread watches the part file instead.
    hung = {"flag": False}

    def watchdog():
        last_size, last_change = -1, time.time()
        while proc.poll() is None and not stop.is_set():
            time.sleep(5)
            size = os.path.getsize(part) if os.path.exists(part) else 0
            if size != last_size:
                last_size, last_change = size, time.time()
            elif time.time() - last_change > HLS_STALL_SECONDS:
                hung["flag"] = True
                try:
                    proc.terminate()
                except OSError:
                    pass
                return

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        for line in proc.stdout:
            if stop.is_set():
                proc.terminate()
                proc.wait(timeout=10)
                return False
            if line.startswith("out_time_ms=") and duration:
                seconds = int(line.strip().split("=")[1] or 0) / 1000000.0
                size = os.path.getsize(part) if os.path.exists(part) else 0
                store.update(entry["id"], {
                    "progress": round(min(seconds / duration, 0.999), 4),
                    "downloaded_bytes": size,
                    "speed": int(size / max(time.time() - started_at, 0.001)),
                }, flush=False)
    finally:
        if proc.poll() is None:
            proc.wait()
        _unregister(proc)
    if hung["flag"]:
        raise socket.timeout("no data for %ds - the host went quiet" % HLS_STALL_SECONDS)
    if proc.returncode != 0:
        # the last line is the real failure; earlier ones are connection chatter
        lines = [l for l in (proc.stderr.read() or "").splitlines()
                 if l.strip() and "Cannot reuse HTTP connection" not in l]
        raise RuntimeError((lines[-1] if lines else "ffmpeg exited with an error")[:300])
    os.replace(part, path)
    try:
        os.remove(os.path.splitext(path)[0] + ".master.m3u8")
    except OSError:
        pass
    size = os.path.getsize(path)
    store.update(entry["id"], {"progress": 1.0, "total_bytes": size, "downloaded_bytes": size, "speed": 0})
    return True


# --------------------------------------------------------------------------- #
# worker
# --------------------------------------------------------------------------- #
SEGMENT_TIMEOUT = 30          # per socket operation; a slow host is fine, a silent one is not
SEGMENT_TRIES = 4             # per segment, before drawing other hosts
MAX_REDRAWS = 4               # host re-draws after failures, per run
SLOW_SEGMENT_SECONDS = 60     # two segments this slow in a row: try other hosts


def work_dir(entry):
    """Hidden folder next to the film where its segments are mirrored."""
    folder, name = os.path.split(human_path(entry))
    return os.path.join(folder, "." + os.path.splitext(name)[0] + ".hls")


def _fetch_text(url, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def _resolve_plan(entry, fresh_link=False):
    """Segment lists for this entry's quality, from a freshly fetched master.

    Every fetch of the master draws new CDN hosts, which is how a dead one is
    escaped. An expired link (the API issues them for 24h) is renewed once.
    """
    if fresh_link:
        _refresh_source(entry["id"])
        entry = store.get(entry["id"]) or entry
    master_url = entry["source_url"]
    try:
        text = _fetch_text(master_url)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404, 410) and not fresh_link:
            return _resolve_plan(entry, fresh_link=True)
        raise
    master = hls_mirror.parse_master(text, master_url)
    if not master["variants"]:                  # already a media playlist
        return {"video": hls_mirror.parse_media(text, master_url), "audio": None, "bandwidth": 0}
    variant = hls_mirror.pick_variant(master["variants"], entry.get("quality"))
    video = hls_mirror.parse_media(_fetch_text(variant["uri"]), variant["uri"])
    rendition = hls_mirror.pick_audio(master["audio"], variant["audio"])
    audio = hls_mirror.parse_media(_fetch_text(rendition["uri"]), rendition["uri"]) if rendition else None
    return {"video": video, "audio": audio, "bandwidth": variant["bandwidth"]}


def _redraw_plan(entry, old):
    """Same segments, other hosts. Refuses if the stream itself changed."""
    new = _resolve_plan(entry)
    for kind in ("video", "audio"):
        before = len(old[kind]["segments"]) if old.get(kind) else 0
        after = len(new[kind]["segments"]) if new.get(kind) else 0
        if before != after:
            raise RuntimeError("the stream changed while downloading - start it over")
    return new


def _fetch_segment(url, dest, limiters, stop, count):
    """Download one segment to dest via a temp name. False means stop was asked."""
    tmp = dest + ".tmp"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=SEGMENT_TIMEOUT) as response, open(tmp, "wb") as fh:
            expected = int(response.headers.get("Content-Length") or 0)
            got = 0
            while True:
                if stop.is_set():
                    return False
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                for limiter in limiters:
                    limiter.take(len(chunk), stop)
                fh.write(chunk)
                got += len(chunk)
                count(len(chunk))
        if not got or (expected and got < expected):
            raise TransferTruncated("segment ended early (%d of %d bytes)" % (got, expected))
        os.replace(tmp, dest)                   # only a whole segment gets the real name
        return True
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _hls_segment_download(entry, stop):
    """Resumable HLS download. Raises hls_mirror.Unsupported for odd streams."""
    ffmpeg = which_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found - install it (brew install ffmpeg) or switch stream type to http")
    path = human_path(entry)
    folder = os.path.dirname(path)
    work = work_dir(entry)
    dirs = {"video": os.path.join(work, "v"), "audio": os.path.join(work, "a")}
    for directory in dirs.values():
        os.makedirs(directory, exist_ok=True)
        for name in os.listdir(directory):      # half-written leftovers from a crash
            if name.endswith(".tmp"):
                try:
                    os.remove(os.path.join(directory, name))
                except OSError:
                    pass

    plan = _resolve_plan(entry)
    store.update(entry["id"], {"stream_seconds": sum(seg["duration"] for seg in plan["video"]["segments"])},
                 flush=False)
    jobs = []
    for index in range(max(len(plan["video"]["segments"]),
                           len(plan["audio"]["segments"]) if plan["audio"] else 0)):
        for kind in ("video", "audio"):
            if plan.get(kind) and index < len(plan[kind]["segments"]):
                jobs.append((kind, index))
    target = lambda kind, index: os.path.join(dirs[kind], "%05d.ts" % index)  # noqa: E731

    done_count = sum(1 for kind, index in jobs if os.path.exists(target(kind, index)))
    done_bytes = sum(os.path.getsize(target(k, i)) for k, i in jobs if os.path.exists(target(k, i)))
    seconds = sum(seg["duration"] for seg in plan["video"]["segments"])
    estimate = int(plan.get("bandwidth", 0) / 8.0 * seconds) or done_bytes
    if done_count >= 3:
        estimate = int(done_bytes / float(done_count) * len(jobs))
    # room for the rest of the segments, then the joined file beside them
    _check_space(max(estimate - done_bytes, 0) + estimate, folder)

    limiters = (entry_limiter(entry), global_limiter())
    window = [(time.time(), 0)]
    live = {"bytes": 0, "last": 0.0}

    def count(n):
        live["bytes"] += n
        now = time.time()
        if now - live["last"] < PROGRESS_INTERVAL:
            return
        live["last"] = now
        window.append((now, live["bytes"]))
        while len(window) > 2 and now - window[0][0] > 10:
            window.pop(0)
        span = now - window[0][0]
        speed = (live["bytes"] - window[0][1]) / span if span > 0 else 0
        total = int(done_bytes / float(done_count) * len(jobs)) if done_count >= 3 else estimate
        have = done_bytes + live["current"]
        store.update(entry["id"], {
            "progress": round(min(done_count / float(len(jobs)), 0.999), 4),
            "downloaded_bytes": have, "total_bytes": max(total, have), "speed": int(speed),
            "eta": int((total - have) / speed) if speed > 0 and total > have else 0,
        }, flush=False)

    store.update(entry["id"], {"error": None, "attempt": 0}, flush=False)
    redraws, slow_redraws, slow_streak = 0, 0, 0
    for kind, index in jobs:
        dest = target(kind, index)
        if os.path.exists(dest):
            continue
        tries = 0
        while True:
            live["current"] = 0
            started = time.time()

            def counted(n):
                live["current"] += n
                count(n)
            try:
                if not _fetch_segment(plan[kind]["segments"][index]["uri"], dest, limiters, stop, counted):
                    return False                # paused or cancelled: finished segments stay
            except Exception as exc:            # noqa: BLE001 - classified below
                if stop.is_set():
                    return False
                if not (_is_retryable(exc) or isinstance(exc, urllib.error.HTTPError)):
                    raise
                tries += 1
                if tries < SEGMENT_TRIES:
                    if not _sleep_interruptible(min(2 ** tries, 16), stop):
                        return False
                    continue
                if redraws >= MAX_REDRAWS:
                    raise
                redraws, tries = redraws + 1, 0
                store.update(entry["id"], {"error": "A server stopped answering - switching to another…"}, flush=False)
                plan = _redraw_plan(entry, plan)
                continue
            if time.time() - started > SLOW_SEGMENT_SECONDS:
                slow_streak += 1
            else:
                slow_streak = 0
            if slow_streak >= 2 and slow_redraws < 5:
                slow_redraws, slow_streak = slow_redraws + 1, 0
                try:
                    plan = _redraw_plan(entry, plan)
                except Exception:               # noqa: BLE001 - keep the slow one
                    pass
            done_count += 1
            done_bytes += os.path.getsize(dest)
            live["current"] = 0
            store.update(entry["id"], {"error": None}, flush=False)
            break

    # every segment is on disk: join them locally
    playlists = []
    for kind in ("video", "audio"):
        if not plan.get(kind):
            continue
        playlist = os.path.join(work, kind + ".m3u8")
        names = [os.path.join(dirs[kind], "%05d.ts" % i) for i in range(len(plan[kind]["segments"]))]
        with open(playlist, "w", encoding="utf-8") as handle:
            handle.write(hls_mirror.local_playlist(plan[kind]["segments"], names, plan[kind]["target"]))
        playlists.append(playlist)
    _check_space(done_bytes, folder)
    part = os.path.splitext(path)[0] + ".part.mp4"
    cmd = [ffmpeg, "-y", "-loglevel", "error"]
    for playlist in playlists:
        cmd += ["-protocol_whitelist", "file", "-i", playlist]
    cmd += (["-map", "0:v:0", "-map", "1:a:0"] if len(playlists) == 2 else ["-map", "0"])
    cmd += ["-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", part]
    store.update(entry["id"], {"error": "Joining the pieces…", "speed": 0, "progress": 0.999}, flush=False)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    _register(proc)
    try:
        while proc.poll() is None:
            if stop.is_set():
                proc.terminate()
                proc.wait(timeout=10)
                return False                    # segments are all still there
            time.sleep(0.5)
    finally:
        _unregister(proc)
    errors = proc.stderr.read() or ""
    proc.stderr.close()
    if proc.returncode != 0:
        lines = [l for l in errors.splitlines() if l.strip()]
        raise RuntimeError("joining the segments failed: " + (lines[-1] if lines else "ffmpeg error")[:240])
    os.replace(part, path)
    shutil.rmtree(work, ignore_errors=True)
    size = os.path.getsize(path)
    store.update(entry["id"], {"progress": 1.0, "total_bytes": size, "downloaded_bytes": size,
                               "speed": 0, "error": None})
    return True


NETWORK_ERRORS = (urllib.error.URLError, socket.timeout, socket.error,
                  http.client.HTTPException, ConnectionError, TimeoutError)


def _is_retryable(exc):
    """Transient network trouble is worth retrying; a 404 or a bad URL is not."""
    if isinstance(exc, NotEnoughSpace):
        return False
    if isinstance(exc, TransferTruncated):
        return True
    if isinstance(exc, RuntimeError) and any(w in str(exc).lower() for w in
                                             ("timed out", "timeout", "connection", "i/o error", "server returned 5")):
        return True                            # ffmpeg lost the host mid-file
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 429, 500, 502, 503, 504)
    return isinstance(exc, NETWORK_ERRORS)


def _sleep_interruptible(seconds, stop):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if stop.is_set():
            return False
        time.sleep(0.25)
    return True


def _lookup_stream(entry):
    """Ask the API for this video's HLS url, matching the quality we queued."""
    from . import api                        # imported here to keep startup light
    item_id, media_id = entry.get("item_id"), str(entry.get("media_id") or "")
    if not item_id:
        return None
    try:
        payload = api.call("items/%s" % item_id) or {}
    except api.ApiError:
        return None
    from . import item_cache
    item_cache.save(item_id, payload)
    item = payload.get("item") or {}
    medias = list(item.get("videos") or [])
    for season in item.get("seasons") or []:
        medias.extend(season.get("episodes") or [])
    media = next((m for m in medias if str(m.get("id")) == media_id), None)
    if media is None and len(medias) == 1:
        media = medias[0]
    if media is None:
        return None
    wanted = str(entry.get("quality") or "").lower()
    files = media.get("files") or []
    chosen = next((f for f in files if str(f.get("quality", "")).lower() == wanted), None)
    chosen = chosen or (files[0] if files else None)
    urls = (chosen or {}).get("url") or {}
    return urls.get("hls4") or urls.get("hls2") or urls.get("hls")


def _switch_to_hls(entry):
    """Move an entry onto its HLS stream, discarding the unusable partial file.

    The CDN will not serve a byte range far into a big mp4, so a direct
    download that drops past roughly 240 MB can never be resumed. The HLS
    stream is fetched segment by segment and has no such limit.
    """
    if entry.get("stream_type") != "http":
        return None
    urls = entry.get("urls") or {}
    stream = urls.get("hls4") or urls.get("hls2") or urls.get("hls")
    if not stream:
        stream = _lookup_stream(entry)      # queued before we kept the urls
    if not stream:
        return None
    path = human_path(entry)
    for partial in (path + ".part",):
        try:
            if os.path.exists(partial):
                os.remove(partial)
        except OSError:
            pass
    store.update(entry["id"], {
        "source_url": stream, "stream_type": "hls",
        "downloaded_bytes": 0, "progress": 0.0, "total_bytes": 0,
        "error": "Direct download cannot resume here - switching to the stream…",
    })
    updated = store.get(entry["id"])
    return updated


def _transfer_with_retry(entry, stop):
    """Run the transfer, retrying transient failures with backoff.

    HTTP downloads resume from the .part file, so a retry costs nothing already
    transferred. HLS restarts, so it gets fewer attempts.
    """
    is_hls = entry.get("stream_type", "http") != "http" or ".m3u8" in entry["source_url"]
    attempts = MAX_ATTEMPTS                     # both resume now, so both get the full ladder
    last_error = None
    attempt = 0
    while attempt < attempts:
        attempt += 1
        try:
            if is_hls:
                try:
                    completed = _hls_segment_download(entry, stop)
                except hls_mirror.Unsupported:
                    completed = _hls_download(entry, stop)   # odd stream: one-shot ffmpeg
            else:
                completed = _http_download(entry, stop)
            store.update(entry["id"], {"attempt": 0}, flush=False)
            return completed
        except Exception as exc:                   # noqa: BLE001 - classified below
            if stop.is_set():
                return False
            if not _is_retryable(exc) or attempt >= attempts:
                raise
            last_error = exc
            # a stalled direct download is unrecoverable on this CDN: switch
            # to the segmented stream rather than retrying into a dead end
            if attempt >= 2 and entry.get("stream_type", "http") == "http":
                switched = _switch_to_hls(entry)
                if switched:
                    entry = switched
                    is_hls = True
                    attempt, attempts = 0, MAX_ATTEMPTS
                    continue
            delay = min(2 ** attempt, BACKOFF_CAP)
            store.update(entry["id"], {
                "status": "downloading",
                "speed": 0,
                "attempt": attempt,
                "error": "Connection lost, reconnecting %d/%d in %ds…" % (attempt, attempts - 1, delay),
            })
            if not _sleep_interruptible(delay, stop):
                return False
    if last_error:
        raise last_error
    return False


def _friendly_error(exc):
    """Turn a raw exception into something readable in the UI."""
    if isinstance(exc, NotEnoughSpace):
        return "Not enough disk space: %s. Free some space and press ⟳." % exc
    if isinstance(exc, TransferTruncated):
        return "Connection dropped, file is incomplete (%s). Press ⟳ to resume from where it stopped." % exc
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return "Server rejected the request (%d) — the link expired. Reopen the film and queue it again." % exc.code
        if exc.code == 404:
            return "File not found on the server (404) — the link expired."
        return "Server returned error %d" % exc.code
    if isinstance(exc, NETWORK_ERRORS):
        return "No connection to the server. Check your internet and press ⟳."
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "The disk is full."
    return str(exc)[:400]


def _claim(entry_id):
    """Reserve an entry for one worker. None means somebody else got it.

    The check and the insert happen under one lock, so a second scheduler pass
    cannot hand the same entry to a second thread while the first is still
    starting up - two workers on one .part file would corrupt it.
    """
    with _controls_lock:
        if entry_id in _controls:
            return None
        control = {"stop": threading.Event(), "reason": None}
        _controls[entry_id] = control
        return control


class RateLimiter:
    """Token bucket in bytes per second. A rate of 0 means no limit.

    Some CDNs treat a flat-out download as abuse and throttle or cut the
    connection, so pacing can finish a file faster than grabbing at full speed.
    """

    def __init__(self, rate):
        self.rate = float(rate or 0)
        self.allowance = self.rate
        self.checked = time.time()
        self.lock = threading.Lock()

    def take(self, amount, stop=None):
        if self.rate <= 0:
            return
        while True:
            with self.lock:
                now = time.time()
                self.allowance = min(self.rate,
                                     self.allowance + (now - self.checked) * self.rate)
                self.checked = now
                if self.allowance >= amount:
                    self.allowance -= amount
                    return
                wait = (amount - self.allowance) / self.rate
            wait = min(wait, 0.25)
            if stop is not None:
                if stop.wait(wait):
                    return
            else:
                time.sleep(wait)


_global_limiter = RateLimiter(0)
_global_rate = 0.0


def global_limiter():
    """One shared bucket for every download, rebuilt when the setting changes."""
    global _global_limiter, _global_rate
    rate = float(config.get("speed_limit_total_mb") or 0) * 1024 * 1024
    if rate != _global_rate:
        _global_rate = rate
        _global_limiter = RateLimiter(rate)
    return _global_limiter


def entry_limiter(entry):
    """Per-download bucket: the entry's own limit, else the global default."""
    own = entry.get("speed_limit_mb")
    if own is None:
        own = config.get("speed_limit_mb")
    return RateLimiter(float(own or 0) * 1024 * 1024)


def effective_limit_bytes(entry):
    """Bytes per second this download should not exceed (0 = unlimited)."""
    own = entry.get("speed_limit_mb")
    if own is None:
        own = config.get("speed_limit_mb")
    per = float(own or 0) * 1024 * 1024
    total = float(config.get("speed_limit_total_mb") or 0) * 1024 * 1024
    limits = [x for x in (per, total) if x > 0]
    return min(limits) if limits else 0


INTEGRITY_ERROR_LIMIT = 50      # clean files report 0; damaged ones tens of thousands


def check_integrity(path, timeout=900):
    """Parse the whole file without decoding; returns the number of error lines.

    None means the check could not run (no ffmpeg, file missing, timed out).
    Damaged downloads still remux cleanly and look complete, but the parser
    trips over them constantly - a broken stream produced ~90,000 lines.
    """
    ffmpeg = which_ffmpeg()
    if not ffmpeg or not os.path.exists(path):
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-v", "error", "-i", path, "-c", "copy", "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return sum(1 for line in proc.stderr.splitlines() if line.strip())


def probe_media(path):
    """Length in seconds and which kinds of stream the file holds (from ffmpeg -i)."""
    ffmpeg = which_ffmpeg()
    if not ffmpeg or not os.path.exists(path):
        return None
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-i", path], stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None
    info = proc.stderr
    length = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", info)
    seconds = (int(length.group(1)) * 3600 + int(length.group(2)) * 60 + float(length.group(3))) if length else None
    return {"seconds": seconds, "video": " Video: " in info, "audio": " Audio: " in info}


def fmt_clock(seconds):
    seconds = int(round(seconds or 0))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def completeness_problem(path, expected_seconds, strict=True):
    """None when the file is whole; otherwise what is wrong, in plain words.

    A download that stopped early can still parse cleanly, so the parse check
    alone cannot tell. The reference should be the *stream's* length - the sum
    of its segment durations - which is exact (strict: within 1%, 30s slack).
    The API's episode length is not: it runs up to minutes longer than what is
    actually served, so it is only good for a loose check (10%, 60s slack).
    """
    media = probe_media(path)
    if media is None:
        return None                              # cannot check (no ffmpeg): no verdict
    if not media["video"]:
        return "the file has no video"
    if not media["audio"]:
        return "the file has no sound"
    if media["seconds"] is None:
        return "the file has no readable length"
    expected = float(expected_seconds or 0)
    if expected > 0:
        short = expected - media["seconds"]
        slack = max(30.0, expected * 0.01) if strict else max(60.0, expected * 0.10)
        if short > slack:
            return "only %s of %s was downloaded" % (fmt_clock(media["seconds"]), fmt_clock(expected))
    return None


def stream_seconds_for(entry):
    """Exact length of the stream this entry downloads, looked up once and kept."""
    if entry.get("stream_seconds"):
        return entry["stream_seconds"]
    try:
        stream = _lookup_stream(entry)           # a fresh link for its quality
        if not stream:
            return None
        plan = _resolve_plan(dict(entry, source_url=stream))
        seconds = sum(seg["duration"] for seg in plan["video"]["segments"])
    except Exception:                           # noqa: BLE001 - offline, odd stream
        return None
    if seconds:
        store.update(entry["id"], {"stream_seconds": seconds}, flush=False)
    return seconds or None


def expected_length(entry, allow_lookup=True):
    """(seconds, strict) - the stream's length if known, else the API's, loosely."""
    seconds = entry.get("stream_seconds") or (stream_seconds_for(entry) if allow_lookup else None)
    if seconds:
        return seconds, True
    return entry.get("duration"), False


def is_damaged(path):
    errors = check_integrity(path)
    return errors is not None and errors > INTEGRITY_ERROR_LIMIT


def verify_one(entry_id):
    """Integrity-check a single downloaded file and record the verdict."""
    entry = store.get(entry_id)
    if not entry:
        return None
    path = human_path(entry)
    if not os.path.exists(path):
        return {"damaged": None, "missing": True}
    errors = check_integrity(path)
    if errors is None:
        return {"damaged": None, "errors": None}
    expected, strict = expected_length(entry)
    problem = "the video data is damaged" if errors > INTEGRITY_ERROR_LIMIT else \
        completeness_problem(path, expected, strict)
    media = probe_media(path) or {}
    store.update(entry_id, {"damaged": bool(problem), "problem": problem,
                            "verified_at": time.time(), "verified_seconds": media.get("seconds")})
    return {"damaged": bool(problem), "problem": problem, "errors": errors,
            "seconds": media.get("seconds"), "expected": expected, "strict": strict}


LOOKUP_SPACING = 4.0           # seconds between stream-length lookups during a scan
_verify_state = {"running": False, "checked": 0, "total": 0, "damaged": 0}


def _needs_check(entry):
    """Unchecked, or changed on disk since its last check."""
    try:
        return not entry.get("verified_at") or os.path.getmtime(human_path(entry)) > entry["verified_at"]
    except OSError:
        return False


def verify_library(only_changed=False):
    """Scan finished downloads in the background and flag damaged ones."""
    if _verify_state["running"]:
        return dict(_verify_state)
    entries = [e for e in store.all_entries() if e.get("status") == "done"
               and (not only_changed or _needs_check(e))]
    _verify_state.update({"running": True, "checked": 0, "total": len(entries), "damaged": 0})

    def scan():
        try:
            for entry in entries:
                path = human_path(entry)
                if not os.path.exists(path):
                    _verify_state["checked"] += 1
                    continue
                store.update(entry["id"], {"verifying": True}, flush=False)
                if not entry.get("stream_seconds"):
                    # this one looks its stream length up online: pace those, or
                    # the CDN starts answering 403 to a burst of playlist fetches
                    wait = LOOKUP_SPACING - (time.time() - _verify_state.get("last_lookup", 0))
                    if wait > 0:
                        time.sleep(wait)
                    _verify_state["last_lookup"] = time.time()
                result = verify_one(entry["id"]) or {}
                if result.get("damaged"):
                    _verify_state["damaged"] += 1
                store.update(entry["id"], {"verifying": False})
                _verify_state["checked"] += 1
        finally:
            _verify_state["running"] = False

    threading.Thread(target=scan, daemon=True).start()
    return dict(_verify_state)


def verify_status():
    return dict(_verify_state)


def _run_entry(entry_id, control):
    entry = store.get(entry_id)
    if not entry:
        with _controls_lock:
            _controls.pop(entry_id, None)
        return
    stop = control["stop"]
    store.update(entry_id, {"status": "downloading", "error": None})
    try:
        completed = _transfer_with_retry(entry, stop)
        if not completed:
            with _controls_lock:
                reason = (_controls.get(entry_id) or {}).get("reason") or "paused"
            if reason == "canceled":
                _cleanup_files(entry, keep_dir=False)
                store.remove(entry_id)
            elif reason == "restart":
                pass                      # restart() owns the entry from here
            else:
                store.update(entry_id, {"status": "paused", "speed": 0})
            return
        store.update(entry_id, {"error": "Checking the file is complete…", "speed": 0}, flush=False)
        final = human_path(entry)
        entry = store.get(entry_id) or entry     # the mirror recorded the stream length
        expected, strict = expected_length(entry, allow_lookup=False)
        problem = "the video data is damaged" if is_damaged(final) else \
            completeness_problem(final, expected, strict)
        if problem:
            # keep it out of the library; ⟳/↻ fetch it again
            store.update(entry_id, {
                "status": "error", "speed": 0, "progress": 0.0, "attempt": 0,
                "damaged": True, "problem": problem,
                "error": "Not saved: %s. Press ⟳ to download it again." % problem,
            })
            return
        media = probe_media(final) or {}
        subs = []
        if config.get("download_subtitles"):
            subs = download_subtitles(entry, entry.get("pending_subtitles"))
        store.update(entry_id, {
            "status": "done", "speed": 0, "progress": 1.0, "error": None, "attempt": 0,
            "damaged": False, "problem": None, "verified_at": time.time(),
            "verified_seconds": media.get("seconds"),
            "finished_at": time.time(), "subtitles": subs, "pending_subtitles": None,
        })
    except Exception as exc:                       # noqa: BLE001 - surfaced in the UI
        store.update(entry_id, {"status": "error", "speed": 0, "error": _friendly_error(exc)})
    finally:
        with _controls_lock:
            _controls.pop(entry_id, None)
        _wake.set()


def _scheduler():
    while True:
        _wake.wait(timeout=2)
        _wake.clear()
        try:
            limit = int(config.get("max_parallel_downloads") or 2)
        except (TypeError, ValueError):
            limit = 2
        with _controls_lock:
            running = len(_controls)
        for entry in store.pending():
            if running >= max(limit, 1):
                break
            control = _claim(entry["id"])
            if control is None:
                continue
            running += 1
            threading.Thread(target=_run_entry, args=(entry["id"], control),
                             daemon=True).start()


def start():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_scheduler, daemon=True).start()
    _wake.set()


# --------------------------------------------------------------------------- #
# queue control
# --------------------------------------------------------------------------- #
def enqueue(entry):
    created = store.add(entry)
    _wake.set()
    return created


def pause(entry_id):
    store.update(entry_id, {"paused_by_user": True}, flush=False)
    with _controls_lock:
        control = _controls.get(entry_id)
        if control:
            control["reason"] = "paused"
            control["stop"].set()
            return True
    entry = store.get(entry_id)
    if entry and entry.get("status") == "queued":
        store.update(entry_id, {"status": "paused"})
        return True
    return False


def pause_all():
    """Pause everything queued or downloading; they stay paused across restarts."""
    count = 0
    for entry in store.all_entries():
        if entry.get("status") in ("queued", "downloading") and pause(entry["id"]):
            count += 1
    return count


def resume_all():
    """Resume everything paused and try every failed download again."""
    count = 0
    for entry in store.all_entries():
        if entry.get("status") in ("paused", "error") and resume(entry["id"]):
            count += 1
    return count


def _refresh_source(entry_id):
    """Swap in a freshly issued stream link. Best effort: offline keeps the old one.

    The API's stream links expire 24h after they are issued, so fetching a
    file again later with the link stored at queue time would fail at once.
    """
    entry = store.get(entry_id)
    if not entry:
        return
    fresh = _lookup_stream(entry)
    if fresh:
        store.update(entry_id, {"source_url": fresh, "stream_type": "hls"}, flush=False)


def resume(entry_id):
    entry = store.get(entry_id)
    if not entry or entry.get("status") not in ("paused", "error"):
        return False
    if entry.get("status") == "error":
        _refresh_source(entry_id)
    store.update(entry_id, {"status": "queued", "error": None, "paused_by_user": False})
    _wake.set()
    return True


def restart(entry_id):
    """Throw away what was downloaded and fetch it again from the beginning.

    For a download that resumed into a bad state - a stalled transfer, a part
    file the server will no longer match - where continuing cannot work.
    """
    entry = store.get(entry_id)
    if not entry:
        return False

    with _controls_lock:
        control = _controls.get(entry_id)
        if control:
            control["reason"] = "restart"
            control["stop"].set()
    if control:                            # let the worker notice and let go
        for _ in range(60):
            with _controls_lock:
                if entry_id not in _controls:
                    break
            time.sleep(0.05)

    path = human_path(entry)
    shutil.rmtree(work_dir(entry), ignore_errors=True)     # start over means from zero
    for partial in (path + ".part", os.path.splitext(path)[0] + ".part.mp4", path):
        try:
            if os.path.exists(partial):
                os.remove(partial)
        except OSError:
            pass

    store.update(entry_id, {
        "status": "queued", "error": None, "progress": 0.0,
        "downloaded_bytes": 0, "total_bytes": 0, "speed": 0,
        "attempt": 0, "paused_by_user": False, "damaged": False,
    })
    _refresh_source(entry_id)
    _wake.set()
    return True


def cancel(entry_id):
    with _controls_lock:
        control = _controls.get(entry_id)
        if control:
            control["reason"] = "canceled"
            control["stop"].set()
            return True
    entry = store.get(entry_id)
    if entry:
        _cleanup_files(entry, keep_dir=False)
        store.remove(entry_id)
        return True
    return False


def _cleanup_files(entry, keep_dir=False):
    try:
        path = human_path(entry)
    except Exception:
        return
    stem = os.path.splitext(path)[0]
    for candidate in (path, path + ".part", stem + ".part.mp4", stem + ".master.m3u8"):
        try:
            if os.path.exists(candidate):
                os.remove(candidate)
        except OSError:
            pass
    shutil.rmtree(work_dir(entry), ignore_errors=True)
    for sub in entry.get("subtitles") or []:
        try:
            os.remove(os.path.join(os.path.dirname(path), sub["file"]))
        except OSError:
            pass
    if not keep_dir:
        folder = os.path.dirname(path)
        try:
            if os.path.isdir(folder) and not os.listdir(folder):
                os.rmdir(folder)
        except OSError:
            pass


def delete(entry_id):
    entry = store.get(entry_id)
    if not entry:
        return False
    cancel_running = False
    with _controls_lock:
        if entry_id in _controls:
            cancel_running = True
    if cancel_running:
        return cancel(entry_id)
    _cleanup_files(entry)
    store.remove(entry_id)
    return True
