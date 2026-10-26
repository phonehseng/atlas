"""Development: tools/devserver.py plus the storage routes (marquee.storage_api) and the storage test harness at
http://127.0.0.1:8600/storage-harness. Reads this PC's Jellyfin/Seerr keys like tools/dev.ps1 (never printed).
Removing files is switched off (MARQUEE_STORAGE_NO_DELETE); requests are only sent if you press Request yourself,
so use the harness's dry-run buttons. Downloads are left alone (MARQUEE_DEV_NO_ARRIVALS)."""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from storage_devenv import dev_env  # noqa: E402

dev_env()
os.environ["MARQUEE_DEV_NO_ARRIVALS"] = "1"
os.environ["MARQUEE_STORAGE_NO_DELETE"] = "1"

from flask import send_from_directory  # noqa: E402

from marquee import server  # noqa: E402
from marquee.app import App  # noqa: E402
from marquee.storage_api import bp  # noqa: E402


class NoPlayer:
    playing = False
    mpv = None
    on_credits = []

    def play(self, *a, **k):
        print("would play", a)

    def play_file(self, *a, **k):
        print("would play file")

    def settings_changed(self):
        pass


server.app.register_blueprint(bp)


@server.app.get("/storage-harness")
def harness():
    r = send_from_directory(HERE, "storage_harness.html")
    r.headers["Cache-Control"] = "no-cache"
    return r


# The routes above must win over server.py's catch-all static route: move it to the end.
rules = server.app.url_map._rules
for r in list(rules):
    if r.endpoint == "static_files":
        rules.remove(r)
        rules.append(r)
server.app.url_map._remap = True
server.app.url_map.update()

app = App()
app.player = NoPlayer()
app.notify_ui = lambda *a, **k: print("ui event:", a[0])
app.arrivals = None
server.APP = app
PORT = int(os.environ.get("MARQUEE_STORAGE_PORT") or 8600)
print(f"http://127.0.0.1:{PORT}/storage-harness")
server.serve(PORT)
