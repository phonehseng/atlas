"""Ask Atlas: questions about what's on screen (or about to be), answered in the background.

Asking never blocks playback: the frame, the position and the item are captured the moment you ask (the video is
paused for the Ask Atlas button), then the model works in a thread. Press play whenever you like. While it works a
small "Atlas is thinking..." pill shows at the top of the video; the answer arrives quietly as a banner there (no
sound, no pause). Asking again replaces the pending question. Times in answers are measured from when you asked.

Player protocol (drawn by the OSC):  script-message marquee-atlas <state> <text> <seconds> <id>
    state: listening | thinking | answer | clear      seconds: how long an answer stays (0 = until replaced)
Off the video (browsing pages), the same goes to the web UI as notify_ui("atlas-answer", {...})."""
import re
import threading
import time

from . import cast, config, frame, intents, loudness, subs, vision

VISUAL = {"who_screen", "same_as", "where_seen", "look", "loud_soon", "scene_when"}
DEFAULT_QUESTION = "Who's on screen and what's going on?"


class Asker:
    def __init__(self, assistant):
        self.assistant = assistant
        self.app = assistant.app
        self.ctl = assistant.ctl
        self.gen = 0
        self.current = {"id": 0, "state": "idle"}
        self.pending_context = None   # captured by the Ask Atlas button while we wait for a spoken question
        self._lock = threading.Lock()

    @property
    def name(self):
        p = vision.provider()
        return (p or {}).get("name") or "Atlas"

    # ------------------------------------------------------------------ capture
    def capture(self, need_frame=True, pause=False):
        """What's needed to answer later: item, position, audio track, and the picture (taken now)."""
        p = self.ctl.player
        item = getattr(p, "item", None)
        st = self.ctl.state()
        ctx = {"item": item, "position": float(st.get("time") or st.get("position") or 0), "at": time.time(),
               "playing": self.ctl.playing(), "frame": None, "audio_index": None}
        try:
            cur = next((t for t in (p.mpv.track_list or []) if t.get("type") == "audio" and t.get("selected")), None)
            ctx["audio_index"] = cur.get("ff-index") if cur and not cur.get("external") else None
        except Exception:
            pass
        if pause and ctx["playing"]:
            try:
                self.ctl.pause("pause")
            except Exception:
                pass
        if need_frame and item:
            try:
                ctx["frame"] = frame.grab(p)
            except Exception as e:
                print("frame grab failed:", e)
        return ctx

    # ------------------------------------------------------------------ entry points
    def button(self, question=""):
        """The Ask Atlas button / A key: pause, keep the picture, then take the question (spoken if voice is
        installed, else a default 'who and what' look)."""
        ctx = self.capture(need_frame=True, pause=True)
        if question:
            return self.ask(question, ctx=ctx)
        from . import installer
        if installer.have("voice"):
            try:
                with self._lock:
                    self.pending_context = ctx
                v = self.assistant.listener()
                v.listen_once(8)
                self._publish("listening", f"Ask {self.name}...", 0, self.gen)
                return {"id": self.gen, "state": "listening"}
            except Exception as e:
                print("ask atlas: no voice:", e)
        return self.ask(DEFAULT_QUESTION, ctx=ctx, kind="look")

    def take_pending(self, max_age=45):
        with self._lock:
            ctx, self.pending_context = self.pending_context, None
        return ctx if ctx and time.time() - ctx["at"] < max_age else None

    def ask(self, question, ctx=None, kind=None, pause=False):
        """Start answering; returns at once {"id", "state": "thinking"}. The answer is published when ready."""
        question = (question or "").strip() or DEFAULT_QUESTION
        it = intents.parse(question) or {}
        kind = kind or (it.get("intent") if it.get("intent") in VISUAL else None)
        if kind is None:
            kind = "look" if vision.available() else "text"
        if ctx is None:
            ctx = self.capture(need_frame=kind in ("who_screen", "same_as", "where_seen", "look"), pause=pause)
        elif ctx.get("frame") is None and kind in ("who_screen", "same_as", "where_seen", "look"):
            ctx["frame"] = self.capture(need_frame=True)["frame"]
        with self._lock:
            self.gen += 1
            my = self.gen
            self.current = {"id": my, "state": "thinking", "question": question, "kind": kind,
                            "position": ctx["position"], "asked": ctx["at"]}
        self._publish("thinking", f"{self.name} is thinking...", 0, my)
        threading.Thread(target=self._run, args=(my, question, kind, it, ctx), daemon=True).start()
        return {"id": my, "state": "thinking", "kind": kind}

    # ------------------------------------------------------------------ work
    def _run(self, my, question, kind, it, ctx):
        t0 = time.perf_counter()
        ctx["_still_current"] = lambda: my == self.gen
        try:
            text = self._answer(question, kind, it, ctx)
        except Exception as e:
            print("ask atlas failed:", repr(e))
            text = f"Sorry, I couldn't work that out ({e})." if str(e) else "Sorry, I couldn't work that out."
        ctx["frame"] = None  # the picture is dropped as soon as it's been looked at
        ms = int((time.perf_counter() - t0) * 1000)
        with self._lock:
            if my != self.gen:
                return  # replaced by a newer question
            if text is None:  # answered some other way (the scene picker): just drop the pill
                self.current = {**self.current, "state": "done", "ms": ms}
                return
            self.current = {**self.current, "state": "answer", "text": text, "ms": ms}
        secs = max(8, min(12, 6 + len(text) // 25))
        self._publish("answer", text, secs, my)
        self.assistant.remember(question, text, "atlas")

    def _answer(self, question, kind, it, ctx):  # noqa: C901
        app, item, pos = self.app, ctx["item"], ctx["position"]
        if not item and kind != "text":
            return "Nothing is playing right now."
        if kind == "scene_jump":
            return self.assistant.scenes.run(it.get("query") or question, ctx, ctx["_still_current"])
        if kind == "loud_soon":
            text, _events, _flags = loudness.answer(app, item, pos, audio_index=ctx.get("audio_index"))
            return text
        if kind == "scene_when":
            from .core import search_index
            hits = search_index(item["Id"], it.get("query") or question)
            if hits is None:
                return "This video isn't indexed for scene search yet."
            good = [h for h in hits if (h.get("z") or 0) >= 3] or [h for h in hits if (h.get("z") or 0) >= 2]
            if not good:
                return "I couldn't find that in this video."
            h = good[0]
            rel = h["time"] - pos
            when = (f"in about {loudness.clock(rel)}" if rel > 0 else f"{loudness.clock(-rel)} ago")
            lead = "Around" if (h.get("z") or 0) >= 3 else "Maybe around"
            return f"{lead} {loudness.clock(h['time'])} ({when})."
        if kind in ("who_screen", "same_as", "where_seen"):
            person = None
            other = it.get("other")
            current = item.get("SeriesName") or item.get("Name")
            named_part = re.split(r"\bfrom\b|\bin\b", it.get("text") or question)[0]
            if kind != "who_screen":
                person = cast.find_person(app, item, named_part)
            lead = ""
            if person is None:
                if ctx.get("frame") is None:
                    return "I couldn't see the picture."
                if not vision.available():
                    return "Looking at the screen needs a vision model (Settings > AI)."
                lead, person, _ms = cast.who_on_screen(app, item, ctx["frame"], question)
                if person is None:
                    return lead
            if kind == "who_screen":
                return lead
            if kind == "same_as" and other:
                return (lead + " " if lead else "") + cast.same_as(app, person, other, current)
            roles = cast.known_for(app, person, current=current)
            tail = f"You may know {person['Name']} from {', '.join(roles)}." if roles else \
                f"I couldn't get {person['Name']}'s other roles."
            return (lead + " " if lead else "") + tail
        if kind == "look":
            if ctx.get("frame") is None:
                return "I couldn't see the picture."
            return self._look(question, item, pos, ctx["frame"])
        # no vision model: the text assistant answers
        reply, _ = self.assistant._ask_llm(question)
        return reply

    def _look(self, question, item, pos, jpeg):
        title = (f"{item.get('SeriesName')} S{item.get('ParentIndexNumber')}E{item.get('IndexNumber')} "
                 f"'{item.get('Name')}'" if item.get("Type") == "Episode" else item.get("Name") or "a video")
        names = []
        try:
            names = [f"{p['Name']} as {p['Role']}" if p.get("Role") else p["Name"] for p in cast.people(self.app, item, 10)]
        except Exception:
            pass
        said = ""
        try:
            lines, _ = subs.window(self.app, 45, position=pos, item=item)
            said = subs.as_text(lines, limit=500).replace("\n", " / ")
        except Exception:
            pass
        prompt = (f"This is a frame from {title} at {loudness.clock(pos)}."
                  + (f" Cast: {'; '.join(names)}." if names else "")
                  + (f" Recent lines: {said}." if said else "")
                  + f"\nQuestion: {question}\nAnswer in at most 40 words, plain text. Name people only from the cast "
                    f"list, and only if you're fairly sure; don't reveal later plot.")
        text, _ms, _ = vision.ask(prompt, [jpeg], max_tokens=120)
        return re.sub(r"\s+", " ", text).strip() or "I'm not sure."

    # ------------------------------------------------------------------ showing it
    def _publish(self, state, text, seconds, ident):
        data = {"state": state, "text": text, "seconds": seconds, "id": ident, "name": self.name,
                "question": self.current.get("question")}
        shown = False
        try:
            m = getattr(self.ctl.player, "mpv", None)
            if m is not None and self.ctl.playing():
                m.command("script-message", "marquee-atlas", state, str(text or ""), str(seconds), str(ident))
                shown = True
        except Exception as e:
            print("atlas banner failed:", e)
        if not shown or state == "answer":
            self.ctl.notify("atlas-answer", {**data, "on_video": shown})

    def say_quietly(self, text):
        """An answer banner that isn't the reply to a pending question (e.g. after a scene was picked)."""
        with self._lock:
            self.gen += 1
            my = self.gen
            self.current = {"id": my, "state": "answer", "text": text}
        self._publish("answer", text, max(8, min(12, 6 + len(text) // 25)), my)

    def clear(self):
        with self._lock:
            self.gen += 1
            self.current = {"id": self.gen, "state": "idle"}
        self._publish("clear", "", 0, self.gen)
