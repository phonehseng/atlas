"""Getting SDH ("hearing impaired") English subtitles from Bazarr for titles that don't have them.

They carry sound cues ([gunshot], (SCREAMING)) and speaker names, which make scene search and the loud-moment
helper much better. Bazarr saves them next to the video (*.en.hi.srt); Marquee reads that file for searching only:
the subtitles the viewer picked don't change.

When: a few minutes into playing a title (background, low priority, one at a time); right away when a scene search
finds no SDH track (with a ~30 s limit); and a slow idle sweep over Continue watching / Next up. At most a few per
hour (subtitle providers have daily quotas), and a title that found nothing isn't tried again for 7 days.
Settings: sdh_fetch (on), bazarr_url / bazarr_key (else mediastack.json's "bazarr" entry); no Bazarr = skip quietly."""
import json
import os
import re
import threading
import time
from pathlib import Path

import requests

from .. import settings

STATE = settings.DIR / "assistant" / "sdh_fetch.json"
PER_HOUR = 4
RETRY_AFTER = 7 * 86400
SIDE = re.compile(r"\.(?:en|eng|english)?\.?(?:hi|sdh|cc)(?:\.forced)?\.(?:srt|ass|ssa|vtt)$", re.I)
_lock = threading.Lock()
_busy = threading.Lock()
_lists = {}


# ------------------------------------------------------------------ where Bazarr is
def bazarr():
    """{"url", "key"} or None."""
    s = settings.load()
    if not s.get("sdh_fetch", True):
        return None
    url, key = (s.get("bazarr_url") or "").rstrip("/"), s.get("bazarr_key") or ""
    if not (url and key):
        url = url or (os.environ.get("MARQUEE_DEV_BAZARR") or "").rstrip("/")
        key = key or os.environ.get("MARQUEE_DEV_BAZARR_KEY") or ""
    if not (url and key):
        try:
            from ..stack import stack_config
            b = ((stack_config() or {}).get("apps") or {}).get("bazarr") or {}
            url, key = url or (b.get("url") or "").rstrip("/"), key or b.get("key") or ""
        except Exception:
            pass
    return {"url": url, "key": key} if url and key else None


def _api(b, method, path, timeout=60, **kw):
    r = requests.request(method, f"{b['url']}/api{path}", headers={"X-API-KEY": b["key"]}, timeout=timeout, **kw)
    r.raise_for_status()
    return r.json() if r.content and "json" in r.headers.get("content-type", "") else None


def _norm(p):
    return os.path.normcase(os.path.normpath(p or "")).replace("\\", "/")


def _list(b, kind, **params):
    key = (kind, tuple(sorted(params.items())))
    hit = _lists.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    data = (_api(b, "GET", f"/{kind}", params={"length": -1, **params}) or {}).get("data") or []
    _lists[key] = (time.time(), data)
    return data


def bazarr_ids(b, item):
    """("movie", {"radarrid"}) | ("episode", {"seriesid", "episodeid"}) for the item's file, or None."""
    path = _norm((item.get("MediaSources") or [{}])[0].get("Path") or item.get("Path"))
    if not path:
        return None
    if item.get("Type") == "Movie":
        m = next((m for m in _list(b, "movies") if _norm(m.get("path")) == path), None)
        return ("movie", {"radarrid": m["radarrId"]}) if m else None
    for s in _list(b, "series"):
        sp = _norm(s.get("path"))
        if sp and path.startswith(sp.rstrip("/") + "/"):
            eps = _list(b, "episodes", **{"seriesid[]": s["sonarrSeriesId"]})
            e = next((e for e in eps if _norm(e.get("path")) == path), None)
            if e:
                return "episode", {"seriesid": s["sonarrSeriesId"], "episodeid": e["sonarrEpisodeId"]}
    return None


# ------------------------------------------------------------------ does it have SDH already?
def sidecar(item):
    """A *.hi/sdh/cc subtitle file next to the video, or None."""
    path = (item.get("MediaSources") or [{}])[0].get("Path") or item.get("Path")
    if not path or not os.path.exists(path):
        return None
    p = Path(path)
    try:  # (no glob: titles often contain [brackets])
        for f in sorted(p.parent.iterdir()):
            if f.name.startswith(p.stem + ".") and SIDE.search(f.name):
                return f
    except OSError:
        pass
    return None


def sound_cues(f):
    """How many cues of a subtitle file carry [sound] / (SOUND) / SPEAKER: marks (real SDH has plenty)."""
    from . import subs
    try:
        cues = subs.parse_srt(Path(f).read_text(encoding="utf-8-sig", errors="replace"), keep=True)
    except Exception:
        return 0, 0
    return sum(1 for c in cues if subs.CUE.search(c["text"]) or re.match(r"[A-Z][A-Z .'-]{2,}:", c["text"])), len(cues)


def has_sdh(item):
    from . import subs
    streams = (item.get("MediaSources") or [{}])[0].get("MediaStreams") or []
    if any(s.get("Type") == "Subtitle" and not subs._forced(s) and subs._sdh(s)
           and (s.get("Language") or "eng").lower().startswith("en") for s in streams):
        return True
    return sidecar(item) is not None


