"""Your stack: the media stack (Jellyfin, Seerr, Sonarr, Radarr, Prowlarr, qBittorrent...) as Atlas sees it.

Atlas is the stack's app, not its caretaker. The Media Hub (mediahub/hub.py from Media Stack Setup) keeps doing
the work headless: the watchdog, the VPN leak check, webhooks, the hourly health check and nightly backups (scheduled
tasks on Windows, the hub container on Linux). This module only reads the hub's JSON API and presses the hub's own
buttons (restart an app, re-run the request doctor, back up now, restore). Nothing here monitors in the background.

Where the hub is: settings "hub_url", else "stack_config" (<root>/config/shared/mediastack.json, written into
Atlas's settings by the installer) -> apps.hub.url, else a quick look at http://127.0.0.1:8585 (the default port).
A hub on another computer wants its token (settings "hub_token", GET /api/token on the hub).

Without a hub, Atlas still answers read-only: it pings each service it knows (mediastack.json, else its own
settings), asks Seerr, Radarr/Sonarr and qBittorrent for their status, health and queue, and reads the disks. It
never invents a status: anything it couldn't check is "unknown"."""
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import requests

from . import settings

DEFAULT_HUB = "http://127.0.0.1:8585"
_mem = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="stack")
LEVEL_ORDER = {"error": 0, "down": 0, "warn": 1, "setup": 2, "unknown": 3, "ok": 4}
# Hub actions Atlas may press, with what the button says and whether it asks first.
ACTIONS = {
    "restart": ("Restart {name}", True),
    "retry-imports": ("Retry failed imports", False),
    "search-missing": ("Search for everything missing", False),
    "requests": ("Check every request again", False),
    "healthcheck": ("Run the health check now", False),
    "backup": ("Back up now", False),
    "recyclarr": ("Re-sync quality profiles", False),
}
KNOWN = {  # id -> (name, role) for services found without mediastack.json
    "jellyfin": ("Jellyfin", "Watch"), "seerr": ("Seerr", "Requests"), "sonarr": ("Sonarr", "TV & anime"),
    "radarr": ("Radarr", "Movies"), "prowlarr": ("Prowlarr", "Indexers"), "qbittorrent": ("qBittorrent", "Downloads"),
    "lidarr": ("Lidarr", "Music"), "bazarr": ("Bazarr", "Subtitles"),
}


def cached(key, seconds, fn):
    hit = _mem.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    val = fn()
    _mem[key] = (time.time(), val)
    return val


def forget(prefix=""):
    for k in list(_mem):
        if k.startswith(prefix):
            _mem.pop(k, None)


# ------------------------------------------------------------------ where things are
def stack_config():
    """mediastack.json (installer installs), or None. Holds every app's address and API key: never sent to the page."""
    s = settings.load()
    path = s.get("stack_config") or os.environ.get("MEDIASTACK_CONFIG") or ""
    if not path:
        return None

    def read():
        try:
            return json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except Exception:
            return None
    return cached("cfg:" + path, 15, read)


class Hub:
    def __init__(self, base, token=""):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        if token:
            self.s.headers["X-Hub-Token"] = token

    @property
    def host(self):
        return urlsplit(self.base).hostname or "127.0.0.1"

    def get(self, path, timeout=20, **params):
        r = self.s.get(self.base + path, params=params or None, timeout=timeout)
        if r.status_code == 404:
            raise LookupError(path)
        if r.status_code in (401, 403):
            raise PermissionError("The Media Hub refused Atlas (it needs the hub's token for a hub on another computer)")
        r.raise_for_status()
        return r.json()

    def post(self, path, body=None, timeout=30, **params):
        r = self.s.post(self.base + path, json=body or {}, params=params or None, timeout=timeout)
        if r.status_code in (401, 403):
            raise PermissionError("The Media Hub refused Atlas (it needs the hub's token for a hub on another computer)")
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400 and not isinstance(data, dict):
            r.raise_for_status()
        return data if isinstance(data, dict) else {"ok": r.ok, "data": data}


