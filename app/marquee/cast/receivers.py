"""Talking to the TV: one class per protocol, the same small interface for both.

  connect()                 reach the device (raises RuntimeError with a sentence for the page)
  load(media)               start playing: {url, content_type, hls, start, title, subtitle, image, tracks, active}
  status()                  {state, position, duration, volume, muted, idle_reason, error, gone}
                            state: LOADING | BUFFERING | PLAYING | PAUSED | IDLE
  play() pause() seek(t) set_volume(0..1) set_muted(bool) set_text_track(track id | None)
  stop()                    stop playing, leave the TV on its home screen, disconnect
  can                       what this receiver takes from us: {"pause", "seek", "volume", "text_tracks"}

Google Cast: pychromecast and Google's Default Media Receiver (no app of our own to register). WebVTT subtitles are
side-loaded text tracks, switched without reloading. HLS here is fragmented MP4, which the receiver must be told
(hlsSegmentFormat / hlsVideoSegmentFormat: FMP4).

AirPlay: pyatv's play_url (the TV keeps the request open while it plays). Pause and volume need the Apple TV's
remote protocols, which tvOS only allows after pairing, so without them the casting bar offers play/stop and seeks
by starting again at the new place.
"""
import threading
import time
import uuid

DMR_APP = "CC1AD845"   # Google's Default Media Receiver


class Receiver:
    kind = ""
    can = set()

    def __init__(self, dev):
        self.dev = dev
        self.error = None

    def close(self):
        pass


