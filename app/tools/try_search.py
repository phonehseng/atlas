"""Development: time the instant search and the AI search for a few queries (needs tools/dev.ps1)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from marquee import server  # noqa: E402
from marquee.app import App  # noqa: E402


class NoPlayer:
    playing = False


a = App()
a.player = NoPlayer()
server.APP = a
c = server.app.test_client()
for q in sys.argv[1:] or ["dune", "horror movies", "shows that are funny", "give me something id like"]:
    t = time.time()
    r = c.get("/api/search", query_string={"q": q}).get_json()
    print(f"{q!r}: {time.time() - t:.1f}s prompt={r['prompt']} genres={r['genres']} "
          f"library={[i['name'] for i in r['items'][:5]]} requestable={[x['title'] for x in r['requestable'][:4]]}")
    if r["prompt"]:
        t = time.time()
        r = c.get("/api/search/ai", query_string={"q": q}).get_json()
        print(f"   AI {time.time() - t:.1f}s: {r['answer']!r} picks={[i['name'] for i in r['items'][:6]]} "
              f"requestable={[x['title'] for x in r['requestable'][:3]]}")
