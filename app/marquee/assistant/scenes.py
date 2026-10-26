"""Scene jumps: "go to the part where Spider-Man gets shot".

The moment is found two ways and the two are ranked together:
* pictures: the media index's CLIP search over the title's frames (GET /api/index/<item>/search; z >= 3 is a real
  match, "start" is the start of the shot). With the AI on, the description is first rewritten into 2-3 short
  visual queries ("man in a spider-man suit hit by gunfire", ...) by one fast-model call (asked for, so allowed
  during playback); classic mode searches the words as they were said.
* dialogue: the title's subtitles, by fuzzy word overlap (quoted words and "where he says ..." count most).
A strong match on either side wins over a weak one on the other. One clear winner: jump ~3 s before it and say so
in the top banner, with Undo (B, or "go back") for 15 s. Several: pause and show up to 3 thumbnails to pick from
(1/2/3, a click, or "the second one"). Not indexed yet: indexing starts now (urgent) and the dialogue alone is used.

OSC protocol (marquee/osc/marquee.lua):
  -> script-message marquee-scene-pick <id> <title> <w> <h> [<time label> <caption> <bgra file>]x1..3
  -> script-message marquee-scene-undo <seconds>        (0 = hide the Undo button)
  <- script-message marquee-scene-choose <id> <n>       (n = 1..3; 0 = closed without choosing)
  <- script-message marquee-scene-undo-now"""
import json
import re
import threading
import time

from .. import settings
from . import llm, subs

UNDO_SECONDS = 15
OVERLAY_DIR = settings.DIR / "assistant" / "scene-thumbs"
STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "is", "are", "was", "were", "be", "it", "its",
        "that", "this", "where", "when", "he", "she", "they", "them", "his", "her", "their", "gets", "get", "got",
        "part", "scene", "bit", "moment", "with", "for", "from", "by", "says", "said", "say", "tells", "told", "asks",
        "goes", "go", "just", "then", "there", "who", "what", "him", "i", "you", "we", "me", "my", "your"}


def clock(s):
    s = int(max(0, s or 0))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _stem(w):
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[:-len(suf)]
    return w


def _words(text):
    return [_stem(w) for w in re.findall(r"[a-z0-9']+", (text or "").lower().replace("'", "")) if w not in STOP]


def spoken_part(text):
    """The words someone says in the scene, if the request quotes them: 'where he says "with great power"'."""
    m = re.search(r"[\"“‘']([^\"”’']{3,})[\"”’']", text or "")
    if m:
        return m.group(1)
    m = re.search(r"\b(?:says?|said|tells (?:\w+ )?|asks?|shouts?|yells?|whispers?|goes)\s+(.{3,})$", text or "", re.I)
    return m.group(1) if m else None


# ------------------------------------------------------------------ the two searches
def visual_queries(text, title=None):
    """2-3 short visual descriptions of the moment (one fast-model call), or just the words as said. Also whether
    the moment can be in this title at all (False only when it names someone or something that certainly isn't)."""
    if not llm.available():
        return [text], None, None
    try:
        out, _ms, _ = llm.complete(
            [{"role": "system", "content": "Turn a request for a moment in a film into 2-3 plain descriptions of what "
                                           "the camera shows (people, objects, action, setting), max 8 words each, with "
                                           "generic words instead of character names (a soldier, a woman, a tank); "
                                           "never invent details. Also give a short line a character probably says in "
                                           'it, or null. Reply JSON only: {"queries": [...], "spoken": "..." | null}'},
             {"role": "user", "content": text}],
            max_tokens=140, temperature=0.0, timeout=15, json_mode=True)
        data = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
        qs = [q.strip() for q in data.get("queries") or [] if isinstance(q, str) and q.strip()][:3]
        spoken = data.get("spoken") if isinstance(data.get("spoken"), str) else None
        return ([text] + qs)[:4], spoken, None
    except Exception as e:
        print("scene query rewrite failed:", e)
        return [text], None, None


