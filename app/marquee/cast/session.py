"""One cast: a title playing on a TV, with this PC streaming it and Marquee as the remote.

Only one cast runs at a time. start() returns at once; the work (probing the file, choosing how to stream it,
reaching the TV, loading) happens on the cast's own thread, and the window follows it through "cast-state" events.

How the video goes out: Marquee Mobile's streaming engine (marquee.mobile.stream) decides between the file itself
(MP4 the TV plays as it is), a remux (the video copied into fragmented-MP4 HLS, audio made AAC when needed) or a GPU
transcode (H.264, HDR tone-mapped, picture subtitles burned in), from what the TV can play (marquee.cast.devices)
and the casting settings. Text subtitles go to the TV as WebVTT and switch without reloading on Google Cast; a new
audio track or burned-in subtitles start a new stream at the same place.

While it plays: the position is saved to the catalog every ~10 s (and on stop), Jellyfin gets the same session
reports as the built-in player, intros are skipped when "Skip intros" is on, and the next episode follows when the
episode finishes and "Autoplay next episode" is on.
"""
import secrets
import threading
import time
import uuid

from .. import settings
from ..mobile import api as mapi
from ..mobile import media, stream
from . import devices, endpoint, receivers

QUALITIES = ("auto", "original", "1080", "720")
SAVE_EVERY = 10
START_TIMEOUT = 75     # the TV has this long to start playing
TICKS = 10_000_000

APP = None
_lock = threading.RLock()
current = None         # the CastSession, while one runs
listeners = []         # fn(state dict): the window, the player, the phone remote
_last = {"state": None}


def enabled():
    return bool(settings.load().get("cast", True))


def cat():
    from .. import server
    return server.catalog()


def _clock(t):
    t = int(max(0, t or 0))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


class CastError(Exception):
    pass


