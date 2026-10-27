"""Development helper for the storage tests: a headless Atlas app (no window, no player, catalog not syncing)
wired to this PC's Jellyfin/Seerr the same way tools/dev.ps1 does. Keys are read from the local config files at
runtime and never printed. Deletions stay off (MARQUEE_STORAGE_NO_DELETE) unless a test points at a mock."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def dev_env():
    cfg = json.loads(Path(r"D:\Videos\MediaSorter\config.json").read_text(encoding="utf-8-sig"))
    s = json.loads(Path(r"D:\Videos\Apps\Seerr\config\settings.json").read_text(encoding="utf-8-sig"))
    os.environ.setdefault("MARQUEE_DEV_SERVER", "http://127.0.0.1:8096")
    os.environ.setdefault("MARQUEE_DEV_KEY", cfg["jellyfin"]["api_key"])
    os.environ.setdefault("MARQUEE_DEV_USER", "")
    os.environ.setdefault("MARQUEE_DEV_SEERR", "http://127.0.0.1:5055")
    os.environ.setdefault("MARQUEE_DEV_SEERR_KEY", s["main"]["apiKey"])
    os.environ.setdefault("MARQUEE_DEV_SEERR_USER", "1")
    os.environ.setdefault("MARQUEE_HOME", str(ROOT / ".devhome"))
    os.environ["MARQUEE_STORAGE_NO_DELETE"] = "1"


class HeadlessApp:
    def __init__(self, with_catalog=True):
        from marquee.jellyfin import Jellyfin
        from marquee.seerr import Seerr
        self.jf = Jellyfin()
        self.seerr = Seerr()
        self.player = type("P", (), {"playing": False})()
        self.events = []
        self.catalog = None
        if with_catalog:
            from marquee.catalog import Catalog
            self.catalog = Catalog(self)  # loaded from .devhome, never started: nothing syncs to Jellyfin

    def notify_ui(self, event, data=None):
        self.events.append((event, data))