def clip_search(item_id, queries):
    """{"indexed": bool, "state": ..., "hits": [{"time", "start", "z", "q"}]} from the media index."""
    from .. import server
    client = server.app.test_client()
    hits, state = [], None
    for q in queries:
        try:
            r = client.get(f"/api/index/{item_id}/search", query_string={"q": q, "k": 6})
            d = r.get_json(silent=True) or {}
        except Exception as e:
            print("index search failed:", e)
            return {"indexed": False, "state": None, "hits": []}
        if r.status_code >= 400 or not d.get("available", True) or d.get("indexed") is False:
            return {"indexed": False, "state": d.get("index"), "available": d.get("available", r.status_code < 400),
                    "hits": []}
        for h in d.get("results") or []:
            hits.append({"time": float(h.get("t", 0)), "start": float(h.get("start", h.get("t", 0))),
                         "z": float(h.get("z") or 0), "q": q})
    return {"indexed": True, "state": state, "hits": hits}


def subtitle_search(app, item, text, spoken=None):
    """[{"time", "score", "text"}] best first: cues sharing the most (stemmed) words with the request."""
    cues = subs.cues_for(app, item) or []
    if not cues:
        return []
    quoted = spoken_part(text)  # words the viewer quoted: trusted; a line the model guessed: much less
    want_spoken = quoted or spoken
    targets = [(want_spoken, 1.0 if quoted else 0.6)] if want_spoken else []
    targets.append((text, 0.8))  # names and key words in the description itself, a little less trusted
    out = []
    for words_src, weight in targets:
        want = set(_words(words_src))
        if not want:
            continue
        words = [set(_words(c["text"])) for c in cues]
        exact = want_spoken.lower().strip(" .!?") if (want_spoken and words_src is want_spoken) else None
        for i, c in enumerate(cues):
            one = len(want & words[i])
            nxt = len(want & words[i + 1]) if i + 1 < len(cues) else 0
            pair = len(want & (words[i] | words[i + 1])) if i + 1 < len(cues) else one
            # a line split over two cues counts for the first; a line in the next cue belongs to that cue
            hit = pair if pair > max(one, nxt) else one
            if not hit:
                continue
            text = c["text"] + (" " + cues[i + 1]["text"] if pair > max(one, nxt) else "")
            score = hit / len(want)
            if len(want) == 1:
                score *= 0.6  # one word alone (a name) is weak evidence
            if exact and exact in text.lower():
                score = 1.0  # the exact line
            out.append({"time": c["start"], "score": round(score * weight, 3), "text": text})
    out.sort(key=lambda h: -h["score"])
    best, seen = [], []
    for h in out:
        if any(abs(h["time"] - s) < 20 for s in seen):
            continue
        seen.append(h["time"])
        best.append(h)
        if len(best) >= 5:
            break
    return best


def rank(clip_hits, sub_hits, raw=None):
    """One list, best first: {"time", "jump", "conf", "source", "z"?, "text"?}. conf ~1 = sure."""
    cands = []
    for h in sorted(clip_hits, key=lambda h: -h["z"]):
        if h["z"] < 2.5:
            continue
        same = next((c for c in cands if c["source"] == "picture" and abs(c["time"] - h["time"]) < 12), None)
        if same:
            same["agree"] += 1
            continue
        penalty = 0 if (raw is None or h.get("q") == raw) else 0.2
        cands.append({"time": h["time"], "jump": h["start"], "z": h["z"], "source": "picture", "agree": 1,
                      "conf": min(1.2, (h["z"] - 2.0) / 2.0 - penalty)})  # z 3 -> 0.5, z 3.5 -> 0.75, z 4 -> 1
    for c in cands:
        if c["agree"] > 1:
            c["conf"] = min(1.2, c["conf"] + 0.1 * (c["agree"] - 1))   # several rewrites found the same shot
    for h in sub_hits:
        if h["score"] < 0.45:
            continue
        conf = 1.2 if h["score"] >= 0.99 else h["score"] * 0.85  # the exact line beats everything else
        near = next((c for c in cands if abs(c["time"] - h["time"]) < 30), None)
        if near:  # picture and dialogue agree: the shot start, with the line as the caption
            near["conf"] = min(1.3, max(near["conf"], conf) + 0.2)
            near["text"] = h["text"]
            near["source"] = "both"
        else:
            cands.append({"time": h["time"], "jump": h["time"], "source": "dialogue", "text": h["text"], "conf": conf})
    cands.sort(key=lambda c: -c["conf"])
    return cands