# ------------------------------------------------------------------ rate limit and memory
def _state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"recent": [], "failed": {}, "done": {}}


def _save(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st), encoding="utf-8")


def allowed(item_id):
    st = _state()
    now = time.time()
    if now - st["failed"].get(item_id, 0) < RETRY_AFTER:
        return False, "nothing found for it recently"
    if len([t for t in st["recent"] if now - t < 3600]) >= PER_HOUR:
        return False, "hourly limit"
    return True, None


# ------------------------------------------------------------------ the fetch
def fetch(app, item, timeout=None, why="background"):
    """Ask Bazarr for an English hearing-impaired subtitle. {"ok", "path"?, "provider"?, "message"}."""
    b = bazarr()
    if not b:
        return {"ok": False, "message": "no Bazarr configured"}
    if has_sdh(item):
        return {"ok": True, "message": "already has SDH", "path": str(sidecar(item) or "")}
    with _lock:
        ok, reason = allowed(item["Id"])
        if not ok:
            return {"ok": False, "message": reason}
    if not _busy.acquire(timeout=timeout or 0.1):
        return {"ok": False, "message": "another fetch is running"}
    try:
        ids = bazarr_ids(b, item)
        if not ids:
            return {"ok": False, "message": "Bazarr doesn't know this file"}
        kind, params = ids
        with _lock:
            st = _state()
            st["recent"] = [t for t in st["recent"] if time.time() - t < 3600] + [time.time()]
            _save(st)
        results = (_api(b, "GET", f"/providers/{kind}s", params={k: v for k, v in params.items()
                                                                   if kind == "movie" or k == "episodeid"},
                        timeout=timeout or 180) or {}).get("data") or []

        def is_hi(r):
            return str(r.get("hearing_impaired")).lower() == "true" or bool(
                re.search(r"\b(sdh|hi|cc|hearing.impaired)\b", " ".join(r.get("release_info") or []), re.I))
        pick = sorted([r for r in results if (r.get("language") or "").lower().startswith("en")
                       and str(r.get("forced")).lower() != "true" and is_hi(r)],
                      key=lambda r: -(r.get("score") or 0))
        if not pick:
            with _lock:
                st = _state()
                st["failed"][item["Id"]] = time.time()
                _save(st)
            provs = sorted({r.get("provider") for r in results if r.get("provider")})
            return {"ok": False, "message": f"no hearing-impaired English subtitle from Bazarr's providers"
                                            f" ({len(results)} results{', from ' + ', '.join(provs) if provs else ''})"}
        r = pick[0]
        form = {**params, "hi": "True", "forced": "False", "original_format": str(r.get("original_format") or "False"),
                "provider": r["provider"], "subtitle": r["subtitle"]}
        _api(b, "POST", f"/providers/{kind}s", data=form, timeout=timeout or 180)
        f = None
        for _ in range(40):  # Bazarr writes the file a moment after answering
            f = sidecar(item)
            if f:
                break
            time.sleep(0.5)
        marked, total = sound_cues(f) if f else (0, 0)
        with _lock:
            st = _state()
            st["done"][item["Id"]] = {"at": time.time(), "provider": r["provider"], "path": str(f or ""), "why": why,
                                      "sound_cues": marked, "lines": total}
            _save(st)
        try:
            from . import subs
            with subs._lock:
                subs._cache.pop((item["Id"], "sdh"), None)
        except Exception:
            pass
        real = total and marked >= max(5, total * 0.03)
        return {"ok": True, "provider": r["provider"], "path": str(f or ""), "score": r.get("score"),
                "sound_cues": marked, "lines": total,
                "message": "downloaded" + ("" if real else " (labelled hearing-impaired, but it has hardly any sound cues)")}
    except Exception as e:
        return {"ok": False, "message": f"Bazarr: {e}"}
    finally:
        _busy.release()


def fetch_soon(app, item, delay=180, why="playing"):
    """In the background, a few minutes into playing a title (if it's still the one playing)."""
    def run():
        time.sleep(delay)
        p = getattr(app, "player", None)
        if getattr(p, "playing", False) and (getattr(p, "item", None) or {}).get("Id") == item.get("Id"):
            try:
                import ctypes
                k = ctypes.windll.kernel32
                k.SetThreadPriority(k.GetCurrentThread(), -1)
            except Exception:
                pass
            res = fetch(app, item, why=why)
            if res.get("ok") and res.get("provider"):
                print("detailed subtitles fetched:", res.get("provider"))
    if bazarr() and not has_sdh(item):
        threading.Thread(target=run, daemon=True).start()


def idle_sweep(app):
    """One quiet pass over Continue watching / Next up (call every ~20 minutes when nothing plays)."""
    if not bazarr() or getattr(getattr(app, "player", None), "playing", False):
        return None
    jf = app.jf
    try:
        items = jf.resume(limit=10) + jf.next_up(limit=10)
    except Exception:
        return None
    for it in items:
        if str(it.get("Id", "")).startswith("file:") or has_sdh(it):
            continue
        ok, _ = allowed(it["Id"])
        if ok:
            return fetch(app, it, why="idle")
    return None
