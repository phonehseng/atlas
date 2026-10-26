"""The "what to watch next" screen at the credits: a short comment on the ending and a few picks.

The player calls prepare(item, credits_at) about 6 minutes before the credits (once per play). A low-priority
thread then works it out ahead of time: the picks (rule-based: next episode, the next film of a collection, similar
titles from the library), the dialogue of the last ~12 minutes before the credits (sidecar subtitle file, else the
embedded text track via ffmpeg, else Jellyfin), optionally how this exact title's ending was received (a web
search sending only title/year/episode, setting "ai_web_search"; snippets mentioning later episodes, seasons or
sequels are dropped), optionally one frame looked at by the vision model when it's already loaded, and finally one
call to the fast model for the
comment (and the picks' one-line reasons). get() answers instantly: "ready" from the cache, else "pending" with the
picks; the comment follows through notify_ui("atlas-ending", ...). Spoilers about what was just watched are fine;
the next episode and the picks are never spoiled (their plots are not even given to the model, and a comment that
names them is regenerated from local data only)."""
import ctypes
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import requests

from .. import settings
from . import llm, subs, vision

TICKS = 10_000_000
LOW = 0x00004000        # BELOW_NORMAL_PRIORITY_CLASS for ffmpeg
TAIL = 12 * 60          # dialogue window before the credits
_jobs = {}              # item id -> Job
_lock = threading.Lock()


class Job:
    def __init__(self, item_id, credits_at):
        self.item_id = item_id
        self.credits_at = credits_at
        self.state = "pending"
        self.picks = []
        self.comment = None
        self.cancelled = False
        self.started = time.time()
        self.finished = None
        self.notes = {}

    def public(self):
        return {"item_id": self.item_id, "state": self.state, "thinking": self.state == "pending",
                "whole": bool(self.notes.get("whole")), "moments": self.notes.get("moments", 0),
                "comment": self.comment, "picks": self.picks, "credits_at": self.credits_at}


# ------------------------------------------------------------------ entry points
def prepare(app, item_id, credits_at=None, late=False):
    with _lock:
        job = _jobs.get(item_id)
        if job and not job.cancelled and (job.state == "ready" or time.time() - job.started < 900):
            return job.public()
        job = _jobs[item_id] = Job(item_id, credits_at)
        job.late = late
        if len(_jobs) > 30:
            for k in list(_jobs)[:10]:
                _jobs.pop(k, None)
    threading.Thread(target=_work, args=(app, job), daemon=True).start()
    return job.public()


def get(app, item_id):
    with _lock:
        job = _jobs.get(item_id)
    if job is None or job.cancelled:
        return prepare(app, item_id, late=True)  # credits started without a prepare: start now, 25 s budget
    if job.state == "pending" and not job.picks:
        for _ in range(30):  # the picks come first and fast: give them a moment
            if job.picks or job.state != "pending":
                break
            time.sleep(0.1)
    return job.public()


def cancel(item_id):
    with _lock:
        job = _jobs.pop(item_id, None)
    if job and job.state == "pending":
        job.cancelled = True
    return bool(job)


# ------------------------------------------------------------------ the work
def _low_priority():
    try:
        k = ctypes.windll.kernel32
        k.SetThreadPriority(k.GetCurrentThread(), -1)  # THREAD_PRIORITY_BELOW_NORMAL
    except Exception:
        pass


