"""Playback: libmpv drawn into a panel inside the app window, with the Marquee controls (osc/marquee.lua).

* Local first: Original quality plays the file itself when it's on this PC (no transcoding, full 4K HDR, every
  audio and subtitle track), started from what we already know about the item (LocalMap, learned from every
  Jellyfin response) without waiting for Jellyfin. Details, intro markers, the next episode and progress reports
  happen in the background with short timeouts; reports that can't be delivered are queued and sent later. Only
  lower qualities (or a file that isn't on this PC) need Jellyfin to stream.
* NVIDIA RTX Video Super Resolution is mpv's d3d11vpp filter in "nvidia" mode, sized to the window.
* Stable volume is ffmpeg's dynaudnorm: it looks ahead a few seconds, so it turns sudden loud scenes down and
  quiet dialogue up before you hear the jump. Night mode (a compressor) and Dialogue boost (centre channel /
  speech emphasis) chain in front of it. Pass-through sends Dolby/DTS untouched to a receiver instead.
* Seek previews: Jellyfin trickplay tiles, else frames from a small hidden second mpv (class Thumbs).
* Picture-in-picture: the same window turned into a small borderless always-on-top player.
* Progress goes to Jellyfin (resume points, Continue Watching, Next Up).
"""
import ctypes
import json
import os
import sys
import threading
import time
import queue
import re
import subprocess
import uuid
from collections import deque

from . import bookmarks, clips, settings
from .jellyfin import FIELDS

BASE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
for cand in (os.path.join(BASE, "mpv"), os.path.join(BASE, "..", "vendor", "mpv"), os.path.join(BASE, "vendor", "mpv")):
    if os.path.exists(os.path.join(cand, "libmpv-2.dll")):
        os.environ["PATH"] = os.path.abspath(cand) + os.pathsep + os.environ["PATH"]
        break
import mpv  # noqa: E402

OSC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "osc", "marquee.lua")
GLASS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "osc", "glass.glsl")
TICKS = 10_000_000
QUALITY_LABEL = {"original": "Original", "2160": "2160p 4K", "1080": "1080p", "720": "720p", "480": "480p"}
UPSCALER_LABEL = {"auto": "Auto", "nvidia": "NVIDIA RTX", "anime4k": "Anime4K", "hq": "High quality", "off": "Off"}
VENDOR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vendor"))
# Anime4K "Mode A (HQ)" (MIT, github.com/bloc97/Anime4K), in order; only used when the files are in vendor/anime4k
ANIME4K = ["Anime4K_Clamp_Highlights.glsl", "Anime4K_Restore_CNN_VL.glsl", "Anime4K_Upscale_CNN_x2_VL.glsl",
           "Anime4K_AutoDownscalePre_x2.glsl", "Anime4K_AutoDownscalePre_x4.glsl", "Anime4K_Upscale_CNN_x2_M.glsl"]
CHAPTER_INTRO = ("opening", "intro", "op", "prologue", "cold open", "recap")
CHAPTER_CREDITS = ("ending", "credits", "ed", "outro", "end credits")
STABLE = "dynaudnorm=f=200:g=21:p=0.9:m=10:r=0.6:s=12"
NIGHT = "acompressor=threshold=-28dB:ratio=6:attack=8:release=250:makeup=6dB:knee=6dB"
LIMITER = "alimiter=limit=0.97:level=disabled"
SPDIF = "ac3,eac3,dts,dts-hd,truehd"
SPDIF_CODECS = {"ac3", "eac3", "dts", "truehd", "mlp"}
# channel order of ffmpeg's layouts, for the dialogue boost's centre-channel mix
LAYOUTS = {
    "3.0": "FL FR FC", "4.0": "FL FR FC BC", "5.0": "FL FR FC BL BR", "5.0(side)": "FL FR FC SL SR",
    "5.1": "FL FR FC LFE BL BR", "5.1(side)": "FL FR FC LFE SL SR", "6.1": "FL FR FC LFE BC SL SR",
    "7.1": "FL FR FC LFE BL BR SL SR", "7.1(wide)": "FL FR FC LFE BL BR FLC FRC",
    "7.1(wide-side)": "FL FR FC LFE FLC FRC SL SR",
}
HWDEC_ON = "d3d11va,auto-safe"
TEXT_SUBS = {"ass": "ass", "ssa": "ass", "subrip": "srt", "srt": "srt", "webvtt": "vtt", "vtt": "vtt", "mov_text": "srt"}


