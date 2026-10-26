"""The background indexer: one title at a time, a few minutes of video per step, saved after every step so it
can stop and pick up again at any point. It waits while a video plays (unless asked for that very title
urgently), runs ffmpeg at below-normal priority, and lets go of the model when there's nothing to do."""
import ctypes
import heapq
import itertools
import json
import os
import threading
import time
import traceback


from . import analysis, deps, store
from .deps import np
from .frames import SAMPLE_EVERY, Decoder, DecodeError, prepare

CHUNK = 300.0          # seconds of video per step
IDLE_UNLOAD = 180.0    # free the model after this long without work
CONFIG = deps.HOME / "index" / "config.json"
CONFIG_DEFAULTS = {"library": False,  # index the whole library in idle time
                   "sensitive": {"violence": "off", "gore": "off", "nudity": "off"}}  # off | blur | skip

PRIO_NOW, PRIO_SIBLING, PRIO_LIBRARY = 0, 1, 2


def load_config():
    try:
        c = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        c = {}
    return {**CONFIG_DEFAULTS, **c, "sensitive": {**CONFIG_DEFAULTS["sensitive"], **(c.get("sensitive") or {})}}


def save_config(changes):
    c = load_config()
    for k, v in changes.items():
        if k == "sensitive" and isinstance(v, dict):
            c["sensitive"].update({cat: mode for cat, mode in v.items()
                                   if cat in CONFIG_DEFAULTS["sensitive"] and mode in ("off", "blur", "skip")})
        elif k == "library":
            c["library"] = bool(v)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(c, indent=1), encoding="utf-8")
    return c


def _low_priority_thread():
    if os.name == "nt":
        try:
            k = ctypes.windll.kernel32
            k.SetThreadPriority(k.GetCurrentThread(), -2)  # THREAD_PRIORITY_LOWEST
        except Exception:
            pass


