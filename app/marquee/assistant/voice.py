"""Hands-free listening: every microphone at once, voice activity detection on each, the clearest one wins.

* All input devices are opened (one entry per physical device, WASAPI where available, 16 kHz mono), each with its
  own webrtcvad and noise floor. When someone speaks, the utterance is cut from the device that heard it best
  (most voiced energy above its own noise floor) and handed to faster-whisper.
* Activation: push-to-talk (hold the hotkey while speaking; a short tap listens for one sentence) or a wake word
  ("Hey Atlas": short voiced segments are transcribed and matched, or openWakeWord when chosen). Off by default:
  then no microphone is opened at all, except for one sentence when asked from the app.
* While it listens for a command the video is turned down and "Listening..." shows; audio only ever lives in
  memory (a ring buffer of a few seconds per device) and is dropped right after transcription."""
import collections
import ctypes
import difflib
import os
import queue
import re
import threading
import time

from . import config, installer

RATE = 16000
FRAME = 480                 # 30 ms, what webrtcvad wants
PREROLL = 0.45              # seconds kept from before speech was detected
END_SILENCE = 0.8
VK = {**{f"f{i}": 0x6F + i for i in range(1, 25)}, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
      "win": 0x5B, "space": 0x20, "capslock": 0x14, "scrolllock": 0x91, "pause": 0x13, "insert": 0x2D,
      "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "mouse4": 0x05, "mouse5": 0x06,
      "xbutton1": 0x05, "xbutton2": 0x06, "tab": 0x09, "backquote": 0xC0, "`": 0xC0}
JUNK = {"", "you", "thank you", "thanks for watching", "thank you for watching", "bye", "so", "the", "uh", "um", "hmm"}


def hotkey_codes(spec):
    codes = []
    for part in (spec or "").lower().replace(" ", "").split("+"):
        if part in VK:
            codes.append(VK[part])
        elif len(part) == 1 and part.isalnum():
            codes.append(ord(part.upper()))
    return codes


def _foreground_is_us():
    try:
        u32 = ctypes.windll.user32
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), ctypes.byref(pid))
        return pid.value == os.getpid()
    except Exception:
        return True


def list_inputs(cfg=None, sd=None):
    """[{index, name, hostapi, channels}] - one per physical microphone, filtered by the settings."""
    cfg = cfg or config.load()
    if sd is None:
        import sounddevice as sd
    apis = sd.query_hostapis()
    order = ["Windows WASAPI", "MME", "Windows DirectSound"]
    chosen = next((i for name in order for i, a in enumerate(apis) if a["name"] == name), 0)
    out, seen = [], set()
    for idx, d in enumerate(sd.query_devices()):
        if d["hostapi"] != chosen or d["max_input_channels"] < 1:
            continue
        name = d["name"]
        low = name.lower()
        if "sound mapper" in low or "primary sound capture" in low or low in seen:
            continue
        if any(x and x.lower() in low for x in cfg["exclude_devices"]):
            continue
        if cfg["devices"] and not any(x.lower() in low for x in cfg["devices"]):
            continue
        seen.add(low)
        out.append({"index": idx, "name": name, "hostapi": apis[chosen]["name"], "channels": d["max_input_channels"],
                    "rate": d["default_samplerate"]})
    return out