def _font_installed(name):
    """True if a font file whose name starts with `name` is installed for the machine or this user."""
    key = name.lower().replace(" ", "")
    for d in (os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts")):
        try:
            if any(f.lower().replace(" ", "").replace("-", "").startswith(key) for f in os.listdir(d)):
                return True
        except OSError:
            pass
    return False


class Player:
    def __init__(self, app):
        self.app = app          # the App (window, jellyfin, ui callbacks)
        self.mpv = None
        self.item = None
        self.next_item = None
        self.quality = "original"
        self.panel = None
        self.form = None
        self.playing = False
        self.session = None
        self._last_report = 0
        self._lock = threading.RLock()
        self._vf = None
        self._vf_timer = None
        self._announce = False
        self._credits = None
        self._auto_skip = None
        self._pos = 0
        self._af = None
        self._shaders = None
        self._upscale_cap = None   # the upscaler couldn't keep up: don't go above this for the rest of the video
        self._seg_source = None
        self.clip = None
        self.mini = False          # credits: the video shrunk into a corner of the app window
        self._credits_done = None  # item id whose credits already went mini (or were skipped on purpose)
        self._index_outro = None
        self._last_tick_pos = None
        self.on_credits = []       # listeners: fn(payload) when the credits start (the assistant's comment)
        self.pip = False
        self._pip_saved = None
        self.hwnd = None
        self.thumbs = Thumbs(self)
        self.map = LocalMap()
        app.jf.on_items.append(self.map.learn)
        self._reports = queue.Queue()
        threading.Thread(target=self._report_worker, daemon=True, name="jellyfin-reports").start()
        self.on_state = []      # listeners: fn(state_dict), called from a worker thread on changes
        self._subs = deque(maxlen=400)   # (time, text) of subtitle lines as they were shown
        self._emit_at = 0
        self._dev_mute = bool(os.environ.get("MARQUEE_DEV_MUTE"))
        self._fs_before = False  # the window was already fullscreen when playback started (then it stays so)

    # ------------------------------------------------------------------ setup (GUI thread)
    def attach(self, form):
        """Add a black panel to the window for mpv to draw into (hidden until something plays)."""
        import clr  # noqa: F401
        from System.Drawing import Color
        from System.Windows.Forms import DockStyle, Panel

        self.form = form
        self.panel = Panel()
        self.panel.BackColor = Color.Black
        self.panel.Dock = DockStyle.Fill
        self.panel.Visible = False
        form.Controls.Add(self.panel)
        form.Activated += lambda *_: self.focus()  # back from another app: keys go to the video again
        self.hwnd = form.Handle.ToInt64()
        wid = str(self.panel.Handle.ToInt64())
        _raise_priority()
        st = settings.load()
        font = "Google Sans" if _font_installed("GoogleSans") else "Segoe UI Semibold"
        extra = {}
        if self._dev_mute:  # development runs never make a sound
            extra.update(ao="null", mute="yes")
        elif st["audio_device"] and st["audio_device"] != "auto":
            extra["audio_device"] = st["audio_device"]
        if os.environ.get("MARQUEE_DEV_IPC"):
            extra["input_ipc_server"] = os.environ["MARQUEE_DEV_IPC"]
        self.mpv = mpv.MPV(
            wid=wid, vo="gpu-next", gpu_api="d3d11", hwdec=HWDEC_ON if st["hwdec"] else "no", hwdec_codecs="all",
            audio_spdif=SPDIF if st["passthrough"] else "", audio_exclusive="yes" if st["passthrough"] else "no",
            audio_channels="auto", audio_pitch_correction="yes", window_dragging="no", osc="no", osd_bar="no", osd_level=0,
            input_default_bindings=False, input_vo_keyboard=True, keep_open="yes", idle="yes", force_window="yes",
            scripts=OSC, glsl_shaders=GLASS, hr_seek="yes", sub_auto="fuzzy", slang=st["sub_lang"] + ",en,eng", cache="yes",
            # read far ahead into RAM so a busy disk (or Jellyfin updating) never reaches the picture: a 4K remux
            # at ~80 Mbit/s needs ~10 MB/s, so 1.5 GiB is about two minutes
            demuxer_max_back_bytes="300MiB", video_sync="display-resample" if st.get("interpolation") else "audio",
            interpolation="yes" if st.get("interpolation") else "no", tscale="oversample",
            subs_with_matching_audio="forced", demuxer_max_bytes="1500MiB", demuxer_readahead_secs=120,
            screenshot_directory=os.path.expanduser("~\\Pictures"), cursor_autohide="no",
            sub_font=font, sub_font_size=44, sub_border_size=2.4, sub_shadow_offset=1.5,
            sub_color="#FFFFFFFF", sub_border_color="#E0000000", ytdl=False, log_handler=self._log, loglevel="info",
            **extra)
        self._set("font", font)
        self._set("icons", "Segoe Fluent Icons" if _font_installed("SegoeIcons") else "Segoe MDL2 Assets")
        self._set("glass_shader", any("glass.glsl" in x for x in (self.mpv.glsl_shaders or [])))
        self.mpv.register_message_handler("marquee", self._message)
        self.mpv.observe_property("eof-reached", self._eof)
        self.mpv.observe_property("video-params", lambda *_: self._upscaler_soon(0.05))
        self.mpv.observe_property("osd-dimensions", lambda *_: self._upscaler_soon(0.35))
        # a different audio track (codec, channel layout) needs the audio chain worked out again
        self.mpv.observe_property("current-tracks/audio/codec", lambda *_: self._audio_soon())
        self.mpv.observe_property("audio-params/channels", lambda *_: self._audio_soon())
        self.mpv.event_callback("file-loaded")(self._file_loaded)
        self.mpv.observe_property("sub-text", self._sub_text)
        threading.Thread(target=self._watchdog, daemon=True, name="frame-watchdog").start()
        self.mpv.observe_property("pause", lambda *_: self._emit())
        self.mpv.event_callback("seek")(lambda *_: self._emit(throttle=0.5))
        threading.Thread(target=self._reporter, daemon=True).start()

    def _log(self, level, component, message):
        msg = message.strip()
        token = getattr(self.app.jf, "token", None)
        if token:
            msg = msg.replace(token, "***")
        if level in ("error", "fatal") or "Super Resolution" in msg or (component == "d3d11vpp" and level != "debug"):
            print(f"mpv {component}: {msg}", flush=True)

    def _ui(self, fn):
        """Run fn on the window's thread."""
        from System import Action
        self.form.Invoke(Action(fn))

    def _set(self, key, value):
        self.mpv.command("set", f"user-data/marquee/{key}", json.dumps(value))

    # ------------------------------------------------------------------ play
    def play(self, item_id, start=None, quality=None, card=None):
        """Play a Jellyfin item (from any thread). start: seconds, or None for the resume point. card: what the
        UI already knows about the item (path, name, ...); helps when the item isn't in the local map yet."""
        self._t_play = time.time()
        st = settings.load()
        want = quality or st["quality"]
        info = self._lookup(item_id)
        if info and info.get("id"):
            item_id = info["id"]  # a show or season resolves to the episode to play
        item = _item_from_catalog(info) if info and info.get("exists") else self.map.item(item_id, card)
        local_ok = bool(item and _exists(item.get("Path")))
        if want != "original" or not local_ok:
            # a transcode, or a file that isn't on this PC: Jellyfin has to stream it, so ask it
            try:
                fresh = self.app.jf.get(f"/Items/{item_id}", userId=self.app.jf.user, Fields=FIELDS + ",Trickplay",
                                        timeout=8 if not local_ok else 3)
                if item and item.get("_catalog"):  # keep what the catalog knows (segments, next episode)
                    fresh = {**fresh, **{k: v for k, v in item.items() if k.startswith("_")}}
                item = fresh
            except Exception as e:
                if not local_ok:
                    self.toast("Jellyfin isn't answering, and this video isn't on this PC")
                    print("play failed:", e)
                    return
                want = "original"  # Jellyfin is down: the file is here, play it as it is
                self.toast("Jellyfin isn't answering · playing the original file")
        with self._lock:
            self._play(item_id, start, want if quality or want != st["quality"] else None, item=item)

    def _catalog(self):
        cat = getattr(self.app, "catalog", None)
        if cat is None:
            try:
                from . import server
                cat = server.catalog() if server.APP is self.app else None
            except Exception:
                cat = None
        return cat

    def _lookup(self, item_id):
        try:
            cat = self._catalog()
            return cat.lookup(item_id) if cat else None
        except Exception as e:
            print("catalog lookup failed:", e)
            return None

    def play_file(self, card, start=None):
        """Play a local file that Jellyfin doesn't know yet ("Just arrived"). card: path, name, series, season,
        episode, year, poster. No Jellyfin reporting; the position is remembered locally by path."""
        self._t_play = time.time()
        path = card["path"]
        episode = card.get("episode") is not None and card.get("series")
        item = {"Id": "file:" + path, "_local": True, "Path": path, "Type": "Episode" if episode else "Movie",
                "Name": card.get("name") or os.path.splitext(os.path.basename(path))[0],
                "SeriesName": card.get("series"), "ParentIndexNumber": card.get("season"),
                "IndexNumber": card.get("episode"), "ProductionYear": card.get("year"),
                "MediaSources": [{"Path": path, "Id": ""}], "UserData": {},
                "RunTimeTicks": int((card.get("runtime") or 0) * TICKS),
                "Genres": ["Anime"] if card.get("anime") else [],
                "_catalog_id": card.get("id") or ("file:" + path), "_segments": card.get("segments"),
                "_next_id": card.get("next_episode_id")}
        if start is None:
            info = self._lookup(item["_catalog_id"])
            start = (info or {}).get("position") or (_positions().get(path) or {}).get("pos", 0)
        with self._lock:
            self._play(None, start, "original", item=item)

    def _play(self, item_id, start, quality, item=None):
        jf = self.app.jf
        if item is None:
            item = jf.get(f"/Items/{item_id}", userId=jf.user, Fields=FIELDS + ",Trickplay", timeout=8)
        local = bool(item.get("_local"))
        st = settings.load()
        if not self.item:  # starting from the browsing UI (not the next episode or a quality switch)
            self._fs_before = bool(self.app.fullscreen)
        if self.item:  # switching video or quality: close the old session first
            self._report("Stopped", self._position())
        switching_quality = bool(self.item and self.item["Id"] == item["Id"] and quality)
        self.item = item
        self.quality = "original" if local else (quality or st["quality"])
        ud = item.get("UserData") or {}
        if start is None:
            start = (ud.get("PlaybackPositionTicks") or 0) / TICKS
            if not start and not local:  # watched as a "Just arrived" file before Jellyfin had it?
                path = (item.get("MediaSources") or [{}])[0].get("Path") or item.get("Path")
                start = (_positions(pop=path) or {}).get("pos", 0)
        anime = self._is_anime(item)
        self.mpv.alang = (st["audio_lang_anime"] + ",ja,jpn,eng,en") if anime else "eng,en"
        self.mpv.slang = st["sub_lang"] + ",en,eng"
        if not switching_quality:  # a quality change keeps speed and volume as they are
            self.mpv.speed = 1
            self.mpv.volume = st["base_volume"]
        self.mpv.mute = self._dev_mute
        self._publish(item, st)
        session_id = uuid.uuid4().hex
        url = item["Path"] if local else jf.stream_url(item, self.quality, session_id)
        self._set("source_kind", "local file" if not url.startswith("http") else
                  ("Jellyfin transcode" if self.quality != "original" else "Jellyfin stream"))
        self.session = {"ItemId": item["Id"], "MediaSourceId": (item.get("MediaSources") or [{}])[0].get("Id"),
                        "PlayMethod": "DirectPlay" if url == (item.get("MediaSources") or [{}])[0].get("Path")
                        else ("DirectStream" if self.quality == "original" else "Transcode"),
                        "CanSeek": True, "PlaySessionId": session_id}
        self._vf = None
        self._announce = not switching_quality
        self.mpv.command("script-message", "marquee-reset")
        self._af = None
        if not switching_quality:  # previews come from the original file, whatever quality plays
            src_path = (item.get("MediaSources") or [{}])[0].get("Path") or item.get("Path")
            self.thumbs.reset(item, src_path if src_path and os.path.exists(src_path) or local
                              else jf.stream_url(item, "original"))
        self.mpv.loadfile(url, start=f"{max(0.0, start):.3f}")
        self.mpv.pause = False
        self.playing = True
        self._last_report = time.time()
        self._pos = max(0.0, start)
        self._ui(self._show)
        if st.get("fullscreen_on_play", True) and not self.app.fullscreen and not self.pip and not self.mini:
            self.app.toggle_fullscreen()   # "Fullscreen when playing"; stop() goes back to a window
            self._set("fullscreen", True)
        self._report("", start)
        if item.get("_segments"):
            self._publish_segments(item["Id"], [{"Type": g.get("type"), "StartTicks": int(g.get("start", 0) * TICKS),
                                                 "EndTicks": int(g.get("end", 0) * TICKS)} for g in item["_segments"]])
        self._local_next(item)   # instant: from the catalog (or the local map)
        if not local:
            threading.Thread(target=self._details, args=(item["Id"],), daemon=True).start()
        self._emit()

    def _report(self, what, pos, session=None, item=None, sync=False):
        """Playback state to Jellyfin ('' started, 'Progress', 'Stopped'); local files keep it in positions.json."""
        item = item or self.item
        session = session if session is not None else (self.session or {})
        if not item:
            return
        cat = self._catalog()
        saved = False
        if what in ("Progress", "Stopped") and cat:
            try:
                saved = bool(cat.save_position(item.get("_catalog_id") or item["Id"], pos))
            except Exception as e:
                print("catalog position failed:", e)
        if item.get("_local"):
            if what in ("Progress", "Stopped") and not saved:  # a file the catalog doesn't know (yet)
                _remember_position(item["Path"], pos, (self.mpv.duration if self.mpv else None) or 0)
            return
        body = {**session, "PositionTicks": int(pos * TICKS)}
        if what == "Progress" and self.mpv:
            body.update(IsPaused=bool(self.mpv.pause), PlaybackRate=float(self.mpv.speed or 1))
        if what in ("Progress", "Stopped"):
            dur = (self.mpv.duration if self.mpv else 0) or (item.get("RunTimeTicks") or 0) / TICKS
            self.map.position(item["Id"], pos, dur)
        # with the catalog, positions reach Jellyfin through its sync, so a failed session report isn't kept
        self._reports.put((what, body, session.get("PlayMethod") == "Transcode" and what == "Stopped", not cat))
        if sync:  # the window is closing: deliver (or queue) right now
            self._drain_reports(deadline=time.time() + 2.5)

    def _report_worker(self):
        """Sends reports in order, 3 s per try; whatever Jellyfin doesn't take goes to pending_reports.json
        and is retried every minute."""
        last_retry = 0
        while True:
            try:
                job = self._reports.get(timeout=5)
            except queue.Empty:
                job = None
            if job:
                self._send_report(*job)
            if time.time() - last_retry > 60:
                last_retry = time.time()
                _retry_pending(self.app.jf)

    def _drain_reports(self, deadline):
        """Closing: send what's waiting within the deadline; once Jellyfin fails (or time is up) the rest goes
        straight to the pending file."""
        down = False
        while True:
            try:
                job = self._reports.get_nowait()
            except queue.Empty:
                return
            left = deadline - time.time()
            if down or left < 0.3:
                if job[0] in ("Progress", "Stopped") and job[3]:
                    _queue_pending(job[0], job[1])
                continue
            down = not self._send_report(*job, timeout=min(1.5, left))

    def _send_report(self, what, body, stop_transcode, keep=True, timeout=3):
        try:
            self.app.jf.report(what, body, timeout=timeout)
            ok = True
        except Exception as e:
            ok = False
            if keep and what in ("Progress", "Stopped"):
                _queue_pending(what, body)
            if os.environ.get("MARQUEE_DEV_DEBUG"):
                print("report queued:", what, type(e).__name__, flush=True)
        if stop_transcode and ok:
            self.app.jf.stop_transcode(body.get("PlaySessionId"))
        return ok

    def _position(self):
        """Where we are, or where we last were if the video isn't loaded (yet, or it failed to open)."""
        pos = self.mpv.time_pos if self.mpv else None
        if pos is None:
            return self._pos or 0
        self._pos = pos
        return pos

    def _is_anime(self, item):
        path = (item.get("Path") or "").lower()
        return "anime" in path or "Anime" in (item.get("Genres") or [])

    def _publish(self, item, st):
        if item.get("Type") == "Episode":
            title = item.get("SeriesName") or item.get("Name")
            sub = f"S{item.get('ParentIndexNumber', 0)}:E{item.get('IndexNumber', 0)}  ·  {item.get('Name', '')}"
        else:
            title, sub = item.get("Name"), " · ".join(x for x in (str(item.get("ProductionYear") or ""),
                                                                  item.get("OfficialRating") or "") if x)
        self._set("title", title)
        self._set("subtitle", sub)
        self._set("quality", self.quality)
        video = next((x for x in ((item.get("MediaSources") or [{}])[0].get("MediaStreams") or [])
                      if x.get("Type") == "Video"), {})
        self._set("source_height", video.get("Height") or 0)
        self._set("quality_locked", bool(item.get("_local")))
        self._set("quality_label", QUALITY_LABEL.get(self.quality, self.quality))
        self._set("upscaler", st["upscaler"])
        self._set("upscaler_label", UPSCALER_LABEL.get(st["upscaler"], "Off"))
        self._set("rtx_hdr", st["rtx_hdr"])
        self._publish_settings(st)
        self._set("has_next", False)
        self._set("next", None)
        self._set("fullscreen", bool(self.app.fullscreen))
        self._set("segments", [])
        self._set("intro", None)
        self._set("after_credits", None)
        self._set("credits_at", None)
        self._after_credits = []
        self._set("bookmarks", bookmarks.for_item(item["Id"]))
        self._credits = None
        self._auto_skip = None
        self._seg_source = None
        self._upscale_cap = None
        self.clip = None
        self._index_outro = None
        self._last_tick_pos = None
        self._credits_done = None
        self._watched = 0.0          # seconds actually played this session (for "is someone watching?")
        self._paused_since = None
        self._ending_prepared = False

    def _publish_segments(self, item_id, segs):
        if not self.item or self.item["Id"] != item_id:
            return
        st = settings.load()
        marks, intro, credits = [], None, None
        if segs:
            self._seg_source = "jellyfin"
        for s in segs:
            a, b = s.get("StartTicks", 0) / TICKS, s.get("EndTicks", 0) / TICKS
            kind = (s.get("Type") or "").capitalize()
            if kind in ("Intro", "Recap") and b > a:
                intro = intro or {"s": a, "e": b}
                marks.append({"s": a, "e": b})
            if kind == "Outro" and b > a:
                credits = a
        self._set("segments", marks)
        self._set("intro", intro)
        self._credits = credits
        self._auto_skip = intro if (intro and st["skip_intros"]) else None
        if credits and self.next_item:
            self._set_next(item_id, self.next_item)

    def _chapter_segments(self):
        """No intro/credits markers from Jellyfin: use the file's own chapters when they're named for it."""
        if self._seg_source or not self.item:
            return
        chapters = self.mpv.chapter_list or []
        dur = self.mpv.duration or 0
        segs = []
        for i, c in enumerate(chapters):
            name = (c.get("title") or "").strip().lower()
            start = c.get("time") or 0
            end = chapters[i + 1]["time"] if i + 1 < len(chapters) else dur
            words = set(name.replace("-", " ").replace("_", " ").split())
            if any(k == name or k in words or name.startswith(k + " ") for k in CHAPTER_INTRO):
                segs.append({"Type": "Intro", "StartTicks": int(start * TICKS), "EndTicks": int(end * TICKS)})
            elif any(k == name or k in words or name.startswith(k + " ") for k in CHAPTER_CREDITS) and start > dur * 0.5:
                segs.append({"Type": "Outro", "StartTicks": int(start * TICKS), "EndTicks": int(end * TICKS)})
        if segs:
            self._publish_segments(self.item["Id"], segs)
            self._seg_source = "chapters"

    def _publish_settings(self, st):
        """What the controls show for the settings (toggles, values)."""
        self._set("stable", st["stable_volume"])
        self._set("autoplay", st["autoplay_next"])
        self._set("night", st["night_mode"])
        self._set("dialogue", st["dialogue_boost"])
        self._set("passthrough", st["passthrough"])
        self._set("hwdec", st["hwdec"])
        self._set("seek_step", int(st["seek_step"] or 5))
        self._set("audio_device", st["audio_device"] or "auto")
        self._set("pip", self.pip)
        self._set("voice_focus", _voice_focus(st))
        self._set("voice_model", bool(_rnnoise_model()))
        self._set("sdr_to_hdr", "auto" if (st.get("sdr_to_hdr", "auto") == "auto" or st["rtx_hdr"]) else "off")
        self._set("anime4k", bool(_anime4k_files()))
        self._set("clips_ok", bool(clips.tools()[0]))
        self._set("cast_on", bool(st.get("cast", True)))

    def _details(self, item_id):
        """After the first frame: the full item (trickplay, streams), intro/credits markers and the next episode
        from Jellyfin, each with a short timeout. Nothing here can hold up playback."""
        jf = self.app.jf
        if getattr(jf, "down_since", None) is not None:
            return  # known to be down: the catalog already gave us what we need
        try:
            fresh = jf.get(f"/Items/{item_id}", userId=jf.user, Fields=FIELDS + ",Trickplay", timeout=3)
            if self.item and self.item["Id"] == item_id:
                keep = {k: v for k, v in self.item.items() if k.startswith("_")}
                self.item = {**fresh, **keep}
                self.thumbs.update_item(self.item)
        except Exception:
            pass
        if not (self.item or {}).get("_segments"):
            try:
                self._publish_segments(item_id, jf.get(f"/MediaSegments/{item_id}", timeout=3).get("Items", []))
            except Exception:
                pass
        if self.next_item:
            return  # the catalog knew the next episode
        item = self.item
        if not item or item["Id"] != item_id or item.get("Type") != "Episode" or not item.get("SeriesId"):
            return
        try:
            eps = jf.get(f"/Shows/{item['SeriesId']}/Episodes", userId=jf.user, startItemId=item_id, Limit=2,
                         Fields="Path,MediaSources,UserData", timeout=3).get("Items", [])
            nxt = eps[1] if len(eps) > 1 and eps[0]["Id"] == item_id else None
            if nxt:
                self._set_next(item_id, nxt)
        except Exception:
            pass

    def _local_next(self, item):
        self.next_item = None
        nxt = None
        if item.get("_next_id"):
            nxt = _item_from_catalog(self._lookup(item["_next_id"]))
        elif item.get("Type") == "Episode" and not item.get("_local"):
            nxt = self.map.next_episode(item)
        if nxt:
            self._set_next(item["Id"], nxt)

    def _set_next(self, item_id, nxt):
        if self.item and self.item["Id"] == item_id:
            self.next_item = nxt
            self._set("next", {"title": f"S{nxt.get('ParentIndexNumber', 0)}:E{nxt.get('IndexNumber', 0)} · {nxt.get('Name', '')}",
                               "credits": self._credits})
            self._set("has_next", True)

    def _file_loaded(self, _event=None):
        """A transcode carries no subtitles: add the source's text subtitles from Jellyfin."""
        self._audio_soon()
        threading.Timer(1.5, self._chapter_segments).start()  # after catalog/Jellyfin markers had their chance
        if os.environ.get("MARQUEE_DEV_DEBUG") and getattr(self, "_t_play", None):
            print(f"file loaded {1000 * (time.time() - self._t_play):.0f} ms after play()", flush=True)
        try:
            item, session = self.item, self.session or {}
            if not item or session.get("PlayMethod") != "Transcode":
                return
            src = (item.get("MediaSources") or [{}])[0]
            jf = self.app.jf
            pick, prefs = None, [x for x in settings.load()["sub_lang"].split(",") + ["eng", "en"] if x]
            for s in src.get("MediaStreams") or []:
                fmt = TEXT_SUBS.get((s.get("Codec") or "").lower())
                if s.get("Type") != "Subtitle" or not fmt:
                    continue
                url = (f"{jf.base}/Videos/{item['Id']}/{src.get('Id')}/Subtitles/{s['Index']}/0/Stream.{fmt}"
                       f"?ApiKey={jf.token}")
                title = s.get("Title") or s.get("DisplayTitle") or ""
                self.mpv.command("sub-add", url, "auto", title, s.get("Language") or "")
                if pick is None and (s.get("Language") or "") in prefs and not s.get("IsForced"):
                    pick = len([t for t in self.mpv.track_list if t.get("type") == "sub"])
            if pick and self._is_anime(item):
                subs = [t for t in self.mpv.track_list if t.get("type") == "sub"]
                self.mpv.sid = subs[pick - 1]["id"]
        except Exception as e:
            print("subtitles failed:", e)

    # ------------------------------------------------------------------ window
    def _show(self):
        self.app.webview_control().Visible = False
        self.panel.Visible = True
        self.panel.BringToFront()
        self._focus_video()

    def _focus_video(self):
        """mpv creates its window inside the panel *disabled* (an embedding host is expected to forward input).
        Enable it, so the controls get the mouse directly, and give it keyboard focus for the key bindings."""
        import ctypes
        u32 = ctypes.windll.user32
        u32.FindWindowExW.restype = ctypes.c_void_p
        u32.FindWindowExW.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p)
        child = u32.FindWindowExW(self.panel.Handle.ToInt64(), None, "mpv", None)
        if not child:  # mpv makes its window when the video output starts: try again shortly
            self._focus_tries = getattr(self, "_focus_tries", 0) + 1
            if self._focus_tries < 25:
                threading.Timer(0.2, self.focus).start()
            return
        self._focus_tries = 0
        u32.EnableWindow(ctypes.c_void_p(child), True)
        me = ctypes.windll.kernel32.GetCurrentThreadId()
        other = u32.GetWindowThreadProcessId(ctypes.c_void_p(child), None)
        if _focused_window(other) == child:
            return  # already there; refocusing would make mpv release held keys/buttons (breaks hold & drag)
        u32.ReleaseCapture()  # the form can hold the mouse capture, which starves mpv's window of mouse moves
        # mpv's window lives on mpv's own thread: share input state just long enough to hand it the focus
        u32.AttachThreadInput(me, other, True)
        u32.SetFocus(ctypes.c_void_p(child))
        u32.AttachThreadInput(me, other, False)

    def focus(self):
        """From any thread: put keyboard focus back on the video (window activated, video clicked)."""
        if self.playing and self.panel is not None:
            from System import Action

            def go():
                try:
                    if self.playing:
                        self.form.BeginInvoke(Action(self._focus_video))
                except Exception:
                    pass
            go()
            # WinForms restores focus to its last control right after activation: claim it again after that
            threading.Timer(0.25, go).start()

    def _hide(self):
        self._unmini_now()
        self.panel.Visible = False
        ctl = self.app.webview_control()
        ctl.Visible = True
        ctl.Focus()

    def stop(self):
        with self._lock:
            if not self.item:
                return
            pos = self._position()
            item, session = self.item, self.session or {}
            self.playing = False
            self.item = None
            self.next_item = None
            self.thumbs.stop()
            self.mpv.command("stop")
            self._report("Stopped", pos, session=session, item=item)
        if self.pip:
            self._ui(self._pip_exit)
        if self.app.fullscreen and not self._fs_before:   # back to a window, unless it was fullscreen before playing
            self.app.toggle_fullscreen()
        self._ui(self._hide)
        self.app.notify_ui("playback-ended", {"id": item["Id"]})
        self._emit()

    def shutdown(self):
        """The window is closing: save where we were. (Doesn't touch the window: it's going away.)"""
        if self.item and self.mpv:
            pos = self._position()
            self.playing = False
            try:
                self.mpv.pause = True
            except Exception:
                pass
            self._report("Stopped", pos, sync=True)
            self.item = None

    def terminate(self):
        if self.mpv:
            m, self.mpv = self.mpv, None
            m.terminate()

    # ------------------------------------------------------------------ controls from the OSC
    def _message(self, action=b"", value=b"", *more):
        # mpv's event thread: anything slow (Jellyfin calls, reloading) goes to a worker so the
        # controls stay responsive. python-mpv hands the message arguments over as bytes.
        dec = lambda v: v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)  # noqa: E731
        action, value, more = dec(action), dec(value), [dec(m) for m in more]
        if action == "thumb":  # hover previews: many per second, straight to their own worker
            try:
                self.thumbs.request(float(value), *[int(float(m)) for m in more[:5]])
            except (ValueError, TypeError):
                pass
            return
        if action == "thumb-hide":
            self.thumbs.hide()
            return
        if action == "dev-call" and os.environ.get("MARQUEE_DEV_IPC"):  # development: try the public API
            allowed = {"play", "play_file", "pause", "seek", "set_volume", "toast", "show_card", "state",
                       "subtitle_window"}
            if value in allowed:
                def call():
                    try:
                        print("dev-call", value, "->", getattr(self, value)(*json.loads(more[0] if more else "[]")),
                              flush=True)
                    except Exception as e:
                        print("dev-call failed:", value, e, flush=True)
                threading.Thread(target=call, daemon=True).start()
            return
        if action == "debug-dump" and os.environ.get("MARQUEE_DEV_IPC"):  # development: where is every thread?
            import faulthandler
            faulthandler.dump_traceback(all_threads=True)
            return
        if os.environ.get("MARQUEE_DEV_DEBUG"):
            print("action", action, value, more, flush=True)
        threading.Thread(target=self._action, args=(action, value, more), daemon=True).start()

    def _action(self, action, value, more=()):
        try:
            if action == "focus":
                self.focus()
            elif action == "close":
                self.stop()
            elif action == "fullscreen":
                if self.pip:  # from the mini player, fullscreen means "back to the big player" first
                    self._ui(self._pip_exit)
                    time.sleep(0.1)
                self._announce = True  # say what the upscaler does at the new size
                self.app.toggle_fullscreen()
                self._set("fullscreen", bool(self.app.fullscreen))
                time.sleep(0.15)
                self.focus()
            elif action == "next" and self.next_item:
                self.play(self.next_item["Id"], start=0)
            elif action == "quality" and self.item:
                pos = self._position()
                settings.save({"quality": value})
                self.play(self.item["Id"], start=pos, quality=value)
                self.toast(f"Quality: {QUALITY_LABEL.get(value, value)}")
            elif action == "upscaler":
                settings.save({"upscaler": value})
                self._set("upscaler", value)
                self._set("upscaler_label", UPSCALER_LABEL.get(value, "Off"))
                self.apply_upscaler(announce=True)
            elif action == "rtx_hdr":
                settings.save({"rtx_hdr": value == "on"})
                self._set("rtx_hdr", value == "on")
                self.apply_upscaler(announce=True)
            elif action == "stable":
                on = value == "on"
                settings.save({"stable_volume": on})
                self._set("stable", on)
                self._apply_audio()
                self.toast("Stable volume on" if on else "Stable volume off")
            elif action == "autoplay":
                on = value == "on"
                settings.save({"autoplay_next": on})
                self._set("autoplay", on)
            elif action in ("night", "dialogue"):
                on = value == "on"
                settings.save({"night_mode" if action == "night" else "dialogue_boost": on})
                if action == "dialogue":
                    settings.save({"voice_focus": "dialogue" if on else "off"})
                    self._set("voice_focus", "dialogue" if on else "off")
                self._set(action, on)
                self._apply_audio()
                self.toast(("Night mode " if action == "night" else "Dialogue boost ") + ("on" if on else "off"))
            elif action == "voice_focus":
                mode = value if value in ("off", "dialogue", "isolate") else "off"
                if mode == "isolate" and not _rnnoise_model():
                    self.toast("AI voice isolation needs its speech model (vendor/rnnoise)")
                    mode = "dialogue"
                settings.save({"voice_focus": mode, "dialogue_boost": mode != "off"})
                self._set("voice_focus", mode)
                self._set("dialogue", mode != "off")
                self._apply_audio()
                self.toast({"off": "Voice focus off", "dialogue": "Dialogue boost on",
                            "isolate": "AI voice isolation on"}[mode])
            elif action == "sdr_to_hdr":
                mode = "auto" if value == "auto" else "off"
                settings.save({"sdr_to_hdr": mode, "rtx_hdr": mode == "auto"})
                self._set("sdr_to_hdr", mode)
                self._set("rtx_hdr", mode == "auto")
                self.apply_upscaler(announce=True)
            elif action == "bookmark":
                ms = int(float(value or 0))
                note, tag = (more + ["", ""])[:2]
                item = self.item or {}
                st_ = self.state()
                b = bookmarks.add(item.get("Id", ""), ms, note, tag, st_.get("title") or "", item.get("Name") or "")
                self._set("bookmarks", bookmarks.for_item(item.get("Id", "")))
                self.toast("Bookmarked " + _clock(ms / 1000) + (f" · {b['tag']}" if b["tag"] else ""))
            elif action == "bookmark-remove":
                item = self.item or {}
                bookmarks.remove(item.get("Id", ""), value)
                self._set("bookmarks", bookmarks.for_item(item.get("Id", "")))
            elif action == "clip-save":
                self._save_clips(json.loads(value or "{}"))
            elif action == "clip-open":
                folder = value or (self.clip or {}).get("folder")
                if folder and os.path.isdir(folder):
                    os.startfile(folder)
            elif action == "passthrough":
                on = value == "on"
                settings.save({"passthrough": on})
                self._set("passthrough", on)
                if on:
                    self._apply_audio()  # filters off first (bitstream can't be filtered), then switch
                self.mpv.audio_spdif = SPDIF if on else ""
                self.mpv.audio_exclusive = "yes" if on else "no"
                self._reinit_audio()
                time.sleep(0.4)
                self._apply_audio()
                self.toast("Pass-through to receiver on" if on else "Pass-through off")
            elif action == "audio_device":
                settings.save({"audio_device": value or "auto"})
                self._set("audio_device", value or "auto")
                if not self._dev_mute:
                    self.mpv.audio_device = value or "auto"
                desc = next((d.get("description") for d in (self.mpv.audio_device_list or [])
                             if d.get("name") == value), value)
                self.toast(f"Audio output: {desc}")
            elif action == "hwdec":
                on = value == "on"
                settings.save({"hwdec": on})
                self._set("hwdec", on)
                self.mpv.hwdec = HWDEC_ON if on else "no"
                self._announce = True
                self.toast("Hardware decoding on" if on else "Hardware decoding off (software)")
            elif action == "seek_step":
                step = 10 if str(value) == "10" else 5
                settings.save({"seek_step": step})
                self._set("seek_step", step)
            elif action == "pip":
                if self.mini:
                    self.restore_full()
                self.toggle_pip()
            elif action == "restore":
                self.restore_full()
            elif action == "mini":
                self.to_mini()
            elif action == "cast-scan":   # the Cast menu opened: look for TVs (marquee.cast)
                from .cast import blueprint as cast
                cast.osc_scan(self)
            elif action == "cast":        # a TV was picked: hand the video over at this position
                from .cast import blueprint as cast
                cast.osc_cast(self, value)
            elif action == "pip-drag":
                self._pip_track("move")
            elif action == "pip-resize":
                self._pip_track(value or "br")
        except Exception as e:
            print("player action failed:", action, e)

    def settings_changed(self):
        """Settings saved in the app: apply what affects a running video."""
        if self.mpv and self.playing:
            st = settings.load()
            self._publish_settings(st)
            self.mpv.audio_spdif = SPDIF if st["passthrough"] else ""
            self.mpv.hwdec = HWDEC_ON if st["hwdec"] else "no"
            self._apply_audio()
            self._set("upscaler", st["upscaler"])
            self._set("upscaler_label", UPSCALER_LABEL.get(st["upscaler"], "Off"))
            self._set("rtx_hdr", st["rtx_hdr"])
            self.apply_upscaler()

    def _audio_soon(self):
        threading.Thread(target=self._apply_audio, daemon=True).start()

    def passthrough_active(self, st=None):
        """Is the current audio track going to the receiver untouched (so no filters can apply)?"""
        st = st or settings.load()
        if not (st["passthrough"] and self.mpv):
            return False
        # from the track list, not current-tracks: that's briefly empty while the audio restarts
        codec = next((t.get("codec") for t in (self.mpv.track_list or [])
                      if t.get("type") == "audio" and t.get("selected")), None)
        if codec is None:
            aid = self.mpv.aid
            codec = next((t.get("codec") for t in (self.mpv.track_list or [])
                          if t.get("type") == "audio" and t.get("id") == aid), None)
        return codec in SPDIF_CODECS

    def _dialogue_filter(self):
        """Speech up: a centre-channel lift for surround, ffmpeg's dialoguenhance for stereo."""
        ap = self.mpv.audio_params or {}
        layout, n = ap.get("channels") or "", ap.get("channel-count") or 0
        names = LAYOUTS.get(layout)
        if names and "FC" in names:
            gains = ",".join(f"{c}={'1.7' if c == 'FC' else '0.8'}*{c}" for c in names.split())
            return f"pan={layout}|" + gains.replace(",", "|")
        if n == 2:
            return "dialoguenhance=voice=4:enhance=1.5,pan=stereo|FL=FL+0.7*FC|FR=FR+0.7*FC"
        return "equalizer=f=2200:t=q:w=1.1:g=5"  # mono or odd layouts: lift the speech band

    def _isolate_filter(self):
        """AI voice isolation: RNNoise (a small neural speech model) cleans a copy down to the voices, which is
        mixed over the original turned down, so speech stands out from music and effects."""
        model = _rnnoise_model().replace("\\", "/").replace(":", "\\:")
        return (f"asplit=2[orig][v];[v]arnndn=m='{model}':mix=1[voice];[orig]volume=0.45[bed];"
                f"[voice][bed]amix=inputs=2:normalize=0:weights='1.25 1'")

    def _apply_audio(self, *_):
        """Dialogue boost -> Night mode -> Stable volume (-> a limiter so the boosts can't clip). Nothing while
        the track is passed through to a receiver: bitstreamed audio can't be filtered."""
        if not self.mpv:
            return
        try:
            st = settings.load()
            active = self.passthrough_active(st)
            self._set("passthrough_active", active)
            chain = []
            if not active:
                focus = _voice_focus(st)
                if focus == "isolate" and _rnnoise_model():
                    chain.append(self._isolate_filter())
                if focus in ("dialogue", "isolate"):
                    chain.append(self._dialogue_filter())
                if st["night_mode"]:
                    chain.append(NIGHT)
                if st["stable_volume"]:
                    chain.append(STABLE)
                if focus != "off" or st["night_mode"]:
                    chain.append(LIMITER)
            af = f"lavfi=[{','.join(chain)}]" if chain else ""
            if af != self._af:
                self._af = af
                self.mpv.af = af
        except Exception as e:
            print("audio filters failed:", e)
            try:
                self.mpv.af = ""
            except Exception:
                pass

    def _reinit_audio(self):
        """Pass-through on/off only takes effect when the audio decoder starts again: re-pick the track."""
        aid = self.mpv.aid
        if aid not in (None, False, "no"):
            self.mpv.aid = "no"
            self.mpv.aid = aid
            self._af = None  # the chain is rebuilt with the new decoder

    # ------------------------------------------------------------------ picture-in-picture
    def toggle_pip(self):
        if not self.playing and not self.pip:
            return
        if self.pip:
            self._ui(self._pip_exit)
        else:
            if self.app.fullscreen:
                self.app.toggle_fullscreen()
                self._set("fullscreen", False)
                time.sleep(0.25)
            self._ui(self._pip_enter)
        self._set("pip", self.pip)
        self._announce = False
        self.focus()

    def _aspect(self):
        vp = (self.mpv.video_params or {}) if self.mpv else {}
        w, h = vp.get("dw") or vp.get("w"), vp.get("dh") or vp.get("h")
        return (w / h) if w and h else 16 / 9

    def _pip_enter(self):
        from System.Drawing import Rectangle, Size
        from System.Windows.Forms import FormBorderStyle, FormWindowState, Screen

        f = self.form
        self._pip_saved = (f.WindowState, f.MinimumSize, f.FormBorderStyle,
                           f.RestoreBounds if f.WindowState != FormWindowState.Normal else f.Bounds)
        if f.WindowState != FormWindowState.Normal:
            f.WindowState = FormWindowState.Normal
        f.MinimumSize = Size(160, 90)
        f.FormBorderStyle = getattr(FormBorderStyle, "None")
        f.TopMost = True
        scale = f.DeviceDpi / 96.0
        area = Screen.FromControl(f).WorkingArea
        saved = settings.load().get("pip_bounds")
        x = y = w = h = None
        if saved and len(saved) == 4:  # where the mini player was last time, if that's still on a screen
            from System.Drawing import Point
            sx, sy, sw, sh = (int(v) for v in saved)
            if sw >= 160 and sh >= 90 and Screen.FromPoint(Point(sx + sw // 2, sy + sh // 2)).WorkingArea.Contains(
                    Point(sx + sw // 2, sy + sh // 2)):
                x, y, w, h = sx, sy, sw, sh
        if w is None:
            w = int(480 * scale)
            h = int(round(w / self._aspect()))
            margin = int(24 * scale)
            x, y = area.Right - w - margin, area.Bottom - h - margin
        f.Bounds = Rectangle(x, y, w, h)
        self._corners(2)  # DWMWCP_ROUND
        self.pip = True

    def _pip_exit(self):
        if not self.pip:
            return
        f = self.form
        state, min_size, border, bounds = self._pip_saved
        f.TopMost = False
        f.FormBorderStyle = border
        f.MinimumSize = min_size
        f.Bounds = bounds
        f.WindowState = state
        self._corners(0)  # DWMWCP_DEFAULT
        self.pip = False
        self._set("pip", False)

    def _corners(self, pref):
        try:
            v = ctypes.c_int(pref)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(self.hwnd), 33, ctypes.byref(v), 4)
        except Exception:
            pass

    def _pip_track(self, mode):
        """Move ("move") or resize the mini player while the left button is held: corners "tl"/"tr"/"bl"/"br" and
        edges "l"/"r"/"t"/"b", keeping the video's shape (hold Shift to resize freely), from 240 px wide up to
        the screen. Follows the real cursor, so it doesn't matter that the window moves under it. The size and
        place are remembered for next time."""
        if not self.pip:
            return
        u32 = ctypes.windll.user32

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]

        hwnd = ctypes.c_void_p(self.hwnd)
        start, rect = POINT(), RECT()
        u32.GetCursorPos(ctypes.byref(start))
        u32.GetWindowRect(hwnd, ctypes.byref(rect))
        aspect = self._aspect()
        x0, y0, w0, h0 = rect.l, rect.t, rect.r - rect.l, rect.b - rect.t
        scale = self.form.DeviceDpi / 96.0
        # the screen the window is on bounds the size
        mon = u32.MonitorFromWindow(hwnd, 2)

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        u32.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi))
        max_w, max_h = mi.rcWork.r - mi.rcWork.l, mi.rcWork.b - mi.rcWork.t
        min_w, min_h = int(240 * scale), int(135 * scale)
        flags = 0x0004 | 0x0010  # SWP_NOZORDER | SWP_NOACTIVATE
        cur = POINT()
        last = None
        while u32.GetAsyncKeyState(0x01) & 0x8000 and self.pip:
            u32.GetCursorPos(ctypes.byref(cur))
            dx, dy = cur.x - start.x, cur.y - start.y
            if mode == "move":
                pos = (x0 + dx, y0 + dy, w0, h0)
                if pos != last:
                    u32.SetWindowPos(hwnd, None, pos[0], pos[1], 0, 0, flags | 0x0001)  # SWP_NOSIZE
            else:
                free = bool(u32.GetAsyncKeyState(0x10) & 0x8000)  # Shift: any shape
                w = w0 + (dx if "r" in mode else -dx if "l" in mode else 0)
                h = h0 + (dy if "b" in mode else -dy if "t" in mode else 0)
                if not free:
                    if mode in ("t", "b"):
                        w = h * aspect
                    elif mode in ("l", "r"):
                        h = w / aspect
                    elif abs(dx) * h0 >= abs(dy) * w0:  # corners: whichever way the mouse went further
                        h = w / aspect
                    else:
                        w = h * aspect
                w = int(max(min_w, min(max_w, w)))
                h = int(max(min_h, min(max_h, h)))
                if not free:  # clamping may have broken the shape: fix the other side
                    if w / aspect > max_h:
                        w = int(max_h * aspect)
                    h = int(round(w / aspect))
                x = x0 + w0 - w if "l" in mode else (x0 + (w0 - w) // 2 if mode in ("t", "b") else x0)
                y = y0 + h0 - h if "t" in mode else (y0 + (h0 - h) // 2 if mode in ("l", "r") else y0)
                pos = (x, y, w, h)
                if pos != last:
                    u32.SetWindowPos(hwnd, None, x, y, w, h, flags)
            last = pos
            time.sleep(0.008)
        u32.GetWindowRect(hwnd, ctypes.byref(rect))
        settings.save({"pip_bounds": [rect.l, rect.t, rect.r - rect.l, rect.b - rect.t]})

    def _watchdog(self):
        """If upscaling makes frames late (dropped frames climbing), step it down for this video:
        Anime4K -> NVIDIA -> high quality -> off."""
        last = None
        while True:
            time.sleep(5)
            try:
                if not (self.playing and self.mpv) or self.mpv.pause or self.mpv.seeking:
                    last = None
                    continue
                drops = (self.mpv.frame_drop_count or 0) + (self.mpv.decoder_frame_drop_count or 0)
                if last is not None and drops - last >= 6:
                    mode = getattr(self, "_upscale_mode", None)
                    order = ["anime4k", "nvidia", "hq", "off"]
                    if mode in order[:-1]:
                        self._upscale_cap = order[order.index(mode) + 1]
                        self.toast("Upscaling eased off to keep playback smooth")
                        self.apply_upscaler()
                last = drops
            except Exception:
                last = None

    def _upscaler_soon(self, delay):
        """Window resizes come in bursts: re-size the upscaler once they settle."""
        if self._vf_timer:
            self._vf_timer.cancel()
        self._vf_timer = threading.Timer(delay, self._upscaler_tick)
        self._vf_timer.daemon = True
        self._vf_timer.start()

    def _upscaler_tick(self):
        try:
            if self.playing:
                self.apply_upscaler(announce=self._announce)
        except Exception as e:
            print("upscaler failed:", e)

    def apply_upscaler(self, announce=False):
        """NVIDIA RTX Video Super Resolution sized to the window; or mpv's high-quality scalers; or off."""
        if not self.mpv:
            return
        st = settings.load()
        mode = st["upscaler"]
        vp = self.mpv.video_params or {}
        chosen_auto = mode == "auto"
        osd = self.mpv.osd_dimensions or {}
        vw, vh = vp.get("w") or 0, vp.get("h") or 0
        ow, oh = osd.get("w") or 0, osd.get("h") or 0
        if not (vw and vh and ow and oh):
            return  # not playing video yet; video-params will call again
        hw = (self.mpv.hwdec_current or "no") not in ("no", "")
        source_hdr = (vp.get("gamma") or "") in ("pq", "hlg")
        scale = min(ow / vw, oh / vh)
        if chosen_auto:  # anime -> Anime4K, live action -> NVIDIA RTX VSR, already big enough -> nothing
            anime = self._is_anime(self.item or {}) or "jpn" in str(self.mpv.alang or "")
            if scale <= 1.05:
                mode = "none"
            elif anime and _anime4k_files():
                mode = "anime4k"
            else:
                mode = "nvidia" if hw else "hq"
        order = ["anime4k", "nvidia", "hq", "off"]
        if self._upscale_cap and mode in order and order.index(mode) < order.index(self._upscale_cap):
            mode = self._upscale_cap  # it couldn't keep up earlier in this video
        self._upscale_mode = mode
        # SDR -> HDR: RTX Video HDR on NVIDIA, else libplacebo's inverse tone mapping; only on an HDR display
        want_hdr = (st.get("sdr_to_hdr", "auto") == "auto" or st["rtx_hdr"]) and not source_hdr and _display_hdr()
        hdr = ":nvidia-true-hdr" if want_hdr and hw else ""
        try:
            self.mpv["inverse-tone-mapping"] = "yes" if (want_hdr and not hw) else "no"
            self.mpv["target-colorspace-hint"] = "yes" if want_hdr or source_hdr else "auto"
        except Exception:
            pass
        filters, msg = [], None
        shaders = [os.path.join(VENDOR, "anime4k", f) for f in _anime4k_files()] if mode == "anime4k" else []
        if shaders != self._shaders:
            self._shaders = shaders
            self.mpv.glsl_shaders = shaders + [GLASS]  # the glass hook stays last
        if mode == "anime4k":
            if hdr:
                filters.append("d3d11vpp=nvidia-true-hdr")
            msg = f"Anime4K upscaling · {vh}p → {round(vh * scale)}p"
        elif mode == "none":
            if hdr:
                filters.append("d3d11vpp=nvidia-true-hdr")
            msg = f"{vh}p already fills the screen · no upscaling needed" + (" · RTX HDR on" if hdr else "")
        elif mode == "nvidia":
            if not hw:
                msg = "NVIDIA upscaling needs hardware decoding, which this video isn't using"
            elif scale > 1.05:
                filters.append(f"d3d11vpp=scaling-mode=nvidia:scale={scale:.2f}{hdr}")
                msg = f"NVIDIA RTX Super Resolution · {vh}p → {round(vh * scale)}p"
            else:
                if hdr:
                    filters.append("d3d11vpp=nvidia-true-hdr")
                msg = f"{vh}p already fills the screen · no upscaling needed" + (" · RTX HDR on" if hdr else "")
        elif hdr and hw:
            filters.append("d3d11vpp=nvidia-true-hdr")
            msg = "RTX HDR on"
        vf = ",".join(filters)
        if self._vf is not None and bool(vf) != bool(self._vf):
            announce = True  # upscaling switched on or off (e.g. going fullscreen): say so
        if vf != self._vf:
            try:
                self.mpv.vf = vf
                self._vf = vf
            except Exception:
                self.mpv.vf = ""
                self._vf = ""
                msg = "NVIDIA upscaling isn't available for this video"
        if chosen_auto and msg:
            msg = "Auto · " + msg
        if want_hdr and not hw:
            msg = (msg + " · " if msg else "") + "HDR from SDR (inverse tone mapping)"
        if mode == "hq":
            self.mpv["scale"] = "ewa_lanczossharp"
            self.mpv["cscale"] = "ewa_lanczossharp"
            msg = "High-quality upscaling (mpv)" if scale > 1.05 else f"{vh}p already fills the screen · no upscaling needed"
        else:
            self.mpv["scale"] = "lanczos"
            self.mpv["cscale"] = "lanczos"
        if mode == "off":
            msg = "Upscaling off"
        if st["rtx_hdr"] and source_hdr and msg and "HDR" not in msg:
            msg += " · already HDR, RTX HDR not needed"
        if self.pip:
            announce = False  # the mini player stays quiet
        if announce and msg:
            self._announce = False
            self.toast(msg)

    # ------------------------------------------------------------------ credits: mini video + "Up next"
    def credits_start(self):
        """Where the credits begin: Jellyfin/catalog/chapter markers, then the media index's outro (or a film's
        subtitles), then a closing stretch of black screen with text found in the file itself. Episodes with none
        of those get no credits screen: the title plays to its end and the normal autoplay takes over. (No guessing:
        a guess lands in the middle of a quiet finale.)"""
        if self._credits:
            return self._credits
        if self._index_outro:
            return self._index_outro
        if getattr(self, "_black_credits", None):
            return self._black_credits
        return None

    def _credits_tick(self, pos):
        last, self._last_tick_pos = self._last_tick_pos, pos
        item = self.item or {}
        dur = self.mpv.duration or 0
        if self.mpv.pause:
            self._paused_since = self._paused_since or time.time()
        else:
            self._paused_since = None
            if last is not None and 0 < pos - last < 3:
                self._watched += pos - last
        self._prepare_ending(item, pos, dur)
        if not dur or self.mini or self.pip or self._credits_done == item.get("Id"):
            return
        # ask the media index for the outro once we're past halfway (no work at the start of a film)
        if self._index_outro is None and not self._credits and pos > dur * 0.5 and item.get("Type") == "Episode":
            self._index_outro = 0
            self._black_credits = None
            threading.Thread(target=self._fetch_index_outro, args=(item.get("Id"),), daemon=True).start()
            threading.Thread(target=self._find_black_credits, args=(item.get("Id"), dur, 240), daemon=True).start()
        # films: where the talking stops for good (the subtitles), instead of guessing the last few minutes
        if self._index_outro is None and not self._credits and pos > dur * 0.5 and item.get("Type") != "Episode":
            self._index_outro = 0
            threading.Thread(target=self._find_film_credits, args=(item.get("Id"), dur), daemon=True).start()
        start = self.credits_start()
        if os.environ.get("MARQUEE_DEV_DEBUG") and start and pos > start - 15:
            print(f"credits tick pos={pos:.1f} last={last} start={start:.1f}", flush=True)
        if not start or pos < start or last is None:
            return
        if last < start - 0.5 and pos - last > 3:
            self._credits_done = item.get("Id")  # seeked into the credits on purpose: let them play big
            return
        if last <= start <= pos + 0.5 and settings.load().get("credits_mini", True):
            self._credits_done = item.get("Id")
            self.to_mini(reason="credits")

    def _prepare_ending(self, item, pos, dur):
        """~6 minutes before the credits, ask Atlas to get its ending comment ready (once per play, only when
        someone is really watching: 10 % of it played this session and not paused for long)."""
        if getattr(self, "_ending_prepared", True) or not dur or item.get("_local"):
            return
        start = self.credits_start() or dur
        if not (start - 360 <= pos < start):
            return
        if self._watched < dur * 0.10 or (self._paused_since and time.time() - self._paused_since > 600):
            return
        self._ending_prepared = True
        item_id, port = item.get("Id"), getattr(self.app, "port", None)

        def go():
            try:
                asst = getattr(self.app, "assistant", None)
                if asst is not None and hasattr(asst, "prepare_ending"):
                    asst.prepare_ending(item_id, start)
                elif port:
                    import requests
                    requests.post(f"http://127.0.0.1:{port}/api/assistant/ending/{item_id}/prepare",
                                  json={"credits_at": start}, timeout=10)
            except Exception as e:
                print("ending prepare failed:", e)
        threading.Thread(target=go, daemon=True).start()

    def _fetch_index_outro(self, item_id):
        try:
            import requests
            port = getattr(self.app, "port", None)
            if not port:
                return
            r = requests.get(f"http://127.0.0.1:{port}/api/index/{item_id}/intro", timeout=10).json()
            outro = (r or {}).get("outro") or {}
            if outro.get("start") and self.item and self.item.get("Id") == item_id:
                self._index_outro = float(outro["start"])
        except Exception:
            pass

    def _find_film_credits(self, item_id, dur):
        """A film's credits from its subtitles: the end credits are a long stretch with no spoken line (only music
        notes like "[UPBEAT MUSIC PLAYING]", or nothing at all), even when a post-credits scene talks again later.
        Reads only the last fifth of the subtitle track (a fast seek, not the whole file)."""
        try:
            path = self.mpv.path if self.mpv else None
            if not path or not os.path.exists(str(path)):
                return
            from . import details
            ff = details._ffmpeg()
            offset = dur * 0.8
            side = details._sidecar_subs(path)
            if side:
                raw = side.read_bytes()
                cues = [(a - offset, b - offset, t) for a, b, t in details._parse_cues(raw.decode("utf-8-sig", "replace"))
                        if b > offset]
            else:
                idx = details._embedded_sub_stream(ff, path) if ff else None
                if idx is None:
                    return
                p = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-ss", f"{offset:.1f}", "-i", path,
                                    "-map", f"0:{idx}", "-f", "srt", "pipe:1"], capture_output=True, timeout=120)
                cues = details._parse_cues(p.stdout.decode("utf-8", "replace")) if p.returncode == 0 else []
            cues_abs = [(a + offset, b + offset, t) for a, b, t in cues]
            hint = credits_from_cues(cues_abs, dur)
        except Exception as e:
            print("credits from subtitles failed:", e)
            hint = None
        # The picture decides: the closing credits crawl is a long black stretch that runs to the very end. The
        # subtitles' "the talking stops here" only pulls the start earlier when titles roll over images first.
        # Nothing found: no credits screen (the film just ends), never a guess in the middle of the finale.
        try:
            block = self._film_black_block(dur)
        except Exception as e:
            print("credits from black frames failed:", e)
            block = None
        start = None
        if block:
            # the crawl often follows a minute or two of titles over the last pictures: start just after the last
            # spoken line before it (a dark final scene can look "black", but people still talk in it)
            spoken = [b for a, b, t in (locals().get("cues_abs") or []) if b <= block and _SOUND_ONLY.sub("", t or "")]
            last_line = max(spoken) if spoken else None
            start = last_line + 15 if last_line and block - last_line <= 300 else block
        elif hint and hint >= dur - 12 * 60:
            start = hint
        if start and self.item and self.item.get("Id") == item_id:
            self._index_outro = start
            try:  # a scene after (or during) the credits: the talking comes back a while into them
                after = after_credits(locals().get("cues_abs") or [], start, dur)
            except Exception:
                after = []
            self._after_credits = after
            self._set("after_credits", after or None)
            self._set("credits_at", start)
            if after:
                print("credits: scene after the credits at " + ", ".join(f"{t / 60:.1f} min" for t in after), flush=True)
            print(f"credits: film credits at {start / 60:.1f} min (black {block and round(block / 60, 1)}, "
                  f"subtitles {hint and round(hint / 60, 1)})", flush=True)
        elif self.item and self.item.get("Id") == item_id:
            self._index_outro = None
            self._no_credits = True
            print("credits: none found; the film plays to its end", flush=True)

    def _film_black_block(self, dur, scan=15 * 60):
        """Start of the last long mostly-black stretch (30 s+ runs, joined across breaks of up to 4 minutes) that
        reaches the end of the film, from its keyframes only (fast: no full decode). None if there isn't one."""
        path = self.mpv.path if self.mpv else None
        if not path or not os.path.exists(str(path)):
            return None
        from . import details
        ff = details._ffmpeg()
        if not ff:
            return None
        start = max(0.0, dur - scan)
        p = subprocess.run([ff, "-hide_banner", "-nostats", "-skip_frame", "nokey", "-ss", f"{start:.1f}", "-i", path,
                            "-an", "-sn", "-dn", "-vf", "scale=320:-2,showinfo,blackframe=amount=80:threshold=32",
                            "-vsync", "0", "-f", "null", "-"], capture_output=True, timeout=900)
        err = p.stderr.decode("utf-8", "replace")
        keys = sorted({round(start + float(x), 1) for x in re.findall(r"showinfo.*?pts_time:([\d.]+)", err)})
        black = {round(start + float(x), 1) for x in re.findall(r"blackframe.*? t:([\d.]+)", err)}
        return credits_block(keys, black, dur)

    def _find_black_credits(self, item_id, dur, scan):
        """Credits rolling as text on a black screen, from the file's last `scan` seconds (one small frame a second,
        low priority): the first stretch of 6+ seconds that is mostly black and stays mostly black to the end."""
        try:
            path = self.mpv.path if self.mpv else None
            if not path or not os.path.exists(str(path)):
                return
            from . import details
            ff = details._ffmpeg()
            if not ff:
                return
            start = max(0.0, dur - scan)
            p = subprocess.run([ff, "-hide_banner", "-nostats", "-ss", f"{start:.1f}", "-i", path, "-an", "-sn", "-dn",
                                "-vf", "fps=1,scale=320:-2,blackframe=amount=80:threshold=40", "-f", "null", "-"],
                               capture_output=True, timeout=600)
            black = sorted({int(start + float(m.group(1))) for m in
                            re.finditer(r"blackframe.*? t:([\d.]+)", p.stderr.decode("utf-8", "replace"))})
            at = credits_from_black(black, start, dur)
            if at and self.item and self.item.get("Id") == item_id:
                self._black_credits = at
                print(f"credits: black-screen credits at {at / 60:.1f} min", flush=True)
        except Exception as e:
            print("credits from black frames failed:", e)

    def to_mini(self, reason="ui"):
        """Shrink the video into the bottom-right of the app window (about a quarter wide, rounded), so the page
        behind it can show what to watch next. The credits keep playing."""
        if not self.playing or self.mini or self.pip:
            return
        item = self.item or {}
        self.mini = True
        self._set("mini", True)
        self._ui(lambda: self._animate_panel(True))
        nxt = self.next_item or {}
        dur = self.mpv.duration or 0
        pos = self._position()
        payload = {"item_id": item.get("Id"), "next_episode_id": nxt.get("Id") or item.get("_next_id"),
                   "position": pos, "remaining": max(0.0, dur - pos), "reason": reason,
                   "autoplay": bool(settings.load()["autoplay_next"] and nxt), "type": item.get("Type"),
                   "title": self.state().get("title"), "after_credits": list(getattr(self, "_after_credits", []) or [])}
        if self.next_item:  # the next-episode countdown starts now (the Up-next page shows it)
            nx = self.next_item
            self._set("next", {"title": f"S{nx.get('ParentIndexNumber', 0)}:E{nx.get('IndexNumber', 0)} · {nx.get('Name', '')}",
                               "credits": pos})
        self.app.notify_ui("credits", payload)
        for fn in list(self.on_credits):
            threading.Thread(target=fn, args=(payload,), daemon=True).start()

    def skip_after_credits(self):
        """Jump to the scene after the credits (the next one still ahead), back at full size."""
        times = [t for t in (getattr(self, "_after_credits", None) or []) if t > self._position() + 2] or             list(getattr(self, "_after_credits", None) or [])
        if not times or not self.mpv:
            return False
        if self.mini:
            self.restore_full()
        self.mpv.command("seek", str(times[0]), "absolute", "exact")
        self.mpv.pause = False
        return True

    def restore_full(self):
        """Back to the full video (keep watching the credits)."""
        if not self.mini:
            return
        self.mini = False
        self._set("mini", False)
        self.mpv.command("script-message", "marquee-keep-credits")  # no autoplay: they're watching the credits
        self._ui(lambda: self._animate_panel(False))
        self.app.notify_ui("credits-closed", {"item_id": (self.item or {}).get("Id")})
        self.focus()

    def _animate_panel(self, to_mini):
        """GUI thread: the panel slides between filling the window and a rounded corner tile."""
        from System.Drawing import Rectangle
        from System.Drawing.Drawing2D import GraphicsPath
        from System.Windows.Forms import Application, DockStyle

        f, pnl = self.form, self.panel
        cw, ch = f.ClientSize.Width, f.ClientSize.Height
        scale = f.DeviceDpi / 96.0
        w = int(cw * 0.26)
        h = int(round(w / self._aspect()))
        margin = int(24 * scale)
        small = (cw - w - margin, ch - h - margin, w, h)
        big = (0, 0, cw, ch)
        a, b = (big, small) if to_mini else (small, big)
        web = self.app.webview_control()
        if to_mini:
            web.Visible = True
            pnl.Dock = getattr(DockStyle, "None")
        pnl.Region = None
        steps = 14
        for i in range(1, steps + 1):
            k = 1 - (1 - i / steps) ** 3
            r = [int(a[j] + (b[j] - a[j]) * k) for j in range(4)]
            pnl.Bounds = Rectangle(r[0], r[1], r[2], r[3])
            pnl.BringToFront()
            Application.DoEvents()
            time.sleep(0.012)
        if to_mini:
            rad = int(16 * scale)
            path = GraphicsPath()
            path.AddArc(0, 0, rad * 2, rad * 2, 180, 90)
            path.AddArc(w - rad * 2, 0, rad * 2, rad * 2, 270, 90)
            path.AddArc(w - rad * 2, h - rad * 2, rad * 2, rad * 2, 0, 90)
            path.AddArc(0, h - rad * 2, rad * 2, rad * 2, 90, 90)
            path.CloseFigure()
            from System.Drawing import Region
            pnl.Region = Region(path)
            self._round_video(w, h, rad)
            web.Focus()  # the Up-next page takes the keyboard (Enter plays, Esc/B restores via the UI)
        else:
            pnl.Dock = DockStyle.Fill
            web.Visible = False
            self._round_video(0, 0, 0)

    def _round_video(self, w, h, rad):
        """mpv draws with its own child window (a GPU swapchain), which the panel's region doesn't clip: round
        that window itself (w=0: square again)."""
        u32, gdi = ctypes.windll.user32, ctypes.windll.gdi32
        u32.FindWindowExW.restype = ctypes.c_void_p
        u32.FindWindowExW.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p)
        child = u32.FindWindowExW(self.panel.Handle.ToInt64(), None, "mpv", None)
        if not child:
            return
        gdi.CreateRoundRectRgn.restype = ctypes.c_void_p
        rgn = gdi.CreateRoundRectRgn(0, 0, w + 1, h + 1, rad * 2, rad * 2) if w else None
        u32.SetWindowRgn(ctypes.c_void_p(child), ctypes.c_void_p(rgn) if rgn else None, True)

    def _unmini_now(self):
        """GUI thread: put the panel back to full size without animation (playback ended)."""
        if self.panel is None:
            return
        from System.Windows.Forms import DockStyle
        self.panel.Region = None
        self.panel.Dock = DockStyle.Fill
        self._round_video(0, 0, 0)
        if self.mini:
            self.mini = False
            try:
                self._set("mini", False)
            except Exception:
                pass

    # ------------------------------------------------------------------ clips
    def _save_clips(self, req):
        """req: {"slices": [[in, out], ...], "join": bool, "gif": bool} from the clip bar."""
        item = self.item or {}
        src = (item.get("MediaSources") or [{}])[0].get("Path") or item.get("Path")
        slices = [(float(a), float(b)) for a, b in req.get("slices") or [] if float(b) - float(a) >= 0.2]
        if not slices:
            self.toast("Set an IN and an OUT point first")
            return
        if not _exists(src):
            self.toast("Clips need the file on this PC")
            return
        st_ = self.state()
        title = st_.get("title") or "Clip"
        if item.get("Type") == "Episode":
            title = f"{title} S{item.get('ParentIndexNumber', 0):02d}E{item.get('IndexNumber', 0):02d}"
        aid = next((t.get("ff-index") for t in self.mpv.track_list or [] if t.get("type") == "audio" and t.get("selected")), None)
        last = [0.0]

        def progress(f, label):
            if f - last[0] >= 0.1 or f >= 1 or f == 0:
                last[0] = f
                self.mpv.command("script-message", "marquee-clip-progress", label, f"{f:.2f}")
        try:
            folder, files = clips.export(src, slices, title, join=bool(req.get("join")), gif=bool(req.get("gif")),
                                         audio_index=aid, progress=progress)
            self.clip = {"folder": str(folder)}
            names = ", ".join(os.path.basename(f) for f in files[:3]) + (" …" if len(files) > 3 else "")
            self.mpv.command("script-message", "marquee-clip-done", str(folder), f"Saved {len(files)} file(s) · {names}")
        except Exception as e:
            print("clip export failed:", e)
            self.mpv.command("script-message", "marquee-clip-done", "", f"Couldn't save the clip: {e}"[:160])

    # ------------------------------------------------------------------ public API
    # For the other parts of the app (phone remote, assistant, media index). Safe to call from any thread.
    def pause(self, mode="toggle"):
        """mode: "toggle", "on" (pause) or "off" (play)."""
        if not (self.mpv and self.playing):
            return
        if mode == "toggle":
            self.mpv.command("cycle", "pause")
        else:
            self.mpv.pause = mode in ("on", True, "pause")

    def seek(self, seconds, relative=False):
        """Absolute (seconds from the start) or relative (+/- seconds) seek, frame-accurate."""
        if self.mpv and self.playing:
            self.mpv.command("seek", f"{float(seconds):.3f}", "relative+exact" if relative else "absolute+exact")

    def set_volume(self, v):
        if self.mpv:
            self.mpv.volume = max(0, min(100, float(v)))
            if float(v) > 0 and not self._dev_mute:
                self.mpv.mute = False
            self.mpv.command("script-message", "marquee-volume")

    def toast(self, msg):
        if self.mpv:
            self.mpv.command("script-message", "marquee-toast", str(msg))

    def show_card(self, title, text, seconds=15):
        """A glass card on the video (assistant answers and the like); click or Esc closes it."""
        if self.mpv:
            self.mpv.command("script-message", "marquee-card", str(title or ""), str(text or ""), str(seconds))

    def state(self):
        """What's playing, where, and the subtitle on screen right now."""
        m, item = self.mpv, self.item
        if not (m and item and self.playing):
            return {"playing": False}
        title = item.get("SeriesName") or item.get("Name") if item.get("Type") == "Episode" else item.get("Name")
        return {
            "playing": True, "id": item.get("Id"), "title": title, "name": item.get("Name"),
            "type": item.get("Type"), "season": item.get("ParentIndexNumber"), "episode": item.get("IndexNumber"),
            "time": self._position(), "duration": m.duration or 0, "paused": bool(m.pause),
            "volume": float(m.volume or 0), "muted": bool(m.mute), "speed": float(m.speed or 1),
            "subtitle": (m.sub_text or "").strip(), "local_file": bool(item.get("_local")), "pip": self.pip,
        }

    def subtitle_window(self, seconds_back=60):
        """Subtitle lines shown in the last `seconds_back` seconds of the video (as seen during playback),
        oldest first: [{"time": s, "text": "..."}]."""
        now = self._position()
        return [{"time": round(t, 2), "text": txt} for t, txt in list(self._subs)
                if now - seconds_back <= t <= now + 0.5]

    def _sub_text(self, _name, value):
        text = (value or "").strip()
        if text and (not self._subs or self._subs[-1][1] != text):
            pos = self._pos if self.mpv is None else (self.mpv.time_pos or self._pos)
            # after a seek backwards, lines later than now are stale
            while self._subs and self._subs[-1][0] > pos + 1:
                self._subs.pop()
            self._subs.append((pos, text))

    def _emit(self, throttle=0.0):
        if not self.on_state or (throttle and time.time() - self._emit_at < throttle):
            return
        self._emit_at = time.time()

        def run():
            st = self.state()
            for fn in list(self.on_state):
                try:
                    fn(st)
                except Exception as e:
                    print("state listener failed:", e)
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------ progress
    def _eof(self, _name, value):
        if value and self.playing and self.item:
            if self.next_item and settings.load()["autoplay_next"]:
                self._message("next", "")
            else:
                self._message("close", "")

    def _reporter(self):
        while True:
            time.sleep(1)
            try:
                if not (self.playing and self.item and self.mpv):
                    continue
                pos = self.mpv.time_pos
                if pos is None:
                    continue
                self._pos = pos
                self._credits_tick(pos)
                skip = self._auto_skip
                if skip and skip["s"] <= pos < skip["e"] - 1:
                    self._auto_skip = None
                    self.mpv.seek(skip["e"], "absolute")
                    self.toast("Skipped the intro")
                if time.time() - self._last_report >= 10:
                    self._last_report = time.time()
                    self._report("Progress", pos)
            except Exception as e:
                print("progress report failed:", e)


