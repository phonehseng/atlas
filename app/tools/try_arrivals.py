"""Development: list what Arrivals sees in qBittorrent right now (needs tools/dev.ps1)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from marquee.app import App  # noqa: E402
from marquee.arrivals import Arrivals  # noqa: E402

a = App()
arr = Arrivals(a)
arr.scan()
data = arr.list()
print("ready:", len(data["ready"]), "coming:", len(data["coming"]), "imported (hidden):",
      sum(1 for c in arr.items.values() if c.get("jellyfin_id")))
for c in data["ready"][:5] + data["coming"][:5]:
    state = "READY" if c["done"] else f"{c['progress']:.0%}"
    print(f"  {state} {c.get('series') or c['name']} "
          f"S{c.get('season')}E{c.get('episode')} poster={bool(c['poster'])} path={(c.get('path') or '')[-40:]}")
