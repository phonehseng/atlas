"""Whole-title ending comments and Up-next moments: simulates watching (all of / a little of) a title in a private dev
home, then prepares the credits screen and prints the comment, the moments and what fed them. No Jellyfin writes.
Run with MARQUEE_HOME set to a private copy of .devhome:
  .venv\\Scripts\\python tools\\test_ending_whole.py "Chamber of Secrets" [--episode] [--season-before]"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if not os.environ.get("MARQUEE_DEV_KEY"):
    cfg = json.loads(Path(r"D:\Videos\MediaSorter\config.json").read_text(encoding="utf-8-sig"))
    os.environ.update(MARQUEE_DEV_SERVER="http://127.0.0.1:8096", MARQUEE_DEV_KEY=cfg["jellyfin"]["api_key"],
                      MARQUEE_DEV_USER="1fd488d22e3045f2a576d03ee7766859")
assert os.environ.get("MARQUEE_HOME"), "set MARQUEE_HOME to a private copy of .devhome"

from marquee import server  # noqa: E402
from marquee.assistant import ending, scene_search, watched  # noqa: E402
from marquee.jellyfin import FIELDS, Jellyfin  # noqa: E402
from marquee.seerr import Seerr  # noqa: E402


class App:
    player = None
    window = None


def simulate(item_id, a, b, step=10.0):
    """As the player tracker would record it: consecutive played chunks."""
    t = a
    while t < b:
        watched.add(item_id, t, min(b, t + step))
        t += step


def prepare_and_wait(app, item_id, timeout=420):
    ending._jobs.pop(item_id, None)
    ending.prepare(app, item_id)
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = ending._jobs.get(item_id)
        if j and j.state == "ready":
            return j, time.time() - t0
        time.sleep(1)
    return ending._jobs.get(item_id), time.time() - t0


def show(label, job, secs):
    print(f"\n--- {label} ({secs:.0f} s) notes={job.notes}")
    print("comment:", job.comment)


def main():
    app = App()
    app.jf = Jellyfin()
    app.seerr = Seerr()
    server.APP = app
    title = sys.argv[1]
    kind = "Episode" if "--episode" in sys.argv else "Movie"
    item = app.jf.items(searchTerm=title, Recursive="true", IncludeItemTypes=kind, Limit=1, Fields=FIELDS)[0]
    iid = item["Id"]
    credits_at = ending._credits_at(app, item)
    run = (item.get("RunTimeTicks") or 0) / 1e7
    print(f"{item.get('SeriesName') or ''} {item['Name']}: runtime {run:.0f} s, credits at {credits_at:.0f} s")
    if "--season-before" in sys.argv and kind == "Episode":
        eps = app.jf.get(f"/Shows/{item['SeriesId']}/Episodes", userId=app.jf.user, Fields=FIELDS).get("Items", [])
        for e in eps:
            if e.get("ParentIndexNumber") == item.get("ParentIndexNumber") and (e.get("IndexNumber") or 0) < (item.get("IndexNumber") or 0):
                simulate(e["Id"], 0, (e.get("RunTimeTicks") or 0) / 1e7)
                t0 = time.time()
                scene_search.summaries(app, e, build=True)
                print(f"earlier episode E{e.get('IndexNumber')} watched + summarised ({time.time() - t0:.0f} s)")
    # 1. a partial watch: the opening and the last ten minutes (jumped ahead)
    watched._load().pop(iid, None)
    simulate(iid, 0, credits_at * 0.15)
    simulate(iid, credits_at * 0.9, credits_at)
    print(f"partial watch: coverage {watched.coverage(iid, run, until=credits_at):.2f}")
    job, secs = prepare_and_wait(app, iid)
    show("partial watch (old behaviour expected)", job, secs)
    # 2. the whole thing
    simulate(iid, 0, credits_at)
    watched.flush()
    print(f"\nfull watch: coverage {watched.coverage(iid, run, until=credits_at):.2f}")
    job, secs = prepare_and_wait(app, iid)
    show("whole watch", job, secs)
    from marquee.assistant import moments
    for m in (moments.cached(iid) or {}).get("moments", []):
        print(f"  moment {m['label']:>8}  {m['caption']}")
    print("picks:", [(p["kind"], p["title"]) for p in job.picks][:5])


if __name__ == "__main__":
    main()