def _hub_candidates():
    s = settings.load()
    if s.get("hub_url"):
        return [s["hub_url"]], True
    cfg = stack_config()
    hub = ((cfg or {}).get("apps") or {}).get("hub") or {}
    if hub.get("url") and (cfg or {}).get("platform") != "docker":
        return [hub["url"]], True
    if cfg and (cfg.get("hub") or {}).get("public_port"):
        return [f"http://127.0.0.1:{cfg['hub']['public_port']}"], True
    return [DEFAULT_HUB], False


def hub():
    """The Media Hub to talk to, or None. Found once a minute (a hub that went away is noticed then)."""
    def find():
        cands, configured = _hub_candidates()
        token = settings.load().get("hub_token") or ""
        for base in cands:
            h = Hub(base, token)
            try:
                info = h.get("/api/info", timeout=3)
                if info.get("app") == "mediahub":
                    return {"base": h.base, "info": info, "configured": configured}
            except LookupError:  # an older hub without /api/info: /api/me says it's a Media Hub
                try:
                    me = h.get("/api/me", timeout=3)
                    if isinstance(me, dict) and "local" in me:
                        return {"base": h.base, "info": {"app": "mediahub", "api": 1, "features": [], "ui": "hub"},
                                "configured": configured}
                except Exception:
                    pass
            except Exception:
                pass
        return None
    found = cached("hub", 60, find)
    if not found:
        return None
    h = Hub(found["base"], settings.load().get("hub_token") or "")
    h.info = found["info"]
    h.configured = found["configured"]
    return h


def has(h, feature):
    return feature in ((getattr(h, "info", None) or {}).get("features") or [])


# ------------------------------------------------------------------ shaping the hub's answers
def _fix(action, names, app_id=None):
    """A hub problem's "action" ("restart:sonarr", "retry-imports") -> a button {action, arg, label, confirm}."""
    if not action:
        return None
    verb, _, arg = action.partition(":")
    if verb not in ACTIONS:
        return None
    label, confirm = ACTIONS[verb]
    arg = arg or (app_id if verb == "search-missing" else "")
    return {"action": verb, "arg": arg, "label": label.format(name=names.get(arg, arg or "it")), "confirm": confirm}


def _ping(url, timeout=3):
    """(ms, http status) for a GET of the app's page, or (None, None) when it doesn't answer."""
    t = time.perf_counter()
    try:
        r = requests.get(url, timeout=timeout, allow_redirects=False, stream=True)
        r.close()
        return round((time.perf_counter() - t) * 1000), r.status_code
    except requests.RequestException:
        return None, None


def _apps_from_hub(h, st):
    names = {a["id"]: a["name"] for a in st.get("apps", [])}
    out = []
    for a in st.get("apps", []):
        link = a.get("link") or "/"
        open_url = f"http://{h.host}:{a['port']}{link}" if a.get("port") else None
        probs = []
        for p in a.get("problems") or []:
            probs.append({"level": p.get("level", "warn"), "text": p.get("text", ""), "link": p.get("link"),
                          "fix": _fix(p.get("action"), names, a["id"])})
        out.append({"id": a["id"], "name": a["name"], "role": a.get("role", ""), "status": a.get("status", "unknown"),
                    "version": a.get("version"), "summary": a.get("summary"), "process": a.get("process"),
                    "open": open_url, "local_only": bool(a.get("local_only")), "problems": probs, "ms": None,
                    "now_playing": a.get("nowPlaying"), "missing": a.get("missing"),
                    "fixes": [_fix(f"restart:{a['id']}", names)] if a.get("status") == "down" else []})
    # response times: the app's own page, all at once
    futs = {a["id"]: _pool.submit(_ping, a["open"]) for a in out if a["open"] and a["id"] != "byparr"}
    for a in out:
        f = futs.get(a["id"])
        if f:
            try:
                a["ms"] = f.result(timeout=5)[0]
            except Exception:
                a["ms"] = None
    return out


def _downloads_from_hub(st):
    apps = {a["id"]: a for a in st.get("apps", [])}
    q = apps.get("qbittorrent") or {}
    queue = []
    for kind in ("sonarr", "radarr"):
        for r in (apps.get(kind) or {}).get("queue") or []:
            queue.append({**r, "app": kind})
    return {"connection": q.get("connection"), "summary": q.get("summary"), "torrents": q.get("torrents") or [],
            "queue": queue}


