"""Key moments of a title: a handful of frames straight from the media file, one per part of the story, each with a
time and a short caption from the story-stretch summaries. Shown on the Up-next screen (click = play from there) and
used as visual context for the whole-title ending comment. Built in the prepare-ahead window; cached on disk."""
import json
import re

from .. import settings
from . import llm

DIR = settings.DIR / "assistant" / "moments"
VERSION = 2  # 2: HDR frames tone mapped


def _cache(item_id):
    return DIR / item_id / "moments.json"


def cached(item_id):
    try:
        d = json.loads(_cache(item_id).read_text(encoding="utf-8"))
        return d if d.get("v") == VERSION else None
    except Exception:
        return None


def image_path(item_id, n):
    f = DIR / item_id / f"{int(n)}.jpg"
    return f if f.exists() else None


def _cuts(item_id):
    try:
        from ..index import store
        d = store.load(item_id.lower().replace("-", ""))
        if d is not None and len(d.get("cuts", [])):
            return [float(x) for x in d["cuts"]]
    except Exception:
        pass
    return []


def _shot_near(cuts, t, within=40.0):
    """A moment at the start of a shot near t looks better than a frame in the middle of a cut."""
    best = min(cuts, key=lambda c: abs(c - t), default=None) if cuts else None
    return best + 1.0 if best is not None and abs(best - t) <= within else t


def _captions(item, parts):
    """3-6 word captions for the chosen stretches (one fast-model call), else the summaries cut short."""
    short = [re.sub(r"\s+", " ", p["summary"]).split(";")[0].split(",")[0][:60] for p in parts]
    if not llm.available():
        return short
    try:
        listing = "\n".join(f"{n}. {p['summary']}" for n, p in enumerate(parts, 1))
        out, _ms, _ = llm.complete(
            [{"role": "system", "content": "For each numbered summary of a part of a film, write a caption of 3 to 6 "
                                           "words (like a chapter title, no full stop). Reply JSON only: "
                                           '{"captions": ["...", ...]}'},
             {"role": "user", "content": listing}], max_tokens=40 * len(parts), temperature=0.2, timeout=40,
            json_mode=True)
        m = re.search(r"\{.*\}", out, re.S)
        caps = json.loads(m.group(0)).get("captions") if m else None
        if isinstance(caps, list) and len(caps) == len(parts):
            return [str(c).strip().rstrip(".")[:60] or s for c, s in zip(caps, short)]
    except Exception as e:
        print("moment captions failed:", e)
    return short


def build(app, item, n=5, credits_at=None):
    """[{"n", "t", "label", "caption", "img"}] (cached). Needs the story stretches (scene_search.summaries)."""
    from . import frame, scene_search
    got = cached(item["Id"])
    if got:
        return got["moments"]
    d = scene_search.summaries(app, item, build=True)
    if not d or not d.get("stretches"):
        return []
    path = (item.get("MediaSources") or [{}])[0].get("Path") or item.get("Path")
    import os
    if not path or not os.path.exists(path):
        return []
    parts = [p for p in d["stretches"] if p["start"] >= 60 and (not credits_at or p["end"] <= credits_at + 30)]
    if not parts:
        return []
    k = min(n, len(parts))
    chosen = [parts[round(i * (len(parts) - 1) / max(1, k - 1))] for i in range(k)] if k > 1 else parts[:1]
    chosen = list({p["i"]: p for p in chosen}.values())
    cuts = _cuts(item["Id"])
    caps = _captions(item, chosen)
    out = []
    (DIR / item["Id"]).mkdir(parents=True, exist_ok=True)
    for i, (p, cap) in enumerate(zip(chosen, caps)):
        mid = (p["start"] + min(p["end"], credits_at or p["end"])) / 2
        t = _shot_near(cuts, mid)
        try:
            (DIR / item["Id"] / f"{i}.jpg").write_bytes(frame.from_file(path, t, 480, hdr=frame.is_hdr(item)))
        except Exception as e:
            print("moment frame failed:", e)
            continue
        out.append({"n": i, "t": round(t, 1), "label": scene_search.clock(t), "caption": cap,
                    "summary": p["summary"], "img": f"/api/assistant/moments/{item['Id']}/{i}.jpg"})
    if out:
        _cache(item["Id"]).write_text(json.dumps({"v": VERSION, "moments": out}), encoding="utf-8")
    return out