def _clock(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def _voice_focus(st):
    v = st.get("voice_focus")
    if v in ("off", "dialogue", "isolate"):
        return v
    return "dialogue" if st.get("dialogue_boost") else "off"


def _rnnoise_model():
    """An RNNoise model (.rnnn, e.g. GregorR/rnnoise-models, BSD) in vendor/rnnoise or the settings folder."""
    for d in (os.path.join(VENDOR, "rnnoise"), str(settings.DIR / "models" / "rnnoise")):
        try:
            for f in sorted(os.listdir(d)):
                if f.lower().endswith(".rnnn"):
                    return os.path.join(d, f)
        except OSError:
            pass
    return None


def _anime4k_files():
    d = os.path.join(VENDOR, "anime4k")
    return [f for f in ANIME4K if os.path.exists(os.path.join(d, f))] if os.path.isdir(d) and all(
        os.path.exists(os.path.join(d, f)) for f in ANIME4K) else []


_hdr_cache = [0, False]


def _display_hdr():
    """Is Windows HDR ("advanced color") on for a connected display? (DisplayConfig API; cached 10 s)"""
    if time.time() - _hdr_cache[0] < 10:
        return _hdr_cache[1]
    result = False
    try:
        from ctypes import wintypes as wt

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wt.DWORD), ("HighPart", ctypes.c_long)]

        class PATH_SOURCE(ctypes.Structure):
            _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                        ("statusFlags", ctypes.c_uint32)]

        class RATIONAL(ctypes.Structure):
            _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]

        class PATH_TARGET(ctypes.Structure):
            _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                        ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32),
                        ("scaling", ctypes.c_uint32), ("refreshRate", RATIONAL), ("scanLineOrdering", ctypes.c_uint32),
                        ("targetAvailable", ctypes.c_int32), ("statusFlags", ctypes.c_uint32)]

        class PATH_INFO(ctypes.Structure):
            _fields_ = [("sourceInfo", PATH_SOURCE), ("targetInfo", PATH_TARGET), ("flags", ctypes.c_uint32)]

        class MODE_INFO(ctypes.Structure):
            _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32), ("adapterId", LUID),
                        ("data", ctypes.c_byte * 48)]

        class HEADER(ctypes.Structure):
            _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapterId", LUID),
                        ("id", ctypes.c_uint32)]

        class COLOR_INFO(ctypes.Structure):
            _fields_ = [("header", HEADER), ("value", ctypes.c_uint32), ("colorEncoding", ctypes.c_uint32),
                        ("bitsPerColorChannel", ctypes.c_uint32)]

        u32 = ctypes.windll.user32
        np_, nm = ctypes.c_uint32(), ctypes.c_uint32()
        if u32.GetDisplayConfigBufferSizes(2, ctypes.byref(np_), ctypes.byref(nm)) == 0:
            paths, modes = (PATH_INFO * np_.value)(), (MODE_INFO * nm.value)()
            if u32.QueryDisplayConfig(2, ctypes.byref(np_), paths, ctypes.byref(nm), modes, None) == 0:
                for i in range(np_.value):
                    info = COLOR_INFO()
                    info.header.type = 9  # DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO
                    info.header.size = ctypes.sizeof(COLOR_INFO)
                    info.header.adapterId = paths[i].targetInfo.adapterId
                    info.header.id = paths[i].targetInfo.id
                    if u32.DisplayConfigGetDeviceInfo(ctypes.byref(info)) == 0 and (info.value >> 1) & 1:
                        result = True
    except Exception:
        result = False
    _hdr_cache[:] = [time.time(), result]
    return result