def _work(app, job):
    _low_priority()
    try:
        item = _item(app, job.item_id)
        if item is None:
            job.state = "ready"
            return
        if not job.credits_at:
            job.credits_at = _credits_at(app, item)
        job.picks = picks(app, item)
        if job.cancelled:
            return
        _notify(app, job)
        from .. import ai, scripted
        if ai.mode() == "classic":  # no model: a prepared, spoiler-free line and rule-made reasons
            comment, whys = scripted.ending_comment(app, item, job.picks)
            job.comment = comment
            for p in job.picks:
                w = whys.get(p.get("n")) or whys.get(str(p.get("n")))
                if w and p.get("kind") == "similar":
                    p["why"] = w
            return
        deadline = job.started + 25 if getattr(job, "late", False) else None
        dialogue = ""
        try:
            dialogue = ending_dialogue(app, item, job.credits_at)
        except Exception as e:
            print("ending subtitles failed:", e)
        job.notes["dialogue_chars"] = len(dialogue)
        whole = None
        try:  # watched (nearly) all of it: comment on the whole story, not just the last minutes
            from . import watched
            cov = watched.coverage(item["Id"], (item.get("RunTimeTicks") or 0) / TICKS, until=job.credits_at)
            job.notes["watched"] = round(cov, 2)
            if cov >= WHOLE and not getattr(job, "late", False) and not job.cancelled:
                whole = whole_story(app, item, job.credits_at)
                job.notes["whole"] = bool(whole)
                job.notes["season_context"] = bool(whole and whole.get("season"))
        except Exception as e:
            print("whole-story outline failed:", e)
        try:  # the moments for the Up-next screen (frames from the file), whatever was watched
            if not getattr(job, "late", False) and not job.cancelled:
                from . import moments
                job.notes["moments"] = len(moments.build(app, item, 5, job.credits_at))
        except Exception as e:
            print("moments failed:", e)
        forbidden = future_names(app, item, job.picks)
        snippets = []
        if web_search_on() and not job.cancelled:
            try:
                raw = web_snippets(search_query(item))
                snippets = spoiler_free(raw, item, forbidden)
                job.notes["snippets"] = f"{len(snippets)} of {len(raw)} kept"
            except Exception as e:
                print("ending search failed:", e)
        scene = None
        if job.cancelled:
            return
        try:
            if whole:  # one vision call over a few key moments and the final shot
                scene = _look_at_story(app, item, job.credits_at)
            elif not deadline or time.time() < deadline - 12:
                scene = _look_at_end(item, job.credits_at)
        except Exception as e:
            print("ending frame failed:", e)
        job.notes["scene"] = bool(scene)
        if job.cancelled or (deadline and time.time() > deadline - 3):
            return
        try:
            budget = max(5, deadline - time.time() - 1) if deadline else 60  # prepared ahead: more time
            comment, whys = _comment(item, dialogue, scene, job.picks, snippets, forbidden, budget, whole)
            if comment and mentions_future(comment, forbidden):
                job.notes["regenerated"] = True  # touched on what comes next: again, from local data only
                comment, whys = _comment(item, dialogue, scene, job.picks, None, forbidden, budget, whole)
                if comment and mentions_future(comment, forbidden):
                    comment = None
            job.comment = comment
            for p in job.picks:
                w = whys.get(str(p.get("n")))
                if w and p.get("kind") == "similar":
                    p["why"] = w
        except Exception as e:
            print("ending comment failed:", e)
    finally:
        if not job.cancelled:
            job.state = "ready"
            job.finished = time.time()
            _notify(app, job)


def _notify(app, job):
    try:
        if getattr(app, "window", None) is not None:
            app.notify_ui("atlas-ending", job.public())
    except Exception:
        pass


def _item(app, item_id):
    p = getattr(app, "player", None)
    cur = getattr(p, "item", None)
    if cur and cur.get("Id") == item_id:
        return cur if cur.get("Overview") is not None or str(item_id).startswith("file:") else app.jf.item(item_id)
    if str(item_id).startswith("file:"):
        return None
    try:
        return app.jf.item(item_id)
    except Exception:
        return None


def _credits_at(app, item):
    try:
        for s in app.jf.segments(item["Id"]):
            if s.get("Type") == "Outro":
                return s["StartTicks"] / TICKS
    except Exception:
        pass
    run = (item.get("RunTimeTicks") or 0) / TICKS
    return run * (0.93 if run > 3000 else 0.95) if run else None