class Mic:
    """One input device: 30 ms frames into a short ring buffer, each marked voiced or not."""

    def __init__(self, info, vad_level):
        import numpy as np
        import sounddevice as sd
        import webrtcvad
        self.np = np
        self.info = info
        self.name = info["name"]
        self.vad = webrtcvad.Vad(vad_level)
        self.frames = collections.deque(maxlen=int(20 / 0.03))  # (t, int16 frame, rms, voiced)
        self.lock = threading.Lock()
        self.floor = 300.0
        self.level = 0.0
        self.error = None
        self.stream = None
        self._carry = np.zeros(0, dtype=np.int16)
        extra = sd.WasapiSettings(auto_convert=True) if "WASAPI" in info["hostapi"] else None
        last = None
        for ch in sorted({1, min(2, info["channels"])}):
            try:
                self.stream = sd.InputStream(device=info["index"], samplerate=RATE, channels=ch, dtype="int16",
                                             blocksize=FRAME, callback=self._cb, extra_settings=extra, latency="low")
                break
            except Exception as e:
                last = e
        if self.stream is None:
            raise last
        self.stream.start()

    def _cb(self, indata, frames, _time, _status):
        np = self.np
        mono = indata[:, 0] if indata.shape[1] == 1 else indata.mean(axis=1).astype(np.int16)
        buf = np.concatenate((self._carry, mono)) if self._carry.size else mono
        now = time.monotonic()
        n = buf.size // FRAME
        for k in range(n):
            f = buf[k * FRAME:(k + 1) * FRAME].copy()
            rms = float(np.sqrt(np.mean(f.astype(np.float32) ** 2)))
            try:
                voiced = self.vad.is_speech(f.tobytes(), RATE)
            except Exception:
                voiced = False
            if not voiced:  # noise floor follows the room, slowly
                self.floor = 0.97 * self.floor + 0.03 * max(rms, 30.0)
            self.level = 0.8 * self.level + 0.2 * rms
            with self.lock:
                self.frames.append((now - (n - 1 - k) * 0.03, f, rms, voiced))
        self._carry = buf[n * FRAME:].copy()

    def recent(self, count=10):
        with self.lock:
            return list(self.frames)[-count:]

    def active(self):
        fr = self.recent(10)
        if len(fr) < 10:
            return False
        voiced = [x for x in fr if x[3]]
        return len(voiced) >= 5 and sum(x[2] for x in voiced) / len(voiced) >= max(self.floor * 2.2, 120)

    def window(self, t0, t1):
        with self.lock:
            return [x for x in self.frames if t0 <= x[0] <= t1]

    def score(self, t0, t1):
        return sum(max(0.0, rms - self.floor) for _, _, rms, voiced in self.window(t0, t1) if voiced)

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
        with self.lock:
            self.frames.clear()