def _item_from_catalog(info):
    """The catalog's lookup() as the Jellyfin-shaped item the rest of the player works with."""
    if not info:
        return None
    genres = list(info.get("genres") or [])
    if info.get("anime") and "Anime" not in genres:
        genres.append("Anime")
    path = info.get("path")
    return {
        "Id": info["id"], "Type": info.get("type") or "Movie", "Name": info.get("name"),
        "SeriesName": info.get("series"), "SeriesId": info.get("series_id"), "ParentIndexNumber": info.get("season"),
        "IndexNumber": info.get("episode"), "ProductionYear": info.get("year"), "Path": path,
        "RunTimeTicks": int((info.get("runtime") or 0) * TICKS), "Genres": genres,
        "UserData": {"PlaybackPositionTicks": int((info.get("position") or 0) * TICKS),
                     "Played": bool(info.get("played"))},
        "MediaSources": [{"Id": info["id"], "Path": path, "MediaStreams": []}],
        "_catalog": True, "_catalog_id": info["id"], "_segments": info.get("segments"),
        "_next_id": info.get("next_episode_id"), "_local": info.get("source") == "local",
    }


def _exists(path):
    try:
        return bool(path) and os.path.exists(path)
    except OSError:
        return False


def _raise_priority():
    """Video first: the process above normal, its disk reads at high priority (falls back quietly)."""
    try:
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00008000)  # ABOVE_NORMAL_PRIORITY_CLASS
        for level in (3, 2):  # IoPriorityHigh needs a privilege; Normal at least undoes any "low"
            v = ctypes.c_ulong(level)
            if ctypes.windll.ntdll.NtSetInformationProcess(k32.GetCurrentProcess(), 33, ctypes.byref(v), 4) == 0:
                break
    except Exception:
        pass


