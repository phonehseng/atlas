"""The assistant's handle on the player. Uses the player's public methods (play, pause, seek, set_volume, toast,
show_card, state, subtitle_window) when it has them, else talks to its mpv directly."""
import threading


class Control:
    def __init__(self, app):
        self.app = app

    # ------------------------------------------------------------------ basics
    @property
    def player(self):
        return getattr(self.app, "player", None)

    @property
    def mpv(self):
        return getattr(self.player, "mpv", None)

    def _has(self, name):
        return callable(getattr(self.player, name, None))

    def playing(self):
        p = self.player
        return bool(p and getattr(p, "playing", False) and getattr(p, "item", None))

    def item(self):
        return getattr(self.player, "item", None)

    def state(self):
        """{playing, paused, item_id, title, type, series_id, position, duration, volume, muted, subs, speed}"""
        base = {}
        if self._has("state"):
            try:
                base = dict(self.player.state() or {})
            except Exception:
                base = {}
        m, item = self.mpv, self.item() or {}
        out = {"playing": self.playing(), "item_id": item.get("Id"), "type": item.get("Type"),
               "series_id": item.get("SeriesId"), "title": item.get("Name"), "series": item.get("SeriesName"),
               "season": item.get("ParentIndexNumber"), "episode": item.get("IndexNumber"),
               "runtime": (item.get("RunTimeTicks") or 0) / 10_000_000}
        if m is not None and out["playing"]:
            for key, prop in (("position", "time_pos"), ("duration", "duration"), ("paused", "pause"),
                              ("volume", "volume"), ("muted", "mute"), ("speed", "speed"), ("sub_visible", "sub_visibility"),
                              ("sid", "sid")):
                try:
                    out[key] = getattr(m, prop)
                except Exception:
                    out[key] = None
        if out.get("position") is None:
            out["position"] = getattr(self.player, "_pos", 0) or 0
        out.update({k: v for k, v in base.items() if v is not None})
        return out

    def position(self):
        return float(self.state().get("position") or 0)

    # ------------------------------------------------------------------ actions
    def play(self, item_id, start=None):
        threading.Thread(target=self.player.play, args=(item_id,), kwargs={"start": start}, daemon=True).start()

    def pause(self, mode="toggle"):
        if self._has("pause"):
            return self.player.pause(mode)
        m = self.mpv
        m.pause = {"pause": True, "resume": False}.get(mode, not m.pause)

    def seek(self, value, relative=True):
        if self._has("seek"):
            return self.player.seek(value, relative)
        self.mpv.seek(value, "relative" if relative else "absolute")

    def volume(self):
        try:
            return float(self.state().get("volume") or self.mpv.volume or 0)
        except Exception:
            return 0.0

    def set_volume(self, value):
        value = max(0, min(130, round(value)))
        if self._has("set_volume"):
            self.player.set_volume(value)
        else:
            self.mpv.volume = value
        return value

    def mute(self, on):
        self.mpv.mute = bool(on)

    def speed(self, value):
        self.mpv.speed = value

    def stop(self):
        threading.Thread(target=self.player.stop, daemon=True).start()

    def fullscreen(self, on):
        if bool(getattr(self.app, "fullscreen", False)) != bool(on):
            threading.Thread(target=self.player._action, args=("fullscreen", ""), daemon=True).start()

    def pip(self):
        threading.Thread(target=self.player.toggle_pip, daemon=True).start()

    def toast(self, msg):
        try:
            if self.playing() and self._has("toast"):
                self.player.toast(msg)
        except Exception:
            pass

    def card(self, title, text):
        """A longer answer over the video (falls back to a toast)."""
        try:
            if not self.playing():
                return
            if self._has("show_card"):
                self.player.show_card(title, text)
            else:
                self.toast(text if len(text) < 140 else text[:137] + "...")
        except Exception:
            pass

    def notify(self, event, data):
        try:
            if getattr(self.app, "window", None) is not None and callable(getattr(self.app, "notify_ui", None)):
                self.app.notify_ui(event, data)
        except Exception:
            pass

    # ------------------------------------------------------------------ tracks
    def tracks(self, kind):
        try:
            return [t for t in (self.mpv.track_list or []) if t.get("type") == kind]
        except Exception:
            return []

    def subtitles(self, on, lang=None, cycle=False):
        m = self.mpv
        subs = self.tracks("sub")
        if not on:
            m.sub_visibility = False
            return "Subtitles off"
        if not subs:
            return "This video has no subtitles"
        pick = None
        if lang:
            pick = next((t for t in subs if _lang_match(t.get("lang"), lang) and not t.get("forced")), None) or \
                next((t for t in subs if _lang_match(t.get("lang"), lang)), None)
            if not pick:
                return f"No {_lang_name(lang)} subtitles on this one"
        elif cycle:
            ids = [t["id"] for t in subs]
            cur = m.sid
            pick = subs[(ids.index(cur) + 1) % len(ids)] if cur in ids else subs[0]
        elif m.sid in (None, False, "no"):
            pref = (getattr(m, "slang", None) or ["eng"])
            pref = pref if isinstance(pref, list) else str(pref).split(",")
            pick = next((t for p in pref for t in subs if _lang_match(t.get("lang"), p) and not t.get("forced")), subs[0])
        if pick:
            m.sid = pick["id"]
        m.sub_visibility = True
        cur = next((t for t in subs if t["id"] == m.sid), pick or {})
        label = cur.get("title") or _lang_name(cur.get("lang")) or ""
        return f"Subtitles on{': ' + label if label else ''}"

    def audio(self, lang=None, cycle=False):
        m = self.mpv
        tracks = self.tracks("audio")
        if len(tracks) < 2 and not lang:
            return "There's only one audio track"
        if lang == "original":
            pick = next((t for t in tracks if t.get("default")), tracks[0] if tracks else None)
        elif lang:
            pick = next((t for t in tracks if _lang_match(t.get("lang"), lang)), None)
            if not pick:
                return f"No {_lang_name(lang)} audio on this one"
        else:
            ids = [t["id"] for t in tracks]
            pick = tracks[(ids.index(m.aid) + 1) % len(ids)] if m.aid in ids else tracks[0]
        m.aid = pick["id"]
        return f"Audio: {pick.get('title') or _lang_name(pick.get('lang')) or 'track ' + str(pick['id'])}"