class CastSession:
    def __init__(self, dev):
        self.id = secrets.token_hex(6)
        self.dev = dev
        self.token = endpoint.add(self)
        self.receiver = None
        self.stream = None
        self.info = None
        self.probe = None
        self.kf = None
        self.plan = None
        self.state = "connecting"   # connecting | loading | buffering | playing | paused | ended | stopped | error
        self.message = None
        self.position = 0.0
        self.duration = 0.0
        self.volume = None
        self.muted = False
        self.audio = None           # audio stream index
        self.sub = None             # subtitle id (text: shown as a TV text track; image: burned in)
        self.track_ids = {}         # text subtitle id -> Cast trackId
        self.segments = []
        self.next = None
        self.play_session = uuid.uuid4().hex
        self.started_playing = False
        self.load_at = 0.0
        self.last_save = 0.0
        self.skipped = set()
        self.stop_evt = threading.Event()
        self.thread = None
        self.loads = 0
        self.handover = None        # the local item we took over (to stop once the TV plays)
        self.done = False

    # ------------------------------------------------------------------ preparing a title
    def prepare(self, item_id, start=None, audio=None, sub=None):
        """Look the title up and probe its file (no network). Raises CastError."""
        info = cat().lookup(item_id)
        if not info:
            raise CastError("That title isn't in the library on this PC")
        if not info.get("exists"):
            raise CastError("That file isn't on this PC, so it can't be cast from here")
        try:
            p = media.probe(info["path"])["info"]
        except Exception as e:
            raise CastError(f"Couldn't read the file: {e}")
        if not p.get("video"):
            raise CastError("That file has no video")
        self.info, self.probe = info, p
        self.kf = media.keyframes(info["path"], p)
        self.duration = float(p.get("duration") or info.get("runtime") or 0)
        pos = float(start) if start is not None else float(info.get("position") or 0)
        if self.duration and pos > self.duration - 30:
            pos = 0.0
        self.position = max(0.0, pos)
        d_audio, d_sub = mapi._default_tracks(p, info.get("anime"))
        self.audio = audio if audio is not None else d_audio
        self.sub = sub if sub is not None else d_sub
        if self.sub is not None and not any(s["id"] == str(self.sub) for s in p["subs"]):
            self.sub = None
        self.segments = mapi._segments(info, p)
        self.next = None
        if info.get("next_episode_id"):
            r = cat().get(info["next_episode_id"])
            n = cat().lookup(info["next_episode_id"])
            if r and n and n.get("exists"):
                c = cat().card(r)
                self.next = {"id": c["id"], "title": f"S{c.get('season') or 0}:E{c.get('episode') or 0} · "
                                                      f"{c.get('name') or ''}"}
        self.track_ids = {s["id"]: k + 1 for k, s in enumerate(x for x in p["subs"] if x["kind"] == "text")}
        if any(s["kind"] == "text" and not s.get("external") for s in p["subs"]):
            media.start_extraction(info["path"])   # so the TV's first subtitle request finds them ready
        self.skipped = set()

    def _sub_obj(self):
        return next((s for s in self.probe["subs"] if s["id"] == str(self.sub)), None) if self.sub is not None else None

    def make_stream(self, pos):
        """A new stream session at pos (replacing this cast's previous one)."""
        st = settings.load()
        caps = devices.caps(self.dev, bool(st.get("cast_4k_hdr", True)))
        quality = st.get("cast_quality") or "auto"
        quality = quality if quality in QUALITIES else "auto"
        v = self.probe["video"]
        force = None
        if v.get("codec") == "h264" and (v.get("level") or 0) > caps["h264_level"]:
            force = "transcode"   # e.g. a level 5.1 Blu-ray rip on a 1080p Chromecast
        sub = self._sub_obj()
        burn = sub if sub and sub["kind"] == "image" else None
        prefer = (st.get("audio_lang_anime") or "jpn") if self.info.get("anime") else None
        bandwidth = None if quality == "original" else caps["max_bitrate"] / 0.75
        q = quality if quality in ("1080", "720") else "auto"
        if q == "1080" and caps["max_height"] < 1080:
            q = "720"
        plan = stream.decide(self.probe, caps, quality=q, audio=self.audio, sub=burn, bandwidth=bandwidth,
                             force=force, prefer_lang=prefer, keyframes=self.kf)
        if plan["mode"] == "transcode" and (plan.get("height") or 0) > caps["max_height"]:
            plan = stream.decide(self.probe, caps, quality="720", audio=self.audio, sub=burn, bandwidth=None,
                                 force="transcode", prefer_lang=prefer, keyframes=self.kf)
        # mobile's words are about phones; here it's a TV. A transcode is labelled by the height it really has.
        plan["reason"] = (plan.get("reason") or "").replace("this phone", "this TV").replace("phones", "TVs") \
            .replace("connection about", "more than the TV's")
        if plan["mode"] == "transcode" and plan.get("height"):
            plan["label"] = f"{plan['height']}p"
        info = self.info
        title = info.get("series") or info.get("name")
        if info["type"] == "Episode":
            title = f"{title} S{info.get('season') or 0}:E{info.get('episode') or 0}"
        try:
            s = stream.create({"id": info["id"], "title": title, "poster": mapi.img(info.get("poster"))},
                              info["path"], self.probe, plan, {"id": "cast", "name": f"TV · {self.dev['name']}"},
                              self.kf, pos)
        except OverflowError as e:
            raise CastError(str(e))
        self.plan, self.stream = plan, s
        if plan.get("audio") is not None:
            self.audio = plan["audio"]
        return s

    # ------------------------------------------------------------------ what the TV loads
    def media(self, base, pos):
        s, info = self.stream, self.info
        root = f"{base}/c/{self.token}/"
        bust = f"?s={s.id}"
        airplay = self.dev["kind"] == "airplay"
        texts = [x for x in self.probe["subs"] if x["kind"] == "text"]
        if s.mode == "direct":
            url, ctype, hls = root + "file.mp4" + bust, "video/mp4", False
        elif airplay:   # a master playlist: subtitle renditions and the HDR flag are only said there
            url, ctype, hls = root + "master.m3u8" + bust, "application/x-mpegurl", True
        else:
            url, ctype, hls = root + "index.m3u8" + bust, "application/x-mpegurl", True
        movie = info["type"] != "Episode"
        sub = self._sub_obj()
        tracks = [] if airplay else [{"id": self.track_ids[x["id"]], "url": root + f"sub/{x['id']}.vtt",
                                      "lang": x.get("lang") or "und", "name": x.get("label") or "Subtitles"}
                                     for x in texts]
        return {"url": url, "content_type": ctype, "hls": hls, "start": pos, "duration": self.duration,
                "movie": movie, "title": info.get("name") if movie else info.get("name") or info.get("series"),
                "series": info.get("series"), "season": info.get("season"), "episode": info.get("episode"),
                "subtitle": str(info.get("year") or "") if movie else (info.get("series") or ""),
                "image": root + "art.jpg" if self.artwork_path() else None, "tracks": tracks,
                "active": self.track_ids.get(sub["id"]) if sub and sub["kind"] == "text" and not airplay else None}

    def artwork_path(self):
        info = self.info or {}
        for url in (info.get("backdrop"), info.get("thumb"), info.get("poster")):
            if isinstance(url, str) and url.startswith("/img/"):
                try:
                    p = cat().images.get_url(url)
                except Exception:
                    p = None
                if p:
                    return p
        return None

    def master_playlist(self):
        """AirPlay: the video plus every text subtitle as an HLS subtitle rendition (the TV's own menu)."""
        sub = self._sub_obj()
        out = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
        for x in (x for x in self.probe["subs"] if x["kind"] == "text"):
            on = "YES" if sub and sub["id"] == x["id"] else "NO"
            name = (x.get("label") or "Subtitles").replace('"', "'")
            out.append(f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="{name}",LANGUAGE="{x.get("lang") or "und"}",'
                       f'DEFAULT={on},AUTOSELECT={on},FORCED={"YES" if x.get("forced") else "NO"},'
                       f'URI="subs/{x["id"]}.m3u8"')
        v = self.probe["video"]
        h = self.plan.get("height") or v.get("height") or 1080
        w = int((v.get("width") or 1920) * h / max(1, v.get("height") or 1080)) // 2 * 2
        bw = int(self.plan.get("bitrate") or self.probe.get("bitrate") or 8_000_000)
        rng = "SDR"
        if v.get("hdr") and self.stream.mode != "transcode":   # copied video keeps its HDR (transcodes are SDR)
            rng = "HLG" if v.get("hlg") else "PQ"
        out.append(f"#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={w}x{h},VIDEO-RANGE={rng}" +
                   (',SUBTITLES="subs"' if len(out) > 3 else ""))
        out.append("index.m3u8")
        return "\n".join(out) + "\n"

    def sub_playlist(self, sub_id):
        d = max(1, int(self.duration + 1))
        return "\n".join(["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{d}", "#EXT-X-MEDIA-SEQUENCE:0",
                          "#EXT-X-PLAYLIST-TYPE:VOD", f"#EXTINF:{d}.0,", f"../sub/{sub_id}.vtt", "#EXT-X-ENDLIST"]) + "\n"

    # ------------------------------------------------------------------ running
    def run(self, item_id, start, audio, sub):
        try:
            self._set("connecting")
            if not self.info:
                self.prepare(item_id, start, audio, sub)
            self._push(force=True)
            endpoint.ensure()
            self.receiver = receivers.for_device(self.dev)
            self.receiver.on_change = lambda: self._push()
            self.receiver.connect()
            if self.stop_evt.is_set():
                return
            self._load(self.position)
            self._report("")
            self._monitor()
        except (CastError, RuntimeError) as e:
            self._fail(str(e))
        except Exception as e:
            print("cast failed:", type(e).__name__, e, flush=True)
            self._fail(f"Casting stopped: {e}")

    def _load(self, pos, keep_stream=False):
        if not keep_stream:
            self.make_stream(pos)
        base = endpoint.ensure()
        m = self.media(base, pos)
        self._set("loading")
        self.load_at = time.time()
        self.loads += 1
        self.started_playing = False
        self.position = pos
        self.receiver.load(m)
        print(f"cast: {self.dev['name']} <- {self.stream.mode} {self.plan.get('label')} "
              f"({self.plan.get('reason') or 'plays as it is'}) at {_clock(pos)}", flush=True)

    def _monitor(self):
        while not self.stop_evt.wait(1.0):
            try:
                st = self.receiver.status()
            except Exception as e:
                print("cast status:", type(e).__name__, e)
                continue
            if st.get("volume") is not None:
                self.volume = st["volume"]
            self.muted = bool(st.get("muted"))
            rs = st.get("state")
            if st.get("gone"):
                self._finish("stopped", "Something else is playing on the TV now")
                return
            if rs in ("PLAYING", "PAUSED", "BUFFERING"):
                if not self.started_playing and rs == "BUFFERING":
                    pass   # still loading: the start timeout below keeps counting
                elif not self.started_playing:
                    self.started_playing = True
                    self._handover_done()
                self.position = st["position"] if st["position"] > 0 or rs != "BUFFERING" else self.position
                if st.get("duration"):
                    self.duration = max(self.duration, st["duration"]) if self.duration else st["duration"]
                self._set({"PLAYING": "playing", "PAUSED": "paused", "BUFFERING": "buffering"}[rs]
                          if self.started_playing else "loading")
            elif rs == "IDLE":
                reason = st.get("idle_reason")
                near_end = self.duration and self.position > self.duration - 45
                if reason == "FINISHED" or (reason is None and near_end and self.started_playing):
                    if self._finished():
                        continue
                    return
                if reason == "ERROR" or st.get("error"):
                    self._fail(st.get("error") or "The TV couldn't play this")
                    return
                if self.started_playing:
                    self._finish("stopped", "Stopped on the TV")
                    return
            if st.get("error") and not self.started_playing:
                self._fail(st["error"])
                return
            if not self.started_playing and time.time() - self.load_at > START_TIMEOUT:
                self._fail(self._no_start_reason())
                return
            if self.state == "playing":
                self._auto_skip()
            if self.started_playing and time.time() - self.last_save >= SAVE_EVERY:
                self._save()
            self._push()

    def _no_start_reason(self):
        hit = endpoint.hits.get(self.token)
        if not hit:
            return ("The TV never asked this PC for the video. Windows Firewall may be blocking it: see "
                    "Settings > Casting.")
        return "The TV didn't start playing. Try a lower casting quality in Settings."

    def _auto_skip(self):
        if not settings.load().get("skip_intros"):
            return
        seg = self.current_segment()
        if seg and seg["kind"] == "intro" and seg["start"] not in self.skipped:
            self.skipped.add(seg["start"])
            self.seek(seg["end"])

    def current_segment(self):
        for s in self.segments:
            if s["start"] - 0.5 <= self.position < s["end"] - 1:
                return s
        return None

    def _finished(self):
        """The episode/film ended on the TV. Returns True when the next episode is now loading."""
        try:
            cat().save_position(self.info["id"], self.duration or self.position, played=True)
        except Exception as e:
            print("cast: couldn't mark watched:", e)
        self._report("Stopped", self.duration or self.position)
        if self.next and settings.load().get("autoplay_next", True):
            return self.play_next()
        self._finish("ended", "Finished")
        return False

    def play_next(self):
        nxt = self.next
        if not nxt:
            return False
        try:
            self.prepare(nxt["id"], start=0, audio=None, sub=None)
            self.play_session = uuid.uuid4().hex
            self._load(0.0)
            self._report("")
            self._push(force=True)
            return True
        except Exception as e:
            self._fail(f"Couldn't start the next episode: {e}")
            return False

    # ------------------------------------------------------------------ progress
    def _save(self):
        self.last_save = time.time()
        if self.position < 5:
            return
        try:
            cat().save_position(self.info["id"], self.position)
        except Exception as e:
            print("cast: position not saved:", e)
        self._report("Progress")

    def _report(self, what, pos=None):
        """The same Jellyfin session reports the built-in player sends (its queue retries them)."""
        info = self.info
        if not info or info.get("source") != "jellyfin" or APP is None:
            return
        pos = self.position if pos is None else pos
        body = {"ItemId": info["id"], "PlayMethod": {"direct": "DirectPlay", "remux": "DirectStream"}.get(
            self.stream.mode if self.stream else "", "Transcode"), "CanSeek": True, "PlaySessionId": self.play_session,
            "PositionTicks": int(pos * TICKS)}
        if what == "Progress":
            body["IsPaused"] = self.state == "paused"
        try:
            APP.player._reports.put((what, body, False, False))
        except Exception as e:
            print("cast: report failed:", e)

    # ------------------------------------------------------------------ hand-over from the PC player
    def _handover_done(self):
        h, self.handover = self.handover, None
        if h and APP is not None and getattr(APP.player, "playing", False):
            try:
                APP.player.stop()
            except Exception as e:
                print("cast: local player didn't stop:", e)

    # ------------------------------------------------------------------ controls
    def toggle(self):
        (self.play if self.state == "paused" else self.pause)()

    def play(self):
        self.receiver.play()
        self._set("playing")

    def pause(self):
        if "pause" not in self.receiver.can:
            raise CastError("This TV doesn't take pause from Marquee: use its own remote")
        self.receiver.pause()
        self._set("paused")
        self._save()

    def seek(self, t):
        t = max(0.0, min(float(t), (self.duration or float(t) + 1) - 1))
        self.receiver.seek(t)
        self.position = t
        self._push(force=True)

    def set_volume(self, v):
        if "volume" not in self.receiver.can:
            raise CastError("This TV's volume is set with its own remote")
        self.receiver.set_volume(v)
        self.volume = v
        if self.muted and v > 0:
            self.receiver.set_muted(False)
            self.muted = False
        self._push(force=True)

    def set_muted(self, m):
        self.receiver.set_muted(m)
        self.muted = bool(m)
        self._push(force=True)

    def set_sub(self, sub_id):
        """None/"off" or a subtitle id. Text tracks switch on the TV; burned-in ones need a new stream."""
        new = None if sub_id in (None, "", "off") else str(sub_id)
        p = self.probe
        new_obj = next((s for s in p["subs"] if s["id"] == new), None) if new else None
        if new and not new_obj:
            raise CastError("No such subtitles")
        old_obj = self._sub_obj()
        self.sub = new
        burned_before = bool(old_obj and old_obj["kind"] == "image")
        burned_now = bool(new_obj and new_obj["kind"] == "image")
        if self.dev["kind"] == "airplay" or burned_before or burned_now:
            self._load(self.position)   # a new stream (burn-in on/off) or a new default rendition (AirPlay)
        elif new_obj:
            self.receiver.set_text_track(self.track_ids[new_obj["id"]])
        else:
            self.receiver.set_text_track(None)
        self._push(force=True)

    def set_audio(self, index):
        if not any(str(a["index"]) == str(index) for a in self.probe["audio"]):
            raise CastError("No such audio track")
        self.audio = int(index)
        self._load(self.position)
        self._push(force=True)

    def skip(self):
        seg = self.current_segment()
        if seg and seg["kind"] == "credits" and self.next:
            self._report("Stopped")
            try:
                cat().save_position(self.info["id"], self.duration, played=True)
            except Exception:
                pass
            self.play_next()
        elif seg:
            self.skipped.add(seg["start"])
            self.seek(seg["end"])

    # ------------------------------------------------------------------ ending
    def _fail(self, msg):
        print("cast:", msg, flush=True)
        self._finish("error", msg)
        h, self.handover = self.handover, None
        if h and APP is not None and getattr(APP.player, "playing", False):
            try:
                APP.player.pause("play")
                APP.player.toast("Couldn't cast · playing here")
            except Exception:
                pass

    def _finish(self, state, msg=None):
        global current
        with _lock:
            if self.done:
                return
            self.done = True
        self.stop_evt.set()
        if self.started_playing and state != "ended" and self.position >= 5:
            try:
                cat().save_position(self.info["id"], self.position)
            except Exception:
                pass
            self._report("Stopped")
        rcv, self.receiver = self.receiver, None
        if rcv is not None:
            try:
                if state in ("stopped", "ended", "error"):
                    rcv.stop()
                else:
                    rcv.close()
            except Exception as e:
                print("cast: receiver stop:", e)
        if self.stream is not None:
            stream.stop(self.stream.id)
        endpoint.revoke(self.token)
        self.message = msg
        self._set(state)
        with _lock:
            if current is self:
                current = None
        self._push(force=True)

    def stop(self, msg="Stopped casting"):
        threading.Thread(target=self._finish, args=("stopped", msg), daemon=True, name="cast-stop").start()

    # ------------------------------------------------------------------ state for the page
    def _set(self, state):
        self.state = state

    def public(self):
        info, p = self.info or {}, self.probe or {}
        seg = self.current_segment() if self.started_playing else None
        tv = endpoint.hits.get(self.token)
        recv = self.receiver
        return {
            "active": self.state not in ("ended", "stopped", "error"), "id": self.id, "state": self.state,
            "message": self.message,
            "device": devices.public(self.dev),
            "item": {"id": info.get("id"), "type": info.get("type"), "name": info.get("name"),
                     "series": info.get("series"), "series_id": info.get("series_id"), "season": info.get("season"),
                     "episode": info.get("episode"), "year": info.get("year"), "poster": info.get("poster"),
                     "backdrop": info.get("backdrop"), "thumb": info.get("thumb")} if info else None,
            "position": round(self.position, 2), "duration": round(self.duration, 2),
            "volume": self.volume, "muted": self.muted,
            "can": sorted(recv.can) if recv else [],
            "subs": [{"id": s["id"], "label": s["label"], "kind": s["kind"], "lang": s.get("lang")}
                     for s in p.get("subs") or []],
            "sub": self.sub,
            "audios": [{"index": a["index"], "label": a["label"]} for a in p.get("audio") or []],
            "audio": self.audio,
            "segment": seg, "next": self.next,
            "mode": self.stream.mode if self.stream else None, "quality": (self.plan or {}).get("label"),
            "reason": (self.plan or {}).get("reason"),
            "tv_fetching": bool(tv and time.time() - tv["at"] < 15),
        }

    def _push(self, force=False):
        st = self.public()
        sig = (st["state"], int(st["position"]), st["sub"], st["audio"], st["volume"], st["muted"],
               (st["item"] or {}).get("id"), bool(st["segment"]))
        now = time.time()
        if not force and sig == _last.get("sig") and now - _last.get("at", 0) < 5:
            return
        _last.update(sig=sig, at=now, state=st)
        for fn in list(listeners):
            try:
                fn(st)
            except Exception as e:
                print("cast listener:", type(e).__name__, e)


