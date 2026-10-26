"""Hourly health check for the media stack (part of Media Stack Setup).

Checks everything, fixes what is safe to fix, and reports the rest to the Media Hub (Activity feed,
plus a desktop notification on Windows when something needs attention). Safe fixes:
  * restart background apps that stopped / don't answer
  * re-test indexers Prowlarr/Sonarr/Radarr disabled after failures (restarting trawl first, since a
    stale Cloudflare cookie after a VPN route change is the usual cause)
  * import downloads stuck only on a folder-name mismatch when every file clearly maps to an episode/movie
  * rescan Jellyfin when it shows fewer items than Sonarr/Radarr have on disk
  * retry failed MediaSorter imports
  * restart Jellyfin to finish a plugin install or update, once nobody is watching
  * follow every unfinished Seerr request and fix why it's stuck (requestdoctor.py)
  * warn when the nightly settings backup stops happening (backup.py)
  * prove torrents go through the VPN: qBittorrent's public address vs your home one (vpncheck.py)
Windows: a scheduled task runs it hourly. Linux: the Media Hub container runs it hourly.
"""
import calendar
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import arr  # noqa: E402
import stackconf  # noqa: E402

# Rejections that only mean "the download's folder name confused me" - safe to import past.
HARMLESS_REJECTION = re.compile(r"was unexpected considering .* folder name|already imported", re.I)
SCHEDULED = ("Recyclarr", "Health check", "Backup", "Restore")  # tasks that are meant to be idle between runs


class Report:
    def __init__(self):
        self.fixed, self.attention, self.ok = [], [], []

    def fix(self, msg):
        self.fixed.append(msg)
        print("FIXED:", msg)

    def need(self, msg):
        self.attention.append(msg)
        print("ATTENTION:", msg)

    def good(self, msg):
        self.ok.append(msg)
        print("ok:", msg)


def hub_url(cfg, path=""):
    return f"http://127.0.0.1:{cfg['hub']['listen_port']}{path}"


def check_background(rep, cfg):
    if stackconf.docker(cfg):
        down = [svc for svc, c in stackconf.containers(cfg).items() if c["state"] != "running"]
        for svc in down:
            stackconf.restart_container(cfg, svc)
            rep.fix(f"restarted stopped container '{svc}'")
    else:
        down = [name for name, state in stackconf.task_states(cfg).items()
                if state != "Running" and name not in SCHEDULED]
        for name in down:
            stackconf.schtasks("/Run", "/TN", stackconf.task_name(cfg, name))
            rep.fix(f"restarted stopped background app '{name}'")
    if not down:
        rep.good("all background apps running")


def check_hub_apps(rep, cfg):
    try:
        status = requests.get(hub_url(cfg, "/api/status"), timeout=120).json()
    except Exception:
        rep.need("Media Hub isn't responding")
        return None
    for a in status["apps"]:
        if a["status"] == "down":
            requests.post(hub_url(cfg, "/api/action/restart"), params={"arg": a["id"]}, timeout=30)
            rep.fix(f"restarted {a['name']} (wasn't responding)")
    disk = status["system"]["disk"]
    (rep.need if disk["free_gb"] < 200 else rep.good)(f"{disk['free_gb']:,} GB free for the library")
    return status


def check_qbittorrent(rep, cfg):
    if not stackconf.has(cfg, "qbittorrent"):
        return
    try:
        t = stackconf.qbt_get(cfg, "/transfer/info")
        torrents = stackconf.qbt_get(cfg, "/torrents/info")
    except Exception:
        rep.need("qBittorrent's API isn't answering")
        return
    vpn = cfg.get("vpn") or {}
    vpn_up = True
    if vpn.get("mode") == "app" and stackconf.WINDOWS:
        import psutil
        nic = psutil.net_if_stats().get(vpn.get("adapter"))
        vpn_up = bool(nic and nic.isup)
    elif vpn.get("mode") == "gluetun":
        vpn_up = (stackconf.containers(cfg).get("gluetun") or {}).get("health") == "healthy"
    if not vpn_up:
        rep.need("The VPN is disconnected - torrents are paused until it reconnects")
    elif t["dht_nodes"] == 0 and t["connection_status"] != "connected":
        rep.need("VPN is up but qBittorrent has no peers" + (" (MediaSorter restarts it automatically)" if stackconf.WINDOWS else ""))
    else:
        rep.good(f"qBittorrent connected ({t['dht_nodes']} DHT nodes)")
    bad = [x["name"] for x in torrents if x["state"] in ("error", "missingFiles")]
    if bad:
        rep.need(f"{len(bad)} torrent(s) in error: {', '.join(n[:50] for n in bad[:3])}")


