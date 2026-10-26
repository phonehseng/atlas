"""Live library updates: listens to Jellyfin's WebSocket and tells the page when something was added, removed or
watched elsewhere, so new downloads show up in the rows the moment Jellyfin imports them (no polling, no folder
scanning of our own: Jellyfin already watches the folders)."""
import json
import threading
import time

import websocket  # websocket-client


class Live:
    def __init__(self, app):
        self.app = app
        self.ws = None
        self.stop_flag = False
        self.last = {}

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="jellyfin-live").start()

    def _url(self):
        jf = self.app.jf
        base = jf.base.replace("https://", "wss://").replace("http://", "ws://")
        from .jellyfin import DEVICE_ID
        return f"{base}/socket?ApiKey={jf.token}&deviceId={DEVICE_ID}-live"

    def _run(self):
        backoff = 2
        while not self.stop_flag:
            jf = self.app.jf
            if not (jf.base and jf.token):
                time.sleep(5)
                continue
            try:
                self.ws = websocket.WebSocketApp(self._url(), on_message=self._message,
                                                 on_open=lambda ws: ws.send(json.dumps({"MessageType": "SessionsStart",
                                                                                        "Data": "0,1500"})))
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
                backoff = 2
            except Exception as e:
                print("jellyfin live connection failed:", e)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _message(self, _ws, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        kind, data = msg.get("MessageType"), msg.get("Data") or {}
        if kind == "KeepAlive" or kind == "ForceKeepAlive":
            return
        catalog = getattr(self.app, "catalog", None)
        if catalog and kind in ("LibraryChanged", "UserDataChanged"):
            catalog.on_live(kind, data)  # updates the local catalog first, then tells the page
            return
        if kind == "LibraryChanged":
            self._tell("library-changed", {"added": data.get("ItemsAdded", []), "removed": data.get("ItemsRemoved", []),
                                           "updated": data.get("ItemsUpdated", [])})
        elif kind == "UserDataChanged":
            self._tell("userdata-changed", {"items": [u.get("ItemId") for u in data.get("UserDataList", [])]})

    def _tell(self, event, data):
        # Collapse bursts (a season pack imports 24 files): at most one event of a kind every 3 s.
        now = time.time()
        if now - self.last.get(event, 0) < 3:
            return
        self.last[event] = now
        from . import server
        server._cache.clear()
        self.app.notify_ui(event, data)