def _title(item, year=False):
    if item.get("Type") == "Episode":
        return f"{item.get('SeriesName')} season {item.get('ParentIndexNumber')} episode {item.get('IndexNumber')}"
    return item.get("Name", "") + (f" ({item['ProductionYear']})" if year and item.get("ProductionYear") else "")


# ------------------------------------------------------------------ picks (no model needed)
def picks(app, item):
    from ..server import card
    jf, out = app.jf, []
    genres = set(item.get("Genres") or [])
    if item.get("Type") == "Episode" and item.get("SeriesId"):
        try:
            eps = jf.get(f"/Shows/{item['SeriesId']}/Episodes", userId=jf.user, startItemId=item["Id"], Limit=2,
                         Fields="UserData,PrimaryImageAspectRatio").get("Items", [])
            nxt = eps[1] if len(eps) > 1 and eps[0]["Id"] == item["Id"] else None
        except Exception:
            nxt = None
        if nxt:
            out.append({"id": nxt["Id"], "kind": "next", "type": "Episode",
                        "title": f"S{nxt.get('ParentIndexNumber', 0)}:E{nxt.get('IndexNumber', 0)} · {nxt.get('Name', '')}",
                        "plain_title": nxt.get("Name"), "why": "Up next", "card": card(jf.item(nxt["Id"]))})
        try:
            genres |= set(jf.item(item["SeriesId"]).get("Genres") or [])
        except Exception:
            pass
    elif item.get("Type") == "Movie":
        nxt = _next_in_collection(app, item)
        if nxt:
            out.append(nxt)
    base = item.get("SeriesId") if item.get("Type") == "Episode" else item["Id"]
    try:
        sim = jf.similar(base, limit=16)
    except Exception:
        sim = []
    sim = sorted(sim, key=lambda i: (bool((i.get("UserData") or {}).get("Played")), -(i.get("CommunityRating") or 0)))
    taken = {p.get("id") for p in out}
    for i in sim:
        if len([p for p in out if p["kind"] == "similar"]) >= 4:
            break
        if i["Id"] in taken:
            continue
        shared = [g for g in (i.get("Genres") or []) if g in genres]
        why = f"More {shared[0].lower()}" + (f" and {shared[1].lower()}" if len(shared) > 1 else "") if shared else "A similar feel"
        out.append({"id": i["Id"], "kind": "similar", "type": i.get("Type"), "title": i.get("Name"),
                    "year": i.get("ProductionYear"), "why": why, "genres": (i.get("Genres") or [])[:3], "card": card(i)})
    for n, p in enumerate(out, 1):
        p["n"] = n
    return out


def _next_in_collection(app, item):
    tmdb = (item.get("ProviderIds") or {}).get("Tmdb")
    sr = app.seerr
    if not tmdb or not getattr(sr, "ready", False):
        return None
    try:
        d = sr.call("GET", f"/movie/{tmdb}")
        col = (d.get("collection") or {}).get("id")
        if not col:
            return None
        c = sr.call("GET", f"/collection/{col}")
        parts = sorted([p for p in c.get("parts", []) if p.get("releaseDate")], key=lambda p: p["releaseDate"])
        ids = [p["id"] for p in parts]
        if int(tmdb) not in ids or ids.index(int(tmdb)) + 1 >= len(ids):
            return None
        p = sr.card({**parts[ids.index(int(tmdb)) + 1], "mediaType": "movie"})
        later = [x.get("title") for x in parts[ids.index(int(tmdb)) + 1:] if x.get("title")]
        out = {"tmdb": p["id"], "kind": "sequel", "type": "Movie", "title": p["title"], "year": p.get("year"),
               "why": f"Next in {c.get('name') or 'the series'}", "request": p, "later_titles": later}
        if p.get("jellyfin"):
            from ..server import card
            out["id"] = p["jellyfin"]
            try:
                out["card"] = card(app.jf.item(p["jellyfin"]))
            except Exception:
                pass
        return out
    except Exception as e:
        print("collection lookup failed:", e)
        return None


