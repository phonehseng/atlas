r"""Development: the phone app (Atlas Mobile) without the desktop window or the PC player.

    . tools\dev.ps1; $env:MARQUEE_DEV_NO_ARRIVALS = '1'
    python tools\devmobile.py [--port 8738] [--host 127.0.0.1] [--host <tailscale ip>] [--pair]

Uses the MARQUEE_DEV_* variables from tools/dev.ps1 (Jellyfin/Seerr API keys, no passwords) and MARQUEE_HOME.
--pair prints a pairing link. With MARQUEE_DEV_NO_PUSH=1 watch-history changes stay on this PC (nothing is sent to
Jellyfin), which is what tests should use.
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from marquee import server  # noqa: E402  (first: it registers the remote's desk blueprint)
from marquee import remote  # noqa: E402
from marquee.app import App  # noqa: E402
from marquee.mobile import auth  # noqa: E402


class NoPlayer:
    playing = False
    mpv = None
    item = None
    next_item = None
    on_credits = []

    def play(self, item_id, start=None, card=None):
        print("would play on the PC", item_id, start, flush=True)

    def play_file(self, card, start=None):
        print("would play file on the PC", card.get("path"), start, flush=True)

    def toast(self, msg):
        print("toast:", msg)

    def settings_changed(self):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8738)
    ap.add_argument("--host", action="append", default=None)
    ap.add_argument("--pair", action="store_true")
    a = ap.parse_args()
    if os.environ.get("MARQUEE_DEV_NO_PUSH"):
        from marquee.catalog import Catalog
        Catalog.push = lambda self: 0
        print("dev: watch-history changes are not sent to Jellyfin", flush=True)
    app = App()
    app.player = NoPlayer()
    app.notify_ui = lambda *a_, **k: None
    app.arrivals = None
    server.APP = app
    app.jf.ping()
    server.catalog()
    info = remote.start(app, a.port, a.host)
    print("listening on", info["hosts"], "port", info["port"], flush=True)
    if a.pair:
        host = (a.host or ["127.0.0.1"])[-1]
        print(f"pair: http://{host}:{a.port}/r#{auth.pairing_token()}", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
