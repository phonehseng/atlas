"""Finding a described moment in a title ("the part where David's mom dies"): the search behind scene jumps.

Signals:
* the story: the title's subtitles (the SDH/CC track when there is one: its [sound cues] and speaker names make
  events findable) cut into ~4-minute stretches. Each stretch gets a one-line summary from the fast model and two
  embeddings (summary, dialogue), made ONCE per title and cached on disk. A request is embedded and matched against
  them (fast, local), then the fast model reads the best stretches' numbered lines to find the moment.
* the exact words: a quoted line, or the line asked for in other words, chosen by the model from the closest lines.
* the picture: the media index's CLIP search (the words as said + 2-3 visual rewrites), inside the chosen stretches
  and over the whole title.
* a look: the vision model describes each candidate frame first and then judges it against the request.
Decisions are strict: jump on an exact quoted line or when two independent signals agree on the same moment; offer a
picker only for plausible candidates; otherwise say it couldn't be found (with a hint)."""
import concurrent.futures as cf
import json
import re
import threading
import time

from .. import settings
from . import llm, subs

WINDOW = 240.0                      # seconds of dialogue per stretch
CACHE = settings.DIR / "assistant" / "scenes"
SUMMARY_VERSION = 2
_building = {}
_lock = threading.Lock()


def clock(s):
    s = int(max(0, s or 0))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def _title(item):
    name = item.get("SeriesName") and f"{item['SeriesName']} - {item.get('Name')}" or item.get("Name") or "a film"
    return name + (f" ({item['ProductionYear']})" if item.get("ProductionYear") else "")


def search_cues(app, item):
    """The cues used for finding things: SDH/CC when available (sound cues kept), never a forced track."""
    import os
    return subs.cues_for(app, item, sdh=os.environ.get("MARQUEE_SCENE_SDH") != "0") or []  # 0: comparison runs only