# ------------------------------------------------------------------ the last minutes' dialogue
def _sidecar(path):
    p = Path(path)
    try:  # (no glob: titles often contain [brackets])
        cands = [x for x in p.parent.iterdir() if x.name.startswith(p.stem + ".")
                 and x.suffix.lower() in (".srt", ".ass", ".ssa", ".vtt")]
    except OSError:
        cands = []
    cands.sort(key=lambda x: (0 if re.search(r"\.(en|eng|english)\b", x.name, re.I) else 1,
                              1 if re.search(r"forced|sdh", x.name, re.I) else 0))
    return cands[0] if cands else None


def _parse_ass(text):
    cues = []
    for line in text.splitlines():
        if line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            if len(parts) == 10:
                try:
                    a, b = subs._ts(parts[1].strip()), subs._ts(parts[2].strip())
                except Exception:
                    a = b = 0
                t = subs.clean(parts[9])
                if t:
                    cues.append({"start": a, "end": b, "text": t})
    return cues


def _embedded(path, item, start, length):
    from ..index import deps
    ff = deps.find_ffmpeg()
    src = (item.get("MediaSources") or [{}])[0]
    streams = [s for s in src.get("MediaStreams") or [] if s.get("Type") == "Subtitle" and not s.get("IsExternal")
               and (s.get("Codec") or "").lower() in subs.TEXT_CODECS]
    streams = [s for s in streams if not subs._forced(s)] or streams  # forced tracks only cover foreign lines
    if not ff or not streams:
        return None
    prefs = [x for x in settings.load()["sub_lang"].split(",") + ["eng", "en"] if x]
    pick = next((s for lang in prefs for s in streams if (s.get("Language") or "").lower().startswith(lang[:2])
                 and not s.get("IsForced")), streams[0])
    r = subprocess.run([ff, "-v", "error", "-ss", f"{start:.1f}", "-t", f"{length:.1f}", "-i", path,
                        "-map", f"0:{pick['Index']}", "-f", "srt", "pipe:1"], capture_output=True, timeout=120,
                       creationflags=0x08000000 | LOW)
    if r.returncode:
        return None
    return subs.parse_srt(r.stdout.decode("utf-8", "replace"))


def ending_dialogue(app, item, credits_at, length=TAIL):
    """Condensed dialogue of the `length` seconds before the credits ('' when there are no text subtitles)."""
    if not credits_at:
        return ""
    start = max(0.0, credits_at - length)
    path = (item.get("MediaSources") or [{}])[0].get("Path") or item.get("Path")
    cues = None
    if path and os.path.exists(path):
        side = _sidecar(path)
        if side:
            text = side.read_text(encoding="utf-8-sig", errors="replace")
            cues = _parse_ass(text) if side.suffix.lower() in (".ass", ".ssa") else subs.parse_srt(text)
            cues = [c for c in cues if start <= c["start"] <= credits_at]
        if not cues:
            cues = _embedded(path, item, start, length)  # times are relative to `start` here; only text matters
    if not cues and not str(item.get("Id", "")).startswith("file:"):
        all_cues = subs.cues_for(app, item)
        cues = [c for c in all_cues or [] if start <= c["start"] <= credits_at]
    return condense([c["text"] for c in cues or []])


