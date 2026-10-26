"""End-to-end test of marquee.storage's deletion path against tools/storage_mock.py (no real library touched).

    python tools/test_storage_mock.py <scratch folder>

Uses its own MARQUEE_HOME inside the scratch folder. Checks: the cleanup plan (watched by all / in progress /
unwatched / next-up and current season protected), a real run removing what's due (unmonitor, delete via the app,
Jellyfin + Seerr told, the seeding torrent removed once no library file uses it), "removed" history records,
deleting by hand with its preview, a file no *arr manages going to the Windows Recycle Bin, restoring the played
state when a title comes back, and a smart request's payload."""
import json
import os
import shutil
import sys
import time
from pathlib import Path

scratch = Path(sys.argv[1]).resolve()
for k in [k for k in os.environ if k.startswith("MARQUEE_")]:
    os.environ.pop(k)
os.environ["MARQUEE_HOME"] = str(scratch / "home")
shutil.rmtree(scratch, ignore_errors=True)
scratch.mkdir(parents=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import storage_mock  # noqa: E402
from marquee import settings  # noqa: E402

PORT = 28600
S, srv = storage_mock.serve(scratch / "stack", PORT)
B = f"http://127.0.0.1:{PORT}"
settings.save({"server": f"{B}/jf", "token": "mock-token", "user_id": storage_mock.U1, "user_name": "one",
               "seerr": f"{B}/seerr", "seerr_cookie": "mock", "radarr_url": f"{B}/radarr", "radarr_key": "mock",
               "sonarr_url": f"{B}/sonarr", "sonarr_key": "mock", "delete_after_watch": True,
               "delete_after_watch_days": 3, "delete_unwatched_days": 30, "delete_torrents": True})

from marquee import smart, storage  # noqa: E402
from marquee.catalog import Catalog  # noqa: E402
from marquee.jellyfin import Jellyfin  # noqa: E402
from marquee.seerr import Seerr  # noqa: E402

ok_count = fail_count = 0


def check(cond, what):
    global ok_count, fail_count
    print(("  PASS " if cond else "  FAIL ") + what)
    ok_count += bool(cond)
    fail_count += not cond


class App:
    def __init__(self):
        self.jf, self.seerr = Jellyfin(), Seerr()
        self.player = type("P", (), {"playing": False})()
        self.events = []
        self.catalog = Catalog(self)

    def notify_ui(self, e, d=None):
        self.events.append((e, d))


app = App()
cat = app.catalog
P = S["paths"]
rows = [
    {"id": "jf-a", "type": "Movie", "name": "Mock Movie", "year": 2020, "path": P["A"], "provider": {"Tmdb": "9001"},
     "genres": ["Drama"], "runtime": 6000, "poster": None, "library": "movies", "source": "jellyfin"},
    {"id": "jf-b", "type": "Movie", "name": "Half Watched", "year": 2021, "path": P["B"], "provider": {"Tmdb": "9002"},
     "library": "movies", "source": "jellyfin"},
    {"id": "jf-c", "type": "Movie", "name": "Never Started", "year": 2019, "path": P["C"], "provider": {"Tmdb": "9003"},
     "library": "movies", "source": "jellyfin"},
    {"id": "jf-show", "type": "Series", "name": "Mock Show", "year": 2018, "provider": {"Tmdb": "9100", "Tvdb": "8100"},
     "library": "shows", "source": "jellyfin", "genres": ["Comedy"]},
]
for s in (1, 2):
    rows.append({"id": f"jf-season{s}", "type": "Season", "name": f"Season {s}", "series_id": "jf-show",
                 "season_id": f"jf-season{s}", "season_no": s, "library": "shows", "source": "jellyfin"})
    for e in (1, 2):
        rows.append({"id": f"jf-s{s}e{e}", "type": "Episode", "name": f"Episode {e}", "series_id": "jf-show",
                     "series_name": "Mock Show", "season_id": f"jf-season{s}", "season_no": s, "episode_no": e,
                     "path": P[f"S{s}E{e}"], "runtime": 1800, "library": "shows", "source": "jellyfin"})
local = scratch / "stack" / "loose" / "Home Video (2024).mkv"
local.parent.mkdir(parents=True)
local.write_bytes(os.urandom(1024) * 512)
rows.append({"id": "local:homevideo", "type": "Movie", "name": "Marquee storage test dummy (safe to delete)",
             "path": str(local), "library": "movies", "source": "local", "provider": {}})
with cat.lock:
    for r in rows:
        cat.items[r["id"]] = r
    cat._index()
    # this PC's catalog: user one watched the same things Jellyfin says (plus play counts)
    for rid in ("jf-a", "jf-s1e1", "jf-s1e2", "jf-s2e1"):
        u = cat._ud(rid)
        u.update(played=1, play_count=2, last_played=time.time() - 50 * 86400, changed=time.time() - 50 * 86400)

print("1. plan")
p = storage.plan(app)
keys = {i["key"]: i for i in p["items"]}
for i in p["items"]:
    print("    ", i["rule"], i["text"], "|", i["reason"])
for x in p["skipped"]:
    print("     skipped:", x["title"], x.get("subtitle"), "->", x["why"])
check(p["can_delete"], "deletion possible (all users visible, apps connected)")
check("jf-a" in keys and keys["jf-a"]["rule"] == "watched", "Mock Movie: watched by both users -> planned")
check("jf-a" in keys and keys["jf-a"]["frees_now"] == 0 and keys["jf-a"]["frees_after_torrent"] > 0,
      "Mock Movie: hardlinked seeding copy detected (frees space only with the torrent)")
check("jf-b" not in keys and any(x["title"] == "Half Watched" and "being watched" in x["why"] for x in p["skipped"]),
      "Half Watched: user two is mid-way -> kept")
check("jf-c" in keys and keys["jf-c"]["rule"] == "unwatched", "Never Started: nobody started it in 30+ days -> planned")
check("jf-s1e1" in keys and "jf-s1e2" in keys, "Mock Show S1 episodes: watched -> planned")
check("jf-s2e1" not in keys and "jf-s2e2" not in keys, "Mock Show S2 (current season, S2E2 next up) -> kept")

print("2. dry run changes nothing")
r = storage.run(app, dry_run=True)
check(all(a.get("would") for a in r["actions"]) and os.path.exists(P["A"]), "dry run: only 'would', files still there")
check(not storage_mock.CALLS, "dry run: no write calls reached the apps")

print("3. real run: first announces (Leaving soon), removes nothing yet")
r = storage.run(app, dry_run=False)
check(not [a for a in r["actions"] if a.get("removed")], "nothing removed on the first pass (notice period)")
check(any(e == "storage-leaving" for e, _ in app.events), "UI told: storage-leaving (the day-before notice)")
st = storage._load_state()
for k in st["flagged"]:
    st["flagged"][k] -= 5 * 86400  # pretend the notice went out five days ago
storage._save_state()
storage.forget()

print("4. real run after the notice period")
r = storage.run(app, dry_run=False)
for a in r["actions"]:
    print("    ", a)
removed = {a["key"] for a in r["actions"] if a.get("removed")}
check({"jf-a", "jf-c", "jf-s1e1", "jf-s1e2"} <= removed, "removed: Mock Movie, Never Started, S1E1, S1E2")
check(not os.path.exists(P["A"]) and os.path.exists(os.path.join(S["recycle"], os.path.basename(P["A"]))),
      "file went to the app's recycle bin")
calls = [(c["method"], c["path"]) for c in storage_mock.CALLS]
check(("PUT", "/radarr/api/v3/movie/editor") in calls, "Radarr: unmonitored first")
check(calls.index(("PUT", "/radarr/api/v3/movie/editor")) < calls.index(("DELETE", "/radarr/api/v3/moviefile/11")),
      "unmonitor happens before the delete")
check(("PUT", "/sonarr/api/v3/episode/monitor") in calls and ("DELETE", "/sonarr/api/v3/episodefile/200") in calls,
      "Sonarr: episodes unmonitored, files deleted")
check(("POST", "/jf/Library/Media/Updated") in calls, "Jellyfin told about the deleted paths")
check(("DELETE", "/seerr/api/v1/media/501") in calls, "Seerr media cleared (requestable again)")
check(("POST", "/qbt/api/v2/torrents/delete") in calls and not S["torrents"], "seeding torrent removed")
check(os.path.exists(P["S2E1"]) and os.path.exists(P["S2E2"]) and os.path.exists(P["B"]), "protected files untouched")
log = storage.read_log()
check(len(log) >= 4 and all("freed" in e for e in log), f"deletion log has {len(log)} entries")
rec = cat.removed_state("9001")
check(rec and rec["played"] and set(rec["users"]) == {storage_mock.U1, storage_mock.U2},
      "removed record for Mock Movie keeps both users' watched state")
check(cat.removed_state("9100", 1, 1) and cat.removed_state("9100", 1, 1)["played"], "removed record for S1E1 (watched)")
nev = cat.removed_state("9003")
check(nev and not nev["played"], "Never Started recorded as removed + unplayed")
check(Seerr.card({"mediaType": "movie", "id": 9001, "title": "Mock Movie"}).get("state") == "removed_watched",
      "a Seerr card for Mock Movie carries state removed_watched")
check(any(h["tmdb"] == "9001" and h["played"] for h in cat.removed_history()), "removed_history() feeds taste")

print("5. delete by hand: preview then delete S2E2 (next up for user one)")
storage_mock.CALLS.clear()
pv = storage.delete_preview(app, "jf-s2e2", "episode")
print("    ", pv["text"], "| warnings:", pv["warnings"], "| via", pv["via"], "| recycle bin:", pv["recycle_bin"])
check(pv["files"] and pv["unmonitor"] and pv["recycle_bin"] == S["recycle"], "preview: file, unmonitor, recycle bin")
check(pv["scopes"] == ["episode", "season", "series"], "preview: scopes for an episode")
check(not storage_mock.CALLS, "preview made no write calls")
e = storage.delete_now(app, "jf-s2e2", "episode", keep_monitored=True)
calls = [(c["method"], c["path"]) for c in storage_mock.CALLS]
check(("DELETE", "/sonarr/api/v3/episodefile/203") in calls and ("PUT", "/sonarr/api/v3/episode/monitor") not in calls,
      "manual delete with keep_monitored: file deleted, still monitored")
check(read := storage.read_log(1)[0]["rule"] == "manual", "logged as manual")

print("6. a file no *arr manages goes to the Windows Recycle Bin")
pv = storage.delete_preview(app, "local:homevideo")
check(pv["via"] == "Windows Recycle Bin" and not pv["unmonitor"], "preview: Windows Recycle Bin, nothing to unmonitor")
e = storage.delete_now(app, "local:homevideo")
check(not local.exists() and "moved to the Recycle Bin" in e["steps"], "dummy file moved to the Recycle Bin")

print("7. a removed title comes back: played state restored")
shutil.move(os.path.join(S["recycle"], os.path.basename(P["A"])), P["A"])
with cat.lock:
    cat.ud.pop("jf-a", None)
marks = []
done = cat.restore_removed(mark=lambda uid, iid, r: marks.append((uid, iid)))
check("movie:9001" in done, "Mock Movie record marked restored")
check((storage_mock.U2, "jf-a") in marks, "user two's watched state sent back to Jellyfin")
check((cat.ud.get("jf-a") or {}).get("played") == 1, "this user's watched state restored in the catalog (queued)")
check(cat.removed_state("9001") is None, "restored title no longer shows as removed")

print("8. smart request payload (mock Seerr)")
storage_mock.CALLS.clear()
res = storage.send_request(app, {"type": "tv", "id": 9100, "profile": 1, "server": 0, "root": "X:\\TV",
                                 "is4k": False, "season_mode": "smart", "title": "Mock Show"})
body = next(c["json"] for c in storage_mock.CALLS if c["path"] == "/seerr/api/v1/request")
print("     body sent to the mock:", body)
check(body == {"mediaType": "tv", "mediaId": 9100, "is4k": False, "seasons": [1], "profileId": 1, "serverId": 0,
               "rootFolder": "X:\\TV"}, "smart request asks for season 1 with profile / server / rootFolder")
check(smart.get(9100) and smart.get(9100)["requested"] == [1] and smart.get(9100)["next"] == 2,
      "smart table row: requested [1], next 2")
print(smart.status_line(app, 9100)["text"])

srv.shutdown()
print(f"\n{ok_count} passed, {fail_count} failed")
sys.exit(1 if fail_count else 0)
