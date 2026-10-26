"""How much of a title was really watched: the stretches actually played (this and earlier sessions), not just where
playback ended. Skipping ahead doesn't count. Kept per item in %APPDATA%\\Marquee\\assistant\\watched.json."""
import json
import threading
import time

from .. import settings

FILE = settings.DIR / "assistant" / "watched.json"
_lock = threading.Lock()
_data = None


def _load():
    global _data
    if _data is None:
        try:
            _data = json.loads(FILE.read_text(encoding="utf-8"))
        except Exception:
            _data = {}
    return _data


def _save():
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_data), encoding="utf-8")
    tmp.replace(FILE)


def _merge(ranges, gap=2.0):
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def add(item_id, a, b, runtime=None):
    """Mark [a, b] seconds of the item as played."""
    if b - a < 0.5:
        return
    with _lock:
        d = _load()
        e = d.setdefault(item_id, {"ranges": [], "runtime": 0, "at": 0})
        e["ranges"] = _merge(e["ranges"] + [[round(a, 1), round(b, 1)]])
        if runtime:
            e["runtime"] = runtime
        e["at"] = time.time()
        if len(d) > 2000:  # keep the most recent
            for k in sorted(d, key=lambda k: d[k].get("at", 0))[:200]:
                d.pop(k, None)


def played(item_id):
    e = _load().get(item_id) or {}
    return sum(b - a for a, b in e.get("ranges", []))


def coverage(item_id, runtime=None, until=None):
    """Fraction of the runtime (up to `until`, e.g. the credits) really played, 0..1."""
    e = _load().get(item_id) or {}
    run = until or runtime or e.get("runtime") or 0
    if not run:
        return 0.0
    got = sum(max(0.0, min(b, run) - a) for a, b in e.get("ranges", []) if a < run)
    return min(1.0, got / run)


def flush():
    with _lock:
        if _data is not None:
            _save()


class Tracker:
    """Polls the player: consecutive positions that advance at about the playback speed count as watched."""

    def __init__(self, app, every=2.0):
        self.app, self.every = app, every
        self.last = None   # (item_id, position, wallclock)
        self.saved = time.time()

    def tick(self):
        p = getattr(self.app, "player", None)
        m = getattr(p, "mpv", None)
        item = getattr(p, "item", None) if getattr(p, "playing", False) else None
        now = time.time()
        if not item or m is None:
            self.last = None
            return
        try:
            pos, paused, speed = m.time_pos, bool(m.pause), float(m.speed or 1)
        except Exception:
            return
        if pos is None or paused:
            self.last = None
            return
        iid = item.get("Id")
        if self.last and self.last[0] == iid:
            _, p0, t0 = self.last
            dt, dp = now - t0, pos - p0
            if 0 < dp <= dt * speed * 1.5 + 1.0:   # played through, not a seek
                add(iid, p0, pos, (item.get("RunTimeTicks") or 0) / 10_000_000)
        self.last = (iid, pos, now)
        if now - self.saved > 30:
            self.saved = now
            flush()

    def run(self):
        while True:
            time.sleep(self.every)
            try:
                self.tick()
            except Exception as e:
                print("watch tracking:", e)
