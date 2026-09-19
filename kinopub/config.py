"""Paths, persistent settings and small JSON helpers."""
import json
import os
import threading

APP_NAME = "KinoPub Offline"

HOME = os.path.expanduser("~")
# KP_CONFIG_DIR keeps tests (and throwaway profiles) out of the real config.
CONFIG_DIR = os.environ.get("KP_CONFIG_DIR") or os.path.join(HOME, ".config", "kinopub-offline")
DEFAULT_LIBRARY = os.path.join(HOME, "Movies", "KinoPub")

SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
TOKENS_FILE = os.path.join(CONFIG_DIR, "tokens.json")
LIBRARY_FILE = os.path.join(CONFIG_DIR, "library.json")
QUEUE_FILE = os.path.join(CONFIG_DIR, "queue.json")
INSTANCE_FILE = os.path.join(CONFIG_DIR, "instance.json")
POSTER_CACHE = os.path.join(CONFIG_DIR, "posters")

# KP_API_BASE lets tests (and debugging) point the client at a stand-in server.
API_BASE = os.environ.get("KP_API_BASE") or "https://api.service-kp.com"

DEFAULTS = {
    "client_id": "",
    "client_secret": "",
    "library_dir": DEFAULT_LIBRARY,
    "preferred_quality": "1080p",
    # HLS by default: this CDN refuses byte ranges deep inside a large mp4,
    # so an interrupted direct download can never resume past ~240 MB
    "stream_type": "hls4",          # hls4 | hls2 | hls | http
    "max_parallel_downloads": 2,
    "speed_limit_mb": 0,            # per download, MB/s (0 = unlimited)
    "speed_limit_total_mb": 0,      # across all downloads, MB/s (0 = unlimited)
    "download_subtitles": True,
    "sync_watching": True,          # push playback position back to kino.pub
    "port": 8777,
}

_lock = threading.RLock()


def ensure_dirs():
    for path in (CONFIG_DIR, POSTER_CACHE):
        os.makedirs(path, exist_ok=True)
    try:
        os.makedirs(get("library_dir"), exist_ok=True)
    except OSError:
        pass


def read_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (IOError, ValueError):
        return fallback


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_settings():
    with _lock:
        data = dict(DEFAULTS)
        data.update(read_json(SETTINGS_FILE, {}))
        return data


def save_settings(patch):
    with _lock:
        data = load_settings()
        for key, value in patch.items():
            if key in DEFAULTS:
                data[key] = value
        write_json(SETTINGS_FILE, data)
        return data


def get(key):
    return load_settings().get(key, DEFAULTS.get(key))