def scenes_of(cands, gap=150.0):
    """Group candidates into scenes (hits less than `gap` s apart chain together). Each scene is represented by
    its best confidence, jumping to the earliest of its strong hits (the start of the scene, not its middle)."""
    out = []
    for c in sorted(cands, key=lambda c: c["time"]):
        if out and c["time"] - out[-1]["members"][-1]["time"] <= gap:
            out[-1]["members"].append(c)
        else:
            out.append({"members": [c]})
    scenes = []
    for g in out:
        best = max(g["members"], key=lambda c: c["conf"])
        strong = [c for c in g["members"] if c["conf"] >= max(0.4, best["conf"] * 0.75)] or [best]
        first = min(strong, key=lambda c: c["time"])
        seen = [c.get("seen") for c in g["members"] if c.get("seen") is not None]
        scenes.append({**first, "conf": best["conf"], "rejected": best.get("rejected"),
                       "seen": max(seen) if seen else None, "text": best.get("text") or first.get("text"),
                       "source": best["source"], "hits": len(g["members"])})
    scenes.sort(key=lambda c: -c["conf"])
    return scenes


def decide(cands):
    """("winner", [c]) | ("choices", [up to 3]) | ("none", [])."""
    good = [c for c in scenes_of(cands) if not c.get("rejected")
            and c["conf"] >= (0.5 if c["source"] == "dialogue" and c.get("seen") != 2 else 0.4)]
    if not good:
        return "none", []
    top = good[0]
    second = good[1]["conf"] if len(good) > 1 else 0
    sure_enough = top.get("seen") == 2 or (top["source"] in ("dialogue", "both") and top["conf"] >= 0.9) or \
        (top["source"] == "picture" and top.get("seen") is None)
    if sure_enough and top["conf"] >= 0.75 and (top["conf"] - second >= 0.15 or second < 0.5):
        return "winner", [top]
    if len(good) == 1:
        return ("winner" if top["conf"] >= 0.6 and sure_enough else "choices"), [top]
    return "choices", good[:3]


def credits_start(app, item):
    """Where the end credits begin (Jellyfin's Outro segment, else an estimate): scenes are never there."""
    try:
        for seg in app.jf.segments(item["Id"]):
            if seg.get("Type") == "Outro":
                return seg["StartTicks"] / 10_000_000
    except Exception:
        pass
    run = (item.get("RunTimeTicks") or 0) / 10_000_000
    return run - max(90.0, run * 0.065) if run > 600 else None


def _frame_for(app, item, t, w=448):
    from .. import server
    r = server.app.test_client().get(f"/api/index/{item['Id']}/thumb", query_string={"t": f"{t:.2f}", "w": w})
    if r.status_code == 200 and r.mimetype == "image/jpeg":
        return r.data
    from . import frame
    path = (item.get("MediaSources") or [{}])[0].get("Path") or item.get("Path")
    return frame.from_file(path, t, w)