def _system_from_hub(st, names):
    sysd = st.get("system") or {}
    probs = [{"level": p.get("level", "warn"), "text": p.get("text", ""), "fix": _fix(p.get("action"), names)}
             for p in sysd.get("problems") or []]
    return {"disk": sysd.get("disk"), "vpn": sysd.get("vpn"), "vpncheck": sysd.get("vpncheck"), "problems": probs}


def _from_hub(h):
    """Everything the hub knows, fetched in parallel. Parts an older hub doesn't have are None."""
    jobs = {
        "status": lambda: h.get("/api/status", timeout=60),
        "doctor": lambda: h.get("/api/requests", timeout=15),
        "events": lambda: h.get("/api/events", timeout=10, limit=60),
        "incidents": lambda: h.get("/api/incidents", timeout=10, limit=30),
        "health": lambda: h.get("/api/health", timeout=10),
        "backups": lambda: h.get("/api/backups", timeout=20),
        "phone": lambda: h.get("/api/settings", timeout=10),
        "addresses": lambda: h.get("/api/addresses", timeout=15),
    }
    futs = {k: _pool.submit(fn) for k, fn in jobs.items()}
    got = {}
    for k, f in futs.items():
        try:
            got[k] = f.result(timeout=70)
        except LookupError:
            got[k] = None  # this hub version doesn't have it
        except Exception as e:
            got[k] = {"_error": f"{type(e).__name__}: {e}"[:200]}
    st = got["status"]
    if not isinstance(st, dict) or st.get("_error"):
        raise RuntimeError((st or {}).get("_error") or "The Media Hub didn't answer")
    apps = _apps_from_hub(h, st)
    names = {a["id"]: a["name"] for a in apps}
    events = got["events"] if isinstance(got["events"], list) else []
    health = got["health"] if isinstance(got["health"], dict) and not got["health"].get("_error") else None
    if health is not None:
        health = health.get("report") if isinstance(health.get("report"), dict) else health
        health = {k: health.get(k) for k in ("time", "fixed", "attention", "ok")} | {"source": "report"}
        if not health.get("time"):
            health = None
    if health is None:  # an older hub: the health check's own summary from the activity feed
        ev = next((e for e in events if e.get("source") == "healthcheck"), None)
        if ev:
            health = {"time": ev.get("time"), "title": ev.get("title"), "message": ev.get("message"),
                      "attention": [m.strip() for m in (ev.get("message") or "").split(";") if m.strip()]
                      if ev.get("level") == "warn" else [], "fixed": [], "ok": [], "source": "event"}
    incidents = got["incidents"] if isinstance(got["incidents"], list) else None
    backups = got["backups"] if isinstance(got["backups"], dict) and not got["backups"].get("_error") else None
    phone = got["phone"] if isinstance(got["phone"], dict) and not got["phone"].get("_error") else None
    doctor = got["doctor"] if isinstance(got["doctor"], dict) and not got["doctor"].get("_error") else None
    return {
        "source": "hub",
        "hub": {"url": h.base, "api": h.info.get("api", 1), "ui": h.info.get("ui", "hub"),
                "features": h.info.get("features") or [], "watchdog": st.get("watchdog"),
                "can_restore": (backups or {}).get("can_restore", False)},
        "apps": apps,
        "system": _system_from_hub(st, names),
        "downloads": _downloads_from_hub(st),
        "doctor": doctor and {"time": doctor.get("time"), "running": doctor.get("running"),
                              "counts": doctor.get("counts") or {}, "items": doctor.get("items") or [],
                              "problems": doctor.get("problems") or []},
        "health": health,
        "events": events[:60],
        "incidents": incidents,
        "backups": backups and {"items": backups.get("backups") or [], "can_restore": bool(backups.get("can_restore")),
                                "error": None if backups.get("ok", True) else backups.get("message")},
        "phone": phone and {"on": bool(phone.get("phone")), "topic": phone.get("ntfy_topic"),
                            "server": phone.get("ntfy_server") or "https://ntfy.sh",
                            "notifications": phone.get("notifications"), "watchdog": phone.get("watchdog"),
                            "can_notify": phone.get("can_notify", True), "ui": phone.get("ui")},
        "addresses": got["addresses"] if isinstance(got["addresses"], dict) and not got["addresses"].get("_error") else None,
    }


