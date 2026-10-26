"""Media Hub - one page for the whole media stack (part of Media Stack Setup).

Shows every app's status, every problem the apps report about themselves, live downloads,
requests and recent imports, a combined error log, and embeds each app's own UI as a tab.
Everything it knows about the install comes from mediastack.json (see stackconf.py).
"""
import glob
import ipaddress
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import psutil
import requests
from flask import Flask, jsonify, redirect, request, send_from_directory, session

HERE = Path(__file__).resolve().parent
SORTER = HERE.parent / "mediasorter"
sys.path.insert(0, str(SORTER))
import stackconf  # noqa: E402

TIMEOUT = 4
CFG = stackconf.load()
STATE = stackconf.own_state(CFG, "hub_state_dir")
DOCKER = stackconf.docker(CFG)

app = Flask(__name__, static_folder=str(HERE / "static"))


def cfg():
    """Settings, re-read when setup changes them (cheap: cached for 10 s)."""
    return cached("cfg", 10, stackconf.load)


def APPS():
    return cfg().get("apps", {})


# --------------------------------------------------------------------------- access control
# Windows ("local-or-tailscale"): no login on this PC; Tailscale devices sign in with a Jellyfin administrator
# account; everything else is refused. Linux server ("lan-login"): the home network and Tailscale sign in.
# The internet is always refused. Logins are checked against Jellyfin and never stored.

KEY_FILE = STATE / "secret.key"
if not KEY_FILE.exists():
    KEY_FILE.write_text(secrets.token_hex(32))
app.secret_key = KEY_FILE.read_text().strip()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30))
TAILSCALE_NETS = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]
_failed = {}  # ip -> [timestamps] for login throttling


def client_ip():
    ip = ipaddress.ip_address(request.remote_addr or "0.0.0.0")
    if ip.version == 6 and ip.ipv4_mapped:
        return ip.ipv4_mapped  # "::ffff:100.x.y.z" from a dual-stack socket
    return ip


def is_local():
    return not DOCKER and client_ip().is_loopback


def is_tailscale():
    return any(client_ip() in n for n in TAILSCALE_NETS)


def allowed():
    if is_local() or is_tailscale():
        return True
    ip = client_ip()
    return CFG["hub"].get("access") == "lan-login" and (ip.is_private or ip.is_loopback)


