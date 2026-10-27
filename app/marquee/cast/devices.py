"""Finding TVs to cast to, and what each one can play.

mDNS (zeroconf) browses _googlecast._tcp (Chromecast, Google TV, Nest Hub, TVs with Chromecast built in) and
_airplay._tcp (Apple TV, AirPlay 2 TVs, Macs). Speakers and speaker groups (no video out) are left out. Discovery
runs only while someone is looking (the picker, Settings, a cast in progress) and stops 10 minutes after the last look.

What a TV plays: Google Cast doesn't tell a sender which codecs a device decodes, so it comes from the model the TV
announces (Chromecast Ultra, Google TV Streamer, Nest Hub...). A device that also announces the Android TV remote
service (_androidtvremote2._tcp) on the same address is a Google TV / Android TV. Unknown models get the safe
profile every Cast device supports: H.264 up to 1080p with AAC. AirPlay's "features" bits say whether a receiver
takes video and HLS; Apple TV 4K models (AppleTV6,2 and later) also take HEVC, 4K and HDR.
"""
import ipaddress
import os
import re
import socket
import threading
import time

GOOGLECAST = "_googlecast._tcp.local."
AIRPLAY = "_airplay._tcp.local."
ANDROIDTV = "_androidtvremote2._tcp.local."
SEARCH_SECS = 8           # "Searching…" for this long after discovery (re)starts
IDLE_STOP = 600           # stop browsing this long after the last look

# Google Cast "ca" capability bits (from the mDNS TXT record)
CA_VIDEO_OUT = 0x1

AAC_ONLY = {"aac": True, "mp3": True}
CAST_AUDIO = {"aac": True, "mp3": True, "opus": True, "flac": True}   # the Default Media Receiver (no AC-3 decode)
APPLE_AUDIO = {"aac": True, "ac3": True, "eac3": True, "mp3": True}

# What each kind of receiver plays. max_bitrate: the most we send without converting (4K players on good Wi-Fi
# manage a UHD Blu-ray remux; the casting quality setting lowers it).
PROFILES = {
    "chromecast": {"label": "Chromecast", "h264_level": 42, "max_height": 1080, "max_bitrate": 20_000_000},
    "ultra": {"label": "Chromecast Ultra", "h264_level": 51, "max_height": 2160, "hevc": True, "hevc10": True,
              "vp9": True, "hdr": True, "max_bitrate": 80_000_000},
    "googletv": {"label": "Google TV", "h264_level": 51, "max_height": 2160, "hevc": True, "hevc10": True, "vp9": True,
                 "hdr": True, "max_bitrate": 80_000_000},
    "googletv_hd": {"label": "Google TV (HD)", "h264_level": 42, "max_height": 1080, "hevc": True, "hevc10": True,
                    "vp9": True, "av1": True, "hdr": True, "max_bitrate": 20_000_000},
    "streamer": {"label": "Google TV Streamer", "h264_level": 51, "max_height": 2160, "hevc": True, "hevc10": True,
                 "vp9": True, "av1": True, "hdr": True, "max_bitrate": 80_000_000},
    "androidtv": {"label": "Android TV", "h264_level": 51, "max_height": 2160, "hevc": True, "hevc10": True,
                  "vp9": True, "max_bitrate": 80_000_000},
    "hub": {"label": "Nest Hub", "h264_level": 41, "max_height": 720, "max_bitrate": 8_000_000},
    "casttv": {"label": "TV with Chromecast", "h264_level": 41, "max_height": 1080, "max_bitrate": 20_000_000},
    "casttv4k": {"label": "TV with Chromecast", "h264_level": 51, "max_height": 2160, "hevc": True,
                 "max_bitrate": 40_000_000},
    "appletv4k": {"label": "Apple TV 4K", "h264_level": 52, "max_height": 2160, "hevc": True, "hevc10": True,
                  "hdr": True, "max_bitrate": 80_000_000, "audio": APPLE_AUDIO},
    "appletv": {"label": "Apple TV", "h264_level": 42, "max_height": 1080, "max_bitrate": 20_000_000,
                "audio": APPLE_AUDIO},
    "mac": {"label": "Mac", "h264_level": 51, "max_height": 2160, "hevc": True, "max_bitrate": 40_000_000,
            "audio": APPLE_AUDIO},
    "airplaytv": {"label": "AirPlay TV", "h264_level": 41, "max_height": 1080, "max_bitrate": 20_000_000,
                  "audio": AAC_ONLY},
}
APPLE_4K = re.compile(r"AppleTV(6|1[1-9]|[2-9]\d),")