# ------------------------------------------------------------------ without a hub: read-only checks
def _services(app):
    """id -> {name, role, url, key} from mediastack.json, else from Atlas's own settings."""
    cfg = stack_config()
    out = {}
    if cfg:
        for aid, a in (cfg.get("apps") or {}).items():
            if aid == "hub" or not a.get("url"):
                continue
            out[aid] = {"name": a.get("name") or aid, "role": a.get("role", ""), "url": a["url"].rstrip("/"),
                        "key": a.get("key") or "", "link": a.get("link") or "/"}
        return out
    jf = getattr(app, "jf", None)
    if jf is not None and getattr(jf, "base", ""):
        out["jellyfin"] = {"url": jf.base, "key": "", "link": "/web/"}
    sr = getattr(app, "seerr", None)
    if sr is not None and getattr(sr, "base", ""):
        out["seerr"] = {"url": sr.base, "key": "", "link": "/"}
    try:
        from . import storage
        for a in storage.arrs(app):
            if a.kind not in out:
                out[a.kind] = {"url": a.base, "key": a.key, "link": "/", "arr": a}
        base, _ = storage._qbt(app)
        if base:
            out["qbittorrent"] = {"url": base, "key": "", "link": "/"}
    except Exception as e:
        print("stack: storage connections unavailable:", type(e).__name__, e)
    for aid, v in out.items():
        v.setdefault("name", KNOWN.get(aid, (aid.title(), ""))[0])
        v.setdefault("role", KNOWN.get(aid, ("", ""))[1])
    return out


def _check_one(aid, svc):
    base, key = svc["url"], svc.get("key") or ""
    ms, code = _ping(base + (svc.get("link") or "/"))
    res = {"id": aid, "name": svc["name"], "role": svc.get("role", ""), "open": base + (svc.get("link") or "/"),
           "ms": ms, "version": None, "summary": None, "problems": [], "fixes": [], "status": "unknown"}
    if ms is None:
        res.update(status="down", summary="Not answering")
        res["problems"].append({"level": "error", "text": f"{svc['name']} isn't answering at {base}", "fix": None})
        return res
    try:
        if aid == "jellyfin":
            res["version"] = requests.get(base + "/System/Info/Public", timeout=4).json().get("Version")
        elif aid == "seerr":
            res["version"] = requests.get(base + "/api/v1/status", timeout=4).json().get("version")
        elif aid in ("sonarr", "radarr", "prowlarr", "lidarr") and key:
            api = "v3" if aid in ("sonarr", "radarr") else "v1"
            h = {"X-Api-Key": key}
            res["version"] = requests.get(f"{base}/api/{api}/system/status", headers=h, timeout=4).json().get("version")
            for x in requests.get(f"{base}/api/{api}/health", headers=h, timeout=6).json() or []:
                res["problems"].append({"level": "error" if x.get("type") == "error" else "warn",
                                        "text": x.get("message", ""), "link": x.get("wikiUrl"), "fix": None})
        elif aid == "qbittorrent":
            r = requests.get(base + "/api/v2/app/version", timeout=4)
            if r.status_code == 403:
                res["summary"] = "Answering (Atlas can't read it without its login)"
            else:
                res["version"] = r.text.strip()
                tr = requests.get(base + "/api/v2/transfer/info", timeout=4).json()
                if tr.get("connection_status") == "disconnected":
                    res["problems"].append({"level": "error", "text": "qBittorrent has no network connection (if "
                                            "torrents go through a VPN, the VPN may be off).", "fix": None})
                res["summary"] = (f"↓ {tr.get('dl_info_speed', 0) / 2**20:.1f} MB/s · "
                                  f"↑ {tr.get('up_info_speed', 0) / 2**20:.1f} MB/s")
    except Exception as e:
        res["problems"].append({"level": "warn", "text": f"Answering, but its status couldn't be read ({type(e).__name__})",
                                "fix": None})
    if code and code >= 500:
        res["problems"].append({"level": "error", "text": f"{svc['name']} answers with an error (HTTP {code})", "fix": None})
    res["status"] = ("error" if any(p["level"] == "error" for p in res["problems"]) else
                     "warn" if res["problems"] else "ok")
    res["summary"] = res["summary"] or ("Answering" + (f" in {ms} ms" if ms is not None else ""))
    return res