# ------------------------------------------------------------------ the story, in stretches
def stretches(cues, size=WINDOW):
    out = {}
    for c in cues:
        k = int(c["start"] // size)
        w = out.setdefault(k, {"i": k, "start": k * size, "end": (k + 1) * size, "cues": []})
        w["cues"].append(c)
    return [out[k] for k in sorted(out)]


def _condense(cues, limit=1400):
    text, last = [], None
    for c in cues:
        t = subs.keep_cues(c["text"])
        if t and t != last:
            text.append(t)
            last = t
    s = " / ".join(text)
    return s if len(s) <= limit else s[:limit // 2] + " / ... / " + s[-limit // 2:]


def _cache_file(item):
    return CACHE / f"{item['Id']}.json"


def _load(item, cues):
    try:
        d = json.loads(_cache_file(item).read_text(encoding="utf-8"))
        if d.get("v") == SUMMARY_VERSION and d.get("cues") == len(cues):
            return d
    except Exception:
        pass
    return None


def summaries(app, item, build=True, workers=4, deadline=None):
    """{"stretches": [{"i", "start", "end", "summary"}], "vec_sum", "vec_dlg", "embed_model"} (cached), or None."""
    cues = search_cues(app, item)
    if not cues or not llm.available():
        return None
    got = _load(item, cues)
    if got is not None or not build:
        return got
    with _lock:
        ev = _building.get(item["Id"])
        mine = ev is None
        if mine:
            ev = _building[item["Id"]] = threading.Event()
    if not mine:
        ev.wait(240)
        return _load(item, cues)
    try:
        title = _title(item)
        parts = stretches(cues)

        def one(w):
            if deadline and time.time() > deadline:
                return None
            out, _ms, _ = llm.complete(
                [{"role": "system", "content": f"You summarise a stretch of {title} from its subtitles (sound cues in "
                                               "brackets or parentheses describe what is heard) in ONE line of at most "
                                               "30 words: who does what, where, and any key event (deaths, fights, "
                                               "crashes, arrivals, reveals). Plain text."},
                 {"role": "user", "content": _condense(w["cues"])}], max_tokens=70, temperature=0.0, timeout=60)
            return re.sub(r"\s+", " ", out).strip()
        with cf.ThreadPoolExecutor(workers) as ex:
            lines = list(ex.map(one, parts))
        result = [{"i": w["i"], "start": w["start"], "end": w["end"], "summary": s or ""} for w, s in zip(parts, lines)]
        d = {"v": SUMMARY_VERSION, "cues": len(cues), "stretches": result}
        try:
            from . import embed
            vs = embed.embed([r["summary"] for r in result])
            vd = embed.embed([_condense(w["cues"], 1200) for w in parts])
            if vs is not None and vd is not None:
                d.update(vec_sum=[[round(float(x), 4) for x in row] for row in vs],
                         vec_dlg=[[round(float(x), 4) for x in row] for row in vd], embed_model=embed.provider()["model"])
        except Exception as e:
            print("scene embeddings failed:", e)
        if all(lines):
            CACHE.mkdir(parents=True, exist_ok=True)
            _cache_file(item).write_text(json.dumps(d), encoding="utf-8")
        return d
    finally:
        with _lock:
            _building.pop(item["Id"], None)
        ev.set()


def prepare(app, item):
    """Build the stretch summaries in the background (when a title is opened; never during playback unless asked)."""
    threading.Thread(target=lambda: summaries(app, item), daemon=True).start()


def rank_stretches(item, d, event, k=3, dialogue_only=False):
    """[(stretch, score)] best first: by meaning (embeddings) when available, else by asking the fast model."""
    parts = d["stretches"]
    if d.get("vec_sum") and d.get("vec_dlg"):
        try:
            import numpy as np

            from . import embed
            q = embed.embed([event], query=True)
            if q is not None:
                vs, vd = np.array(d["vec_sum"], dtype=np.float32), np.array(d["vec_dlg"], dtype=np.float32)
                sc = vd @ q[0] if dialogue_only else 0.6 * (vs @ q[0]) + 0.4 * (vd @ q[0])
                order = np.argsort(-sc)[:k]
                return [(parts[i], float(sc[i])) for i in order]
        except Exception as e:
            print("stretch ranking failed:", e)
    listing = "\n".join(f"{n}. [{clock(p['start'])}] {p['summary']}" for n, p in enumerate(parts, 1))
    out, _ms, _ = llm.complete(
        [{"role": "system", "content": f"Numbered stretches of {_title(item)} with one-line summaries. Which stretches "
                                       "could the requested event happen in? Up to 3 numbers, best first. Reply JSON "
                                       'only: {"stretches": [numbers]}'},
         {"role": "user", "content": f"{listing}\n\nEvent: {event}"}], max_tokens=40, temperature=0.0, timeout=30,
        json_mode=True)
    nums = [int(x) for x in _json(out).get("stretches") or [] if str(x).isdigit()]
    return [(parts[n - 1], 0.0) for n in nums if 1 <= n <= len(parts)][:k]


# ------------------------------------------------------------------ model calls
def pick_line(item, cues, event, count=1):
    """Within some subtitle lines: the line(s) where the event happens (or the line asked for), else []."""
    cues = cues[:110]
    listing = "\n".join(f"{n}. [{clock(c['start'])}] {subs.keep_cues(c['text'])[:120]}" for n, c in enumerate(cues, 1))
    ask = ("the line where the requested event happens, or the line closest to it" if count == 1 else
           f"up to {count} lines that best match the request, best first")
    out, _ms, _ = llm.complete(
        [{"role": "system", "content": f"Numbered subtitle lines from {_title(item)}; sound cues in brackets or "
                                       f"parentheses describe what is heard. Give {ask}. Use 0 / [] if these lines have "
                                       "nothing to do with it. Reply JSON only: "
                                       + ('{"line": number}' if count == 1 else '{"lines": [numbers]}')},
         {"role": "user", "content": f"{listing}\n\nRequest: {event}"}], max_tokens=30, temperature=0.0, timeout=30,
        json_mode=True)
    data = _json(out)
    raw = [data.get("line")] if count == 1 else (data.get("lines") or [])
    out_lines = []
    for x in raw:
        try:
            n = int(x)
        except Exception:
            continue
        if 1 <= n <= len(cues):
            out_lines.append(cues[n - 1])
    return out_lines


def look(app, item, event, times):
    """Describe-then-judge each frame with the vision model: [{"t", "describe", "verdict": yes|related|no}]."""
    from . import vision
    from .scenes import _frame_for
    if not times or not vision.available():
        return None
    imgs = [_frame_for(app, item, t, 448) for t in times]
    prompt = (f"Images 1-{len(imgs)} are frames from {_title(item)}. For EACH image: first describe what is visible in "
              f"one plain sentence (who, doing what, where). Then, using only your description, judge it against the "
              f"request \"{event}\": yes = this frame is from the scene the request describes (the moment itself or "
              f"the same scene around it); related = some of the people/things it names are visible, but it looks like "
              f"a different scene; no = anything else, or the request names a person, creature or costume that isn't "
              f"visible. "
              f'Reply JSON only: {{"frames": [{{"describe": "...", "verdict": "yes|related|no"}}]}}')
    out, _ms, _ = vision.ask(prompt, imgs, max_tokens=90 * len(imgs), timeout=60, json_mode=True, temperature=0.0)
    frames = _json(out).get("frames") or []
    if len(frames) != len(times):
        return None
    return [{"t": t, "describe": str(f.get("describe") or ""), "verdict": str(f.get("verdict") or "").lower()}
            for t, f in zip(times, frames)]


# ------------------------------------------------------------------ the search
def find(app, item, text, credits_at=None, budget=12.0):
    """{"kind": winner|choices|none, "cands": [...], "indexed", "why", "timing", "all"}."""
    from .scenes import clip_search, spoken_part, subtitle_search, visual_queries
    t0 = time.time()
    timing = {}
    try:
        cues = search_cues(app, item)
    except Exception as e:
        print("subtitles failed:", e)
        cues = []
    if credits_at:
        cues = [c for c in cues if c["start"] < credits_at]
    quoted = spoken_part(text)
    dialogue_mode = bool(quoted)
    ai = llm.available()

    def visual():
        if dialogue_mode:
            return [text], None, {"indexed": clip_search(item["Id"], [text])["indexed"], "hits": []}
        qs, spoken, _ = visual_queries(text)
        return qs, spoken, clip_search(item["Id"], qs)

    def story():
        d = summaries(app, item, build=True, deadline=t0 + 90) if cues and ai else None
        if not d:
            return []
        if credits_at:
            keep = [i for i, p in enumerate(d["stretches"]) if p["start"] < credits_at]
            d = {**d, "stretches": [d["stretches"][i] for i in keep],
                 **({"vec_sum": [d["vec_sum"][i] for i in keep], "vec_dlg": [d["vec_dlg"][i] for i in keep]}
                    if d.get("vec_sum") else {})}
        return rank_stretches(item, d, quoted or text, dialogue_only=dialogue_mode)

    with cf.ThreadPoolExecutor(2) as ex:
        fv, fs = ex.submit(visual), ex.submit(story)
        queries, _spoken, res = fv.result()
        timing["visual"] = round(time.time() - t0, 1)
        ranked = fs.result()
        timing["story"] = round(time.time() - t0, 1)
    clip = [h for h in res["hits"] if not credits_at or h["time"] < credits_at]
    cands = []

    def add(t, jump, sig, **kw):
        for c in cands:
            if abs(c["time"] - t) < 45:
                c["signals"].add(sig)
                for k, v in kw.items():
                    if v is not None and (k not in c or (k == "z" and v > c[k]) or (k == "rank" and v < c[k])):
                        c[k] = v
                return c
        c = {"time": t, "jump": jump, "signals": {sig}, **{k: v for k, v in kw.items() if v is not None}}
        cands.append(c)
        return c

    if dialogue_mode:
        q = quoted.lower().strip(" .!?")
        exact = [c for c in cues if q and q in subs.clean(c["text"]).lower()]
        firsts = [c for n, c in enumerate(exact) if all(abs(c["start"] - e["start"]) >= 45 for e in exact[:n])]
        for c in firsts[:3]:
            add(c["start"], c["start"], "quote", text=c["text"])
        if not exact and cues and ai:
            from .scenes import _words
            want = set(_words(quoted))
            near = {round(h["time"], 2) for h in subtitle_search(app, item, f"says {quoted}", None)[:20]}
            pool = [c for c in cues if round(c["start"], 2) in near
                    or len(want & set(_words(c["text"]))) >= max(2, len(want) // 2)][:80]
            for p, _s in ranked[:2]:   # plus the stretches whose dialogue means the same
                pool += [c for c in cues if p["start"] <= c["start"] < p["end"] and c not in pool]
            pool.sort(key=lambda c: c["start"])
            try:
                for ln in pick_line(item, pool, f"the line that means the same as \"{quoted}\" (it may be worded "
                                                f"differently; same meaning, not just shared words)", count=2):
                    add(ln["start"], ln["start"], "paraphrase", text=ln["text"])
            except Exception as e:
                print("paraphrase pick failed:", e)
    else:
        def in_stretch(rank, p):
            lo, hi = p["start"] - 20, p["end"] + 20
            inside = [c for c in cues if lo <= c["start"] <= hi]
            try:
                ln = pick_line(item, inside, text) if inside else []
            except Exception as e:
                print("line pick failed:", e)
                ln = []
            return rank, p, ln, sorted([h for h in clip if lo <= h["time"] <= hi], key=lambda h: -h["z"])
        with cf.ThreadPoolExecutor(3) as ex:
            found = list(ex.map(lambda a: in_stretch(*a), [(r, p) for r, (p, _s) in enumerate(ranked[:3], 1)]))
        for rank, p, ln, pics in found:
            if ln:
                add(ln[0]["start"], ln[0]["start"], "line", text=ln[0]["text"], rank=rank, stretch=p["summary"])
            if pics and pics[0]["z"] >= 2.3:
                add(pics[0]["time"], pics[0]["start"], "picture_in", z=pics[0]["z"], rank=rank, stretch=p["summary"])
        for h in sorted(clip, key=lambda h: -h["z"])[:3]:
            if h["z"] >= 3.3:
                c = add(h["time"], h["start"], "picture", z=h["z"])
                for r, (p, _s) in enumerate(ranked[:3], 1):
                    if p["start"] - 300 <= h["time"] <= p["end"] + 300:
                        c["near_rank"] = min(c.get("near_rank", 9), r)
    timing["lines"] = round(time.time() - t0, 1)

    # look at the best few (one vision call; describe first, then judge). Lines of dialogue can't be seen.
    if not dialogue_mode and cands and time.time() - t0 < budget:
        # the two best story candidates and the two strongest pictures elsewhere; a picture is looked at twice
        # (its frame and ~8 s before, since a shot found by its look often comes just after the moment)
        story_c = sorted([c for c in cands if c.get("rank")], key=lambda c: (-len(c["signals"]), c["rank"]))[:2]
        pics = sorted([c for c in cands if c.get("z") and c not in story_c], key=lambda c: -c["z"])[:2]
        order = story_c + pics
        times, owner = [], []
        for c in order:
            times.append(c["time"] + 1)
            owner.append(c)
            if c in pics:
                times.append(max(0.0, c["time"] - 8))
                owner.append(c)
        try:
            verdicts = look(app, item, text, times)
        except Exception as e:
            print("scene look failed:", e)
            verdicts = None
        rank_v = {"yes": 2, "related": 1}
        for c, v in zip(owner, verdicts or []):
            if rank_v.get(v["verdict"][:7].rstrip(), 0) >= rank_v.get(c.get("look", "")[:7].rstrip(), -1) or "look" not in c:
                c["look"], c["describe"] = v["verdict"], v["describe"]
    timing["look"] = round(time.time() - t0, 1)

    for c in cands:
        c["request"] = text
    kind, chosen, why = decide(cands, bool(cues), res["indexed"], dialogue_mode)
    for c in chosen:
        c["target"] = max(0.0, c["jump"] - 3)
        c["source"] = "dialogue" if c["signals"] & {"quote", "line", "paraphrase"} else "picture"
    for c in cands:
        c["signals"] = sorted(c["signals"])
    return {"kind": kind, "cands": chosen, "indexed": res["indexed"], "why": why, "queries": queries,
            "timing": timing, "all": cands}


def _agrees(c):
    """Does the frame's description mention something the request does? (guards a lenient "yes")"""
    from .scenes import _words
    norm = lambda w: {"women": "woman", "men": "man", "children": "child", "people": "person"}.get(w, w)  # noqa: E731
    want = {norm(w) for w in _words(c.get("request", "")) if len(w) > 2}
    seen = {norm(w) for w in _words(c.get("describe", "")) if len(w) > 2}
    return bool(want & seen)


def strength(c, dialogue_mode=False):
    s, lk = c["signals"], c.get("look", "")
    if "quote" in s:
        return 3
    if dialogue_mode:
        return 2 if "paraphrase" in s else 0
    if lk.startswith("no"):
        return 0
    line, pin, pic = "line" in s, "picture_in" in s, "picture" in s
    yes, related = lk.startswith("yes"), lk.startswith("related")
    rank = c.get("rank", 9)
    if yes and (pin or (line and _agrees(c))) and rank <= 3:
        return 3                     # the story and the picture check agree
    if yes and line and rank <= 3:
        return 2                     # a line + a "yes" whose description shares nothing with the request: offer only
    if line and pin and rank == 1 and related:
        return 3                     # top stretch, its line and its picture coincide, and it looks like the scene
    near = c.get("near_rank", 9)       # a strong picture just outside the top stretches
    if yes and pic and near <= 2 and (c.get("z") or 0) >= 3.8:
        return 3
    if (yes or related) and pic and (near <= 3 or (c.get("z") or 0) >= 4.0):
        return 2
    if yes and pic:
        return 2
    if related and (line or pin) and rank <= 3:
        return 2
    if line and rank <= 2 and not lk:
        return 2                     # nothing to look at (no index / no vision): offer, don't jump
    return 0


def decide(cands, have_subs, indexed, dialogue_mode=False):
    for c in cands:
        c["strength"] = strength(c, dialogue_mode)
    ranked = sorted([c for c in cands if c["strength"] > 0],
                    key=lambda c: (-c["strength"], c.get("rank", 9), -(c.get("z") or 0)))
    sure = [c for c in ranked if c["strength"] >= 3]
    if sure:
        quotes = sorted([c for c in sure if "quote" in c["signals"]], key=lambda c: c["time"])
        if len(quotes) > 1:          # the line is said more than once
            return "choices", quotes[:3], None
        if max(c["time"] for c in sure) - min(c["time"] for c in sure) < 300:
            return "winner", [min(sure, key=lambda c: c["time"])], None
        return "choices", sure[:3], None
    plausible = [c for c in ranked if c["strength"] >= 2]
    if plausible:
        return "choices", plausible[:3], None
    if not indexed and not have_subs:
        return "none", [], "this one isn't scanned for scenes yet and has no text subtitles"
    return "none", [], ("try a line someone says, in quotes" if dialogue_mode else
                        "try describing what's on screen or a line someone says")