# ---------------------------------------------------------------------------------------------- Google Cast
class ChromecastReceiver(Receiver):
    kind = "googlecast"
    can = {"pause", "seek", "volume", "text_tracks"}

    def __init__(self, dev):
        super().__init__(dev)
        self.cc = None
        self.mc = None
        self.loaded_at = 0.0
        self.session_seen = False
        self._lock = threading.RLock()
        self._last_poll = 0.0
        self.on_change = None

    def connect(self):
        import pychromecast
        from pychromecast.models import CastInfo, HostServiceInfo
        d = self.dev
        try:
            uid = uuid.UUID(d.get("uuid") or "")
        except ValueError:
            uid = uuid.uuid5(uuid.NAMESPACE_URL, d.get("uuid") or d["host"])
        info = CastInfo({HostServiceInfo(d["host"], int(d.get("port") or 8009))}, uid, d.get("model"), d.get("name"),
                        d["host"], int(d.get("port") or 8009), "cast", "Google Inc.")
        self.cc = pychromecast.Chromecast(info, tries=2, timeout=8, retry_wait=1)
        try:
            self.cc.wait(timeout=15)
        except Exception:
            self._disconnect()
            raise RuntimeError(f"Couldn't reach {d.get('name') or 'the TV'}. Is it on, and on the same network?")
        self.mc = self.cc.media_controller
        self.mc.register_status_listener(self)

    # pychromecast MediaStatusListener
    def new_media_status(self, status):
        if status and status.media_session_id:
            self.session_seen = True
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def load_media_failed(self, queue_item_id, error_code):
        self.error = f"The TV couldn't play this (error {error_code})"
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def load(self, media):
        self.error = None
        self.session_seen = False
        info = {}
        if media.get("hls"):
            info.update(hlsSegmentFormat="fmp4", hlsVideoSegmentFormat="fmp4")
        if media.get("duration"):
            info["duration"] = float(media["duration"])
        tracks = [{"trackId": t["id"], "type": "TEXT", "trackContentId": t["url"], "trackContentType": "text/vtt",
                   "subtype": "SUBTITLES", "name": t.get("name") or "Subtitles", "language": t.get("lang") or "und"}
                  for t in media.get("tracks") or []]
        if tracks:
            info["tracks"] = tracks
            info["textTrackStyle"] = {"backgroundColor": "#00000000", "edgeType": "DROP_SHADOW",
                                      "edgeColor": "#000000CC", "foregroundColor": "#FFFFFFFF", "fontScale": 1.0,
                                      "fontFamily": "Google Sans", "fontGenericFamily": "SANS_SERIF"}
        meta = {"metadataType": 1 if media.get("movie") else 2, "title": media.get("title") or ""}
        if media.get("movie"):
            meta["subtitle"] = media.get("subtitle") or ""
        else:
            meta.update(seriesTitle=media.get("series") or "", subtitle=media.get("subtitle") or "")
            if media.get("season") is not None:
                meta["season"] = media["season"]
            if media.get("episode") is not None:
                meta["episode"] = media["episode"]
        if media.get("image"):
            meta["images"] = [{"url": media["image"]}]
        with self._lock:
            self.loaded_at = time.time()
            self.mc.play_media(media["url"], media["content_type"], current_time=float(media.get("start") or 0),
                               autoplay=True, stream_type="BUFFERED", metadata=meta, media_info=info)
        if media.get("active") is not None and tracks:
            threading.Thread(target=self._enable_track_later, args=(media["active"],), daemon=True).start()

    def _enable_track_later(self, track_id):
        try:
            self.mc.block_until_active(timeout=30)
            time.sleep(0.5)
            self.mc.enable_subtitle(int(track_id))
        except Exception as e:
            print("cast: subtitles didn't turn on:", type(e).__name__, e)

    def status(self):
        out = {"state": "LOADING", "position": 0.0, "duration": 0.0, "volume": None, "muted": False,
               "idle_reason": None, "error": self.error, "gone": False}
        cc, mc = self.cc, self.mc
        if cc is None or mc is None:
            out["gone"] = True
            return out
        if not cc.socket_client.is_connected and time.time() - self.loaded_at > 20:
            out["error"] = out["error"] or "Lost the connection to the TV"
        cs = cc.status
        if cs is not None:
            out["volume"], out["muted"] = cs.volume_level, bool(cs.volume_muted)
            if self.session_seen and cs.app_id and cs.app_id != DMR_APP:
                out["gone"] = True   # someone opened another app on the TV
        if time.time() - self._last_poll > 5 and mc.status.media_session_id:
            self._last_poll = time.time()
            try:
                mc.update_status()
            except Exception:
                pass
        st = mc.status
        if st is None:
            return out
        state = st.player_state or "UNKNOWN"
        pos = st.adjusted_current_time if st.adjusted_current_time is not None else st.current_time
        out["position"] = float(pos or 0)
        out["duration"] = float(st.duration or 0)
        if state in ("PLAYING", "PAUSED", "BUFFERING"):
            out["state"] = state
        elif state == "IDLE" and self.session_seen:
            out["state"] = "IDLE"
            out["idle_reason"] = st.idle_reason
        out["active_tracks"] = list(st.current_subtitle_tracks or []) if hasattr(st, "current_subtitle_tracks") else []
        return out

    def play(self):
        self.mc.play()

    def pause(self):
        self.mc.pause()

    def seek(self, t):
        self.mc.seek(max(0.0, float(t)))

    def set_volume(self, v):
        self.cc.set_volume(max(0.0, min(1.0, float(v))))

    def set_muted(self, m):
        self.cc.set_volume_muted(bool(m))

    def set_text_track(self, track_id):
        if track_id is None:
            self.mc.disable_subtitle()
        else:
            self.mc.enable_subtitle(int(track_id))

    def stop(self):
        try:
            if self.mc is not None and self.mc.status.media_session_id:
                self.mc.stop(timeout=5)
        except Exception as e:
            print("cast stop:", type(e).__name__, e)
        try:
            if self.cc is not None and self.cc.app_id == DMR_APP:
                self.cc.quit_app(timeout=5)   # back to the TV's own screen
        except Exception as e:
            print("cast quit:", type(e).__name__, e)
        self._disconnect()

    def _disconnect(self):
        cc, self.cc = self.cc, None
        if cc is not None:
            try:
                cc.disconnect(timeout=3)
            except Exception:
                pass

    close = _disconnect


