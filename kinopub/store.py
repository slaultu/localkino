"""Thread-safe JSON store for the local library + download queue."""
import threading
import time
import uuid

from . import config

_lock = threading.RLock()
_cache = None

ACTIVE = ("queued", "downloading", "paused", "error")


def _load():
    global _cache
    with _lock:
        if _cache is None:
            data = config.read_json(config.LIBRARY_FILE, {"entries": []})
            if not isinstance(data, dict) or "entries" not in data:
                data = {"entries": []}
            # A download cut short by quitting the app resumes by itself next
            # launch; something the user paused on purpose stays paused.
            for entry in data["entries"]:
                if entry.get("status") == "downloading":
                    entry["status"] = "paused" if entry.get("paused_by_user") else "queued"
                    entry["speed"] = 0
            _cache = data
        return _cache


def _flush():
    with _lock:
        config.write_json(config.LIBRARY_FILE, _cache)


def all_entries():
    with _lock:
        return [dict(e) for e in _load()["entries"]]


def get(entry_id):
    with _lock:
        for entry in _load()["entries"]:
            if entry["id"] == entry_id:
                return dict(entry)
    return None


def find(item_id=None, media_id=None):
    with _lock:
        for entry in _load()["entries"]:
            if media_id is not None and entry.get("media_id") == media_id:
                return dict(entry)
            if media_id is None and item_id is not None and entry.get("item_id") == item_id:
                return dict(entry)
    return None


def add(entry):
    with _lock:
        data = _load()
        entry = dict(entry)
        entry.setdefault("id", uuid.uuid4().hex[:12])
        entry.setdefault("status", "queued")
        entry.setdefault("progress", 0.0)
        entry.setdefault("downloaded_bytes", 0)
        entry.setdefault("total_bytes", 0)
        entry.setdefault("speed", 0)
        entry.setdefault("position", 0)
        entry.setdefault("created_at", time.time())
        data["entries"].append(entry)
        _flush()
        return dict(entry)


def update(entry_id, patch, flush=True):
    with _lock:
        for entry in _load()["entries"]:
            if entry["id"] == entry_id:
                entry.update(patch)
                if flush:
                    _flush()
                return dict(entry)
    return None


def remove(entry_id):
    with _lock:
        data = _load()
        before = len(data["entries"])
        data["entries"] = [e for e in data["entries"] if e["id"] != entry_id]
        if len(data["entries"]) != before:
            _flush()
            return True
    return False


def flush():
    with _lock:
        _flush()


def pending():
    """Entries waiting for a worker, oldest first."""
    with _lock:
        return [dict(e) for e in _load()["entries"] if e.get("status") == "queued"]