def restart_trawl(cfg):
    if stackconf.docker(cfg):
        stackconf.restart_container(cfg, "trawl")
    else:
        stackconf.kill_own(cfg, "bun.exe", "camoufox.exe")  # this install's trawl only
        stackconf.restart_task(cfg, "trawl")
    for _ in range(40):
        time.sleep(3)
        try:
            if requests.get(stackconf.url(cfg, "trawl", "/health"), timeout=5).json()["pool"]["available"] >= 1:
                return
        except Exception:
            pass


def check_indexers(rep, cfg, clients):
    if not stackconf.has(cfg, "prowlarr"):
        return
    p = stackconf.url(cfg, "prowlarr", "/api/v1")
    h = {"X-Api-Key": stackconf.key(cfg, "prowlarr")}
    indexers = {i["id"]: i for i in requests.get(f"{p}/indexer", headers=h, timeout=30).json()}
    tags = {t["id"]: t["label"] for t in requests.get(f"{p}/tag", headers=h, timeout=30).json()}
    disabled = [indexers[s["indexerId"]] for s in requests.get(f"{p}/indexerstatus", headers=h, timeout=30).json()
                if s["indexerId"] in indexers]
    if not indexers:
        rep.need("no indexers yet - add the sites you use in Prowlarr (Indexers > Add Indexer)")
    elif not disabled:
        rep.good(f"{len(indexers)} indexers healthy in Prowlarr")
    trawl_restarted = False
    for idx in disabled:
        def test():
            return requests.post(f"{p}/indexer/test", headers=h, json=idx, timeout=180).ok
        ok = test()
        if not ok and "trawl" in {tags.get(t) for t in idx.get("tags", [])} and not trawl_restarted and stackconf.has(cfg, "trawl"):
            restart_trawl(cfg)  # clears a Cloudflare cookie earned on a different IP
            trawl_restarted = True
            ok = test()
        if ok:
            rep.fix(f"re-enabled indexer {idx['name']}" + (" (after restarting trawl)" if trawl_restarted else ""))
        else:
            rep.need(f"indexer {idx['name']} is failing - check it in Prowlarr")
    # Sonarr/Radarr keep their own failure records for the synced copies.
    for c in clients:
        if any("Indexers unavailable" in x["message"] for x in c.get("health")):
            for i in c.get("indexer"):
                c.s.post(f"{c.url}/api/v3/indexer/test", json=i, timeout=180)
            try:
                c.command({"name": "CheckHealth"}, timeout=180)
            except arr.ArrError:
                rep.good(f"{c.name} is busy (long searches running); its indexer warning is re-checked next hour")
                continue
            if any("Indexers unavailable" in x["message"] for x in c.get("health")):
                rep.need(f"{c.name} still reports unavailable indexers")
            else:
                rep.fix(f"cleared {c.name}'s indexer failure warning")


def check_stuck_imports(rep, clients):
    for c in clients:
        kind = "series" if c.name == "Sonarr" else "movie"
        q = c.get("queue", params={"pageSize": 200})["records"]
        stuck = {r["downloadId"]: r for r in q
                 if r.get("trackedDownloadState") in ("importPending", "importBlocked")
                 and r.get("trackedDownloadStatus") == "warning"}
        for did, r in stuck.items():
            items = c.get("manualimport", params={"downloadId": did, "filterExistingFiles": "true"}, timeout=300)
            todo, safe = [], bool(items)
            for it in items:
                reasons = [x["reason"] for x in it.get("rejections", [])]
                if any("already imported" in x for x in reasons):
                    continue
                matched = it.get("episodes") if kind == "series" else it.get("movie")
                if not matched or not all(HARMLESS_REJECTION.search(x) for x in reasons):
                    safe = False
                    break
                f = {"path": it["path"], "folderName": it.get("folderName", ""), "quality": it["quality"],
                     "languages": it.get("languages") or [], "releaseGroup": it.get("releaseGroup") or "",
                     "indexerFlags": it.get("indexerFlags", 0), "downloadId": did}
                if kind == "series":
                    f.update(seriesId=it["series"]["id"], episodeIds=[e["id"] for e in it["episodes"]],
                             releaseType="multiEpisode" if len(it["episodes"]) > 1 else "singleEpisode")
                else:
                    f.update(movieId=it["movie"]["id"])
                todo.append(f)
            if safe and todo:
                c.command({"name": "ManualImport", "importMode": "auto", "files": todo}, timeout=3600)
                rep.fix(f"imported {len(todo)} file(s) {c.name} was stuck on: {r['title'][:60]}")
            elif not safe:
                rep.need(f"{c.name} needs a manual import decision: {r['title'][:60]} (Activity > Queue in {c.name})")