# ---------------------------------------------------------------------------------------------- AirPlay
class AirPlayReceiver(Receiver):
    kind = "airplay"

    def __init__(self, dev):
        super().__init__(dev)
        self.loop = None
        self.atv = None
        self.task = None
        self.started = 0.0
        self.base = 0.0
        self.paused_at = None
        self.duration = 0.0
        self.media = None
        self.can = {"seek"}
        self.on_change = None

    def _run(self, coro, timeout=15):
        import asyncio
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def connect(self):
        import asyncio
        try:
            import pyatv
            from pyatv.const import Protocol
        except ImportError as e:
            raise RuntimeError(f"AirPlay support isn't ready on this PC ({e})")
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="cast-airplay").start()
        d = self.dev
        try:
            confs = self._run(pyatv.scan(self.loop, hosts=[d["host"]], timeout=5), timeout=12)
        except Exception as e:
            confs = []
            print("airplay scan:", type(e).__name__, e)
        if not confs:
            self.close()
            raise RuntimeError(f"Couldn't reach {d.get('name') or 'the TV'}. Is it on, and on the same network?")
        conf = confs[0]
        creds = (_airplay_credentials().get(d["id"]) or "").strip()
        if creds:
            conf.set_credentials(Protocol.AirPlay, creds)
        try:
            self.atv = self._run(pyatv.connect(conf, self.loop), timeout=20)
        except Exception as e:
            self.close()
            raise RuntimeError(f"{d.get('name') or 'The TV'} didn't accept the connection ({type(e).__name__})")
        feats = self.atv.features
        try:
            from pyatv.const import FeatureName, FeatureState
            if feats.get_feature(FeatureName.Pause).state == FeatureState.Available:
                self.can.add("pause")
            if feats.get_feature(FeatureName.SetVolume).state == FeatureState.Available:
                self.can.add("volume")
        except Exception:
            pass

    def load(self, media):
        import asyncio
        self.error = None
        self.media = media
        self.base = float(media.get("start") or 0)
        self.duration = float(media.get("duration") or 0)
        self.started = time.time()
        self.paused_at = None
        if self.task is not None and not self.task.done():
            self.task.cancel()   # closes the old request, which stops the old playback
        self.task = asyncio.run_coroutine_threadsafe(
            self.atv.stream.play_url(media["url"], position=int(self.base)), self.loop)

    def status(self):
        out = {"state": "PLAYING", "position": 0.0, "duration": self.duration, "volume": None, "muted": False,
               "idle_reason": None, "error": self.error, "gone": False}
        t = self.task
        if t is None:
            out["state"] = "LOADING"
            return out
        if t.done():
            exc = t.exception() if not t.cancelled() else None
            out["state"] = "IDLE"
            if exc is not None:
                name = type(exc).__name__
                if "Authentication" in name:
                    out["error"] = ("The Apple TV refused Atlas. On the Apple TV, open Settings > AirPlay and "
                                    "HomeKit > Allow Access and pick \"Anyone on the Same Network\".")
                else:
                    out["error"] = f"The TV stopped playing ({name}: {str(exc)[:120]})"
                out["idle_reason"] = "ERROR"
            else:
                out["idle_reason"] = "FINISHED"
            return out
        pos = None
        try:
            playing = self._run(self.atv.metadata.playing(), timeout=3)
            pos = playing.position
            if playing.total_time:
                out["duration"] = float(playing.total_time)
            st = str(playing.device_state).lower()
            if "paused" in st:
                out["state"] = "PAUSED"
            elif "loading" in st or "seeking" in st:
                out["state"] = "BUFFERING"
        except Exception:
            pass
        if pos is None:   # estimated from the clock (the TV doesn't tell this sender)
            ref = self.paused_at or time.time()
            pos = self.base + max(0.0, ref - self.started - 2.0)
            if self.paused_at:
                out["state"] = "PAUSED"
        out["position"] = float(pos)
        if "volume" in self.can:
            try:
                out["volume"] = float(self.atv.audio.volume) / 100
            except Exception:
                pass
        return out

    def play(self):
        self._run(self.atv.remote_control.play())
        if self.paused_at:
            self.started += time.time() - self.paused_at
            self.paused_at = None

    def pause(self):
        self._run(self.atv.remote_control.pause())
        self.paused_at = time.time()

    def seek(self, t):
        try:
            self._run(self.atv.remote_control.set_position(int(t)), timeout=5)
            self.base, self.started = float(t), time.time()
        except Exception:
            # no remote control without pairing: start again at the new place
            media = dict(self.media or {}, start=float(t))
            self.load(media)

    def set_volume(self, v):
        self._run(self.atv.audio.set_volume(max(0.0, min(100.0, float(v) * 100))))

    def set_muted(self, m):
        self.set_volume(0 if m else 0.5)

    def set_text_track(self, track_id):
        raise NotImplementedError

    def stop(self):
        try:
            if self.task is not None and not self.task.done():
                try:
                    self._run(self.atv.remote_control.stop(), timeout=5)
                except Exception:
                    pass
                self.task.cancel()
        finally:
            self.close()

    def close(self):
        atv, self.atv = self.atv, None
        if atv is not None:
            try:
                atv.close()
            except Exception:
                pass
        loop, self.loop = self.loop, None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)


def _airplay_credentials():
    from .. import settings
    v = settings.load().get("cast_airplay_credentials") or {}
    return v if isinstance(v, dict) else {}


def for_device(dev):
    return ChromecastReceiver(dev) if dev["kind"] == "googlecast" else AirPlayReceiver(dev)
