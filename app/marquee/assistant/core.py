"""The assistant: takes a sentence (typed or spoken), does it, and answers in a line.

Rules first (intents.py, instant); the language model only for questions, summaries and anything the rules don't
understand, and only because someone just asked."""
import datetime as dt
import re
import threading
import time

from . import config, intents, llm, subs
from .control import Control


def clock(seconds):
    s = int(max(0, seconds or 0))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


class Assistant:
    def __init__(self, app):
        self.app = app
        self.ctl = Control(app)
        self.history = []          # recent exchanges, in memory only
        self._lock = threading.Lock()
        self.last = None
        self.voice = None          # voice.Listener, created on demand
        self._atlas = None

    @property
    def atlas(self):
        if self._atlas is None:
            from .atlas import Asker
            self._atlas = Asker(self)
        return self._atlas

    @property
    def scenes(self):
        if getattr(self, "_scenes", None) is None:
            from .scenes import Scenes
            self._scenes = Scenes(self)
        return self._scenes

    def listener(self):
        if self.voice is None:
            from .voice import Listener
            self.voice = Listener(self)
        return self.voice

    def prepare_ending(self, item_id, credits_at=None):
        """Called by the player ~6 minutes before the credits: work out the ending comment and picks ahead."""
        from . import ending
        return ending.prepare(self.app, item_id, credits_at)

    def cancel_ending(self, item_id):
        from . import ending
        return ending.cancel(item_id)

    def ask_atlas(self, question=""):
        """The Ask Atlas button: pause, capture the frame, answer in the background."""
        return self.atlas.button(question)

    def remember(self, q, a, source):
        with self._lock:
            self.history = (self.history + [{"q": q, "a": a, "at": time.time(), "source": source}])[-20:]

    # ------------------------------------------------------------------ entry point
    def handle(self, text, source="text", item_id=None):
        t0 = time.perf_counter()
        text = (text or "").strip()
        if not text:
            return {"reply": "", "actions": [], "source": "rules", "ms": 0}
        intent = intents.parse(text)
        used = "rules"
        if self.ctl.playing():
            subs.prefetch(self.app)
        pending = self.atlas.take_pending() if source == "voice" else None
        if intent and intent["intent"] == "seek" and intent.get("relative") and intent.get("value") == -10 and                 re.fullmatch(r"(?:go|take me|jump|skip) back", intent.get("text", "")) and self.scenes.undo_active():
            intent = {"intent": "undo", "text": intent["text"]}  # "go back" right after a scene jump = Undo
        if intent and intent["intent"] == "scene" and not self.ctl.playing() and item_id:
            intent["item_id"] = item_id
        try:
            if pending and (intent is None or intent["intent"] in QUESTIONS):
                used = "atlas"  # the question after pressing Ask Atlas: about the picture captured then
                job = self.atlas.ask(text, ctx=pending)
                reply, actions = f"{self.atlas.name} is thinking...", [{"type": "pending", **job}]
            elif intent is None:
                used = "llm"
                reply, actions = self._ask_llm(text)
            else:
                reply, actions = self.run(intent)
        except Exception as e:
            print("assistant failed:", repr(e))
            reply, actions = f"Sorry, that didn't work ({e.__class__.__name__}).", []
        out = {"reply": reply, "actions": actions, "source": used, "intent": (intent or {}).get("intent"),
               "ms": int((time.perf_counter() - t0) * 1000), "heard": text if source == "voice" else None}
        with self._lock:
            self.last = out
            self.history = (self.history + [{"q": text, "a": reply, "at": time.time(), "source": source}])[-20:]
        self.ctl.notify("assistant", {"reply": reply, "actions": actions, "heard": out["heard"]})
        return out

    # ------------------------------------------------------------------ intents
    def run(self, it):  # noqa: C901 - a flat dispatch table
        name = it["intent"]
        ctl = self.ctl
        needs_video = {"pause", "seek", "volume", "mute", "subtitles", "audio", "next_episode", "previous_episode",
                       "skip_intro", "speed", "fullscreen", "pip", "what_said", "summarize", "time_left", "stop"}
        if name in needs_video and not ctl.playing():
            return "Nothing is playing right now.", []
        act = {"type": name, **{k: v for k, v in it.items() if k not in ("intent", "text")}}
        from . import vision
        if name == "scene" and not ctl.playing() and it.get("item_id"):
            return self.scenes.locate(self.app.jf.item(it["item_id"]), it["query"])
        if name == "scene":
            if not ctl.playing():
                return "Nothing is playing right now (open a title and ask from there).", []
            job = self.atlas.ask(it["query"], kind="scene_jump")
            return f"Looking for {it['query']}...", [{"type": "pending", **job}]
        if name == "pick":
            done = self.scenes.choose(it["n"])
            return (done or "There's nothing to pick from right now."), [act]
        if name == "undo":
            done = self.scenes.undo_jump()
            return (done or "Nothing to undo."), [act]
        if name == "cast" and not it.get("role") and ctl.playing() and vision.available():
            name = it["intent"] = "who_screen"
        if name in ATLAS:
            if not ctl.playing():
                return "Nothing is playing right now.", []
            job = self.atlas.ask(it.get("text") or name, kind=name)
            return f"{self.atlas.name} is thinking...", [{"type": "pending", **job}]

        if name == "pause":
            ctl.pause(it["mode"])
            paused = bool(getattr(ctl.mpv, "pause", it["mode"] == "pause"))
            msg = "Paused" if paused else "Playing"
            ctl.toast(msg)
            return msg + ".", [act]
        if name == "stop":
            ctl.stop()
            return "Stopped.", [act]
        if name == "seek":
            v, rel = float(it["value"]), it["relative"]
            ctl.seek(v, rel)
            msg = (f"{'Forward' if v > 0 else 'Back'} {_span(abs(v))}" if rel else f"Jumped to {clock(v)}")
            ctl.toast(msg)
            return msg + ".", [act]
        if name == "skip_intro":
            intro = None
            try:
                for s in self.app.jf.segments(ctl.item()["Id"]):
                    if s.get("Type") in ("Intro", "Recap"):
                        intro = s["EndTicks"] / 10_000_000
            except Exception:
                pass
            if intro and intro > ctl.position():
                ctl.seek(intro, False)
                ctl.toast("Skipped the intro")
                return "Skipped the intro.", [{"type": "seek", "value": intro, "relative": False}]
            ctl.seek(85, True)
            ctl.toast("Forward 85 seconds")
            return "No intro marked for this one, so I jumped ahead 85 seconds.", [{"type": "seek", "value": 85, "relative": True}]
        if name == "volume":
            new = ctl.set_volume((ctl.volume() + it["value"]) if it["relative"] else it["value"])
            try:
                ctl.mpv.mute = False
            except Exception:
                pass
            ctl.toast(f"Volume {new}%")
            return f"Volume {new}%.", [{**act, "now": new}]
        if name == "mute":
            ctl.mute(it["on"])
            ctl.toast("Muted" if it["on"] else "Sound on")
            return ("Muted." if it["on"] else "Sound on."), [act]
        if name == "subtitles":
            msg = ctl.subtitles(it["on"], it.get("lang"), it.get("cycle", False))
            ctl.toast(msg)
            return msg + ".", [act]
        if name == "audio":
            msg = ctl.audio(it.get("lang"), it.get("cycle", False))
            ctl.toast(msg)
            return msg + ".", [act]
        if name == "speed":
            v = it.get("value") or max(0.25, min(4.0, float(getattr(ctl.mpv, "speed", 1) or 1) + it.get("delta", 0)))
            ctl.speed(v)
            ctl.toast(f"Speed {v:g}x")
            return f"Speed {v:g}x.", [{**act, "value": v}]
        if name == "fullscreen":
            ctl.fullscreen(it["on"])
            return ("Fullscreen." if it["on"] else "Left fullscreen."), [act]
        if name == "pip":
            ctl.pip()
            return "Mini player.", [act]
        if name == "next_episode":
            return self._episode(+1)
        if name == "previous_episode":
            return self._episode(-1)
        if name == "play":
            return self._play_title(it)
        if name == "find":
            return self._find(it.get("query") or it.get("text") or "")
        if name == "what_said":
            return self._what_said(it.get("seconds", 20))
        if name == "summarize":
            secs = it.get("seconds", 300)
            if it.get("so_far"):
                secs = max(60, ctl.position())
            return self._summarize(secs)
        if name == "cast":
            return self._cast(it.get("role"))
        if name == "time_left":
            st = ctl.state()
            dur = st.get("duration") or st.get("runtime") or 0
            left = max(0, dur - (st.get("position") or 0))
            ends = (dt.datetime.now() + dt.timedelta(seconds=left / max(0.25, float(st.get("speed") or 1)))).strftime("%I:%M %p").lstrip("0")
            msg = f"{_span(left)} left, ends around {ends}."
            ctl.toast(msg)
            return msg, [act]
        if name == "now_playing":
            if not ctl.playing():
                return "Nothing is playing right now.", []
            i = ctl.item()
            msg = (f"{i.get('SeriesName')}, season {i.get('ParentIndexNumber')} episode {i.get('IndexNumber')}: {i.get('Name')}"
                   if i.get("Type") == "Episode" else f"{i.get('Name')} ({i.get('ProductionYear') or 'year unknown'})")
            ctl.toast(msg)
            return msg + ".", [act]
        return "I can't do that yet.", []

    # ------------------------------------------------------------------ helpers per intent
    def _episode(self, step):
        p = self.ctl.player
        item = self.ctl.item()
        if step > 0 and getattr(p, "next_item", None):
            nxt = p.next_item
        else:
            if not item or item.get("Type") != "Episode":
                return "This isn't an episode of a show.", []
            jf = self.app.jf
            eps = jf.get(f"/Shows/{item['SeriesId']}/Episodes", userId=jf.user, Fields="UserData").get("Items", [])
            ids = [e["Id"] for e in eps]
            k = ids.index(item["Id"]) + step if item["Id"] in ids else -1
            nxt = eps[k] if 0 <= k < len(eps) else None
        if not nxt:
            return ("That was the last episode." if step > 0 else "This is the first episode."), []
        self.ctl.play(nxt["Id"], start=0)
        label = f"S{nxt.get('ParentIndexNumber', 0)}:E{nxt.get('IndexNumber', 0)} {nxt.get('Name', '')}".strip()
        return f"Playing {label}.", [{"type": "play", "id": nxt["Id"], "title": label}]

    def _play_title(self, it):
        jf = self.app.jf
        title = it["title"]
        found = jf.items(searchTerm=title, Recursive="true", IncludeItemTypes="Movie,Series,Episode", Limit=12)
        if not found:
            return f"I couldn't find {title} in your library.", [{"type": "not_found", "title": title}]
        want = norm(title)
        tops = [i for i in found if i.get("Type") in ("Movie", "Series")]
        best = next((i for i in tops if norm(i.get("Name")) == want), None) or \
            next((i for i in tops if norm(i.get("Name")).startswith(want)), None) or (tops or found)[0]
        target = best
        if best.get("Type") == "Series":
            season, episode = it.get("season"), it.get("episode")
            if season or episode:
                seasons = jf.seasons(best["Id"])
                s = next((x for x in seasons if x.get("IndexNumber") == (season or 1)), None)
                if not s:
                    return f"{best['Name']} has no season {season} here.", []
                eps = jf.episodes(best["Id"], s["Id"])
                target = next((e for e in eps if e.get("IndexNumber") == (episode or 1)), None)
                if not target:
                    return f"Season {season} of {best['Name']} has no episode {episode} here.", []
            else:
                nu = jf.get("/Shows/NextUp", userId=jf.user, SeriesId=best["Id"], Limit=1).get("Items", []) or \
                    jf.get(f"/Shows/{best['Id']}/Episodes", userId=jf.user, Limit=1).get("Items", [])
                if not nu:
                    return f"{best['Name']} has no episodes yet.", []
                target = nu[0]
        start = 0 if (it.get("season") or it.get("episode")) else None
        self.ctl.play(target["Id"], start=start)
        if target.get("Type") == "Episode":
            label = f"{target.get('SeriesName') or best.get('Name')}, S{target.get('ParentIndexNumber', 0)}:E{target.get('IndexNumber', 0)}"
        else:
            label = f"{target.get('Name')}" + (f" ({target['ProductionYear']})" if target.get("ProductionYear") else "")
        return f"Playing {label}.", [{"type": "play", "id": target["Id"], "title": label}]

    def _find(self, query):
        from .. import ai
        jf = self.app.jf
        q = ai.read_query(query)
        types = {"movie": "Movie", "tv": "Series"}.get(q["kind"], "Movie,Series")
        items = []
        if q["genres"]:
            items = jf.items(Recursive="true", IncludeItemTypes=types, Genres="|".join(q["genres"]), Filters="IsUnplayed",
                             SortBy="Random", Limit=8) or \
                jf.items(Recursive="true", IncludeItemTypes=types, Genres="|".join(q["genres"]), SortBy="Random", Limit=8)
            lead = f"Some {' / '.join(g.lower() for g in q['genres'][:2])} picks"
        elif llm.available():
            res = ai.search(self.app, query, allow_during_playback=True)  # the model picks from the library (asked for, so allowed)
            cards = res.get("items") or []
            names = [c["name"] for c in cards[:5]]
            reply = (res.get("answer") or "Here's what I found.") + (f" {_list(names)}." if names else "")
            self.ctl.card("Marquee", reply)
            return reply, [{"type": "results", "items": cards[:8]}]
        else:
            items = jf.items(searchTerm=query, Recursive="true", IncludeItemTypes=types, Limit=8)
            lead = "From your library"
        if not items:
            return "I didn't find anything like that in your library.", []
        from ..server import cards as to_cards
        names = [i["Name"] for i in items[:5]]
        reply = f"{lead}: {_list(names)}. Say \"play\" and a title to start one."
        self.ctl.card("Marquee", reply)
        return reply, [{"type": "results", "items": to_cards(items)}]

    def _what_said(self, seconds):
        lines, why = subs.window(self.app, seconds)
        if not lines:
            lines, why = subs.window(self.app, seconds * 3)
        if not lines:
            return why + ".", []
        last = [subs.clean(x["text"]) for x in lines[-3:]]
        reply = " / ".join(f"\"{x}\"" for x in last if x)
        self.ctl.card("They said", "\n".join(last))
        return reply, [{"type": "what_said", "lines": last}]

    def _summarize(self, seconds):
        minutes = max(1, round(seconds / 60))
        if not llm.available():
            return "Summaries need the AI provider (Settings > AI).", []
        lines, why = subs.window(self.app, seconds)
        text = subs.as_text(lines)
        if len(text) < 40:
            return (why or "There's too little dialogue in that stretch to summarize") + ".", []
        item = self.ctl.item() or {}
        title = item.get("SeriesName") and f"{item['SeriesName']} S{item.get('ParentIndexNumber')}E{item.get('IndexNumber')}" \
            or item.get("Name") or "this video"
        self.ctl.toast("Thinking...")
        summary, ms, _ = llm.summarize(title, minutes, text)
        self.ctl.card(f"The last {minutes} min", summary)
        return summary, [{"type": "summarize", "seconds": seconds, "llm_ms": ms}]

    def _cast(self, role=None):
        item = self.ctl.item()
        if not item:
            return "Nothing is playing right now.", []
        full = self.app.jf.item(item["Id"])
        people = [p for p in full.get("People") or [] if p.get("Type") in ("Actor", "GuestStar")]
        if not people and full.get("SeriesId"):
            people = [p for p in self.app.jf.item(full["SeriesId"]).get("People") or [] if p.get("Type") == "Actor"]
        if not people:
            return "Jellyfin has no cast for this one.", []
        if role:
            r = norm(role)
            hit = next((p for p in people if r and r in norm(p.get("Role"))), None) or \
                next((p for p in people if r and r in norm(p.get("Name"))), None)
            if hit:
                msg = f"{hit.get('Role') or hit['Name']} is played by {hit['Name']}." if norm(hit.get("Role")).find(r) >= 0 \
                    else f"{hit['Name']} plays {hit.get('Role') or 'a part in this'}."
                self.ctl.card("Cast", msg)
                return msg, [{"type": "cast", "people": [_person(hit)]}]
            return f"I don't see {role} in the cast list.", []
        # who's on screen can't be seen, but a character named in the last minute's lines is a good hint
        hint = None
        try:
            lines, _ = subs.window(self.app, 60)
            said = " " + norm(subs.as_text(lines)) + " "
            best = -1
            for p in people[:25]:  # the character named most recently wins
                first = norm((p.get("Role") or "").split("/")[0]).split(" ")[0]
                at = said.rfind(f" {first} ") if len(first) > 2 else -1
                if at > best:
                    best, hint = at, p
        except Exception:
            pass
        top = people[:5]
        cast = "; ".join(f"{p['Name']} as {p['Role']}" if p.get("Role") else p["Name"] for p in top)
        msg = (f"Just mentioned: {hint.get('Role')}, played by {hint['Name']}. " if hint else "") + f"Main cast: {cast}."
        self.ctl.card("Cast", msg)
        return msg, [{"type": "cast", "people": [_person(p) for p in top], "hint": _person(hint) if hint else None}]

    # ------------------------------------------------------------------ language model
    def _context(self):
        if not self.ctl.playing():
            return "Nothing playing; the viewer is browsing their library."
        i = self.ctl.item() or {}
        st = self.ctl.state()
        name = (f"{i.get('SeriesName')} S{i.get('ParentIndexNumber')}E{i.get('IndexNumber')} '{i.get('Name')}'"
                if i.get("Type") == "Episode" else f"{i.get('Name')} ({i.get('ProductionYear') or '?'})")
        ctx = f"Playing {name} at {clock(st.get('position'))} of {clock(st.get('duration') or st.get('runtime'))}."
        people = i.get("People") or []
        directors = [p["Name"] for p in people if p.get("Type") == "Director"][:2]
        actors = [f"{p['Name']} ({p['Role']})" if p.get("Role") else p["Name"] for p in people if p.get("Type") == "Actor"][:5]
        if directors:
            ctx += " Directed by " + ", ".join(directors) + "."
        if actors:
            ctx += " Starring " + ", ".join(actors) + "."
        if i.get("Overview"):
            ctx += " Plot: " + i["Overview"][:280]
        try:
            lines, _ = subs.window(self.app, 90)
            said = subs.as_text(lines, limit=700)
            if said:
                ctx += " Recent lines: " + said.replace("\n", " / ")
        except Exception:
            pass
        return ctx

    def _ask_llm(self, text):
        if not llm.available():
            return ("I didn't catch that. Try \"pause\", \"skip forward 30 seconds\", \"play Dune\" or \"turn on "
                    "subtitles\" (questions need the AI provider in Settings)."), []
        self.ctl.toast("Thinking...")
        data = llm.route(text, self._context())
        ms = data.pop("_ms", None)
        tool = data.get("tool")
        if tool:
            it = {"intent": str(tool), **(data.get("args") or {})}
            it.setdefault("text", text)
            for k, d in (("relative", True), ("mode", "toggle"), ("on", True)):
                if it["intent"] in {"relative": ("seek", "volume"), "mode": ("pause",), "on": ("mute", "subtitles")}[k]:
                    it.setdefault(k, d)
            if it["intent"] in ("seek", "volume") and "value" not in it:
                return "Sorry, I didn't get how much.", []
            if it["intent"] in ("play", "find", "scene") and not (it.get("title") or it.get("query")):
                return "Sorry, I didn't get what to look for.", []
            reply, actions = self.run(it)
            return reply, actions + [{"type": "llm", "ms": ms}]
        answer = str(data.get("answer") or "").strip()
        self.ctl.card("Marquee", answer)
        return answer, [{"type": "answer", "llm_ms": ms}]


