"""Development: serve the UI and API in a normal browser at http://127.0.0.1:8600 (no window, no player).
Uses MARQUEE_DEV_* environment variables (see tools/dev.ps1) so no password is needed.
MARQUEE_DEV_NO_ARRIVALS=1 leaves the downloads alone (no auto-sort into the real library)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from marquee import server  # noqa: E402
from marquee.app import App  # noqa: E402


class NoPlayer:
    playing = False
    mpv = None
    on_credits = []

    def play(self, item_id, start=None, card=None):
        print("would play", item_id, start)

    def play_file(self, card, start=None):
        print("would play file", card.get("path"), start)

    def to_mini(self):
        print("would shrink to the mini tile")

    def restore_full(self):
        print("would restore the full video")

    def settings_changed(self):
        pass


app = App()
app.player = NoPlayer()
app.notify_ui = lambda *a, **k: None
if not os.environ.get("MARQUEE_DEV_NO_ARRIVALS"):
    from marquee.arrivals import Arrivals  # noqa: E402
    app.arrivals = Arrivals(app)
    app.arrivals.start()
else:
    app.arrivals = None
server.APP = app
print("http://127.0.0.1:8600")
server.serve(8600)