def condense(lines, limit=2800):
    out, last = [], None
    for ln in lines:
        t = subs.clean(ln)
        if t and t != last:
            out.append(t)
            last = t
    text = " / ".join(out)
    if len(text) <= limit:
        return text
    # keep the very end whole (it's the ending) and an even sample of what led up to it
    tail = " / ".join(out[-40:])[-limit // 2:]
    head_lines = out[:-40]
    step = max(1, len(" / ".join(head_lines)) // (limit // 2) + 1)
    head = " / ".join(head_lines[::step])[:limit // 2]
    return head + " / ... / " + tail


# ------------------------------------------------------------------ one frame near the end (only if fast)
def _vision_loaded():
    p = vision.provider()
    if not p or p["kind"] != "ollama":
        return None
    try:
        ps = requests.get(f"{vision.OLLAMA}/api/ps", timeout=2).json().get("models", [])
        return p if any(m.get("name") == p["model"] for m in ps) else None
    except Exception:
        return None


WHOLE = 0.8   # share of the runtime really played for a whole-story comment


def _outline(stretches, credits_at=None, limit=3000):
    from .scene_search import clock
    """The story by time from the stretch summaries, cut to fit (the last stretches are kept whole)."""
    parts = [p for p in stretches if not credits_at or p["start"] < credits_at]
    lines = [f"[{clock(p['start'])}] {p['summary']}" for p in parts]
    if sum(len(x) + 1 for x in lines) <= limit:
        return "\n".join(lines)
    tail = lines[-4:]
    head = lines[:-4]
    room = limit - sum(len(x) + 1 for x in tail)
    step = 1
    while sum(len(x) + 1 for x in head[::step]) > room and step < len(head):
        step += 1
    return "\n".join(head[::step] + tail)


def whole_story(app, item, credits_at=None):
    """{"outline", "season"?} for a title watched (nearly) all the way: the whole plot from the story stretches
    (SDH subtitles preferred; built now if missing, at low priority) and, for an episode, the season so far."""
    from . import scene_search, watched
    d = scene_search.summaries(app, item, build=True, workers=2)
    if not d or not d.get("stretches"):
        return None
    out = {"outline": _outline(d["stretches"], credits_at)}
    if item.get("Type") == "Episode" and item.get("SeriesId"):
        try:
            jf = app.jf
            eps = jf.get(f"/Shows/{item['SeriesId']}/Episodes", userId=jf.user, Fields="RunTimeTicks,Path,MediaSources",
                         seasonId=item.get("SeasonId") or None).get("Items", [])
            earlier = [e for e in eps if e.get("ParentIndexNumber") == item.get("ParentIndexNumber")
                       and (e.get("IndexNumber") or 0) < (item.get("IndexNumber") or 0)]
            lines = []
            for e in earlier[-6:]:
                if watched.coverage(e["Id"], (e.get("RunTimeTicks") or 0) / TICKS) < 0.5:
                    continue
                cues = scene_search.search_cues(app, e)
                got = scene_search._load(e, cues) if cues else None
                if got and got.get("stretches"):
                    st = got["stretches"]
                    pick = [st[0]] + ([st[len(st) // 2]] if len(st) > 2 else []) + [st[-1]]
                    lines.append(f"E{e.get('IndexNumber')} '{e.get('Name')}': " + " ".join(p["summary"][:110] for p in pick))
            if lines:
                out["season"] = "\n".join(lines)[:1400]
        except Exception as e:
            print("season context failed:", e)
    return out


def _look_at_story(app, item, credits_at):
    """One vision call: a few key moments and the final shot -> two sentences about what they show."""
    from . import frame, moments
    path = (item.get("MediaSources") or [{}])[0].get("Path") or item.get("Path")
    if not credits_at or not path or not os.path.exists(path) or not vision.available():
        return None
    imgs = []
    for m in (moments.cached(item["Id"]) or {}).get("moments", [])[:4]:
        f = moments.image_path(item["Id"], m["n"])
        if f:
            imgs.append(f.read_bytes())
    imgs.append(frame.from_file(path, max(0, credits_at - 25), width=448, hdr=frame.is_hdr(item)))
    text, _ms, _ = vision.ask(f"These {len(imgs)} frames are from across a film, in order; the last one is its final "
                              "shot. In two short sentences, what do they show about the story (people, places, "
                              "mood, how it ends)?", imgs, max_tokens=120, timeout=90)
    return re.sub(r"\s+", " ", text).strip() or None


def _look_at_end(item, credits_at):
    if not credits_at or not _vision_loaded():
        return None  # loading a vision model now would cost more than it's worth
    path = (item.get("MediaSources") or [{}])[0].get("Path") or item.get("Path")
    if not path or not os.path.exists(path):
        return None
    from . import frame
    jpeg = frame.from_file(path, max(0, credits_at - 25), width=512, hdr=frame.is_hdr(item))
    text, _ms, _ = vision.ask("Describe this final scene of a film in one short sentence.", [jpeg], max_tokens=50,
                              timeout=20)
    return text.strip() or None


# ------------------------------------------------------------------ how the ending was received (optional)
def web_search_on():
    """Setting "ai_web_search": default on with Atlas on this PC, off with a third-party key."""
    val = settings.load().get("ai_web_search")
    if val is None:
        p = llm.provider()
        return bool(p and p.get("name") == "Atlas")
    return bool(val)


def search_query(item):
    """Only the title, year and episode go out."""
    if item.get("Type") == "Episode":
        return (f"{item.get('SeriesName')} S{int(item.get('ParentIndexNumber') or 0):02d}"
                f"E{int(item.get('IndexNumber') or 0):02d} \"{item.get('Name')}\" ending reaction review")
    return " ".join(x for x in (item.get("Name"), str(item.get("ProductionYear") or ""), "movie ending reception") if x)


def web_snippets(query, n=3):
    """Snippets for the query: Atlas' own search (SearXNG on 127.0.0.1:8888) when present, else DuckDuckGo's HTML
    page. 5 s timeout. A few extra are fetched so the spoiler filter can drop some and still keep 3."""
    from .. import ai
    if ai.atlas_detected():
        try:
            r = requests.get("http://127.0.0.1:8888/search", params={"q": query, "format": "json", "language": "en"},
                             timeout=5)
            r.raise_for_status()
            return [re.sub(r"\s+", " ", x.get("content") or "")[:300] for x in r.json().get("results", [])
                    if x.get("content")][:n * 3]
        except Exception:
            pass
    r = requests.post("https://html.duckduckgo.com/html/", data={"q": query}, timeout=5,
                      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    r.raise_for_status()
    found = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S)
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", x)).strip()[:300] for x in found[:n * 3]]


def future_names(app, item, picks_):
    """Titles that must not be hinted at: the coming episodes' names, the later films of the collection."""
    names = [p.get("plain_title") or p["title"] for p in picks_ if p.get("kind") in ("next", "sequel")]
    for p in picks_:
        names += p.get("later_titles") or []
    if item.get("Type") == "Episode" and item.get("SeriesId"):
        try:
            jf = app.jf
            eps = jf.get(f"/Shows/{item['SeriesId']}/Episodes", userId=jf.user, startItemId=item["Id"],
                         Limit=40).get("Items", [])
            names += [e.get("Name") for e in eps[1:] if e.get("Name")]
        except Exception:
            pass
    generic = {"pilot", "finale", "part 1", "part 2", "episode 1"}
    return [n for n in dict.fromkeys(names) if n and len(n) > 3 and n.lower() not in generic]


def spoiler_free(snippets, item, forbidden):
    """Drop snippets that talk about later episodes or seasons, sequels, or anything coming next."""
    season = int(item.get("ParentIndexNumber") or 0)
    ep = int(item.get("IndexNumber") or 0)
    bad = [r"\bnext (?:episode|season|film|movie|installment|chapter|part)\b", r"\bsequels?\b", r"\bfollow-?up\b",
           r"\bupcoming\b", r"\bpart (?:two|2|ii|three|3)\b", r"\bseries finale\b", r"\bnext week\b",
           r"\blater (?:episodes?|seasons?|in the series)\b"]
    if item.get("Type") == "Episode":
        bad += [rf"\bseason {n}\b" for n in range(season + 1, season + 15)]
        bad += [rf"\bs0?{n}\s*e\d+" for n in range(season + 1, season + 15)]
        bad += [rf"\bs0?{season}\s*e0?{n}\b" for n in range(ep + 1, ep + 40)]
        bad += [rf"\bepisode {n}\b" for n in range(ep + 1, ep + 40)]
        bad += [r"\bseason finale\b"]
    out = []
    for snip in snippets:
        low = snip.lower()
        if any(re.search(b, low) for b in bad) or names_in(snip, forbidden):
            continue
        out.append(snip)
    return out[:3]


def names_in(text, names):
    """Does the text name any of these titles? Multi-word titles: anywhere (any case); one-word titles (often
    ordinary words like "Stay") only as a capitalised word, and only if 6+ letters."""
    for n in names:
        words = n.split()
        if len(words) > 1:
            if re.search(r"(?<!\w)" + re.escape(n.lower()) + r"(?!\w)", text.lower()):
                return True
        elif len(n) >= 6 and n[:1].isupper() and re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", text):
            return True
    return False


def mentions_future(text, forbidden):
    low = (text or "").lower()
    return names_in(text or "", forbidden) or bool(re.search(
        r"\bnext (?:episode|season|film|movie|part)\b|\bsequels?\b|\bwhat(?:'s| is) coming\b|\bcan't wait (?:for|to see)\b|"
        r"\bin the next\b", low))


# ------------------------------------------------------------------ the comment
def _json_reply(text):
    """Small models wrap the JSON in prose or add a stray closing brace: take the first complete object, else
    pull the comment string out, else use the text itself (when it isn't JSON at all)."""
    text = text or ""
    start = text.find("{")
    if start >= 0:
        try:
            data, _end = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
        m = re.search(r'"comment"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        return {"comment": json.loads(f'"{m.group(1)}"')} if m else {}
    return {"comment": text}


def _comment(item, dialogue, scene, picks_, snippets=None, forbidden=(), timeout=25, whole=None):
    if not llm.available():
        return None, {}
    about = item.get("Overview") or ""
    tagline = (item.get("Taglines") or [None])[0]
    sims = [p for p in picks_ if p["kind"] == "similar"]
    lines = [f"Just finished: {_title(item, year=True)}" + (f" - '{item.get('Name')}'" if item.get("Type") == "Episode" else "")]
    if tagline:
        lines.append(f"Tagline: {tagline}")
    if about:
        lines.append(f"About it: {about[:500]}")
    if whole and whole.get("season"):
        lines.append(f"Season so far (episodes they watched):\n{whole['season']}")
    if whole:
        lines.append(f"The whole story, by time (they watched all of it):\n{whole['outline']}")
    if scene:
        lines.append(("What frames across it show: " if whole else "Final shot: ") + scene)
    if dialogue:
        lines.append(f"Dialogue near the end: {dialogue}")
    if snippets:
        lines.append("How viewers received this ending (web): " + " | ".join(snippets))
    if sims:
        lines.append("Similar picks (number: title [genres]): " +
                     "; ".join(f"{p['n']}: {p['title']} [{', '.join(p.get('genres') or [])}]" for p in sims))
    react = ("2-3 short warm sentences about the whole thing they just watched: a character arc or two, what it "
             "built up to and how the ending paid it off (name characters and moments from the outline)"
             if whole else "1-2 short warm sentences reacting to how it ended")
    system = ("You comment on the ending a viewer just watched, like a friend on the couch: " + react + " (spoilers "
              "about THIS title are fine). Say nothing about future episodes, "
              "sequels, later seasons or what happens next, and don't hint at any pick's plot. For each similar pick "
              "give a short positive reason (max 8 words) why someone who enjoyed this would like it, based only on "
              'its genre or premise. Reply JSON only: {"comment": "...", "why": {"<number>": "..."}}')
    text, _ms, _ = llm.complete([{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}],
                                max_tokens=300 if whole else 220, temperature=0.4 if whole else 0.6, timeout=timeout,
                                json_mode=True)
    data = _json_reply(text)
    comment = re.sub(r"\s+", " ", str(data.get("comment") or "")).strip() or None
    whys = {str(k): str(v).strip() for k, v in (data.get("why") or {}).items()
            if v and not mentions_future(str(v), forbidden)}
    return comment, whys