# ---------------------------------------------------------------------------------------------- module API
def state():
    with _lock:
        cs = current
    if cs is not None:
        return cs.public()
    last = _last.get("state")
    if last and not last.get("active") and time.time() - _last.get("at", 0) < 30:
        return last    # the message of a cast that just ended (shown once by the page)
    return {"active": False, "state": "idle"}


def get():
    return current


def start(dev_id, item_id=None, start_at=None, audio=None, sub=None):
    """Cast a title (item_id None: what's playing on the PC, from where it is). Returns the first state."""
    global current
    if not enabled():
        raise CastError("Casting is turned off in Settings")
    dev = devices.discovery.get(dev_id)
    if dev is None:
        raise CastError("That TV isn't on the network any more. Look again?")
    handover = None
    p = APP.player if APP is not None else None
    if p is not None and getattr(p, "playing", False) and getattr(p, "item", None):
        cur = p.item
        cur_id = cur.get("_catalog_id") or cur["Id"]
        pos = p._position()
        same = item_id is None or item_id == cur_id or (cat().lookup(item_id) or {}).get("id") == \
            (cat().lookup(cur_id) or {}).get("id")
        if item_id is None:
            item_id = cur_id
        if same and start_at is None:
            start_at = pos
        handover = cur_id
    if item_id is None:
        raise CastError("Nothing to cast: pick a title first")
    with _lock:
        old = current
        cs = CastSession(dev)
        try:
            cs.prepare(item_id, start_at, audio, sub)
        except CastError:
            endpoint.revoke(cs.token, stop_when_idle=old is None)
            raise
        current = cs
    if old is not None:
        old._finish("stopped", None)
        with _lock:
            current = cs
    if handover and p is not None:
        try:
            p.pause("pause")
            p.toast(f"Casting to {dev['name']}…")
        except Exception:
            pass
        cs.handover = handover
    cs.thread = threading.Thread(target=cs.run, args=(item_id, start_at, audio, sub), daemon=True,
                                 name="cast-session")
    cs.thread.start()
    devices.discovery.ensure()
    return cs.public()


def command(action, value=None):
    cs = current
    if cs is None:
        raise CastError("Nothing is casting")
    if cs.receiver is None or cs.state in ("connecting",):
        if action == "stop":
            cs.stop()
            return
        raise CastError("Still connecting to the TV")
    if action in ("toggle", "pause", "play"):
        {"toggle": cs.toggle, "pause": cs.pause, "play": cs.play}[action]()
    elif action == "seek":
        cs.seek(float(value))
    elif action == "seek_rel":
        cs.seek(cs.position + float(value))
    elif action == "volume":
        cs.set_volume(float(value))
    elif action == "mute":
        cs.set_muted(not cs.muted if value is None else bool(value))
    elif action == "sub":
        cs.set_sub(value)
    elif action == "audio":
        cs.set_audio(value)
    elif action == "skip":
        cs.skip()
    elif action == "next":
        if not cs.next:
            raise CastError("No next episode")
        cs._report("Stopped")
        cs.play_next()
    elif action == "stop":
        cs.stop()
    else:
        raise CastError("unknown action")


def stop_all():
    cs = current
    if cs is not None:
        cs._finish("stopped", None)