@app.before_request
def guard():
    if request.path.startswith("/api/hook/"):
        # Webhooks from the apps themselves (same computer / Docker network).
        ip = client_ip()
        return None if ip.is_loopback or ip.is_private else ("Forbidden", 403)
    if is_local():
        return None
    if not allowed():
        return "The Media Hub is only available on your own network and over Tailscale.", 403
    if request.path in ("/login", "/logout") or session.get("user"):
        return None
    if request.path.startswith("/api/"):
        return jsonify(ok=False, message="Sign in required"), 401
    return redirect("/login")


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="theme-color" content="#eef3fc"><title>Media Hub - Sign in</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Google+Sans:wght@400;500;700&display=swap">
<style>
 :root{color-scheme:light;--bg:#eef3fc;--ink:#14203f;--muted:#4b5a7a;--line:#d2dcf0;--err:#b42318}
 *{box-sizing:border-box}
 body{margin:0;min-height:100vh;display:grid;place-items:center;padding:16px;color:var(--ink);
  font:15px/1.6 "Google Sans",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  background:radial-gradient(900px 480px at 50% -160px,#d9e5fb 0%,#eef3fc00 70%),var(--bg)}
 .glass{width:100%;max-width:420px;padding:14px;border-radius:28px;border:1px solid #ffffffd9;
  background:radial-gradient(130% 75% at 12% 0%,#ffffffb3 0%,#fff0 58%),linear-gradient(140deg,#ffffffc4 0%,#ffffff9c 55%,#ffffffb8 100%);
  box-shadow:inset 0 1px 0 #fff,inset 0 0 0 1px #ffffff8c,0 1px 2px #0b14330d,0 16px 40px #0b14331f;
  backdrop-filter:blur(22px) saturate(1.8);-webkit-backdrop-filter:blur(22px) saturate(1.8)}
 form{background:#fff;border:1px solid var(--line);border-radius:22px;box-shadow:0 4px 14px #14203f0f;padding:30px 26px 26px;text-align:center}
 .mark{width:52px;height:52px;border-radius:15px;box-shadow:0 6px 18px #23408f33;margin-bottom:12px}
 h1{font-size:28px;font-weight:650;letter-spacing:-1.2px;margin:0}
 p{color:var(--muted);margin:4px 0 20px;font-size:14px}
 label{display:block;text-align:left;font-size:13px;font-weight:600;color:var(--muted);margin:12px 0 6px 4px}
 input{width:100%;padding:12px 18px;border-radius:999px;border:1px solid var(--line);background:#fff;color:var(--ink);
  font:16px "Google Sans",system-ui,sans-serif;box-shadow:0 2px 8px #14203f0d;outline:0}
 input:focus{border-color:#9db4e6;box-shadow:0 0 0 4px #2f55b51a}
 button{margin-top:22px;width:100%;padding:12px;border-radius:999px;color:#fff;font:700 15px "Google Sans",system-ui,sans-serif;cursor:pointer;
  border:1px solid #ffffff33;background:radial-gradient(120% 100% at 15% 0%,#ffffff38,#fff0 55%),linear-gradient(140deg,#14203fe6,#23408fbf);
  box-shadow:inset 0 1px 0 #ffffff59,inset 0 -1px 0 #0b14334d,0 8px 22px #14203f33}
 .err{color:var(--err);background:#fde7e5;border-radius:999px;font-size:13px;font-weight:600;margin-top:14px;padding:7px 14px}
</style></head><body><div class="glass"><form method="post">
<svg class="mark" viewBox="0 0 64 64"><defs><linearGradient id="b" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2f55b5"/><stop offset="1" stop-color="#23408f"/></linearGradient></defs>
<rect width="64" height="64" rx="16" fill="url(#b)"/><rect x="14" y="20" width="34" height="24" rx="5" fill="#fff"/><path d="M27 26.5v11l9.5-5.5z" fill="#23408f"/>
<path d="M25 50h12" stroke="#fff" stroke-width="4" stroke-linecap="round"/><circle cx="51" cy="13" r="10" fill="#ff8a1f" opacity=".28"/>
<circle cx="51" cy="13" r="5.4" fill="#ff8a1f" stroke="#fff" stroke-width="1.8"/></svg>
<h1>Media Hub</h1><p>Sign in with your Jellyfin administrator account.</p>
<label for="u">Username</label><input id="u" name="username" autocomplete="username" autocapitalize="none" required autofocus>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password">
<button>Sign in</button>{error}</form></div></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return LOGIN_PAGE.replace("{error}", "")
    ip = str(client_ip())
    recent = [t for t in _failed.get(ip, []) if time.time() - t < 600]
    if len(recent) >= 5:
        return LOGIN_PAGE.replace("{error}", '<div class="err">Too many attempts - try again in 10 minutes.</div>'), 429
    auth = 'MediaBrowser Client="Media Hub", Device="Media Hub", DeviceId="media-hub-login", Version="1.0"'
    try:
        r = requests.post(url("jellyfin", "/Users/AuthenticateByName"), headers={"Authorization": auth}, timeout=15,
                          json={"Username": request.form.get("username", ""), "Pw": request.form.get("password", "")})
    except requests.RequestException:
        return LOGIN_PAGE.replace("{error}", '<div class="err">Jellyfin is not responding.</div>'), 503
    ok = r.status_code == 200 and r.json().get("User", {}).get("Policy", {}).get("IsAdministrator")
    if r.status_code == 200:  # don't leave a Jellyfin session behind for every hub login
        try:
            requests.post(url("jellyfin", "/Sessions/Logout"), timeout=5,
                          headers={"Authorization": f'{auth}, Token="{r.json()["AccessToken"]}"'})
        except requests.RequestException:
            pass
    if not ok:
        _failed[ip] = recent + [time.time()]
        msg = "Only Jellyfin administrators can use the Media Hub." if r.status_code == 200 else "Wrong username or password."
        return LOGIN_PAGE.replace("{error}", f'<div class="err">{msg}</div>'), 401
    session.permanent = True
    session["user"] = r.json()["User"]["Name"]
    _failed.pop(ip, None)
    return redirect("/")


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.get("/api/me")
def me():
    return jsonify(local=is_local(), user=session.get("user"))


# --------------------------------------------------------------------------- helpers

def keys():
    return {a: info.get("key") for a, info in APPS().items()}


def url(app_id, path=""):
    return APPS().get(app_id, {}).get("url", "http://127.0.0.1").rstrip("/") + path


def get(app_id, path, **kw):
    kw.setdefault("timeout", TIMEOUT)
    r = requests.get(url(app_id, path), **kw)
    r.raise_for_status()
    return r.json() if r.content else None


_cache = {}


def cached(key, seconds, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def process_states():
    """App id -> "Running"/"Stopped"/... from Task Scheduler (Windows) or Docker (Linux)."""
    def fetch():
        c = cfg()
        if DOCKER:
            try:
                cons = stackconf.containers(c)
            except Exception:
                return {}
            return {a: ("Running" if (cons.get(info.get("service")) or {}).get("state") == "running" else
                        (cons.get(info.get("service")) or {}).get("state", "Missing").title())
                    for a, info in c["apps"].items() if info.get("service")}
        tasks = stackconf.task_states(c)
        return {a: tasks.get(info.get("task"), "Missing") for a, info in c["apps"].items() if info.get("task")}
    return cached("procs", 20, fetch)


# --------------------------------------------------------------------------- per-app checks
# Each returns {"status": ok|warn|error|down|setup, "version", "summary", "problems": [...], ...}

def arr_check(app_id, k, api="v3"):
    h = {"X-Api-Key": k[app_id]}
    st = get(app_id, f"/api/{api}/system/status", headers=h)
    problems = [{"level": "error" if x["type"] == "error" else "warn", "text": x["message"],
                 "link": x.get("wikiUrl")} for x in get(app_id, f"/api/{api}/health", headers=h)]
    info = {"version": st.get("version"), "problems": problems}
    if app_id in ("sonarr", "radarr"):
        q = get(app_id, f"/api/{api}/queue", headers=h, params={"pageSize": 50, "includeUnknownSeriesItems": "true",
                                                                "includeUnknownMovieItems": "true"})
        recs = q.get("records", [])
        info["queue"] = [{
            "title": r.get("title"), "status": r.get("trackedDownloadState") or r.get("status"),
            "progress": round(100 * (1 - r["sizeleft"] / r["size"])) if r.get("size") else 0,
            "timeleft": r.get("timeleft"),
            "warning": "; ".join(m for s in r.get("statusMessages", []) for m in s.get("messages", [])) or None,
        } for r in recs]
        for r in info["queue"]:
            if r["warning"]:
                problems.append({"level": "warn", "text": f"{r['title']}: {r['warning']}"})
        missing = get(app_id, f"/api/{api}/wanted/missing", headers=h, params={"pageSize": 1})
        info["missing"] = missing.get("totalRecords", 0)
        info["summary"] = f"{len(recs)} downloading · {info['missing']} missing"
    if app_id == "prowlarr":
        idx = get(app_id, "/api/v1/indexer", headers=h)
        enabled = [i for i in idx if i.get("enable")]
        info["summary"] = f"{len(enabled)} indexers"
        if not enabled:
            problems.append({"level": "warn", "text": "No indexers yet - add the ones you use in Prowlarr "
                                                      "(Indexers > Add). They sync to Sonarr/Radarr automatically."})
    return info


def check_jellyfin(k):
    pub = get("jellyfin", "/System/Info/Public")
    info = {"version": pub.get("Version"), "problems": []}
    if not k.get("jellyfin"):
        return {**info, "status": "setup", "summary": "Run the Jellyfin step in Media Stack Setup"}
    h = {"Authorization": f'MediaBrowser Token="{k["jellyfin"]}"'}
    sysinfo = get("jellyfin", "/System/Info", headers=h)
    counts = get("jellyfin", "/Items/Counts", headers=h)
    sessions = [s for s in get("jellyfin", "/Sessions", headers=h) if s.get("NowPlayingItem")]
    info["nowPlaying"] = [{"user": s.get("UserName"), "title": s["NowPlayingItem"].get("Name"),
                           "series": s["NowPlayingItem"].get("SeriesName"), "client": s.get("Client"),
                           "transcoding": bool(s.get("TranscodingInfo"))} for s in sessions]
    if sysinfo.get("HasPendingRestart"):
        info["problems"].append({"level": "warn", "text": "Jellyfin needs a restart to apply updates.", "action": "restart:jellyfin"})
    info["summary"] = (f"{counts.get('MovieCount', 0)} movies · {counts.get('SeriesCount', 0)} shows"
                       + (f" · {len(sessions)} playing" if sessions else ""))
    return info


def check_seerr(k):
    st = get("seerr", "/api/v1/status")
    pub = get("seerr", "/api/v1/settings/public")
    info = {"version": st.get("version"), "problems": []}
    if not pub.get("initialized") or not k.get("seerr"):
        return {**info, "status": "setup", "summary": "Run the Seerr step in Media Stack Setup"}
    h = {"X-Api-Key": k["seerr"]}
    c = get("seerr", "/api/v1/request/count", headers=h)
    reqs = get("seerr", "/api/v1/request", headers=h, params={"take": 12, "sort": "added"}).get("results", [])
    status_names = {1: "Pending approval", 2: "Approved", 3: "Declined", 4: "Failed", 5: "Completed"}
    info["requests"] = []
    for r in reqs:
        media = r.get("media") or {}
        t = media_title(r.get("type"), media.get("tmdbId"))
        status = status_names.get(r.get("status"), "?")
        if r.get("status") == 2:  # approved: show how far the download has got instead
            status = MEDIA_STATUS.get(media.get("status"), "Approved")
        info["requests"].append({"id": r.get("id"), "type": r.get("type"), "status": status,
                                 "pending": r.get("status") == 1, **t,
                                 "by": (r.get("requestedBy") or {}).get("displayName"), "is4k": r.get("is4k")})
    if c.get("pending"):
        info["problems"].append({"level": "warn", "text": f"{c['pending']} request(s) waiting for your approval in Seerr."})
    info["summary"] = f"{c.get('pending', 0)} pending · {c.get('processing', 0)} downloading · {c.get('available', 0)} available"
    return info


def check_qbittorrent(k):
    c = cfg()
    ver = stackconf.qbt_get(c, "/app/version", timeout=TIMEOUT)
    tr = stackconf.qbt_get(c, "/transfer/info", timeout=TIMEOUT)
    torrents = stackconf.qbt_get(c, "/torrents/info", timeout=TIMEOUT)
    problems = []
    if tr.get("connection_status") == "disconnected":
        problems.append({"level": "error", "text": "qBittorrent has no network - connect your VPN "
                                                   "(torrents only run through the VPN)."})
    for t in [t for t in torrents if t["state"] in ("error", "missingFiles")]:
        problems.append({"level": "error", "text": f"Torrent error: {t['name']} ({t['state']})"})
    active = [t for t in torrents if t["progress"] < 1]
    return {
        "version": ver, "problems": problems,
        "summary": f"{len(active)} downloading · ↓ {tr['dl_info_speed']/2**20:.1f} MB/s · ↑ {tr['up_info_speed']/2**20:.1f} MB/s",
        "connection": tr.get("connection_status"),
        "torrents": [{"name": t["name"], "progress": round(t["progress"] * 100, 1), "state": t["state"],
                      "category": t.get("category"), "dlspeed": t["dlspeed"], "eta": t["eta"], "size": t["size"],
                      "seeds": t["num_seeds"], "peers": t["num_leechs"]}
                     for t in sorted(torrents, key=lambda t: (t["progress"] >= 1, -t["added_on"]))[:40]],
    }


def check_bazarr(k):
    h = {"X-API-KEY": k["bazarr"]}
    st = get("bazarr", "/api/system/status", headers=h).get("data", {})
    health = get("bazarr", "/api/system/health", headers=h).get("data", [])
    badges = get("bazarr", "/api/badges", headers=h)
    problems = [{"level": "warn", "text": f"{x.get('object')}: {x.get('issue')}"} for x in health]
    return {"version": st.get("bazarr_version"), "problems": problems,
            "summary": f"{badges.get('episodes', 0)} episodes · {badges.get('movies', 0)} movies missing subs"}


def check_cleanuparr(k):
    st = get("cleanuparr", "/api/auth/status")
    if not st.get("setupCompleted"):
        return {"status": "setup", "summary": "Run the Cleanuparr step in Media Stack Setup", "problems": []}
    return {"summary": "Removing stalled & fake downloads", "problems": []}


def check_autobrr(k):
    requests.get(url("autobrr", "/api/healthz/liveness"), timeout=TIMEOUT).raise_for_status()
    if requests.get(url("autobrr", "/api/auth/onboard"), timeout=TIMEOUT).status_code in (200, 204):  # 204 = no account yet
        return {"status": "setup", "summary": "Run the autobrr step in Media Stack Setup", "problems": []}
    problems, summary = [], "Idle - add IRC-announce trackers to use it"
    if k.get("autobrr"):
        nets = get("autobrr", "/api/irc", headers={"X-API-Token": k["autobrr"]}) or []
        for n in nets:
            if n.get("enabled") and not n.get("healthy", True):
                problems.append({"level": "warn", "text": f"autobrr IRC network {n.get('name')} is disconnected"})
        if nets:
            summary = f"{len(nets)} IRC networks"
    return {"summary": summary, "problems": problems}


def check_trawl(k):
    h = get("trawl", "/health")
    pool = h.get("pool", {})
    problems = []
    if pool.get("stalled"):
        problems.append({"level": "warn", "text": f"trawl has {pool['stalled']} stalled browser(s) - they recycle "
                                                  "automatically; restart trawl if it persists", "action": "restart:trawl"})
    if not pool.get("live"):
        problems.append({"level": "error", "text": "trawl has no running browser", "action": "restart:trawl"})
    summary = f"{pool.get('available', 0)}/{pool.get('total', 0)} browser ready"
    if pool.get("busy"):
        summary += f" · {pool['busy']} solving"
    if pool.get("restarts"):
        summary += f" · {pool['restarts']} restarts"
    return {"version": h.get("version"), "summary": summary, "problems": problems}


def check_jfago(k):
    r = requests.get(url("jfago", "/"), timeout=TIMEOUT)
    r.raise_for_status()
    # Until its setup wizard is finished, jfa-go serves the wizard page.
    if "<title>Setup - jfa-go</title>" in r.text:
        return {"status": "setup", "summary": "Open it and finish the setup wizard", "problems": []}
    return {"summary": "Invite links for friends & family", "problems": []}


def check_requestrr(k):
    requests.get(url("requestrr", "/"), timeout=TIMEOUT).raise_for_status()
    return {"summary": "Discord bot for requests", "problems": []}


def check_byparr(k):
    # Byparr's /health launches a whole browser, so check it every 5 minutes, not every refresh.
    st = cached("byparr-health", 300, lambda: get("byparr", "/health", timeout=30))
    return {"summary": st.get("msg", ""), "problems": []}


def check_unpackerr(k):
    return {"summary": "Extracts .rar releases automatically", "problems": []}


def check_recyclarr(k):
    return {"summary": "Re-syncs TRaSH quality profiles weekly", "problems": []}


def check_gluetun(k):
    c = (stackconf.containers(cfg()).get("gluetun") or {}) if DOCKER else {}
    if c.get("health") == "unhealthy":
        return {"status": "error", "summary": "VPN disconnected", "problems": [
            {"level": "error", "text": "The VPN container can't connect - torrents are paused", "action": "restart:gluetun"}]}
    return {"summary": "Torrents go through the VPN", "problems": []}


def sorter_db():
    return Path(cfg()["state_dir"]) / "state.db"


def check_mediasorter(k):
    if not sorter_db().exists():
        return {"summary": "Waiting for the first download", "problems": [], "recent": []}
    con = sqlite3.connect(sorter_db(), timeout=10)
    rows = con.execute("SELECT ts, status, category, path, detail FROM items ORDER BY ts DESC LIMIT 25").fetchall()
    con.close()
    recent = [{"time": ts, "status": st, "category": cat or "auto", "name": Path(p).name, "detail": d}
              for ts, st, cat, p, d in rows]
    failed = [r for r in recent if r["status"] in ("failed", "partial")]
    problems = [{"level": "error", "text": f"Couldn't sort {r['name']}: {r['detail']}", "action": "retry-imports"}
                for r in failed]
    return {"summary": f"{len([r for r in recent if r['status'] == 'done'])} recent imports", "problems": problems,
            "recent": recent}


CHECKS = {
    "jellyfin": check_jellyfin, "seerr": check_seerr,
    "sonarr": lambda k: arr_check("sonarr", k), "radarr": lambda k: arr_check("radarr", k),
    "prowlarr": lambda k: arr_check("prowlarr", k, "v1"),
    "qbittorrent": check_qbittorrent, "bazarr": check_bazarr, "cleanuparr": check_cleanuparr,
    "autobrr": check_autobrr, "jfago": check_jfago, "requestrr": check_requestrr, "trawl": check_trawl,
    "byparr": check_byparr, "unpackerr": check_unpackerr, "mediasorter": check_mediasorter,
    "recyclarr": check_recyclarr, "gluetun": check_gluetun,
}
# Background-only apps: "not running" comes from Task Scheduler / Docker, not a web page.
BACKGROUND = ("unpackerr", "mediasorter", "recyclarr", "gluetun")


def run_check(app_id, k):
    meta = APPS()[app_id]
    base = {"id": app_id, "name": meta["name"], "role": meta.get("role", ""), "port": meta.get("port"),
            "embed": bool(meta.get("embed")), "remote": bool(meta.get("remote")), "link": meta.get("link"),
            "local_only": bool(meta.get("local_only"))}
    proc_state = process_states().get(app_id)
    base["process"] = proc_state
    try:
        info = CHECKS[app_id](k)
        info.setdefault("status", "error" if any(p["level"] == "error" for p in info.get("problems", []))
                        else "warn" if info.get("problems") else "ok")
    except Exception as e:
        down = proc_state not in ("Running",)
        info = {"status": "down", "summary": "Not running" if down else "Not responding", "problems": [{
            "level": "error", "text": f"{meta['name']} is {'not running' if down else 'not responding'} ({type(e).__name__})",
            "action": f"restart:{app_id}"}]}
    if app_id in BACKGROUND and app_id != "recyclarr" and proc_state not in ("Running", None) and info["status"] != "down":
        info["status"] = "down"
        info["problems"].append({"level": "error", "text": f"{meta['name']} is {proc_state.lower()}",
                                 "action": f"restart:{app_id}"})
    return {**base, **info}


def library_root():
    lib = cfg()["library"]
    p = Path(lib["movies"])
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def system_checks():
    c = cfg()
    problems = []
    du = shutil.disk_usage(library_root())
    free_gb = du.free / 2**30
    if free_gb < 200:
        problems.append({"level": "error" if free_gb < 50 else "warn", "text": f"Only {free_gb:.0f} GB free for the library"})
    vpn = c.get("vpn") or {}
    vpn_up = None
    if vpn.get("mode") == "app" and not DOCKER:
        nic = psutil.net_if_stats().get(vpn.get("adapter"))
        vpn_up = bool(nic and nic.isup)
    elif vpn.get("mode") == "gluetun":
        try:
            vpn_up = (stackconf.containers(c).get("gluetun") or {}).get("health") == "healthy"
        except Exception:
            vpn_up = None
    if vpn_up is False:
        problems.append({"level": "warn", "text": "The VPN is disconnected - torrents are paused until it connects "
                                                  "(qBittorrent only uses the VPN)."})
    return {"disk": {"free_gb": round(free_gb), "total_gb": round(du.total / 2**30)}, "vpn": vpn_up,
            "problems": problems}


_status_lock = threading.Lock()


def server_addresses():
    """How other devices reach this server: Tailscale name/IP (anywhere) and home-network IP."""
    def fetch():
        c = cfg()
        ts = {"name": c.get("tailscale_name"), "ip": c.get("tailscale_ip"), "online": None}
        if not DOCKER:
            exe = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe"
            try:
                out = subprocess.run([str(exe), "status", "--json"], capture_output=True, text=True, timeout=10,
                                     creationflags=stackconf.NO_WINDOW).stdout
                self_ = json.loads(out).get("Self", {})
                ts = {"name": (self_.get("DNSName") or "").split(".")[0] or ts["name"],
                      "ip": (self_.get("TailscaleIPs") or [None])[0] or ts["ip"], "online": bool(self_.get("Online"))}
            except Exception:
                pass
        lan = c.get("lan_ip")
        services = []
        for app_id, use, home in (("jellyfin", "Server address for the Jellyfin app (phone, TV) or a browser", True),
                                  ("seerr", "Request movies & shows in a browser", True),
                                  ("hub", "This dashboard, in a browser only (not the Jellyfin app)", DOCKER),
                                  ("jfago", "Friend & family invite links", True)):
            a = c["apps"].get(app_id)
            if not a or not a.get("port"):
                continue
            p = a["port"]
            services.append({"name": a["name"], "port": p, "use": use,
                             "anywhere": f"http://{ts['name']}:{p}" if ts.get("name") else None,
                             "anywhere_ip": f"http://{ts['ip']}:{p}" if ts.get("ip") else None,
                             "home": f"http://{lan}:{p}" if lan and home else None})
        return {"tailscale": ts if ts.get("name") or ts.get("ip") else {}, "lan": lan, "services": services}
    return cached("addresses", 60, fetch)


@app.get("/api/addresses")
def addresses():
    return jsonify(server_addresses())


@app.get("/api/status")
def status():
    with _status_lock:
        def compute():
            k = keys()
            ids = [a for a in APPS() if a in CHECKS]
            with ThreadPoolExecutor(max(len(ids), 1)) as ex:
                apps = list(ex.map(lambda a: run_check(a, k), ids))
            # "ui" changes whenever the page is edited, so tabs left open reload instead of running stale code.
            return {"apps": apps, "system": system_checks(), "time": time.time(),
                    "ui": (HERE / "static" / "index.html").stat().st_mtime, "name": cfg().get("name"),
                    "platform": cfg().get("platform")}
        return jsonify(cached("status", 5, compute))


# --------------------------------------------------------------------------- logs

LEVEL_RE = [("error", re.compile(r"\|Error\||\|Fatal\||\[ERR\]|\bERROR\b|\bFATAL\b|\bCRITICAL\b| ERR\]|^\(C\)|\[error\]|level=error", re.I)),
            ("warn", re.compile(r"\|Warn\||\[WRN\]|\bWARN(ING)?\b| WRN\]|^\(W\)|\[warn\]|level=warn", re.I))]


def latest(pattern):
    files = glob.glob(pattern)
    return max(files, key=os.path.getmtime) if files else None


def tail(path, max_bytes=400_000):
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - max_bytes))
        return f.read().decode("utf-8", "replace").splitlines()[1:]


def log_lines(meta):
    where = meta.get("log") or ""
    if where.startswith("docker:"):
        return stackconf.container_logs(cfg(), where[7:], 3000)
    path = latest(where) if where else None
    return tail(path) if path else []


@app.get("/api/logs")
def logs():
    only = request.args.get("app")
    level = request.args.get("level", "warn")
    out = []
    for app_id, meta in APPS().items():
        if only and only != app_id:
            continue
        try:
            lines = log_lines(meta)[-3000:]
        except Exception:
            continue
        for line in lines:
            lvl = next((name for name, rx in LEVEL_RE if rx.search(line)), "info")
            if level == "all" or lvl == "error" or (level == "warn" and lvl == "warn"):
                out.append({"app": meta["name"], "level": lvl, "line": line[:600]})
    return jsonify({"lines": out[-400:] if only else sorted_tail(out)})


def sorted_tail(lines):
    # Keep the newest ~60 lines per app so one noisy app can't hide the others.
    per = {}
    for l in lines:
        per.setdefault(l["app"], []).append(l)
    return [l for group in per.values() for l in group[-60:]]


# --------------------------------------------------------------------------- events (webhooks from the apps)
# Sonarr, Radarr, Prowlarr and Seerr post here; the hub keeps a timeline (and pops Windows notifications).

EVENTS_DB = STATE / "events.db"
SETTINGS_FILE = STATE / "hub.json"
_db_lock = threading.Lock()


def hub_settings():
    try:
        return {"notifications": not DOCKER, **json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))}
    except Exception:
        return {"notifications": not DOCKER}


def events_db():
    con = sqlite3.connect(EVENTS_DB, timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, source TEXT, kind TEXT,
                   level TEXT, title TEXT, message TEXT, image TEXT)""")
    return con


def notify(title, message, image=None):
    if DOCKER or not hub_settings()["notifications"]:
        return

    def show():
        try:
            from win11toast import notify as toast
            kw = {"on_click": f"http://localhost:{CFG['hub']['public_port']}", "app_id": "Media Hub"}
            if image and image.startswith("http"):
                kw["image"] = {"src": image, "placement": "hero"}
            toast(title, message, **kw)
        except Exception as e:
            print("notification failed:", e)
    threading.Thread(target=show, daemon=True).start()


def add_event(source, kind, level, title, message="", image=None, toast=False):
    with _db_lock:
        con = events_db()
        con.execute("INSERT INTO events(ts, source, kind, level, title, message, image) VALUES(?,?,?,?,?,?,?)",
                    (time.time(), source, kind, level, title, message, image))
        con.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 2000)")
        con.commit()
        con.close()
    if toast:
        notify(title, message, image)


def ep_label(episodes):
    eps = episodes or []
    if not eps:
        return ""
    first = eps[0]
    tag = f"S{first.get('seasonNumber', 0):02d}E{first.get('episodeNumber', 0):02d}"
    if len(eps) > 1:
        tag += f"-E{eps[-1].get('episodeNumber', 0):02d}"
    return tag


def poster(item):
    for img in (item or {}).get("images") or []:
        if img.get("coverType") == "poster":
            return img.get("remoteUrl") or img.get("url")
    return None


def parse_arr(source, p):
    """Turn a Sonarr/Radarr/Prowlarr webhook into (kind, level, title, message, image, toast)."""
    kind = p.get("eventType", "Unknown")
    app_name = source.title()
    if source == "sonarr":
        media = p.get("series") or {}
        name = " ".join(x for x in (media.get("title"), ep_label(p.get("episodes"))) if x)
    else:
        media = p.get("movie") or p.get("remoteMovie") or {}
        name = media.get("title", "")
        if media.get("year"):
            name += f" ({media['year']})"
    img = poster(media)
    quality = ((p.get("release") or {}).get("quality") or (p.get("episodeFile") or p.get("movieFile") or {}).get("quality") or "")
    if kind == "Grab":
        return kind, "info", f"Downloading {name}", f"{quality} via {(p.get('release') or {}).get('indexer', '')}".strip(), img, False
    if kind == "Download":
        if p.get("isUpgrade"):
            return "Upgrade", "info", f"Upgraded {name}", f"Now {quality}", img, True
        return kind, "info", f"Ready to watch: {name}", f"{quality} · added to Jellyfin", img, True
    if kind == "ManualInteractionRequired":
        return kind, "warn", f"{app_name} needs you: {name}", p.get("downloadStatusMessages") and \
            "; ".join(m for s in p["downloadStatusMessages"] for m in s.get("messages", [])) or "Manual import needed", img, True
    if kind in ("Health", "HealthIssue"):
        lvl = "error" if (p.get("level") or "").lower() == "error" else "warn"
        return kind, lvl, f"{app_name}: problem", p.get("message", ""), None, lvl == "error"
    if kind == "HealthRestored":
        return kind, "info", f"{app_name}: problem resolved", p.get("message", ""), None, False
    if kind in ("SeriesAdd", "MovieAdded"):
        return kind, "info", f"Added {name}", f"{app_name} is now tracking it", img, False
    if kind == "Test":
        return kind, "info", f"Test from {app_name}", "Media Hub is receiving events", None, True
    return kind, "info", f"{app_name}: {kind}", name, img, False


SEERR_KINDS = {
    "MEDIA_PENDING": ("warn", "Request waiting for approval", True),
    "MEDIA_AUTO_APPROVED": ("info", "Requested", False),
    "MEDIA_APPROVED": ("info", "Request approved", False),
    "MEDIA_DECLINED": ("warn", "Request declined", False),
    "MEDIA_AVAILABLE": ("info", "Request now available", False),  # Sonarr/Radarr already announce the import
    "MEDIA_FAILED": ("error", "Request failed", True),
    "ISSUE_CREATED": ("warn", "Issue reported", True),
    "TEST_NOTIFICATION": ("info", "Test from Seerr", True),
}


def jellyfin_has(path):
    h = {"Authorization": f'MediaBrowser Token="{keys().get("jellyfin")}"'}
    name = Path(path).stem
    items = requests.get(url("jellyfin", "/Items"), headers=h, timeout=15, params={
        "Recursive": "true", "IncludeItemTypes": "Movie,Episode", "Fields": "Path", "searchTerm": name[:40]}).json()
    if any((i.get("Path") or "").lower() == path.lower() for i in items.get("Items", [])):
        return True
    # Episode titles rarely match the file name, so fall back to the newest episodes.
    recent = requests.get(url("jellyfin", "/Items"), headers=h, timeout=15, params={
        "Recursive": "true", "IncludeItemTypes": "Movie,Episode", "Fields": "Path", "SortBy": "DateCreated",
        "SortOrder": "Descending", "Limit": 200}).json()
    return any((i.get("Path") or "").lower() == path.lower() for i in recent.get("Items", []))


def ensure_in_jellyfin(source, title, folder, file_path):
    """Radarr/Sonarr tell Jellyfin about imports, but Jellyfin doesn't always act on it.
    Nudge it, check after 90 s, and run a library scan if the file still isn't there."""
    k = keys().get("jellyfin")
    if not k:
        return
    h = {"Authorization": f'MediaBrowser Token="{k}"'}
    try:
        requests.post(url("jellyfin", "/Library/Media/Updated"), headers=h, timeout=15,
                      json={"Updates": [{"Path": folder, "UpdateType": "Created"}]})
        time.sleep(90)
        if file_path and jellyfin_has(file_path):
            return
        requests.post(url("jellyfin", "/Library/Refresh"), headers=h, timeout=15)
        add_event(source, "JellyfinRescan", "info", f"Rescanned Jellyfin for {title}",
                  "Jellyfin hadn't picked up the new file on its own, so the hub ran a library scan.")
    except Exception as e:
        print("jellyfin check failed:", e)


@app.post("/api/hook/<source>")
def hook(source):
    p = request.get_json(force=True, silent=True) or {}
    if source in ("sonarr", "radarr", "prowlarr"):
        add_event(source, *parse_arr(source, p))
        if source in ("sonarr", "radarr") and p.get("eventType") == "Download":
            media = p.get("series") or p.get("movie") or {}
            folder = media.get("path") or media.get("folderPath")
            files = p.get("episodeFiles") or [p.get("episodeFile") or p.get("movieFile") or {}]
            file_path = (files[0] or {}).get("path") if files else None
            if folder:
                threading.Thread(target=ensure_in_jellyfin, daemon=True,
                                 args=(source, media.get("title", ""), folder, file_path)).start()
    elif source == "seerr":
        level, label, toast = SEERR_KINDS.get(p.get("notification_type"), ("info", p.get("event") or "Seerr", False))
        who = p.get("requested_by")
        msg = p.get("message", "") + (f" (by {who})" if who and who not in p.get("message", "") else "")
        add_event("seerr", p.get("notification_type", "?"), level, f"{label}: {p.get('subject', '')}", msg.strip(),
                  p.get("image") or None, toast)
    elif source == "healthcheck":
        # Quiet when all is well; a desktop notification only when something needs the user.
        needs = bool(p.get("attention"))
        add_event(source, "HealthCheck", "warn" if needs else "info", p.get("title", "Health check"),
                  p.get("message", ""), None, needs)
    else:
        add_event(source, "custom", "info", p.get("title", source), p.get("message", ""))
    return jsonify(ok=True)


@app.get("/api/events")
def events():
    con = events_db()
    rows = con.execute("SELECT ts, source, kind, level, title, message, image FROM events ORDER BY id DESC LIMIT ?",
                       (min(int(request.args.get("limit", 60)), 500),)).fetchall()
    con.close()
    return jsonify([dict(zip(("time", "source", "kind", "level", "title", "message", "image"), r)) for r in rows])


@app.route("/api/settings", methods=["GET", "POST"])
def settings_route():
    s = hub_settings()
    if request.method == "POST":
        s.update({k: v for k, v in (request.get_json(force=True) or {}).items() if k in ("notifications",)})
        SETTINGS_FILE.write_text(json.dumps(s), encoding="utf-8")
    return jsonify({**s, "can_notify": not DOCKER})


# --------------------------------------------------------------------------- Seerr (search & request from the hub)

TMDB_IMG = "https://image.tmdb.org/t/p/w342"
MEDIA_STATUS = {2: "Requested", 3: "Downloading", 4: "Partly available", 5: "Available"}
_titles = {}


def seerr(method, path, **kw):
    k = keys().get("seerr")
    if not k:
        raise RuntimeError("Seerr isn't set up yet - run the Seerr step in Media Stack Setup")
    kw.setdefault("timeout", 20)
    r = requests.request(method, url("seerr", f"/api/v1{path}"), headers={"X-Api-Key": k}, **kw)
    r.raise_for_status()
    return r.json() if r.content else None


def media_title(media_type, tmdb_id):
    """Title/year/poster for a TMDB id, via Seerr (cached for the life of the hub)."""
    k = (media_type, tmdb_id)
    if k not in _titles:
        try:
            d = seerr("GET", f"/{'movie' if media_type == 'movie' else 'tv'}/{tmdb_id}", timeout=8)
            date = d.get("releaseDate") or d.get("firstAirDate") or ""
            _titles[k] = {"title": d.get("title") or d.get("name"), "year": date[:4],
                          "poster": TMDB_IMG + d["posterPath"] if d.get("posterPath") else None}
        except Exception:
            return {"title": f"TMDB #{tmdb_id}", "year": "", "poster": None}
    return _titles[k]


def search_item(r):
    if r.get("mediaType") not in ("movie", "tv"):
        return None
    date = r.get("releaseDate") or r.get("firstAirDate") or ""
    info = r.get("mediaInfo") or {}
    return {"id": r["id"], "type": r["mediaType"], "title": r.get("title") or r.get("name"), "year": date[:4],
            "overview": (r.get("overview") or "")[:280],
            "poster": TMDB_IMG + r["posterPath"] if r.get("posterPath") else None,
            "status": MEDIA_STATUS.get(info.get("status"))}


@app.get("/api/seerr/search")
def seerr_search():
    q = request.args.get("q", "").strip()
    try:
        if q:
            # Seerr mis-reads '+' in queries, so encode spaces as %20 ourselves.
            data = seerr("GET", f"/search?query={requests.utils.quote(q, safe='')}&page=1")
        else:
            data = {"results": (seerr("GET", "/discover/trending?page=1") or {}).get("results", [])}
        return jsonify(ok=True, results=[x for x in map(search_item, data.get("results", [])) if x])
    except Exception as e:
        return jsonify(ok=False, message=str(e), results=[])


@app.post("/api/seerr/request")
def seerr_request():
    body = request.get_json(force=True) or {}
    payload = {"mediaType": body["type"], "mediaId": int(body["id"])}
    if body["type"] == "tv":
        payload["seasons"] = "all"
    try:
        seerr("POST", "/request", json=payload)
        t = media_title(body["type"], int(body["id"]))
        return jsonify(ok=True, message=f"Requested {t['title']} - it will download automatically")
    except requests.HTTPError as e:
        return jsonify(ok=False, message=f"Seerr said: {e.response.text[:200]}")
    except Exception as e:
        return jsonify(ok=False, message=str(e))


@app.post("/api/seerr/<int:req_id>/<verb>")
def seerr_decide(req_id, verb):
    if verb not in ("approve", "decline"):
        return jsonify(ok=False, message="Unknown action"), 400
    try:
        seerr("POST", f"/request/{req_id}/{verb}")
        return jsonify(ok=True, message=f"Request {'approved' if verb == 'approve' else 'declined'}")
    except Exception as e:
        return jsonify(ok=False, message=str(e))


# --------------------------------------------------------------------------- actions

def run_healthcheck():
    if DOCKER:
        import healthcheck
        threading.Thread(target=healthcheck.main, daemon=True).start()
    else:
        subprocess.Popen([sys.executable, str(SORTER / "healthcheck.py")], creationflags=stackconf.NO_WINDOW)


def restart_app(app_id):
    c = cfg()
    meta = c["apps"][app_id]
    if DOCKER:
        stackconf.restart_container(c, meta["service"])
    elif app_id == "qbittorrent":
        try:
            stackconf.qbt_post(c, "/app/shutdown", timeout=5)
            time.sleep(5)
        except Exception:
            pass
        stackconf.restart_task(c, meta["task"])
    else:
        stackconf.restart_task(c, meta["task"])


@app.post("/api/action/<action>")
def action(action):
    c = cfg()
    k = keys()
    arg = request.args.get("arg", "")
    if action == "healthcheck":
        run_healthcheck()
        return jsonify(ok=True, message="Health check started - results appear in Activity in a minute or two")
    if action == "retry-imports":
        (Path(c["state_dir"]) / "retry.flag").touch()  # MediaSorter's watcher picks this up within 15 s
        return jsonify(ok=True, message="Re-trying failed imports")
    if action == "search-missing" and arg in ("sonarr", "radarr") and arg in c["apps"]:
        name = "MissingEpisodeSearch" if arg == "sonarr" else "MissingMoviesSearch"
        requests.post(url(arg, "/api/v3/command"), json={"name": name}, headers={"X-Api-Key": k[arg]}, timeout=10)
        return jsonify(ok=True, message=f"{c['apps'][arg]['name']} is searching for everything missing")
    if action == "recyclarr" and "recyclarr" in c["apps"]:
        if DOCKER:
            stackconf.container_exec(c, "recyclarr", ["recyclarr", "sync"])
        else:
            stackconf.schtasks("/Run", "/TN", stackconf.task_name(c, c["apps"]["recyclarr"]["task"]))
        return jsonify(ok=True, message="Quality profiles are re-syncing from TRaSH Guides")
    if action == "restart" and arg in c["apps"]:
        threading.Thread(target=restart_app, args=(arg,), daemon=True).start()
        _cache.pop("procs", None)
        return jsonify(ok=True, message=f"Restarting {c['apps'][arg]['name']}")
    return jsonify(ok=False, message="Unknown action"), 400


@app.get("/")
def index():
    resp = send_from_directory(HERE / "static", "index.html")
    resp.headers["Cache-Control"] = "no-cache"  # always check for a newer page
    return resp


@app.get("/web/")
def jellyfin_redirect():
    # Jellyfin's "/web/" opened on the hub's port by mistake: send it to Jellyfin.
    port = APPS().get("jellyfin", {}).get("port", 8096)
    return redirect(f"{request.scheme}://{request.host.rsplit(':', 1)[0]}:{port}/web/")


def hourly_healthcheck():
    """Linux: the hub runs the health check itself (Windows has a scheduled task for it)."""
    time.sleep(300)
    while True:
        try:
            import healthcheck
            healthcheck.main()
        except Exception as e:
            print("health check failed:", e)
        time.sleep(3600)


if __name__ == "__main__":
    from waitress import serve
    if DOCKER:
        threading.Thread(target=hourly_healthcheck, daemon=True).start()
    # All interfaces, but guard() decides who gets in.
    serve(app, host="0.0.0.0", port=int(CFG["hub"]["listen_port"]), threads=8)