class Listener:
    def __init__(self, assistant):
        self.assistant = assistant
        self.ctl = assistant.ctl
        self.mics = []
        self.errors = {}
        self.mode = "off"
        self.running = False
        self.capturing = False      # a command is being recorded right now
        self.once_until = 0.0
        self.last_transcript = None
        self.last_device = None
        self.last_ms = None
        self._thread = None
        self._q = queue.Queue(maxsize=2)
        self._worker = None
        self._oww = None
        self._duck = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ control
    def start(self, mode=None):
        cfg = config.load()
        mode = mode or cfg["voice"]
        if mode == "off":
            self.stop()
            return self.status()
        if not installer.have("voice"):
            raise RuntimeError("Voice isn't installed yet: POST /api/assistant/install {\"parts\": [\"voice\", \"model\"]}")
        with self._lock:
            self.mode = mode
            if not self.running:
                self._open(cfg)
        from . import stt
        stt.engine().load()  # warm the speech model in the background
        if mode == "wake_word" and cfg["wake_engine"] == "openwakeword":
            self._load_oww(cfg)
        self.ctl.toast("Push-to-talk ready" if mode == "push_to_talk" else "Listening for \"Hey Atlas\"")
        self._notify()
        return self.status()

    def stop(self):
        with self._lock:
            self.mode = "off"
            self.once_until = 0
            self._close()
        self._notify()
        return self.status()

    def listen_once(self, seconds=8):
        """Listen for one sentence now (a button in the app), even when voice is off."""
        if not installer.have("voice"):
            raise RuntimeError("Voice isn't installed yet")
        with self._lock:
            if not self.running:
                self._open(config.load())
        from . import stt
        stt.engine().load()
        self.once_until = time.monotonic() + seconds
        self._indicator(True)
        return self.status()

    def _open(self, cfg):
        self.errors = {}
        for info in list_inputs(cfg):
            try:
                self.mics.append(Mic(info, int(cfg["vad"])))
            except Exception as e:
                self.errors[info["name"]] = str(e)[:160]
        if not self.mics:
            raise RuntimeError("No microphone could be opened" + (f": {self.errors}" if self.errors else ""))
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._work, daemon=True)
            self._worker.start()

    def _close(self):
        self.running = False
        for m in self.mics:
            m.close()
        self.mics = []
        self._restore_volume()
        self.capturing = False

    # ------------------------------------------------------------------ the loop (every 30 ms)
    def _loop(self):
        cfg = config.load()
        keys = hotkey_codes(cfg["hotkey"])
        get = ctypes.windll.user32.GetAsyncKeyState
        in_utt, t_start, last_voice, kind = False, 0.0, 0.0, None
        held_since, was_held = None, False
        cfg_at = time.monotonic()
        while self.running:
            time.sleep(0.03)
            now = time.monotonic()
            if now - cfg_at > 2:
                cfg, cfg_at = config.load(), now
                keys = hotkey_codes(cfg["hotkey"])
            held = bool(keys) and self.mode == "push_to_talk" and all(get(k) & 0x8000 for k in keys) and \
                (getattr(self.ctl.app, "window", None) is None or _foreground_is_us())
            if held and not was_held:
                held_since = now
                self._indicator(True)
            if not held and was_held and not in_utt:
                if held_since and now - held_since < 0.4:   # a tap: listen for one sentence
                    self.once_until = now + 8
                else:
                    self._indicator(False)
            was_held = held
            once = now < self.once_until
            if self.capturing and not (held or once or in_utt):
                self._indicator(False)
            if self._oww is not None and self.mode == "wake_word" and not once:
                self._feed_oww(now)
            wake_gate = self.mode == "wake_word" and cfg["wake_engine"] == "whisper"
            gate = held or once or wake_gate
            active = [m for m in self.mics if m.active()]
            if not in_utt:
                if gate and active:
                    in_utt, t_start, last_voice = True, now - PREROLL, now
                    kind = "ptt" if held else "once" if once else "wake"
                continue
            if active:
                last_voice = now
            limit = 7 if kind == "wake" else 15
            ended = (now - last_voice > (0.25 if kind == "ptt" and not held else END_SILENCE)) or now - t_start > limit
            if kind == "ptt" and held and now - t_start <= limit:
                ended = False  # hold-to-talk: the key decides
            if not ended:
                continue
            in_utt = False
            t_end = now
            if kind in ("once", "ptt"):
                self.once_until = 0
            best = max(self.mics, key=lambda m: m.score(t_start, t_end), default=None)
            if best is None or best.score(t_start, t_end) <= 0:
                continue
            frames = best.window(t_start, t_end)
            if kind == "wake" and len(frames) * 0.03 > 6.5:
                continue  # long stretches while waiting for the wake word are the film talking, not you
            np = best.np
            audio = np.concatenate([f for _, f, _, _ in frames]).astype(np.float32) / 32768.0
            self.last_device = best.name
            try:
                self._q.put_nowait((kind, audio))
            except queue.Full:
                pass  # still busy with the previous sentence: drop this one
            del frames, audio

    # ------------------------------------------------------------------ speech -> text -> assistant
    def _work(self):
        from . import stt
        while True:
            kind, audio = self._q.get()
            try:
                prompt = "Hey Atlas, pause. Skip forward 30 seconds. Turn on subtitles."
                item = self.ctl.item()
                if item:
                    prompt += f" {item.get('SeriesName') or item.get('Name')}."
                text, ms = stt.engine().transcribe(audio, prompt)
                del audio
                self.last_ms = ms
                clean = re.sub(r"[^\w\s']", " ", text.lower()).strip()
                clean = re.sub(r"\s+", " ", clean)
                if kind == "wake":
                    rest = self._after_wake(clean, text)
                    if rest is None:
                        continue  # not for us; nothing is kept
                    self.last_transcript = text
                    if len(re.sub(r"[^a-z]", "", rest.lower())) < 2:
                        self.once_until = time.monotonic() + 7
                        self._indicator(True)
                        continue
                    text = rest
                else:
                    self.last_transcript = text
                    self._indicator(False)
                if clean in JUNK:
                    self.ctl.toast("Didn't catch that")
                    continue
                self.assistant.handle(text, source="voice")
            except Exception as e:
                print("voice command failed:", e)
                self._indicator(False)
            finally:
                self._notify()

    def _after_wake(self, clean, original):
        """The words after the wake phrase, '' if it was only the wake phrase, None if it wasn't said."""
        phrases = [p.lower() for p in config.load()["wake_phrases"]]
        words = clean.split()
        for p in phrases:
            n = len(p.split())
            for start in range(0, min(3, max(1, len(words) - n + 1))):
                head = " ".join(words[start:start + n])
                if head == p or difflib.SequenceMatcher(None, head, p).ratio() >= 0.8:
                    return " ".join(words[start + n:])
        return None

    # ------------------------------------------------------------------ openWakeWord (optional)
    def _load_oww(self, cfg):
        try:
            import openwakeword
            from openwakeword.model import Model
            name = cfg["wake_model"] or "hey_jarvis"
            if not os.path.exists(name):
                try:
                    openwakeword.utils.download_models([name])
                except Exception:
                    pass
            self._oww = Model(wakeword_models=[name], inference_framework="onnx")
            self._oww_buf = []
            self._oww_t = 0.0
        except Exception as e:
            self._oww = None
            self.errors["openwakeword"] = str(e)[:160]

    def _feed_oww(self, now):
        mic = max(self.mics, key=lambda m: m.level, default=None)
        if mic is None:
            return
        fresh = [f for t, f, _, _ in mic.recent(4) if t > self._oww_t]
        if not fresh:
            return
        self._oww_t = mic.recent(1)[0][0]
        self._oww_buf.extend(fresh)
        if len(self._oww_buf) * FRAME < 1280:
            return
        np = mic.np
        chunk = np.concatenate(self._oww_buf)
        self._oww_buf = []
        scores = self._oww.predict(chunk)
        if scores and max(scores.values()) > 0.5:
            self._oww.reset()
            self.once_until = now + 7
            self._indicator(True)

    # ------------------------------------------------------------------ indicator, ducking
    def _indicator(self, on):
        if on == self.capturing:
            return
        self.capturing = on
        if on:
            self.ctl.toast("Listening...")
            try:
                if self.ctl.playing():
                    self._duck = self.ctl.volume()
                    self.ctl.set_volume(min(self._duck, max(10, self._duck * 0.35)))
            except Exception:
                self._duck = None
        else:
            self._restore_volume()
        self._notify()

    def _restore_volume(self):
        if self._duck is not None:
            try:
                self.ctl.set_volume(self._duck)
            except Exception:
                pass
            self._duck = None

    def _notify(self):
        self.ctl.notify("assistant-voice", {"mode": self.mode, "open": self.running, "listening": self.capturing,
                                            "heard": self.last_transcript})

    def status(self):
        return {"mode": self.mode, "open": self.running, "listening": self.capturing,
                "once": time.monotonic() < self.once_until,
                "devices": [{"name": m.name, "hostapi": m.info["hostapi"], "level": round(m.level),
                             "floor": round(m.floor), "active": m.active()} for m in self.mics],
                "errors": self.errors, "last_transcript": self.last_transcript, "last_device": self.last_device,
                "last_stt_ms": self.last_ms, "wake_engine": config.load()["wake_engine"],
                "openwakeword": self._oww is not None}