def _local_downloads(app, svcs):
    out = {"connection": None, "summary": None, "torrents": [], "queue": []}
    q = svcs.get("qbittorrent")
    if q:
        try:
            r = requests.get(q["url"] + "/api/v2/torrents/info", timeout=6)
            if r.ok:
                ts = r.json()
                out["torrents"] = [{"name": t["name"], "progress": round(t["progress"] * 100, 1), "state": t["state"],
                                    "category": t.get("category"), "dlspeed": t.get("dlspeed"), "eta": t.get("eta"),
                                    "size": t.get("size"), "seeds": t.get("num_seeds"), "peers": t.get("num_leechs")}
                                   for t in sorted(ts, key=lambda t: (t["progress"] >= 1, -t.get("added_on", 0)))[:40]]
                tr = requests.get(q["url"] + "/api/v2/transfer/info", timeout=4).json()
                out["connection"] = tr.get("connection_status")
                active = [t for t in ts if t["progress"] < 1]
                out["summary"] = f"{len(active)} downloading · ↓ {tr.get('dl_info_speed', 0) / 2**20:.1f} MB/s"
        except Exception:
            pass
    for kind in ("sonarr", "radarr"):
        s = svcs.get(kind)
        if not s or not s.get("key"):
            continue
        try:
            recs = requests.get(f"{s['url']}/api/v3/queue", headers={"X-Api-Key": s["key"]}, timeout=8,
                                params={"pageSize": 50}).json().get("records", [])
            for r in recs:
                out["queue"].append({
                    "app": kind, "title": r.get("title"), "status": r.get("trackedDownloadState") or r.get("status"),
                    "progress": round(100 * (1 - r["sizeleft"] / r["size"])) if r.get("size") else 0,
                    "timeleft": r.get("timeleft"),
                    "warning": "; ".join(m for x in r.get("statusMessages", []) for m in x.get("messages", [])) or None})
        except Exception:
            pass
    return out


def _local_disk(app, svcs):
    roots = []
    cfg = stack_config()
    if cfg and cfg.get("library"):
        roots += [v for v in cfg["library"].values() if isinstance(v, str)]
    roots += settings.load().get("library_folders") or []
    seen, out = set(), None
    for r in roots:
        p = Path(r)
        while not p.exists() and p != p.parent:
            p = p.parent
        try:
            du = shutil.disk_usage(p)
        except OSError:
            continue
        if (du.total, du.free) in seen:
            continue
        seen.add((du.total, du.free))
        if out is None or du.free < out["free_gb"] * 2**30:
            out = {"free_gb": round(du.free / 2**30), "total_gb": round(du.total / 2**30), "path": str(p)}
    if out is None:
        for kind in ("radarr", "sonarr"):
            s = svcs.get(kind)
            if not s or not s.get("key"):
                continue
            try:
                for rf in requests.get(f"{s['url']}/api/v3/rootfolder", headers={"X-Api-Key": s["key"]}, timeout=6).json():
                    if rf.get("freeSpace") is not None and (out is None or rf["freeSpace"] < out["free_gb"] * 2**30):
                        out = {"free_gb": round(rf["freeSpace"] / 2**30), "total_gb": round((rf.get("totalSpace") or 0) / 2**30) or None,
                               "path": rf.get("path")}
            except Exception:
                pass
    return out


