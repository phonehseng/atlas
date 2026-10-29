"""Atlas: a local, Netflix-style player for your Jellyfin library. Entry point.

Starts the local UI server, opens the window (Edge WebView2), and puts the mpv player inside the same window.
Run: python -m marquee   (or Atlas.exe)
"""
import ctypes
import json
import os
import socket
import sys
import threading
import time

import webview

from . import server
from .jellyfin import Jellyfin
from .player import Player
from .seerr import Seerr

ICON = None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class App:
    def __init__(self):
        self.jf = Jellyfin()
        self.seerr = Seerr()
        self.player = Player(self)
        self.window = None
        self.fullscreen = False
        self.arrivals = self.live = None
        try:  # "Just arrived" downloads, and Jellyfin's live library updates (optional parts)
            from .arrivals import Arrivals
            self.arrivals = Arrivals(self)
        except Exception as e:
            print("arrivals unavailable:", e)
        try:
            from .live import Live
            self.live = Live(self)
        except Exception as e:
            print("live updates unavailable:", e)

    def webview_control(self):
        return self.window.native.webview

    def toggle_fullscreen(self):
        self.window.toggle_fullscreen()
        self.fullscreen = not self.fullscreen
        self._refresh_frame()

    def _refresh_frame(self):
        """pywebview turns Windows 11's rounded corners and window border off (or back on) only after it has
        resized the window, and Windows applies those settings at the next size change: until then fullscreen
        keeps a thin border and rounded corners. Set them again and nudge the size by a pixel so they take."""
        try:
            from System import Action
            form = self.window.native

            def apply():
                hwnd = ctypes.c_void_p(form.Handle.ToInt64())
                for attr, value in ((33, 1 if self.fullscreen else 0),  # corners: don't round / default
                                    (34, 0xFFFFFFFE if self.fullscreen else 0xFFFFFFFF)):  # border: none / default
                    v = ctypes.c_uint(value)
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), 4)
                b = form.Bounds
                flags = 0x0004 | 0x0010  # SWP_NOZORDER | SWP_NOACTIVATE
                ctypes.windll.user32.SetWindowPos(hwnd, None, b.X, b.Y, b.Width, b.Height - 1, flags)
                ctypes.windll.user32.SetWindowPos(hwnd, None, b.X, b.Y, b.Width, b.Height, flags)

            form.Invoke(Action(apply))
        except Exception as e:
            print("window frame refresh failed:", e)

    def notify_ui(self, event, data=None):
        try:
            self.window.evaluate_js(f"window.marquee && marquee.onEvent({json.dumps(event)}, {json.dumps(data or {})})")
        except Exception as e:
            print("ui event failed:", e)

    def started(self):
        self.window.events.shown.wait(30)
        for _ in range(200):  # the native form is set just after the window shows
            if self.window.native is not None:
                break
            time.sleep(0.05)
        form = self.window.native
        from System import Action
        form.Invoke(Action(lambda: self.player.attach(form)))
        try:  # updates: "this build starts" heartbeat for a waiting updater, then the background checks
            from . import updater
            updater.started(self)
        except Exception as e:
            print("updates unavailable:", type(e).__name__, e)
        try:  # the assistant hooks into mpv, so only after the player is attached
            from . import assistant
            assistant.init(self)
        except Exception as e:
            print("assistant unavailable:", type(e).__name__, e)
        try:  # Share to Google Drive: resume unfinished uploads, watch link expiry
            from . import share
            share.init(self)
        except Exception as e:
            print("sharing unavailable:", type(e).__name__, e)
        try:  # storage: hourly cleanup rules + smart season requests (never deletes while something plays)
            from . import server as _s, storage
            _s.catalog()
            storage.start(self)
        except Exception as e:
            print("storage unavailable:", type(e).__name__, e)
        try:  # phone remote, if it was left on
            from . import remote
            remote.apply(self)
        except Exception as e:
            print("phone remote unavailable:", type(e).__name__, e)
        try:  # casting to TVs (nothing runs until a TV is looked for)
            from .cast import blueprint as cast
            cast.init(self)
        except Exception as e:
            print("casting unavailable:", type(e).__name__, e)
        for part in (self.arrivals, self.live):
            if part is self.arrivals and os.environ.get("MARQUEE_DEV_NO_ARRIVALS"):
                continue  # development runs of the player: leave the downloads alone
            if part:
                try:
                    part.start()
                except Exception as e:
                    print("start failed:", type(part).__name__, e)
        dev_play = os.environ.get("MARQUEE_DEV_PLAY")  # development: start playing an item without the UI
        if dev_play:
            item_id, _, start = dev_play.partition("@")
            threading.Thread(target=self.player.play, args=(item_id,),
                             kwargs={"start": float(start) if start else None}, daemon=True).start()

    def closing(self):
        """Window closing: tell Jellyfin where we stopped and shut mpv down."""
        try:
            from . import remote
            remote.stop()
        except Exception:
            pass
        try:
            from . import share
            share.shutdown()
        except Exception:
            pass
        try:  # a cast stops with the app (the TV goes back to its home screen)
            from .cast import blueprint as cast
            cast.shutdown()
        except Exception:
            pass
        try:  # the built-in AI's llama-server (it also dies with the process; this is just tidy)
            from . import localllm
            localllm.ENGINE.stop()
        except Exception:
            pass
        try:
            self.player.shutdown()
        except Exception as e:
            print("player shutdown failed:", e)


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    try:  # its own taskbar group and icon, not Python's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Marquee.Player")
    except Exception:
        pass
    app = App()
    server.APP = app
    app.jf.ping()  # online/offline known before the page asks (short timeout; offline is fine)
    port = free_port()
    app.port = port
    threading.Thread(target=server.serve, args=(port,), daemon=True).start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    app.window = webview.create_window("Atlas", f"http://127.0.0.1:{port}/", width=1500, height=900,
                                       min_size=(960, 600), background_color="#0b1433")
    app.window.events.closing += app.closing
    print(f"Atlas on http://127.0.0.1:{port}/", flush=True)
    webview.start(app.started, private_mode=False, storage_path=str(server.settings.DIR / "webview"),
                  debug="--debug" in sys.argv)
    # The window is gone: shut mpv down, but never let a stuck video driver keep the process alive.
    try:  # os._exit skips atexit: take the local API's discovery file down here
        from . import localapi
        localapi.unpublish()
    except Exception:
        pass
    t = threading.Thread(target=app.player.terminate, daemon=True)
    t.start()
    t.join(3)
    os._exit(0)


if __name__ == "__main__":
    main()
