"""A small Jellyfin client: sign-in, library sync for the local catalog, extras, and playback reporting.
Tracks whether the server answers (`online`, `down_since`) so callers can fall back to the local catalog."""
import os
import threading
import time
import uuid
from contextlib import contextmanager

import requests

from . import __version__ as VERSION, settings

CLIENT = "Atlas"
DEVICE_ID = "marquee-" + uuid.uuid5(uuid.NAMESPACE_DNS, os.environ.get("COMPUTERNAME", "pc")).hex[:12]
FIELDS = "Overview,Genres,PrimaryImageAspectRatio,ImageTags,BackdropImageTags,ParentBackdropImageTags,ChildCount," \
         "RecursiveItemCount,ProductionYear,OfficialRating,CommunityRating,RunTimeTicks,UserData,SeriesPrimaryImageTag," \
         "ParentLogoImageTag,ParentThumbImageTag,People,Studios,Taglines,Path,MediaSources,ProviderIds,DateCreated"


class JellyfinDown(requests.ConnectionError):
    """Raised at once (no network) by quick calls while the server is known to be unreachable."""


class Jellyfin:
    QUICK = (2.5, 4)   # connect, read: for extras a page can live without

    def __init__(self):
        self.s = requests.Session()
        self.on_items = []  # fn(list_of_item_dicts): sees every item Jellyfin returns (the player's local map)
        self.down_since = None  # when the server stopped answering; None while it answers
        self.last_ok = 0        # last time it answered
        self.listeners = []     # fn(online: bool) on every change
        self._tl = threading.local()
        self.reload()

    def reload(self):
        st = settings.load()
        self.base = (os.environ.get("MARQUEE_DEV_SERVER") or st["server"] or "").rstrip("/")
        self.token = os.environ.get("MARQUEE_DEV_KEY") or st["token"]
        self.user = os.environ.get("MARQUEE_DEV_USER") or st["user_id"]

    def auth_header(self, token=None):
        h = f'MediaBrowser Client="{CLIENT}", Device="Windows PC", DeviceId="{DEVICE_ID}", Version="{VERSION}"'
        return h + (f', Token="{token or self.token}"' if (token or self.token) else "")

    # ------------------------------------------------------------------ reachability
    @property
    def configured(self):
        return bool(self.base and self.token)

    @property
    def online(self):
        return self.configured and self.down_since is None

    def _set_state(self, up):
        if up:
            self.last_ok = time.time()
        was = self.down_since is None
        if was == up:
            return
        self.down_since = None if up else time.time()
        for fn in list(self.listeners):
            try:
                fn(up)
            except Exception as e:
                print("jellyfin listener failed:", e)

    def ping(self):
        """Is the server answering? Updates the online state either way."""
        if not self.base:
            return False
        try:
            up = self.s.get(self.base + "/System/Info/Public", timeout=self.QUICK).status_code < 500
        except requests.RequestException:
            up = False
        self._set_state(up)
        return up

    def fresh_online(self, age=30):
        """online, re-checked first (at most a short timeout) when nothing has answered for `age` seconds."""
        if not self.configured:
            return False
        if self.down_since is None and time.time() - self.last_ok > age:
            return self.ping()
        return self.online

    @contextmanager
    def quick(self, timeout=None):
        """Calls inside fail fast: short timeouts, and no network at all while the server is known to be down."""
        old = getattr(self._tl, "timeout", None)
        self._tl.timeout = timeout or self.QUICK
        try:
            yield
        finally:
            self._tl.timeout = old

    def call(self, method, path, **kw):
        quick = getattr(self._tl, "timeout", None)
        if quick:
            if self.down_since is not None or not self.base:
                raise JellyfinDown("Jellyfin isn't reachable")
            kw["timeout"] = quick
        kw.setdefault("timeout", 20)
        try:
            r = self.s.request(method, self.base + path, headers={"Authorization": self.auth_header()}, **kw)
        except (requests.ConnectionError, requests.Timeout):
            self._set_state(False)
            raise
        self._set_state(r.status_code not in (502, 503, 504))
        r.raise_for_status()
        data = r.json() if r.content and "json" in r.headers.get("content-type", "") else None
        if self.on_items and method == "GET" and data:
            items = data.get("Items") if isinstance(data, dict) and isinstance(data.get("Items"), list) else (
                data if isinstance(data, list) else ([data] if isinstance(data, dict) and data.get("Id") else []))
            # recommendations come as groups with their own Items
            items = [x for i in items for x in ((i.get("Items") or []) if "BaselineItemName" in i else [i])]
            for fn in self.on_items:
                try:
                    fn(items)
                except Exception as e:
                    print("item listener failed:", e)
        return data

    def get(self, path, timeout=20, **params):
        return self.call("GET", path, params=params, timeout=timeout)

    # ------------------------------------------------------------------ sign-in
    def login(self, server, user, password):
        base = server.rstrip("/")
        r = requests.post(base + "/Users/AuthenticateByName", json={"Username": user, "Pw": password}, timeout=20,
                          headers={"Authorization": f'MediaBrowser Client="{CLIENT}", Device="Windows PC", '
                                                    f'DeviceId="{DEVICE_ID}", Version="{VERSION}"'})
        r.raise_for_status()
        d = r.json()
        settings.save({"server": base, "token": d["AccessToken"], "user_id": d["User"]["Id"], "user_name": d["User"]["Name"]})
        self.reload()
        return d["User"]["Name"]

    # ------------------------------------------------------------------ browsing
    def items(self, **params):
        params.setdefault("Fields", FIELDS)
        params.setdefault("EnableImageTypes", "Primary,Backdrop,Logo,Thumb")
        want = params.get("AnyProviderIdEquals")
        if want and "ProviderIds" not in params["Fields"]:
            params["Fields"] += ",ProviderIds"
        items = self.get("/Items", userId=self.user, **params).get("Items", [])
        if want:  # some servers ignore this filter and return everything: check it here
            pairs = {tuple(p.split(".", 1)) for p in str(want).split(",") if "." in p}
            items = [i for i in items if any(str(v) == val for k, v in (i.get("ProviderIds") or {}).items()
                                             for key, val in pairs if k.lower() == key.lower())]
        return items

    def views(self):
        return self.get("/UserViews", userId=self.user).get("Items", [])

    def resume(self, limit=20):
        return self.get("/UserItems/Resume", userId=self.user, Limit=limit, MediaTypes="Video", Fields=FIELDS,
                        EnableImageTypes="Primary,Backdrop,Logo,Thumb").get("Items", [])

    def next_up(self, limit=20):
        return self.get("/Shows/NextUp", userId=self.user, Limit=limit, Fields=FIELDS,
                        EnableImageTypes="Primary,Backdrop,Logo,Thumb", EnableResumable="false").get("Items", [])

    def latest(self, parent=None, limit=24):
        return self.get("/Items/Latest", userId=self.user, Limit=limit, Fields=FIELDS, GroupItems="true",
                        **({"ParentId": parent} if parent else {})) or []

    def recommendations(self):
        return self.get("/Movies/Recommendations", userId=self.user, categoryLimit=4, ItemLimit=16, Fields=FIELDS) or []

    def similar(self, item_id, limit=16):
        return self.get(f"/Items/{item_id}/Similar", userId=self.user, Limit=limit, Fields=FIELDS).get("Items", [])

    def item(self, item_id):
        return self.get(f"/Items/{item_id}", userId=self.user, Fields=FIELDS)

    def seasons(self, series_id):
        return self.get(f"/Shows/{series_id}/Seasons", userId=self.user, Fields=FIELDS).get("Items", [])

    def episodes(self, series_id, season_id):
        return self.get(f"/Shows/{series_id}/Episodes", userId=self.user, seasonId=season_id, Fields=FIELDS).get("Items", [])

    def segments(self, item_id):
        try:
            return self.get(f"/MediaSegments/{item_id}").get("Items", [])
        except Exception:
            return []

    def mark(self, item_id, played):
        self.call("POST" if played else "DELETE", f"/UserPlayedItems/{item_id}", params={"userId": self.user})

    # ------------------------------------------------------------------ playback
    def report(self, what, body, timeout=3):
        """what: '' (started), 'Progress' or 'Stopped'. Raises if Jellyfin can't be reached (the player queues
        the report and sends it later)."""
        self.call("POST", "/Sessions/Playing" + (f"/{what}" if what else ""), json=body, timeout=timeout)
        # Development sign-in uses an API key, whose sessions have no user, so Jellyfin doesn't keep the
        # resume point from the reports above: store it on the user directly.
        if os.environ.get("MARQUEE_DEV_KEY") and os.environ.get("MARQUEE_DEV_PUSH") and what in ("Progress", "Stopped") and body.get("ItemId"):
            self.call("POST", f"/UserItems/{body['ItemId']}/UserData", params={"userId": self.user},
                      json={"PlaybackPositionTicks": int(body.get("PositionTicks") or 0)}, timeout=timeout)

    def stream_url(self, item, quality, play_session=None):
        """Where mpv plays from. Original quality: the file itself when it's on this PC, else the untouched
        file over the network. Lower qualities: a Jellyfin transcode (HLS)."""
        src = (item.get("MediaSources") or [{}])[0]
        if quality == "original":
            path = src.get("Path") or item.get("Path")
            if path and os.path.exists(path):
                return path
            return f"{self.base}/Videos/{item['Id']}/stream?static=true&MediaSourceId={src.get('Id', '')}&ApiKey={self.token}"
        height = int(quality)
        bitrate = {2160: 60_000_000, 1080: 12_000_000, 720: 5_000_000, 480: 2_000_000}.get(height, 8_000_000)
        return (f"{self.base}/Videos/{item['Id']}/master.m3u8?MediaSourceId={src.get('Id', '')}&ApiKey={self.token}"
                f"&VideoCodec=hevc,h264&AudioCodec=aac,ac3,eac3&MaxStreamingBitrate={bitrate}&MaxHeight={height}"
                f"&VideoBitrate={bitrate}&SubtitleMethod=External&TranscodingMaxAudioChannels=6&SegmentContainer=mp4"
                f"&RequireAvc=false&PlaySessionId={play_session or uuid.uuid4().hex}&DeviceId={DEVICE_ID}")

    def stop_transcode(self, play_session):
        """End the server-side transcode for a play session (quality switch, stop)."""
        try:
            self.call("DELETE", "/Videos/ActiveEncodings", params={"deviceId": DEVICE_ID, "playSessionId": play_session},
                      timeout=3)
        except Exception:
            pass

    def preview_url(self, item_id, start_ticks):
        """A small muted clip for hover previews (the browser plays it)."""
        return (f"{self.base}/Videos/{item_id}/stream.mp4?ApiKey={self.token}&Static=false&VideoCodec=h264&AudioCodec=aac"
                f"&MaxWidth=960&VideoBitrate=3000000&AudioBitrate=64000&StartTimeTicks={start_ticks}&Container=mp4"
                f"&DeviceId={DEVICE_ID}-preview&PlaySessionId={uuid.uuid4().hex}")

    def image(self, item_id, kind="Primary", **params):
        q = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.base}/Items/{item_id}/Images/{kind}" + (f"?{q}" if q else "")