def _local_doctor(app, downloads):
    """Without the hub: the requests that aren't here yet and what's plainly visible (approval, the queue)."""
    sr = getattr(app, "seerr", None)
    if not (sr and sr.ready):
        return None
    try:
        data = sr.call("GET", "/request", params={"take": 50, "sort": "added", "filter": "all"}, timeout=10) or {}
    except Exception:
        return None
    items = []
    for r in data.get("results") or []:
        m = r.get("media") or {}
        if m.get("status") == 5 or r.get("status") == 3:
            continue
        kind = r.get("type")
        try:
            d = sr.call("GET", f"/{'movie' if kind == 'movie' else 'tv'}/{m.get('tmdbId')}", timeout=8) or {}
        except Exception:
            d = {}
        title = d.get("title") or d.get("name") or f"TMDB {m.get('tmdbId')}"
        date = d.get("releaseDate") or d.get("firstAirDate") or ""
        hits = [q for q in downloads.get("queue") or [] if _norm(title) and _norm(title) in _norm(q.get("title"))]
        if r.get("status") == 1:
            state, reason = "pending", "waiting for an admin to approve it in Seerr"
        elif hits:
            state = "downloading"
            reason = f"{len(hits)} download(s) in the queue, furthest at {max(h.get('progress') or 0 for h in hits)}%"
        elif m.get("status") == 4:
            state, reason = "partial", "partly in your library; nothing for it is downloading right now"
        else:
            state, reason = "searching", ("not downloading right now. Atlas can't see why without the Media Hub "
                                          "(its request doctor checks the indexers).")
        items.append({"key": f"{kind}:{m.get('tmdbId')}", "type": kind, "tmdb": m.get("tmdbId"), "title": title,
                      "year": int(date[:4]) if date[:4].isdigit() else None, "state": state, "reason": reason,
                      "fixed": [], "requested_by": (r.get("requestedBy") or {}).get("displayName")})
    counts = {}
    for i in items:
        counts[i["state"]] = counts.get(i["state"], 0) + 1
    return {"time": time.time(), "running": False, "counts": counts, "items": items, "problems": [], "local": True}


def _local(app):
    svcs = _services(app)
    futs = {aid: _pool.submit(_check_one, aid, s) for aid, s in svcs.items()}
    apps = []
    for aid, f in futs.items():
        try:
            apps.append(f.result(timeout=15))
        except Exception as e:
            apps.append({"id": aid, "name": svcs[aid]["name"], "status": "unknown", "problems": [], "fixes": [],
                         "summary": f"Couldn't check ({type(e).__name__})", "ms": None, "open": svcs[aid]["url"]})
    downloads = _local_downloads(app, svcs)
    disk = _local_disk(app, svcs)
    sys_probs = []
    if disk and disk["free_gb"] < 50:
        sys_probs.append({"level": "error", "text": f"Only {disk['free_gb']} GB free for the library", "fix": None})
    elif disk and disk["free_gb"] < 200:
        sys_probs.append({"level": "warn", "text": f"Only {disk['free_gb']} GB free for the library", "fix": None})
    vpn = None
    cfg = stack_config()
    adapter = ((cfg or {}).get("vpn") or {}).get("adapter")
    if adapter:
        try:
            import psutil
            nic = psutil.net_if_stats().get(adapter)
            vpn = bool(nic and nic.isup)
        except Exception:
            vpn = None
    if vpn is False:
        sys_probs.append({"level": "warn", "text": "The VPN adapter is disconnected - torrents are paused until it "
                                                   "connects.", "fix": None})
    return {
        "source": "marquee", "hub": None, "apps": apps,
        "system": {"disk": disk, "vpn": vpn, "vpncheck": None, "problems": sys_probs},
        "downloads": downloads, "doctor": _local_doctor(app, downloads), "health": None, "events": [],
        "incidents": None, "backups": None, "phone": None, "addresses": None,
    }


# ------------------------------------------------------------------ the snapshot everyone reads
def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _summarise(snap):
    probs = []
    for a in snap["apps"]:
        for p in a.get("problems") or []:
            probs.append({**p, "app": a["id"], "name": a["name"]})
        if a.get("status") == "setup":
            probs.append({"level": "setup", "text": a.get("summary") or "Not set up yet", "app": a["id"],
                          "name": a["name"], "fix": None})
    for p in (snap.get("system") or {}).get("problems") or []:
        probs.append({**p, "app": "system", "name": "This server"})
    probs.sort(key=lambda p: LEVEL_ORDER.get(p["level"], 5))
    errors = [p for p in probs if p["level"] == "error"]
    warns = [p for p in probs if p["level"] == "warn"]
    down = [a["name"] for a in snap["apps"] if a.get("status") == "down"]
    if errors:
        level = "error"
        text = (f"{', '.join(down)} {'is' if len(down) == 1 else 'are'} down" if down else
                f"{len(errors)} problem{'s' if len(errors) != 1 else ''} need{'s' if len(errors) == 1 else ''} you")
    elif warns:
        level, text = "warn", f"Running, with {len(warns)} warning{'s' if len(warns) != 1 else ''}"
    elif not snap["apps"]:
        level, text = "unknown", "Atlas doesn't know any part of your stack yet"
    else:
        level, text = "ok", "Everything's running"
    snap["problems"] = probs
    snap["summary"] = {"level": level, "text": text, "errors": len(errors), "warnings": len(warns),
                       "apps": len(snap["apps"]), "down": down}
    return snap