def check_arr_health(rep, clients):
    for c in clients:
        msgs = [x["message"] for x in c.get("health") if x["type"] in ("error", "warning")
                and "Indexers unavailable" not in x["message"]]
        for m in msgs:
            rep.need(f"{c.name}: {m[:140]}")
        # Failed downloads from the last 24 h that are still blocklisted: worth a look after any outage,
        # since a release that only failed because the network was down won't be tried again.
        blocked = {b.get("sourceTitle") for b in c.get("blocklist", params={"pageSize": 200}).get("records", [])}
        failed = [e for e in c.get("history", params={"pageSize": 100, "sortKey": "date", "sortDirection": "descending"})["records"]
                  if e["eventType"] == "downloadFailed" and e.get("sourceTitle") in blocked
                  and time.time() - calendar.timegm(time.strptime(e["date"][:19], "%Y-%m-%dT%H:%M:%S")) < 86400]
        if failed:
            rep.need(f"{c.name}: {len(failed)} download(s) failed and were blocklisted in the last 24 h - if that was "
                     "during an outage, remove them from Activity > Blocklist so they're retried")
        if not msgs and not failed:
            rep.good(f"{c.name} healthy")


def check_jellyfin(rep, cfg, sonarr, radarr):
    if not stackconf.key(cfg, "jellyfin"):
        return
    h = stackconf.jellyfin_headers(cfg)
    counts = requests.get(stackconf.url(cfg, "jellyfin", "/Items/Counts"), headers=h, timeout=30).json()
    episodes = sum((s.get("statistics") or {}).get("episodeFileCount", 0) for s in sonarr.get("series")) if sonarr else 0
    movies = sum(1 for m in radarr.get("movie") if m.get("hasFile")) if radarr else 0
    if counts["EpisodeCount"] < episodes or counts["MovieCount"] < movies:
        requests.post(stackconf.url(cfg, "jellyfin", "/Library/Refresh"), headers=h, timeout=30)
        rep.fix(f"Jellyfin was missing items ({counts['MovieCount']}/{movies} movies, "
                f"{counts['EpisodeCount']}/{episodes} episodes) - started a library scan")
    else:
        rep.good(f"Jellyfin has everything ({counts['MovieCount']} movies, {counts['EpisodeCount']} episodes)")
    # Plugin installs and updates only take effect after a restart: do it while nobody is watching.
    if requests.get(stackconf.url(cfg, "jellyfin", "/System/Info"), headers=h, timeout=30).json().get("HasPendingRestart"):
        sessions = requests.get(stackconf.url(cfg, "jellyfin", "/Sessions"), headers=h, timeout=30).json()
        if any(s.get("NowPlayingItem") for s in sessions):
            rep.good("Jellyfin has an update/plugin waiting for a restart (after people stop watching)")
        else:
            requests.post(stackconf.url(cfg, "jellyfin", "/System/Restart"), headers=h, timeout=30)
            rep.fix("restarted Jellyfin to finish installing an update or plugin (nobody was watching)")


def check_sorter(rep, cfg):
    db = Path(cfg["state_dir"]) / "state.db"
    if not db.exists():
        return
    con = sqlite3.connect(db, timeout=30)
    failed = con.execute("SELECT COUNT(*) FROM items WHERE status IN ('failed','partial')").fetchone()[0]
    try:
        track_errors = con.execute("SELECT COUNT(*) FROM tracks WHERE result LIKE 'error%'").fetchone()[0]
    except sqlite3.OperationalError:
        track_errors = 0
    con.close()
    if failed:
        (Path(cfg["state_dir"]) / "retry.flag").touch()  # the MediaSorter watcher picks this up within 15 s
        rep.fix(f"retrying {failed} failed manual import(s)")
    if track_errors:
        rep.need(f"language cleanup failed on {track_errors} file(s) - see MediaSorter's log")


