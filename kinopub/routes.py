"""JSON API used by the local web UI."""
import os
import subprocess
import threading
import urllib.parse
import time

from . import api, config, downloader, store

_device = {"code": None, "user_code": None, "verification_uri": None, "expires_at": 0, "interval": 5}
_device_lock = threading.Lock()


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- #
# state / settings / auth
# --------------------------------------------------------------------------- #
def state(_params, _body):
    settings = config.load_settings()
    return {
        "authorized": api.is_authorized(),
        "has_credentials": bool(settings["client_id"] and settings["client_secret"]),
        "settings": _public_settings(settings),
        "ffmpeg": bool(downloader.which_ffmpeg()),
        "version": "1.0.0",
    }


def _public_settings(settings):
    data = dict(settings)
    data["client_secret_set"] = bool(data.pop("client_secret", ""))
    return data


def get_settings(_params, _body):
    return {"settings": _public_settings(config.load_settings())}


def save_settings(_params, body):
    patch = {}
    for key in ("client_id", "client_secret", "library_dir", "preferred_quality",
                "stream_type", "download_subtitles", "sync_watching"):
        if key in body and body[key] != "":
            patch[key] = body[key]
    for key in ("speed_limit_mb", "speed_limit_total_mb"):
        if key in body:
            try:
                patch[key] = max(0.0, float(body[key]))
            except (TypeError, ValueError):
                pass
    if "max_parallel_downloads" in body:
        try:
            patch["max_parallel_downloads"] = max(1, min(6, int(body["max_parallel_downloads"])))
        except (TypeError, ValueError):
            pass
    settings = config.save_settings(patch)
    config.ensure_dirs()
    return {"settings": _public_settings(settings)}


def auth_start(_params, _body):
    payload = api.device_code()
    with _device_lock:
        _device.update({
            "code": payload.get("code"),
            "user_code": payload.get("user_code"),
            "verification_uri": payload.get("verification_uri") or "https://kino.pub/device",
            "expires_at": time.time() + int(payload.get("expires_in") or 600),
            "interval": int(payload.get("interval") or 5),
        })
        return dict(_device)


def auth_poll(_params, _body):
    with _device_lock:
        code = _device.get("code")
        expires_at = _device.get("expires_at", 0)
    if not code:
        raise HttpError(400, "Request a device code first")
    if time.time() > expires_at:
        raise HttpError(410, "Device code expired — request a new one")
    result = api.device_token(code)
    if result.get("authorized"):
        threading.Thread(target=api.notify_device, daemon=True).start()
    return result


def auth_logout(_params, _body):
    api.clear_tokens()
    return {"authorized": False}


# --------------------------------------------------------------------------- #
# transparent proxy to the kino.pub API
# --------------------------------------------------------------------------- #
def kp_proxy(path, params, method, body):
    clean = {k: v for k, v in params.items() if k != "_"}
    if method == "POST":
        return api.call(path, params=clean, method="POST", data=body or {})
    return api.call(path, params=clean)


# --------------------------------------------------------------------------- #
# downloads
# --------------------------------------------------------------------------- #
def _extension_for(url, stream_type):
    """Keep the source container for direct files; HLS is always remuxed to mp4."""
    if stream_type != "http" or ".m3u8" in (url or ""):
        return ".mp4"
    path = urllib.parse.urlsplit(url or "").path
    ext = os.path.splitext(path)[1].lower()
    return ext if ext in (".mp4", ".mkv", ".avi", ".m4v", ".mov", ".ts", ".webm") else ".mp4"


def _build_rel_path(payload, quality, extension=".mp4"):
    folder = downloader.safe_name("%s%s" % (payload.get("show_title") or payload.get("title"),
                                            " (%s)" % payload["year"] if payload.get("year") else ""))
    season, episode = payload.get("season"), payload.get("episode")
    if season is not None and episode is not None:
        stem = "%s - S%02dE%02d%s" % (
            downloader.safe_name(payload.get("show_title") or payload.get("title"), 80),
            int(season), int(episode),
            " - %s" % downloader.safe_name(payload.get("episode_title"), 60) if payload.get("episode_title") else "")
    else:
        stem = downloader.safe_name(payload.get("title"), 90)
    return os.path.join(folder, "%s [%s]%s" % (stem, downloader.safe_name(quality or "sd", 12), extension))


def _unique_rel_path(rel_path, media_id):
    """Two different videos must never share one file on disk."""
    taken = {entry.get("rel_path") for entry in store.all_entries()}
    if rel_path not in taken:
        return rel_path
    stem, extension = os.path.splitext(rel_path)
    candidate = "%s (%s)%s" % (stem, downloader.safe_name(media_id or "copy", 16), extension)
    index = 2
    while candidate in taken:
        candidate = "%s (%s-%d)%s" % (stem, downloader.safe_name(media_id or "copy", 16), index, extension)
        index += 1
    return candidate