def caps(dev, allow_4k_hdr=True):
    """The capability dict marquee.mobile.stream.decide() takes, for a device and the "4K/HDR" setting."""
    p = PROFILES.get(dev.get("profile")) or PROFILES["chromecast"]
    c = {"h264": True, "hevc": bool(p.get("hevc")), "hevc10": bool(p.get("hevc10")), "av1": bool(p.get("av1")),
         "vp9": bool(p.get("vp9")), "hdr": bool(p.get("hdr")), "max_height": p["max_height"],
         "h264_level": p.get("h264_level", 41), "audio": dict(p.get("audio") or CAST_AUDIO), "mkv": False,
         "max_bitrate": p["max_bitrate"]}
    if not allow_4k_hdr:
        c["max_height"] = min(c["max_height"], 1080)
        c["hdr"] = False
        c["max_bitrate"] = min(c["max_bitrate"], 20_000_000)
    c["hevc_out"] = False   # transcodes go out as H.264, which every receiver plays
    return c


def _txt(info):
    out = {}
    for k, v in (info.properties or {}).items():
        try:
            out[k.decode("utf-8", "replace").lower()] = v.decode("utf-8", "replace") if isinstance(v, bytes) else v
        except AttributeError:
            continue
    return out


def _ipv4(info):
    try:
        addrs = info.parsed_addresses()
    except Exception:
        addrs = [socket.inet_ntoa(a) for a in info.addresses if len(a) == 4]
    for a in addrs:
        try:
            if ipaddress.ip_address(a).version == 4:
                return a
        except ValueError:
            continue
    return None


def _service_name(name, kind):
    return name[: -len(kind) - 1] if name.endswith("." + kind) else name.split(".")[0]


def googlecast_profile(model, androidtv=False):
    m = (model or "").lower()
    if "ultra" in m:
        return "ultra"
    if "streamer" in m:
        return "streamer"
    if "chromecast hd" in m:
        return "googletv_hd"
    if "hub" in m:
        return "hub"
    if "google tv" in m or ("chromecast" in m and androidtv):
        return "googletv"
    if m.startswith("chromecast"):
        return "chromecast"
    if androidtv:
        return "androidtv"
    return "casttv4k" if re.search(r"\b(4k|uhd|2160)", m) else "casttv"


def airplay_profile(model):
    m = model or ""
    if m.startswith("AppleTV"):
        return "appletv4k" if APPLE_4K.match(m) else "appletv"
    if m.startswith(("Mac", "iMac")):
        return "mac"
    return "airplaytv"


def _features(txt):
    """AirPlay features: "0x5A7FFFF7,0x1E" -> one int (low word, high word)."""
    raw = txt.get("features") or txt.get("ft") or ""
    try:
        parts = [int(x, 16) for x in raw.split(",") if x.strip()]
    except ValueError:
        return 0
    if not parts:
        return 0
    return parts[0] | ((parts[1] << 32) if len(parts) > 1 else 0)