def check_seerr(rep, cfg):
    if not stackconf.key(cfg, "seerr"):
        return
    try:
        c = requests.get(stackconf.url(cfg, "seerr", "/api/v1/request/count"),
                         headers={"X-Api-Key": stackconf.key(cfg, "seerr")}, timeout=30).json()
    except Exception:
        return
    if c.get("failed"):
        rep.need(f"Seerr has {c['failed']} failed request(s)")
    if c.get("pending"):
        rep.need(f"Seerr has {c['pending']} request(s) waiting for approval")


def check_vpn_leak(rep, cfg):
    import vpncheck
    r = vpncheck.check(cfg, force=True)
    if r["status"] in ("leak", "warn"):
        rep.need(f"VPN leak check: {r['text']}")
    elif r["status"] == "ok":
        rep.good(f"VPN leak check passed: {r['text']}")


def check_backups(rep, cfg):
    import backup
    made = [b["made"] for b in backup.list_backups(cfg) if b["label"] == "nightly"]
    if not made:
        rep.need("no backup of the apps' settings exists yet - press 'Back up now' in the Media Hub")
    elif time.time() - max(made) > 2 * 86400:
        rep.need(f"the last settings backup is {(time.time() - max(made)) / 86400:.0f} days old")
    else:
        rep.good(f"{len(made)} backup(s), newest {(time.time() - max(made)) / 3600:.0f} h old")


def check_requests(rep, cfg):
    """Why requests aren't here yet, with safe fixes (see requestdoctor.py)."""
    if not stackconf.key(cfg, "seerr"):
        return
    import requestdoctor
    doc = requestdoctor.Doctor(cfg, quiet=True)
    report = doc.run()
    for f in report["fixed"]:
        rep.fix(f)
    for p in report["problems"]:
        rep.need(p)
    for s in doc.newly_stuck():
        rep.need(f"a request has been stuck for days - {s}")
    counts = ", ".join(f"{n} {state}" for state, n in report["counts"].items())
    rep.good(f"{len(report['items'])} request(s) on the way ({counts})" if report["items"] else "every request is available")
    if report["searches"]:
        rep.good(f"started {len(report['searches'])} background search(es) for requests that are still missing")
    for u in report["upgrades"]:
        rep.good(u)


def main():
    rep = Report()
    cfg = stackconf.load()
    sonarr = arr.Sonarr(stackconf.app(cfg, "sonarr")) if stackconf.has(cfg, "sonarr") else None
    radarr = arr.Radarr(stackconf.app(cfg, "radarr")) if stackconf.has(cfg, "radarr") else None
    clients = [c for c in (sonarr, radarr) if c]
    for name, fn in (("background apps", lambda: check_background(rep, cfg)), ("apps", lambda: check_hub_apps(rep, cfg)),
                     ("qbittorrent", lambda: check_qbittorrent(rep, cfg)), ("vpn leak", lambda: check_vpn_leak(rep, cfg)),
                     ("indexers", lambda: check_indexers(rep, cfg, clients)),
                     ("stuck imports", lambda: check_stuck_imports(rep, clients)),
                     ("arr health", lambda: check_arr_health(rep, clients)),
                     ("jellyfin", lambda: check_jellyfin(rep, cfg, sonarr, radarr)),
                     ("sorter", lambda: check_sorter(rep, cfg)), ("seerr", lambda: check_seerr(rep, cfg)),
                     ("backups", lambda: check_backups(rep, cfg)), ("requests", lambda: check_requests(rep, cfg))):
        try:
            fn()
        except Exception as e:
            rep.need(f"health check step '{name}' failed: {e}")

    report = {"time": time.time(), "fixed": rep.fixed, "attention": rep.attention, "ok": rep.ok}
    out = Path(cfg["hub_state_dir"]) if not stackconf.docker(cfg) else Path("/state")
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / "health.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except OSError:
        pass
    if rep.attention:
        title = f"Health check: {len(rep.attention)} thing(s) need you" + (f", fixed {len(rep.fixed)}" if rep.fixed else "")
        message = "; ".join(rep.attention + rep.fixed)
    elif rep.fixed:
        title, message = f"Health check: fixed {len(rep.fixed)} issue(s)", "; ".join(rep.fixed)
    else:
        title, message = "Health check: all good", f"{len(rep.ok)} checks passed"
    try:
        requests.post(hub_url(cfg, "/api/hook/healthcheck"), timeout=10,
                      json={"title": title, "message": message[:900], "attention": bool(rep.attention)})
    except Exception:
        pass
    print("\n" + title)


if __name__ == "__main__":
    main()
