"""Last-seen copies of item details, so a series list survives going offline.

Whenever the API hands us an item (a film or a whole series with every
episode), a copy is kept on disk. Offline, that copy is served instead, so
you can still see which episodes exist, which are downloaded and which are
not.
"""
import os
import threading
import time

from . import config

STALE_SECONDS = 6 * 3600
_lock = threading.Lock()


def _path(item_id):
    return os.path.join(config.CONFIG_DIR, "items", "%d.json" % int(item_id))


def save(item_id, payload):
    try:
        path = _path(item_id)
    except (TypeError, ValueError):
        return
    with _lock:
        config.write_json(path, {"cached_at": time.time(), "data": payload})


def load(item_id):
    try:
        return config.read_json(_path(item_id), None)
    except (TypeError, ValueError):
        return None


def age(item_id):
    cached = load(item_id)
    return time.time() - cached["cached_at"] if cached else None


def warm(item_ids):
    """Refresh copies that are missing or stale, one by one in the background."""
    from . import api                           # imported late: api imports config too
    todo = []
    for item_id in item_ids:
        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            continue
        seconds = age(item_id)
        if seconds is None or seconds > STALE_SECONDS:
            todo.append(item_id)

    def run():
        for item_id in todo:
            try:
                save(item_id, api.call("items/%d" % item_id))
            except Exception:                   # noqa: BLE001 - offline, or gone
                pass
            time.sleep(0.5)                     # be gentle with the API

    if todo:
        threading.Thread(target=run, daemon=True).start()
    return len(todo)