def verify(app, item, text, cands, sub_hits, end=None):
    """Look at the best few candidates with the vision model (one call) and keep what really shows the moment.
    Also looks at the frames of weaker dialogue hits, which the picture search may have missed. None = can't check."""
    from . import vision
    if not vision.available():
        return None
    pool = []
    for c in cands:  # the best picture candidates, one per scene (a few minutes apart)
        if c["source"] != "dialogue" and c["conf"] >= 0.3 and not any(abs(c["time"] - q["time"]) < 150 for q in pool):
            pool.append(c)
        if len(pool) >= 3:
            break
    for h in sub_hits:
        if len(pool) >= 5:
            break
        if 0.2 <= h["score"] < 0.99 and (end is None or h["time"] < end) and \
                not any(abs(h["time"] - c["time"]) < 20 for c in pool):
            pool.append(next((c for c in cands if c["source"] == "dialogue" and abs(c["time"] - h["time"]) < 1), None)
                        or {"time": h["time"], "jump": h["time"], "source": "dialogue", "text": h["text"], "conf": 0.3})
    if not pool:
        return None
    imgs = [_frame_for(app, item, c["time"] + (2 if c["source"] == "dialogue" else 1)) for c in pool]
    title = item.get("SeriesName") or item.get("Name") or "a film"
    prompt = (f"Images 1-{len(imgs)} are single frames from {title}. A viewer asked for the moment: \"{text}\". "
              f"Score each frame: 2 = shows that moment (or a frame just before/after it), 1 = clearly the same scene "
              f"(the same people, place and things the request mentions) but not the moment itself, 0 = anything else. "
              f"If the request names a person, costume or thing that isn't in the frame (or isn't in {title} at all), "
              f"score 0. Reply JSON only: {{\"scores\": [one number per frame]}}")
    out, _ms, _ = vision.ask(prompt, imgs, max_tokens=60, timeout=40, json_mode=True, temperature=0.0)
    try:
        data = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
        scores = [int(x) for x in data.get("scores") or []]
    except Exception:
        return None
    if len(scores) != len(pool):
        return None
    matches = False
    for c, sc in zip(pool, scores):
        c["seen"] = sc
        if sc >= 2 and c["source"] == "dialogue":
            # a loose line in a frame the model liked: worth offering, not jumping to (the check is lenient)
            c["seen"] = 1
            c["conf"] = max(c["conf"], 0.55)
        elif sc >= 2:
            matches = True
            c["conf"] = min(1.3, max(c["conf"], 0.75) + 0.2)
        elif sc == 1 and c["source"] != "dialogue":
            matches = True
            c["conf"] = min(1.1, max(c["conf"], 0.55) + 0.1)   # the right scene: worth offering or jumping near
        elif sc == 1:
            pass  # a loose dialogue hit in a plausible-looking frame proves little
        elif c["source"] != "both" and not (c["source"] == "dialogue" and c["conf"] >= 0.8):
            c["conf"] *= 0.4
            c["rejected"] = True
    rest = [c for c in cands if not any(c is q for q in pool)]
    for c in rest:  # unchecked shots near a confirmed one ride along; the others are trusted less
        near_seen = any((q.get("seen") or 0) >= 1 and abs(c["time"] - q["time"]) < 150 for q in pool)
        if c["source"] == "picture" and not near_seen:
            c["conf"] *= 0.6 if not matches else 0.8
    merged = pool + [c for c in rest if not any(abs(c["time"] - q["time"]) < 12 for q in pool)]
    merged.sort(key=lambda c: -c["conf"])
    return merged