def snapshot(app, force=False):
    """The whole stack in one dict (cached 20 s): source, hub, apps, system, downloads, doctor, health, events,
    incidents, backups, phone, problems, summary. Secrets (API keys) never appear in it."""
    if force:
        forget("snap")

    def build():
        h = hub()
        snap = None
        if h:
            try:
                snap = _from_hub(h)
            except Exception as e:
                print("stack: hub unavailable, checking directly:", type(e).__name__, e)
                forget("hub")
                snap = _local(app)
                snap["hub_error"] = f"The Media Hub at {h.base} didn't answer ({type(e).__name__})."
        if snap is None:
            snap = _local(app)
        snap["checked"] = time.time()
        return _summarise(snap)
    with _lock:
        return cached("snap", 20, build)


# ------------------------------------------------------------------ actions (pressed by the person, via the hub)
def action(app, verb, arg=""):
    """Press one of the hub's buttons. Only the ones in ACTIONS; restarts name an app the hub knows."""
    if verb not in ACTIONS:
        raise ValueError("Unknown action")
    h = hub()
    if not h:
        raise PermissionError("This needs the Media Hub (it does the work in the background), and Atlas can't "
                              "find it. Set its address in Your stack > Settings.")
    if arg and not re.fullmatch(r"[a-z0-9_-]{1,40}", arg):
        raise ValueError("Unknown app")
    res = h.post(f"/api/action/{verb}", arg=arg)
    forget("snap")
    if not res.get("ok", False):
        raise RuntimeError(res.get("message") or "The Media Hub said no")
    return res.get("message") or "Done"


def restore(app, name):
    h = hub()
    if not h:
        raise PermissionError("Restoring needs the Media Hub")
    res = h.post("/api/backups/restore", {"name": name}, timeout=60)
    if not res.get("ok", False):
        raise RuntimeError(res.get("message") or "The Media Hub couldn't restore that backup")
    forget("snap")
    return res.get("message")


def phone(app, changes=None, test=False):
    """ntfy phone alerts (the hub sends them). changes: {phone, notifications, watchdog, ntfy_server, new_topic, ui}."""
    h = hub()
    if not h:
        raise PermissionError("Phone alerts are sent by the Media Hub, and Atlas can't find it")
    if test:
        res = h.post("/api/phone/test")
        if not res.get("ok"):
            raise RuntimeError(res.get("message") or "The test didn't go out")
        return {"message": res.get("message")}
    body = {k: v for k, v in (changes or {}).items()
            if k in ("phone", "notifications", "watchdog", "ntfy_server", "new_topic", "ui")}
    res = h.post("/api/settings", body)
    forget("snap")
    return {"settings": {k: v for k, v in res.items() if k != "api_token"}}


# ------------------------------------------------------------------ small readers for the chat
def doctor_for(app, tmdb=None, kind=None, title=None):
    """The request doctor's entry for one title (by TMDB id, else by name), or None."""
    snap = snapshot(app)
    items = (snap.get("doctor") or {}).get("items") or []
    for i in items:
        if tmdb and str(i.get("tmdb")) == str(tmdb) and (not kind or i.get("type") == kind):
            return i
    if title:
        want = _norm(title)
        for i in items:
            if _norm(i.get("title")) == want:
                return i
    return None


def queue_for(app, title):
    """Downloads (Sonarr/Radarr queue + torrents) whose name mentions this title."""
    snap = snapshot(app)
    words = _norm(title).split()
    if not words:
        return []
    dl = snap.get("downloads") or {}

    def hit(name):
        n = _norm(name)
        return all(w in n for w in words)
    out = [{**q, "kind": "queue"} for q in dl.get("queue") or [] if hit(q.get("title"))]
    out += [{**t, "kind": "torrent", "title": t.get("name")} for t in dl.get("torrents") or [] if hit(t.get("name"))
            and t.get("progress", 0) < 100]
    return out