def downloads_list(_params, _body):
    entries = store.all_entries()
    entries.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    return {
        "entries": entries,
        "library_dir": config.get("library_dir"),
        "free_space": downloader.free_space(),
    }


def downloads_add(_params, body):
    url = body.get("url")
    if not url:
        raise HttpError(400, "No file url supplied")
    existing = store.find(media_id=body.get("media_id")) if body.get("media_id") else None
    if existing:
        return {"entry": existing, "duplicate": True}
    quality = body.get("quality") or "sd"
    rel_path = _unique_rel_path(
        _build_rel_path(body, quality, _extension_for(url, body.get("stream_type") or "http")),
        body.get("media_id"))
    poster_file = downloader.cache_poster(body.get("poster"))
    entry = downloader.enqueue({
        "item_id": body.get("item_id"),
        "media_id": body.get("media_id"),
        "kind": "episode" if body.get("season") is not None else "movie",
        "title": body.get("title") or "Untitled",
        "show_title": body.get("show_title"),
        "episode_title": body.get("episode_title"),
        "season": body.get("season"),
        "episode": body.get("episode"),
        "year": body.get("year"),
        "plot": (body.get("plot") or "")[:2000],
        "genres": body.get("genres") or [],
        "duration": body.get("duration") or 0,
        "quality": quality,
        "source_url": url,
        "urls": body.get("urls") or {},
        "stream_type": body.get("stream_type") or "http",
        "poster_file": poster_file,
        "poster_url": body.get("poster"),
        "rel_path": rel_path,
        "pending_subtitles": body.get("subtitles") or [],
        "imdb_rating": body.get("rating"),
    })
    return {"entry": entry}


def downloads_limit(entry_id, body):
    """Set this download's own speed cap; null means follow the global setting."""
    raw = body.get("mb")
    value = None if raw in (None, "", "default") else max(0.0, float(raw))
    entry = store.update(entry_id, {"speed_limit_mb": value})
    if not entry:
        raise HttpError(404, "Entry not found")
    return {"entry": entry}


def downloads_action(entry_id, action):
    handlers = {
        "pause": downloader.pause,
        "resume": downloader.resume,
        "cancel": downloader.cancel,
        "delete": downloader.delete,
        "retry": downloader.resume,
        "restart": downloader.restart,
    }
    handler = handlers.get(action)
    if not handler:
        raise HttpError(404, "Unknown action: %s" % action)
    return {"ok": bool(handler(entry_id))}


# --------------------------------------------------------------------------- #
# library
# --------------------------------------------------------------------------- #
def library_list(_params, _body):
    entries = []
    for entry in store.all_entries():
        if entry.get("status") != "done":
            continue
        path = os.path.join(config.get("library_dir"), entry["rel_path"])
        entry["available"] = os.path.exists(path)
        if entry["available"] and not entry.get("total_bytes"):
            entry["total_bytes"] = os.path.getsize(path)
        entries.append(entry)
    entries.sort(key=lambda e: (e.get("show_title") or e.get("title") or "",
                                e.get("season") or 0, e.get("episode") or 0))
    return {"entries": entries, "library_dir": config.get("library_dir")}


def reveal(_params, body):
    """Open the library folder (or one file) in Finder."""
    target = config.get("library_dir")
    entry_id = body.get("entry_id")
    if entry_id:
        entry = store.get(entry_id)
        if entry:
            candidate = os.path.join(config.get("library_dir"), entry["rel_path"])
            if os.path.exists(candidate):
                target = candidate
    if not os.path.exists(target):
        raise HttpError(404, "Folder not found: %s" % target)
    command = ["open", "-R", target] if os.path.isfile(target) else ["open", target]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "path": target}


def library_progress(entry_id, body):
    entry = store.update(entry_id, {
        "position": float(body.get("position") or 0),
        "watched": bool(body.get("watched")),
        "last_played": time.time(),
    })
    if not entry:
        raise HttpError(404, "Entry not found")
    if config.get("sync_watching") and entry.get("item_id") and body.get("sync"):
        try:
            params = {"id": entry["item_id"], "time": int(float(body.get("position") or 0))}
            if entry.get("media_id"):
                params["video"] = entry.get("episode") or 1
            if entry.get("season"):
                params["season"] = entry["season"]
            api.call("watching/marktime", params=params)
        except api.ApiError:
            pass
    return {"entry": entry}
