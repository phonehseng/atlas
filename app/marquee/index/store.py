"""One compact file per title: %APPDATA%\\Marquee\\index\\<itemId>.npz

  t      float32 [N]      sample times (s)
  emb    float16 [N,512]  L2-normalised CLIP image embeddings
  luma   uint8   [N]      mean brightness of the picture area (black frames, fades)
  cuts   float32 [M]      scene-change times (s)
  sens   float16 [N,C]    sensitive-content scores per category (names in sens_cats)
  meta   json             item id, source, size/mtime, duration, model, decoded up to, complete
"""
import json
import re
import threading

from . import deps
from .deps import np

DIR = deps.HOME / "index"
VERSION = 1
_cache = {}
_lock = threading.Lock()


def path(item_id):
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", item_id or ""):
        raise ValueError("bad item id")
    return DIR / f"{item_id.lower().replace('-', '')}.npz"


def empty(item_id, meta):
    return {"t": np.zeros(0, np.float32), "emb": np.zeros((0, 512), np.float16), "luma": np.zeros(0, np.uint8),
            "cuts": np.zeros(0, np.float32), "sens": np.zeros((0, 0), np.float16), "sens_cats": [],
            "meta": {"version": VERSION, "item": item_id, "done": 0.0, "complete": False, **meta}}


def load(item_id):
    p = path(item_id)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    with _lock:
        hit = _cache.get(p)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        with np.load(p, allow_pickle=False) as z:
            d = {k: z[k] for k in ("t", "emb", "luma", "cuts", "sens")}
            d["sens_cats"] = [str(x) for x in z["sens_cats"]]
            d["meta"] = json.loads(str(z["meta"]))
    except Exception:
        return None
    if d["meta"].get("version") != VERSION:
        return None
    with _lock:
        _cache[p] = (mtime, d)
    return d


def save(item_id, d):
    p = path(item_id)
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp.npz")
    np.savez(tmp, t=d["t"].astype(np.float32), emb=d["emb"].astype(np.float16), luma=d["luma"].astype(np.uint8),
             cuts=d["cuts"].astype(np.float32), sens=d["sens"].astype(np.float16),
             sens_cats=np.array(d["sens_cats"], dtype=str), meta=np.array(json.dumps(d["meta"])))
    tmp.replace(p)


def delete(item_id):
    path(item_id).unlink(missing_ok=True)
    with _lock:
        _cache.pop(path(item_id), None)


def all_ids():
    return [p.stem for p in DIR.glob("*.npz") if not p.stem.endswith(".tmp")] if DIR.is_dir() else []