PENDING = settings.DIR / "pending_reports.json"
_pending_lock = threading.Lock()


def _queue_pending(what, body):
    with _pending_lock:
        try:
            data = json.loads(PENDING.read_text(encoding="utf-8"))
        except Exception:
            data = []
        if what == "Progress":  # only the latest progress of a session matters
            data = [d for d in data if not (d["what"] == "Progress" and
                                            d["body"].get("PlaySessionId") == body.get("PlaySessionId"))]
        data.append({"what": what, "body": body, "at": int(time.time())})
        PENDING.parent.mkdir(parents=True, exist_ok=True)
        PENDING.write_text(json.dumps(data[-500:], indent=1), encoding="utf-8")


def _retry_pending(jf):
    with _pending_lock:
        try:
            data = json.loads(PENDING.read_text(encoding="utf-8"))
        except Exception:
            return
        if not data:
            return
        left = []
        for i, d in enumerate(data):
            if time.time() - d.get("at", 0) > 14 * 86400:
                continue
            try:
                jf.report(d["what"], d["body"], timeout=3)
            except Exception:
                left = data[i:]
                break
        PENDING.write_text(json.dumps(left, indent=1), encoding="utf-8")


class LocalMap:
    """What Marquee knows about items without asking Jellyfin: id -> path, titles, numbering, streams and the
    resume point, learned from every Jellyfin response the app gets (browsing fills it) and from our own
    playback. Kept in localmap.json so a slow or dead Jellyfin never stands between a click and the picture."""
    FILE = settings.DIR / "localmap.json"
    KEEP = ("Id", "Name", "Type", "SeriesName", "SeriesId", "SeasonId", "ParentIndexNumber", "IndexNumber",
            "ProductionYear", "OfficialRating", "Genres", "Path", "RunTimeTicks", "UserData")

    def __init__(self):
        self.lock = threading.Lock()
        try:
            self.items = json.loads(self.FILE.read_text(encoding="utf-8"))
        except Exception:
            self.items = {}
        self._timer = None

    def learn(self, items):
        changed = False
        with self.lock:
            for i in items or []:
                if not isinstance(i, dict) or not i.get("Id") or i.get("Type") not in ("Movie", "Episode", "Video"):
                    continue
                src = (i.get("MediaSources") or [{}])[0]
                path = src.get("Path") or i.get("Path")
                if not path:
                    continue
                rec = dict(self.items.get(i["Id"]) or {})
                rec.update({k: i[k] for k in self.KEEP if k in i and i[k] is not None})
                rec["Path"] = path
                if src.get("Id"):
                    streams = [{k: st.get(k) for k in ("Type", "Codec", "Height", "Width", "Language", "Index",
                                                         "IsForced", "Title", "DisplayTitle", "VideoRangeType")}
                               for st in src.get("MediaStreams") or []]
                    rec["MediaSources"] = [{"Id": src["Id"], "Path": path, "MediaStreams": streams}]
                if rec != self.items.get(i["Id"]):
                    self.items[i["Id"]] = rec
                    changed = True
        if changed:
            self._save_soon()

    def item(self, item_id, card=None):
        """A Jellyfin-shaped item from what we know (or from the UI's card), or None."""
        with self.lock:
            rec = dict(self.items.get(item_id) or {})
        if card and card.get("path") and not rec.get("Path"):
            rec.update({"Id": item_id, "Path": card["path"], "Name": card.get("name"),
                        "Type": card.get("type") or ("Episode" if card.get("episode") is not None else "Movie"),
                        "SeriesName": card.get("series"), "SeriesId": card.get("series_id"),
                        "ParentIndexNumber": card.get("season"), "IndexNumber": card.get("episode"),
                        "ProductionYear": card.get("year"),
                        "UserData": {"PlaybackPositionTicks": int((card.get("position") or 0) * TICKS)}})
        if not rec.get("Path"):
            return None
        rec.setdefault("MediaSources", [{"Id": item_id, "Path": rec["Path"], "MediaStreams": []}])
        rec["_from_map"] = True
        return rec

    def position(self, item_id, pos, duration):
        with self.lock:
            rec = self.items.get(item_id)
            if not rec:
                return
            ud = dict(rec.get("UserData") or {})
            played = bool(duration and pos > duration * 0.9)
            ud["PlaybackPositionTicks"] = 0 if played else int(pos * TICKS)
            if played:
                ud["Played"] = True
            rec["UserData"] = ud
        self._save_soon()

    def next_episode(self, item):
        sid = item.get("SeriesId")
        if not sid:
            return None
        with self.lock:
            eps = [r for r in self.items.values() if r.get("SeriesId") == sid and r.get("Type") == "Episode"
                   and (r.get("ParentIndexNumber") or 0) > 0]
        key = lambda r: (r.get("ParentIndexNumber") or 0, r.get("IndexNumber") or 0)  # noqa: E731
        eps.sort(key=key)
        cur = key(item)
        later = [r for r in eps if key(r) > cur]
        return dict(later[0]) if later else None

    def _save_soon(self):
        if self._timer:
            return
        self._timer = threading.Timer(5, self._save)
        self._timer.daemon = True
        self._timer.start()

    def _save(self):
        self._timer = None
        with self.lock:
            data = json.dumps(self.items)
        try:
            self.FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.FILE.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(self.FILE)
        except OSError as e:
            print("local map not saved:", e)