# ------------------------------------------------------------------ the jumper
class Scenes:
    def __init__(self, assistant):
        self.assistant = assistant
        self.app = assistant.app
        self.ctl = assistant.ctl
        self.pick = None       # {"id", "cands", "item", "from", "at"} while choosing
        self.undo = None       # {"to", "until", "item"} after a jump
        self._seq = 0

    # -- finding
    def find(self, item, text, position=None):
        """Everything needed to act: {"kind", "cands", "indexed", "eta", "queries", "why"?}."""
        import os
        if os.environ.get("MARQUEE_SCENES") != "v1":
            from . import scene_search
            self._get_sdh(item)
            found = scene_search.find(self.app, item, text, credits_start(self.app, item))
            found["eta"] = None
            if not found["indexed"]:
                found["eta"] = self._start_indexing(item, found)
            return found
        return self.find_v1(item, text, position)

    def _get_sdh(self, item, wait=30.0):
        """No hearing-impaired subtitles yet: ask Bazarr now (up to ~30 s, then carry on with the normal track;
        the download finishes in the background for next time)."""
        try:
            from . import sdh_fetch
            if not sdh_fetch.bazarr() or sdh_fetch.has_sdh(item) or not sdh_fetch.allowed(item["Id"])[0]:
                return None
        except Exception:
            return None
        ctl = getattr(self, "ctl", None)
        try:
            if ctl is not None and ctl.playing():
                ctl.toast("Getting the detailed subtitles...")
        except Exception:
            pass
        box = {}
        t = threading.Thread(target=lambda: box.update(sdh_fetch.fetch(self.app, item, why="scene search")), daemon=True)
        t.start()
        t.join(wait)
        self.last_sdh = box or {"ok": False, "message": "still fetching"}
        return self.last_sdh

    def _start_indexing(self, item, found):
        try:
            from .. import index
            st = index.touch(item["Id"], urgent=True) or {}
            eta = st.get("eta")
            if eta is None and st.get("state") in ("queued", "starting"):
                run = (item.get("RunTimeTicks") or 0) / 10_000_000
                eta = run / 25 if run else None  # indexing runs at roughly 20-30x real time
            return eta
        except Exception as e:
            print("index touch failed:", e)
            return None

    def find_v1(self, item, text, position=None):
        """The first version (word overlap + CLIP + a lenient look), kept for comparison: MARQUEE_SCENES=v1."""
        title = (item.get("SeriesName") or item.get("Name") or "") + \
            (f" ({item['ProductionYear']})" if item.get("ProductionYear") else "")
        queries, spoken, in_title = visual_queries(text, title)
        res = clip_search(item["Id"], queries)
        eta = None
        if not res["indexed"]:
            try:
                from .. import index
                st = index.touch(item["Id"], urgent=True) or res.get("state") or {}
                eta = st.get("eta")
                if eta is None and st.get("state") in ("queued", "starting"):
                    run = (item.get("RunTimeTicks") or 0) / 10_000_000
                    eta = run / 25 if run else None  # indexing runs at roughly 20-30x real time
            except Exception as e:
                print("index touch failed:", e)
        try:
            sub_hits = subtitle_search(self.app, item, text, spoken)
        except Exception as e:
            print("subtitle search failed:", e)
            sub_hits = []
        cands = rank(res["hits"], sub_hits, raw=text)
        end = credits_start(self.app, item)
        if end:
            cands = [c for c in cands if c["time"] < end]
        checked = None
        if res["indexed"] or sub_hits:
            try:
                checked = verify(self.app, item, text, cands, sub_hits, end)
            except Exception as e:
                print("scene check failed:", e)
        if checked is not None:
            cands = checked
        kind, chosen = decide(cands)
        for c in chosen:
            c["target"] = max(0.0, c["jump"] - 3)
        return {"kind": kind, "cands": chosen, "indexed": res["indexed"], "eta": eta, "queries": queries,
                "all": cands[:6], "checked": checked is not None}

    # -- acting on the playing item (runs in the Asker's thread)
    def run(self, text, ctx, still_current):
        item, pos = ctx["item"], ctx["position"]
        label = describe(text)
        found = self.find(item, text, pos)
        if not still_current():
            return None
        note = ""
        if not found["indexed"]:
            if found["cands"]:
                note = "Still scanning this one: found it from the dialogue. "
            elif found.get("eta"):
                return f"I'll be able to find scenes in this one in about {max(1, round(found['eta'] / 60))} minutes."
            else:
                return "This one isn't scanned for scenes yet; I've started on it."
        if found["kind"] == "absent":
            return f"I don't think that happens in {item.get('SeriesName') or item.get('Name')}."
        if found["kind"] == "none":
            hint = f" ({found['why']})" if found.get("why") else ""
            return f"I couldn't find the part where {text.strip().rstrip('.?!')}{hint}."
        if found["kind"] == "winner":
            c = found["cands"][0]
            self.jump(c["target"], item, pos)
            return f"{note}Jumped to {clock(c['target'])}: {label}"
        self.offer(item, pos, found["cands"], label)
        return None  # the picker is the answer

    def jump(self, target, item, back_to):
        self.pick = None
        self.ctl.seek(target, False)
        self.undo = {"to": back_to, "until": time.time() + UNDO_SECONDS, "item": item.get("Id")}
        self._osc("marquee-scene-undo", str(UNDO_SECONDS))

    def offer(self, item, pos, cands, label):
        self._seq += 1
        try:
            self.ctl.pause("pause")
        except Exception:
            pass
        w, h = self._thumb_size()
        args = [str(self._seq), f"Which one? {label}", str(w), str(h)]
        for n, c in enumerate(cands, 1):
            f = self._thumb_file(item, c["jump"], w, h, n)
            cap = (c.get("text") or "").strip()
            seen = (c.get("describe") or "").strip()
            args += [clock(c["target"]), (f"“{cap[:60]}”" if cap else (seen[:60] or ("picture match" if c["source"] == "picture" else ""))),
                     f or ""]
        self.pick = {"id": self._seq, "cands": cands, "item": item.get("Id"), "from": pos, "at": time.time(), "label": label}
        self.assistant.atlas._publish("clear", "", 0, self.assistant.atlas.gen)
        self._osc("marquee-scene-pick", *args)
        self.ctl.notify("atlas-scenes", {"id": self._seq, "label": label, "item_id": item.get("Id"),
                                         "choices": [{"n": n, "time": c["target"], "label": clock(c["target"]),
                                                      "text": c.get("text"), "source": c["source"],
                                                      "thumb": f"/api/index/{item.get('Id')}/thumb?t={c['jump']:.2f}"}
                                                     for n, c in enumerate(cands, 1)]})

    def choose(self, n, pick_id=None):
        p = self.pick
        if not p or (pick_id is not None and str(pick_id) != str(p["id"])):
            return None
        self.pick = None
        if not n or not 1 <= int(n) <= len(p["cands"]):
            return "Okay."
        c = p["cands"][int(n) - 1]
        item = self.ctl.item() or {"Id": p["item"]}
        self.jump(c["target"], item, p["from"])
        try:
            self.ctl.pause("resume")
        except Exception:
            pass
        text = f"Jumped to {clock(c['target'])}: {p['label']}"
        self.assistant.atlas.say_quietly(text)
        return text

    def undo_jump(self):
        u = self.undo
        if not u or time.time() > u["until"]:
            return None
        self.undo = None
        self.ctl.seek(u["to"], False)
        self._osc("marquee-scene-undo", "0")
        text = f"Back to {clock(u['to'])}"
        self.ctl.toast(text)
        return text

    def undo_active(self):
        return bool(self.undo and time.time() <= self.undo["until"])

    # -- not playing: the details page asks, we answer with a time
    def locate(self, item, text):
        found = self.find(item, text)
        if found["kind"] == "absent":
            return f"I don't think that happens in {item.get('SeriesName') or item.get('Name')}.", []
        if found["kind"] == "none" or not found["cands"]:
            if not found["indexed"]:
                return ("This one isn't scanned for scenes yet; I've started on it"
                        + (f" (about {max(1, round(found['eta'] / 60))} minutes)." if found.get("eta") else ".")), []
            hint = f" ({found['why']})" if found.get("why") else ""
            return f"I couldn't find the part where {text}{hint}.", []
        acts = [{"type": "play_from", "id": item["Id"], "start": c["target"], "label": clock(c["target"]),
                 "text": c.get("text"), "thumb": f"/api/index/{item['Id']}/thumb?t={c['jump']:.2f}"} for c in found["cands"]]
        if found["kind"] == "winner":
            return f"That's at {clock(found['cands'][0]['target'])}. Play from there?", acts
        return "A few places it could be: " + ", ".join(a["label"] for a in acts) + ". Pick one to play from.", acts

    # -- helpers
    def _osc(self, *args):
        try:
            m = getattr(self.ctl.player, "mpv", None)
            if m is not None:
                m.command("script-message", *[str(a) for a in args])
        except Exception as e:
            print("scene osc message failed:", e)

    def _thumb_size(self):
        """Same scale rule as the controls (S = clamp(H/960, 0.7, 2.4)) so the picture fits the tile exactly."""
        try:
            h = (self.ctl.mpv.osd_dimensions or {}).get("h") or 960
        except Exception:
            h = 960
        s = max(0.7, min(2.4, h / 960))
        w = int(240 * s) // 2 * 2
        return w, int(w * 9 / 16) // 2 * 2

    def _thumb_file(self, item, t, w, h, n):
        """A BGRA file of the frame for mpv's overlay-add (index thumb, else straight from the file)."""
        try:
            from io import BytesIO

            from PIL import Image

            from .. import server
            r = server.app.test_client().get(f"/api/index/{item['Id']}/thumb", query_string={"t": f"{t:.2f}", "w": w})
            data = r.data if r.status_code == 200 and r.mimetype == "image/jpeg" else None
            if data is None:
                from . import frame
                path = (item.get("MediaSources") or [{}])[0].get("Path") or item.get("Path")
                data = frame.from_file(path, t, w)
            img = Image.open(BytesIO(data)).convert("RGB")
            scale = max(w / img.width, h / img.height)
            img = img.resize((max(w, round(img.width * scale)), max(h, round(img.height * scale))))
            left, top = (img.width - w) // 2, (img.height - h) // 2
            img = img.crop((left, top, left + w, top + h)).convert("RGBA")
            buf = bytearray(img.tobytes("raw", "BGRA"))
            try:
                from ..player import _round_corners
                _round_corners(buf, w, h, int(12 * w / 240))
            except Exception:
                pass
            OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
            f = OVERLAY_DIR / f"pick{n}.bgra"
            f.write_bytes(bytes(buf))
            return str(f)
        except Exception as e:
            print("scene thumb failed:", e)
            return None


def describe(text):
    t = re.sub(r"^(?:the (?:part|scene|bit|moment) )?(?:where|when)\s+", "", (text or "").strip(), flags=re.I)
    t = t.rstrip(" .?!")
    return (t[:1].upper() + t[1:]) if t else "that scene"
