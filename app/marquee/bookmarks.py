"""Bookmarks and notes on videos: a moment (to the millisecond), a note, a tag. Kept locally in bookmarks.json
(next to settings.json, i.e. %APPDATA%\\Marquee). The player adds them (key B); the UI server can list them with
`for_item(item_id)` / `all_items()` and change them with `remove()`."""
import json
import threading
import time
import uuid

from . import settings

FILE = settings.DIR / "bookmarks.json"
TAGS = ["Favourite", "Funny", "Review"]
_lock = threading.Lock()


def _load():
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(FILE)


def add(item_id, ms, note="", tag="", title="", subtitle=""):
    """A new bookmark; returns it. ms: position in milliseconds."""
    b = {"id": uuid.uuid4().hex[:12], "ms": int(ms), "note": (note or "").strip(), "tag": (tag or "").strip(),
         "title": title or "", "subtitle": subtitle or "", "created": int(time.time())}
    with _lock:
        data = _load()
        data.setdefault(item_id, []).append(b)
        data[item_id].sort(key=lambda x: x["ms"])
        _save(data)
    return b


def remove(item_id, bookmark_id):
    with _lock:
        data = _load()
        before = len(data.get(item_id, []))
        data[item_id] = [b for b in data.get(item_id, []) if b["id"] != bookmark_id]
        if not data[item_id]:
            data.pop(item_id)
        _save(data)
        return before != len(data.get(item_id, []))


def for_item(item_id):
    with _lock:
        return list(_load().get(item_id, []))


def all_items():
    """{item_id: [bookmarks]} for a Bookmarks page."""
    with _lock:
        return _load()