class Discovery:
    def __init__(self):
        self.lock = threading.RLock()
        self.zc = None
        self.browsers = []
        self.devices = {}         # id -> device dict
        self.androidtv = set()    # addresses that announce the Android TV remote service
        self.started = 0.0
        self.wanted = 0.0
        self.error = None
        self._janitor = None
        self.listeners = []       # fn(devices) on changes

    # ------------------------------------------------------------------ lifecycle
    def ensure(self):
        """Browse (starting if needed) and keep browsing for IDLE_STOP more seconds."""
        with self.lock:
            self.wanted = time.time()
            if self.zc is None:
                self._start()
            if not (self._janitor and self._janitor.is_alive()):
                self._janitor = threading.Thread(target=self._idle_loop, daemon=True, name="cast-discovery-idle")
                self._janitor.start()

    def rescan(self):
        with self.lock:
            self._stop()
            self.devices.clear()
            self.wanted = time.time()
            self._start()

    def _start(self):
        self._dev_devices()
        try:
            from zeroconf import ServiceBrowser, Zeroconf
            self.zc = Zeroconf()
            listener = _Listener(self)
            self.browsers = [ServiceBrowser(self.zc, t, listener) for t in (GOOGLECAST, AIRPLAY, ANDROIDTV)]
            self.started = time.time()
            self.error = None
        except Exception as e:   # no network, or the mDNS port can't be opened
            self.zc = None
            self.error = f"Couldn't look for TVs: {e}"
            print("cast discovery failed:", type(e).__name__, e, flush=True)

    def _dev_devices(self):
        """Development: MARQUEE_DEV_CAST_DEVICES="Name|model|host|port;..." adds Google Cast receivers that aren't
        announced on the network (a test receiver on 127.0.0.1, so nothing shows up on other people's phones)."""
        for k, spec in enumerate(filter(None, (os.environ.get("MARQUEE_DEV_CAST_DEVICES") or "").split(";"))):
            name, model, host, port = (spec.split("|") + ["", "", "127.0.0.1", "8009"])[:4]
            profile = googlecast_profile(model)
            uid = f"dev{k}{re.sub(r'[^A-Za-z0-9]', '', name)}"
            self.devices["gc-" + uid] = {"id": "gc-" + uid, "kind": "googlecast", "name": name or "Test TV",
                                         "model": model, "host": host, "port": int(port or 8009), "uuid": uid,
                                         "profile": profile, "type": PROFILES[profile]["label"], "busy": False,
                                         "seen": time.time(), "_service": None}

    def _stop(self):
        zc, self.zc, self.browsers = self.zc, None, []
        if zc is not None:
            threading.Thread(target=zc.close, daemon=True).start()   # close() waits on the network a little

    def stop(self):
        with self.lock:
            self._stop()

    def _idle_loop(self):
        while True:
            time.sleep(15)
            with self.lock:
                if self.zc is None:
                    return
                if time.time() - self.wanted > IDLE_STOP:
                    self._stop()
                    return

    # ------------------------------------------------------------------ results
    def searching(self):
        return self.zc is not None and time.time() - self.started < SEARCH_SECS

    def list(self):
        with self.lock:
            devs = list(self.devices.values())
        return sorted(devs, key=lambda d: (d["kind"] != "googlecast", d["name"].lower()))

    def get(self, dev_id):
        with self.lock:
            return self.devices.get(dev_id)

    def _changed(self):
        for fn in list(self.listeners):
            try:
                fn(self.list())
            except Exception as e:
                print("cast devices listener:", e)

    def _add(self, kind, name, info):
        txt = _txt(info)
        host = _ipv4(info)
        if not host:
            return
        if kind == ANDROIDTV:
            with self.lock:
                self.androidtv.add(host)
                for d in self.devices.values():   # it may have been found before its Android TV twin
                    if d["host"] == host and d["kind"] == "googlecast" and d["profile"] in ("chromecast", "casttv",
                                                                                           "casttv4k"):
                        d["profile"] = googlecast_profile(d["model"], True)
                        d["type"] = PROFILES[d["profile"]]["label"]
            self._changed()
            return
        if kind == GOOGLECAST:
            if "group" in (txt.get("md") or "").lower():
                return
            try:
                ca = int(txt.get("ca") or 0)
            except ValueError:
                ca = 0
            if ca and not ca & CA_VIDEO_OUT:
                return   # a speaker
            uid = txt.get("id") or name
            model = txt.get("md") or ""
            profile = googlecast_profile(model, host in self.androidtv)
            dev = {"id": "gc-" + re.sub(r"[^A-Za-z0-9]", "", uid)[:40], "kind": "googlecast",
                   "name": txt.get("fn") or _service_name(name, GOOGLECAST), "model": model, "host": host,
                   "port": info.port or 8009, "uuid": uid, "profile": profile, "type": PROFILES[profile]["label"],
                   "busy": (txt.get("rs") or "").strip() not in ("", "Ready To Cast")}
            if profile in ("casttv", "casttv4k", "androidtv") and txt.get("md"):
                dev["type"] = model if len(model) <= 28 else dev["type"]
        else:
            feats = _features(txt)
            if feats and not (feats & 0x1 and feats & 0x10):   # bit 0: video, bit 4: HTTP Live Streaming
                return
            model = txt.get("model") or ""
            if model.startswith(("AudioAccessory", "AirPort")):
                return   # HomePod, AirPort Express
            uid = txt.get("deviceid") or txt.get("pi") or name
            profile = airplay_profile(model)
            dev = {"id": "ap-" + re.sub(r"[^A-Za-z0-9]", "", uid)[:40], "kind": "airplay",
                   "name": _service_name(name, AIRPLAY), "model": model, "host": host, "port": info.port or 7000,
                   "uuid": uid, "profile": profile, "type": PROFILES[profile]["label"],
                   "manufacturer": txt.get("manufacturer") or ("Apple" if profile != "airplaytv" else "")}
            if profile == "airplaytv" and dev["manufacturer"]:
                dev["type"] = f"{dev['manufacturer']} AirPlay TV"
        dev["seen"] = time.time()
        dev["_service"] = name
        with self.lock:
            old = self.devices.get(dev["id"])
            self.devices[dev["id"]] = dev
        if old is None or any(old.get(k) != dev.get(k) for k in ("name", "host", "port", "profile", "busy")):
            self._changed()

    def _remove(self, kind, name):
        if kind == ANDROIDTV:
            return
        with self.lock:
            gone = [k for k, d in self.devices.items() if d.get("_service") == name]
            for k in gone:
                self.devices.pop(k, None)
        if gone:
            self._changed()


class _Listener:
    def __init__(self, disc):
        self.d = disc

    def _resolve(self, zc, kind, name):
        try:
            info = zc.get_service_info(kind, name, 3000)
            if info:
                self.d._add(kind, name, info)
        except Exception as e:
            print("cast discovery:", type(e).__name__, e)

    def add_service(self, zc, kind, name):
        self._resolve(zc, kind, name)

    def update_service(self, zc, kind, name):
        self._resolve(zc, kind, name)

    def remove_service(self, zc, kind, name):
        self.d._remove(kind, name)


discovery = Discovery()


def public(dev):
    """A device for the page (no internal fields)."""
    return {k: dev.get(k) for k in ("id", "kind", "name", "type", "model", "host", "busy")} | {
        "four_k": (PROFILES.get(dev.get("profile")) or {}).get("max_height", 1080) >= 2160}