POSITIONS = settings.DIR / "positions.json"
_pos_lock = threading.Lock()


def _positions(pop=None):
    """Local resume points of files played outside Jellyfin, keyed by path. pop: take one out (and return it)."""
    with _pos_lock:
        try:
            data = json.loads(POSITIONS.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if pop is None:
            return data
        hit = data.pop(pop, None)
        if hit is not None:
            POSITIONS.write_text(json.dumps(data, indent=1), encoding="utf-8")
        return hit


def _remember_position(path, pos, duration):
    with _pos_lock:
        try:
            data = json.loads(POSITIONS.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if duration and pos > duration * 0.92:
            data.pop(path, None)  # finished: start from the top next time
        elif pos > 5:
            data[path] = {"pos": round(pos, 1), "duration": round(duration or 0, 1), "at": int(time.time())}
        POSITIONS.parent.mkdir(parents=True, exist_ok=True)
        tmp = POSITIONS.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(POSITIONS)


def _focused_window(thread_id):
    """The window with keyboard focus in a thread's input state (GetGUIThreadInfo), or None."""
    from ctypes import wintypes as wt

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", ctypes.c_void_p),
                    ("hwndFocus", ctypes.c_void_p), ("hwndCapture", ctypes.c_void_p),
                    ("hwndMenuOwner", ctypes.c_void_p), ("hwndMoveSize", ctypes.c_void_p),
                    ("hwndCaret", ctypes.c_void_p), ("rcCaret", wt.RECT)]
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if ctypes.windll.user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        return info.hwndFocus
    return None


class Thumbs:
    """Seek previews above the progress bar. Jellyfin trickplay tiles when the item has them, else frames
    grabbed on the fly by a small hidden second mpv (keyframe seeks, hardware decoding, scaled on the GPU
    path's copy-back), cached per time bucket. Shown with overlay-add as BGRA, with rounded corners baked in;
    the controls draw the glass frame around it."""
    OVERLAY = 7

    def __init__(self, player):
        self.player = player
        self.cv = threading.Condition()
        self.want = None          # (t, x, y, w, h, r) latest request, or "hide"
        self.src = None           # (path_or_url, item, duration, generation)
        self.gen = 0
        self.cache = {}
        self.tiles = {}
        self.mpv = None
        self.size = None
        self.shown = False
        self.flip = 0
        self.dir = settings.DIR / "thumbs"
        threading.Thread(target=self._run, daemon=True).start()

    # -- from any thread
    def reset(self, item, source):
        with self.cv:
            self.gen += 1
            self.src = (source, item, self.gen)
            self.cache, self.tiles = {}, {}
            self.want = "hide"
            old, self.mpv = self.mpv, None
            self.cv.notify()
        if old:
            threading.Thread(target=old.terminate, daemon=True).start()

    def update_item(self, item):
        with self.cv:
            if self.src and self.src[1] is not None and self.src[1].get("Id") == item.get("Id"):
                self.src = (self.src[0], item, self.src[2])

    def request(self, t, x, y, w, h, r):
        with self.cv:
            self.want = (t, x, y, w, h, r)
            self.cv.notify()

    def hide(self):
        with self.cv:
            self.want = "hide"
            self.cv.notify()

    def stop(self):
        self.reset(None, None)

    # -- worker
    def _run(self):
        while True:
            with self.cv:
                while self.want is None:
                    self.cv.wait()
                want, self.want = self.want, None
                src = self.src
            if os.environ.get("MARQUEE_DEV_DEBUG"):
                print("thumb", want, bool(src and src[0]), flush=True)
            try:
                if want == "hide":
                    if self.shown and self.player.mpv:
                        self.player.mpv.command("overlay-remove", self.OVERLAY)
                    self.shown = False
                    continue
                if not src or not src[0]:
                    continue
                t, x, y, w, h, r = want
                data = self._thumb(src, t, w, h)
                with self.cv:
                    stale = self.want == "hide" or (src is not self.src)
                if data is None or stale:
                    continue
                self._show(data, x, y, w, h, r)
            except Exception as e:
                print("thumbnail failed:", e)

    def _bucket(self, t, src):
        dur = (src[1] or {}).get("RunTimeTicks", 0) / TICKS or 3600
        step = min(15.0, max(2.0, dur / 400))
        return int(t // step), step

    def _thumb(self, src, t, w, h):
        key, step = self._bucket(t, src)
        if (key, w, h) in self.cache:
            return self.cache[(key, w, h)]
        data = self._trickplay(src, key * step + step / 2, w, h)
        if data is None:
            data = self._grab(src, key * step + step / 2, w, h)
        if data is not None:
            if len(self.cache) > 600:
                self.cache.clear()
            self.cache[(key, w, h)] = data
        return data

    def _trickplay(self, src, t, w, h):
        item = src[1] or {}
        info = item.get("Trickplay") or {}
        if not info:
            return None
        try:
            from io import BytesIO

            from PIL import Image
        except ImportError:
            return None
        per_source = next(iter(info.values()))
        width = min(per_source, key=lambda k: abs(int(k) - w))
        tp = per_source[width]
        index = min(int(t * 1000 // tp["Interval"]), tp["ThumbnailCount"] - 1)
        per_tile = tp["TileWidth"] * tp["TileHeight"]
        tile_no, cell = divmod(index, per_tile)
        jf = self.player.app.jf
        tile = self.tiles.get((width, tile_no))
        if tile is None:
            msid = (item.get("MediaSources") or [{}])[0].get("Id", "")
            r = jf.s.get(f"{jf.base}/Videos/{item['Id']}/Trickplay/{width}/{tile_no}.jpg",
                         params={"MediaSourceId": msid}, headers={"Authorization": jf.auth_header()}, timeout=10)
            r.raise_for_status()
            tile = Image.open(BytesIO(r.content)).convert("RGB")
            self.tiles[(width, tile_no)] = tile
        col, row = cell % tp["TileWidth"], cell // tp["TileWidth"]
        tw, th = tp["Width"], tp["Height"]
        img = tile.crop((col * tw, row * th, col * tw + tw, row * th + th)).resize((w, h))
        return img.convert("RGBA").tobytes("raw", "BGRA")

    def _grab(self, src, t, w, h):
        if self.mpv is None or self.size != (w, h):
            if self.mpv:
                old, self.mpv = self.mpv, None
                threading.Thread(target=old.terminate, daemon=True).start()
            st = settings.load()
            self.mpv = mpv.MPV(vo="null", ao="null", aid="no", sid="no", pause=True, hr_seek="no",
                               hwdec="d3d11va-copy,auto-copy" if st["hwdec"] else "no", keep_open="always",
                               vf=f"scale=w={w}:h={h}", ytdl=False, load_scripts=False, osc=False, cache="no",
                               demuxer_readahead_secs=0, input_default_bindings=False, loglevel="no")
            self.size = (w, h)
            self.mpv.loadfile(src[0])
            self.mpv.wait_for_property("duration", lambda v: v, timeout=15)
        try:
            with self.mpv.prepare_and_wait_for_event("playback-restart", timeout=3):
                self.mpv.command("seek", f"{t:.2f}", "absolute+keyframes")
        except Exception:
            pass  # a slow seek: take whatever frame is there
        res = self.mpv.command("screenshot-raw", "video", "bgra")
        if not res or not res.get("w"):
            return None
        # screenshots are corrected to the display aspect, so they can come out a pixel off the asked size:
        # crop or pad to exactly w x h
        rw, rh, stride, data = res["w"], res["h"], res["stride"], res["data"]
        row = min(rw, w) * 4
        out = bytearray(w * h * 4)
        for i in range(min(rh, h)):
            out[i * w * 4:i * w * 4 + row] = data[i * stride:i * stride + row]
        return bytes(out)

    def _show(self, data, x, y, w, h, r):
        buf = bytearray(data)
        _round_corners(buf, w, h, r)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.flip ^= 1
        path = str(self.dir / f"thumb{self.flip}.bgra")
        with open(path, "wb") as f:
            f.write(buf)
        self.player.mpv.command("overlay-add", self.OVERLAY, int(x), int(y), path, 0, "bgra", w, h, w * 4)
        self.shown = True


def _round_corners(buf, w, h, r):
    """Premultiplied BGRA: make the four corners transparent outside a radius-r arc (anti-aliased)."""
    r = int(max(0, min(r, w // 2, h // 2)))
    buf[3::4] = bytes([255]) * (w * h)
    for yy in range(r):
        for xx in range(r):
            dx, dy = r - xx - 0.5, r - yy - 0.5
            d = (dx * dx + dy * dy) ** 0.5
            a = max(0.0, min(1.0, r - d + 0.5))
            if a >= 1.0:
                continue
            for px, py in ((xx, yy), (w - 1 - xx, yy), (xx, h - 1 - yy), (w - 1 - xx, h - 1 - yy)):
                o = (py * w + px) * 4
                buf[o] = int(buf[o] * a)
                buf[o + 1] = int(buf[o + 1] * a)
                buf[o + 2] = int(buf[o + 2] * a)
                buf[o + 3] = int(255 * a)


_SOUND_ONLY = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}|<[^>]+>|[♪♫#\-–—\s.]")


def credits_from_cues(cues, dur):
    """Where a film's end credits start, from its subtitle cues [(start, end, text)] in seconds: the first long
    stretch (2.5+ minutes) in the last 20 % with no spoken line - music notes and sound cues don't count, and a
    post-credits scene that talks again later doesn't either. None when there's no such stretch."""
    spoken = sorted((a, b) for a, b, t in cues if _SOUND_ONLY.sub("", t or ""))
    if not spoken or not dur:
        return None
    floor = dur * 0.8
    prev_end = None
    for a, b in spoken:
        if prev_end is not None and prev_end >= floor and a - prev_end >= 150:
            return prev_end + 20
        prev_end = max(prev_end or 0, b)
    if prev_end and prev_end >= floor and dur - prev_end >= 150:  # the talking simply stops before the end
        return prev_end + 20
    return None


def credits_from_black(black_seconds, scan_start, dur, run=6, rest=0.6):
    """black_seconds: whole seconds (from one sampled frame a second) that were mostly black. The credits start at
    the first run of `run` consecutive black seconds after which at least `rest` of the remaining seconds up to the
    end are black too, and at least 10 s from the end (a fade-out isn't credits). None when there's no such run."""
    secs = set(black_seconds)
    end = int(dur)
    for t in sorted(secs):
        if all(t + i in secs for i in range(run)) and end - t >= 10:
            left = range(t, end)
            if len(left) and sum(1 for x in left if x in secs) / len(left) >= rest:
                return float(t)
    return None


def credits_block(keys, black, dur, min_run=30, max_gap=240, end_slack=25, min_final=60):
    """keys: keyframe times; black: those that were mostly black. Runs of consecutive black keyframes lasting
    min_run+ seconds; the last one must reach the end (within end_slack) and last min_final+; earlier runs join it
    across breaks of up to max_gap (titles over pictures between the crawls). Returns where that block starts."""
    runs, cur = [], None
    for k in keys:
        if k in black:
            cur = [k, k] if cur is None else [cur[0], k]
        else:
            if cur and cur[1] - cur[0] >= min_run:
                runs.append(cur)
            cur = None
    if cur and cur[1] - cur[0] >= min_run:
        runs.append(cur)
    if not runs or dur - runs[-1][1] > end_slack or runs[-1][1] - runs[-1][0] < min_final:
        return None
    start = runs[-1][0]
    for a, b in reversed(runs[:-1]):
        if start - b <= max_gap:
            start = a
        else:
            break
    return float(start)



def after_credits(cues, credits_at, dur, quiet=60, gap=40):
    """Scenes after (or in the middle of) the credits, from subtitle cues [(start, end, text)]: spoken lines that
    come back at least `quiet` seconds into the credits, grouped (a pause of `gap`+ seconds starts a new scene).
    Returns where each scene starts (a few seconds before its first line), at most two."""
    spoken = sorted(a for a, b, t in cues if a > credits_at + quiet and a < dur - 30 and _SOUND_ONLY.sub("", t or ""))
    out, last = [], None
    for a in spoken:
        if last is None or a - last > gap:
            out.append(round(max(credits_at, a - 4), 1))
        last = a
    return out[:2]