ATLAS = {"who_screen", "same_as", "where_seen", "look", "loud_soon", "scene_when"}
QUESTIONS = ATLAS | {"cast", "what_said", "summarize", "time_left", "now_playing"}


def search_index(item_id, query):
    """Scene search through the library index (/api/index/<item>/search, served by Marquee itself).
    None when there's no index for this video; else [{"time", "text"}] best first."""
    from .. import server
    try:
        client = server.app.test_client()
        r = client.get(f"/api/index/{item_id}/search", query_string={"q": query})
        if r.status_code == 405:
            r = client.post(f"/api/index/{item_id}/search", json={"q": query})
        if r.status_code in (404, 409, 501) or r.status_code >= 500:
            return None
        data = r.get_json(silent=True) or {}
    except Exception as e:
        print("scene search failed:", e)
        return None
    if data.get("ok") is False or data.get("indexed") is False:
        return None
    raw = data if isinstance(data, list) else next((data[k] for k in ("results", "hits", "matches", "items", "scenes")
                                                    if isinstance(data.get(k), list)), None)
    if raw is None:
        return None
    out = []
    for h in raw:
        if not isinstance(h, dict):
            continue
        t = next((h[k] for k in ("time", "start", "t", "seconds", "position", "pos") if isinstance(h.get(k), (int, float))), None)
        if t is None and isinstance(h.get("start_ticks"), (int, float)):
            t = h["start_ticks"] / 10_000_000
        if t is not None:
            out.append({"time": float(t), "text": h.get("text") or h.get("line") or h.get("caption") or "", "z": h.get("z"),
                        "score": h.get("score")})
    return out


def _span(seconds):
    s = int(round(seconds))
    if s < 90:
        return f"{s} second{'s' if s != 1 else ''}"
    if s < 3600:
        m, r = divmod(s, 60)
        return f"{m} minute{'s' if m != 1 else ''}" + (f" {r} seconds" if r and m < 5 else "")
    h, m = divmod(s // 60, 60)
    return f"{h} hour{'s' if h != 1 else ''}" + (f" {m} minutes" if m else "")


def _list(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


def _person(p):
    return {"name": p.get("Name"), "role": p.get("Role"), "id": p.get("Id")}
