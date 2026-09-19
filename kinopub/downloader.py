"""Download manager: queue, workers, resumable HTTP transfers, HLS via ffmpeg."""
import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
import http.client
import socket
import urllib.error
import urllib.parse
import urllib.request

from . import config, store

CHUNK = 1024 * 256
PROGRESS_INTERVAL = 0.7
UA = "KinoPubOffline/1.0"
# a flaky hotel/airport wifi should not lose a download (tests shorten this)
MAX_ATTEMPTS = int(os.environ.get("KP_MAX_ATTEMPTS") or 6)
BACKOFF_CAP = 30
DISK_MARGIN = 500 * 1024 * 1024   # keep the boot volume breathing room

class TransferTruncated(Exception):
    """The connection ended before Content-Length bytes arrived."""


class NotEnoughSpace(Exception):
    """The file will not fit on the library volume."""


_controls = {}          # entry_id -> {"stop": Event, "reason": str}
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


def _hls_download(entry, stop):
    ffmpeg = which_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found — install it (brew install ffmpeg) or switch stream type to http")
    path = human_path(entry)
    part = os.path.splitext(path)[0] + ".part.mp4"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    duration = float(entry.get("duration") or 0)
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-user_agent", UA,
           "-reconnect", "1", "-reconnect_streamed", "1",
           "-reconnect_on_network_error", "1", "-reconnect_delay_max", "10"]
    readrate = _readrate_for(entry)
    if readrate:
        # ffmpeg paces by playback speed, so a byte cap becomes a multiplier
        cmd += ["-readrate", "%.2f" % readrate]
    cmd += ["-i", entry["source_url"], "-c", "copy", "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", part]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    store.update(entry["id"], {"error": None, "attempt": 0}, flush=False)
    started_at = time.time()
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
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr.read() or "ffmpeg exited with an error")[:300])
    os.replace(part, path)
    size = os.path.getsize(path)
    store.update(entry["id"], {"progress": 1.0, "total_bytes": size, "downloaded_bytes": size, "speed": 0})
    return True


# --------------------------------------------------------------------------- #
# worker
# --------------------------------------------------------------------------- #
NETWORK_ERRORS = (urllib.error.URLError, socket.timeout, socket.error,
                  http.client.HTTPException, ConnectionError, TimeoutError)


def _is_retryable(exc):
    """Transient network trouble is worth retrying; a 404 or a bad URL is not."""
    if isinstance(exc, NotEnoughSpace):
        return False
    if isinstance(exc, TransferTruncated):
        return True
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
        item = (api.call("items/%s" % item_id) or {}).get("item") or {}
    except api.ApiError:
        return None
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
    attempts = 3 if is_hls else MAX_ATTEMPTS
    last_error = None
    attempt = 0
    while attempt < attempts:
        attempt += 1
        try:
            completed = _hls_download(entry, stop) if is_hls else _http_download(entry, stop)
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
                    attempt, attempts = 0, 3       # the stream starts fresh
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
        subs = []
        if config.get("download_subtitles"):
            subs = download_subtitles(entry, entry.get("pending_subtitles"))
        store.update(entry_id, {
            "status": "done", "speed": 0, "progress": 1.0, "error": None, "attempt": 0,
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


def resume(entry_id):
    entry = store.get(entry_id)
    if not entry or entry.get("status") not in ("paused", "error"):
        return False
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
    for partial in (path + ".part", os.path.splitext(path)[0] + ".part.mp4", path):
        try:
            if os.path.exists(partial):
                os.remove(partial)
        except OSError:
            pass

    store.update(entry_id, {
        "status": "queued", "error": None, "progress": 0.0,
        "downloaded_bytes": 0, "total_bytes": 0, "speed": 0,
        "attempt": 0, "paused_by_user": False,
    })
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
    for candidate in (path, path + ".part", stem + ".part.mp4"):
        try:
            if os.path.exists(candidate):
                os.remove(candidate)
        except OSError:
            pass
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