class Worker:
    def __init__(self, get_app):
        self.get_app = get_app        # -> the App (jf, player); looked up late so it can be created after us
        self._heap, self._seq = [], itertools.count()
        self._queued = {}             # item id -> priority
        self._urgent = set()
        self._cv = threading.Condition()
        self._thread = None
        self._proc = None
        self.clip = None
        self.decoder = None
        self._clip_lock = threading.Lock()
        self._last_work = time.time()
        self._library_checked = 0
        self.current = None           # {id, name, progress, state, mode, started, eta}
        self.errors = {}              # item id -> message
        self.stats = {}               # item id -> {seconds, video_seconds, mode, provider}
        self._sources = {}            # item id -> (src, info)
        self._active = set()

    # ------------------------------------------------------------ public
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="marquee-index", daemon=True)
            self._thread.start()

    def queue(self, item_id, priority=PRIO_NOW, urgent=False):
        item_id = item_id.lower().replace("-", "")
        store.path(item_id)  # validates
        with self._cv:
            if urgent:
                self._urgent.add(item_id)
            if self.current and self.current["id"] == item_id:
                self._cv.notify_all()
                return "running"
            if item_id in self._queued and self._queued[item_id] <= priority:
                return "queued"
            self._queued[item_id] = priority
            heapq.heappush(self._heap, (priority, next(self._seq), item_id))
            self.errors.pop(item_id, None)
            self._cv.notify_all()
        self.start()
        return "queued"

    def cancel(self, item_id):
        with self._cv:
            self._queued.pop(item_id, None)
            self._urgent.discard(item_id)
            self._active.discard(item_id)

    def state(self, item_id):
        item_id = item_id.lower().replace("-", "")
        if self.current and self.current["id"] == item_id:
            return dict(self.current)
        if item_id in self._queued:
            return {"id": item_id, "state": "queued"}
        if item_id in self.errors:
            return {"id": item_id, "state": "error", "error": self.errors[item_id]}
        d = store.load(item_id)
        if d is None:
            return {"id": item_id, "state": "none"}
        m = d["meta"]
        return {"id": item_id, "state": "done" if m.get("complete") else "partial",
                "progress": round(min(1.0, m["done"] / m["duration"]), 3) if m.get("duration") else 0}

    def status(self):
        with self._cv:
            queue = [{"id": i, "priority": p} for p, _, i in sorted(self._heap) if self._queued.get(i) == p]
        return {"current": dict(self.current) if self.current else None, "queue": queue[:50],
                "queued": len(queue), "paused": self._paused_reason(), "model_loaded": self.clip is not None,
                "provider": self.clip.provider if self.clip else None,
                "decoder": self.decoder.last_mode if self.decoder else None, "indexed": len(store.all_ids()),
                "library": load_config()["library"], "recent": dict(list(self.stats.items())[-10:])}

    def get_clip(self):
        """The model, loaded on first use (also used by the API for text queries)."""
        with self._clip_lock:
            if self.clip is None:
                from .clip import Clip
                self.clip = Clip()
            self._last_work = time.time()
            return self.clip

    def get_decoder(self):
        if self.decoder is None:
            ff = deps.find_ffmpeg()
            if not ff:
                raise DecodeError("ffmpeg not found")
            self.decoder = Decoder(ff)
        return self.decoder

    def source(self, item_id):
        """(file path or stream URL, probe info) for an item."""
        hit = self._sources.get(item_id)
        if hit:
            return hit
        app = self.get_app()
        item = app.jf.item(item_id)
        src = app.jf.stream_url(item, "original")
        info = self.get_decoder().probe(src)
        info["name"] = item.get("Name")
        info["item"] = {k: item.get(k) for k in ("Type", "SeriesId", "SeasonId", "IndexNumber", "ParentIndexNumber")}
        self._sources[item_id] = (src, info)
        return src, info

    # ------------------------------------------------------------ loop
    def _playing(self):
        try:
            return bool(self.get_app().player.playing)
        except Exception:
            return False

    def _paused_reason(self):
        return "playing" if self._playing() else None

    def _may_run(self, item_id):
        return not self._playing() or item_id in self._urgent

    def _next(self):
        with self._cv:
            self._heap = [e for e in self._heap if self._queued.get(e[2]) == e[0]]
            heapq.heapify(self._heap)
            for e in sorted(self._heap):
                if self._may_run(e[2]):
                    self._heap.remove(e)
                    heapq.heapify(self._heap)
                    del self._queued[e[2]]
                    return e[2], e[0]
        return None

    def _better_waiting(self, prio):
        with self._cv:
            return any(p < prio and self._queued.get(i) == p for p, _, i in self._heap)

    def _loop(self):
        _low_priority_thread()
        while True:
            try:
                job = self._next()
                if job is None:
                    self._idle()
                    continue
                self._index(*job)
            except Exception:
                traceback.print_exc()
                time.sleep(5)

    def _idle(self):
        if self.clip is not None and time.time() - self._last_work > IDLE_UNLOAD:
            with self._clip_lock:
                self.clip = None
        if not self._heap and not self._playing() and time.time() - self._library_checked > 1800 \
                and load_config()["library"] and deps.availability()["available"]:
            self._library_checked = time.time()
            self._queue_library()
        with self._cv:
            self._cv.wait(5)

    def _queue_library(self):
        try:
            app = self.get_app()
            done = set(store.all_ids())
            items = app.jf.items(Recursive="true", IncludeItemTypes="Movie,Episode", Fields="Path",
                                 SortBy="DateCreated", SortOrder="Descending", Limit=5000)
            for it in items:
                if it["Id"] not in done:
                    self.queue(it["Id"], PRIO_LIBRARY)
        except Exception as e:
            print("index: library scan failed:", e)

    def _stop_requested(self, item_id, prio):
        return (not self._may_run(item_id)) or self._better_waiting(prio) or item_id not in self._active

    def _index(self, item_id, prio):
        self._active = {item_id}
        self.current = {"id": item_id, "state": "starting", "progress": 0.0, "priority": prio}
        t0 = time.time()
        try:
            if not deps.availability()["available"]:
                raise RuntimeError("index components not installed")
            src, info = self.source(item_id)
            self.current["name"] = info.get("name")
            st = os.stat(src) if os.path.exists(src) else None
            ident = {"src": src if st else "stream", "size": st.st_size if st else 0,
                     "mtime": int(st.st_mtime) if st else 0}
            d = store.load(item_id)
            if d is not None and any(d["meta"].get(k) != v for k, v in ident.items()):
                d = None  # the file changed: start over
            if d is not None and d["meta"].get("complete"):
                self.current = None
                return
            if d is None:
                d = store.empty(item_id, {**ident, "duration": info["duration"], "model": deps.MODEL_ID,
                                          "name": info.get("name"), **info["item"]})
            dec, clip = self.get_decoder(), self.get_clip()
            duration = info["duration"]
            video_done = 0.0
            while d["meta"]["done"] < duration - 0.5:
                start = d["meta"]["done"]
                length = min(CHUNK, duration - start)
                self.current.update(state="indexing", progress=round(start / duration, 3))
                res = dec.chunk(src, info, start, length,
                                stop=lambda: self._stop_requested(item_id, prio), on_proc=self._set_proc)
                if res is None:  # a video started, or something more urgent came in: put it back
                    self.current = None
                    if item_id in self._active:
                        self.queue(item_id, prio)
                    return
                frames, cuts = res
                pics, luma = prepare(frames)
                emb = clip.images(pics)
                n = len(emb)
                t = (start + np.arange(n) * SAMPLE_EVERY).astype(np.float32)
                keep = t < duration
                d["t"] = np.concatenate([d["t"], t[keep]])
                d["emb"] = np.concatenate([d["emb"], emb[keep].astype(np.float16)])
                d["luma"] = np.concatenate([d["luma"], luma[keep]])
                d["cuts"] = np.unique(np.concatenate([d["cuts"], np.array(cuts, np.float32)]))
                d["meta"]["done"] = float(start + length)
                d["meta"]["decoder"] = dec.last_mode
                video_done += length
                elapsed = time.time() - t0
                self.current.update(progress=round(d["meta"]["done"] / duration, 3), mode=dec.last_mode,
                                    speed=round(video_done / max(elapsed, 1e-3), 1),
                                    eta=round((duration - d["meta"]["done"]) / max(video_done / elapsed, 1e-3)))
                store.save(item_id, d)
                self._last_work = time.time()
            d["sens"] = analysis.sensitive_scores(d["emb"], clip)
            d["sens_cats"] = list(analysis.SENSITIVE)
            d["meta"].update(complete=True, sens_version=analysis.SENS_VERSION, indexed_at=int(time.time()))
            store.save(item_id, d)
            self.stats[item_id] = {"seconds": round(time.time() - t0, 1), "video_seconds": round(video_done),
                                   "mode": dec.last_mode, "provider": clip.provider}
            self._urgent.discard(item_id)
        except Exception as e:
            traceback.print_exc()
            self.errors[item_id] = str(e)
            self._urgent.discard(item_id)
        finally:
            self.current = None
            self._proc = None

    def _set_proc(self, p):
        self._proc = p