ALIASES = {"eng": ("en", "eng"), "spa": ("es", "spa"), "fre": ("fr", "fre", "fra"), "ger": ("de", "ger", "deu"),
           "jpn": ("ja", "jpn", "jp"), "ita": ("it", "ita"), "por": ("pt", "por", "pob"), "kor": ("ko", "kor"),
           "chi": ("zh", "chi", "zho", "cmn", "yue"), "rus": ("ru", "rus"), "hin": ("hi", "hin"), "ara": ("ar", "ara"),
           "dut": ("nl", "dut", "nld"), "swe": ("sv", "swe"), "pol": ("pl", "pol"), "tur": ("tr", "tur"),
           "tha": ("th", "tha"), "vie": ("vi", "vie"), "khm": ("km", "khm"), "dan": ("da", "dan"), "nor": ("no", "nor", "nob"),
           "fin": ("fi", "fin"), "gre": ("el", "gre", "ell"), "heb": ("he", "heb"), "ind": ("id", "ind"),
           "tgl": ("tl", "tgl", "fil"), "cze": ("cs", "cze", "ces"), "hun": ("hu", "hun")}
NAMES = {"eng": "English", "spa": "Spanish", "fre": "French", "ger": "German", "jpn": "Japanese", "ita": "Italian",
         "por": "Portuguese", "kor": "Korean", "chi": "Chinese", "rus": "Russian", "hin": "Hindi", "ara": "Arabic",
         "dut": "Dutch", "swe": "Swedish", "pol": "Polish", "tur": "Turkish", "tha": "Thai", "vie": "Vietnamese",
         "khm": "Khmer", "dan": "Danish", "nor": "Norwegian", "fin": "Finnish", "gre": "Greek", "heb": "Hebrew",
         "ind": "Indonesian", "tgl": "Tagalog", "cze": "Czech", "hun": "Hungarian"}


def _canon(code):
    c = (code or "").lower().split("-")[0]
    return next((k for k, v in ALIASES.items() if c in v), c)


def _lang_match(track_lang, want):
    return bool(track_lang) and _canon(track_lang) == _canon(want)


def _lang_name(code):
    return NAMES.get(_canon(code), code or "")
