"""Storage: what a request will cost on disk, how much the library takes, and optional cleanup.

Three parts, all working from the *arr apps (Radarr/Sonarr) that Seerr sends requests to:

- Request quality: the quality profiles of each Seerr server in plain words ("4K (upgrades over time)", "1080p",
  "720p / smaller"), each with a size estimate. Estimates come from the apps' quality definitions (MB per minute,
  per quality) times the runtime, with the "typical" figure calibrated on the files already in the library.
- Storage limit: the library's size (Radarr/Sonarr sizeOnDisk, plus a walk of any Jellyfin library folder that no
  *arr manages; hardlinks counted once), compared with `storage_limit_gb` and the disk's free space, with a
  projection of what's still on its way (monitored but missing, and requests waiting for approval).
- Cleanup (off by default): remove titles after everyone has watched them (`delete_after_watch`), titles nobody
  started (`delete_unwatched_days`), or, with `storage_enforce` = auto, watched titles oldest-watched first while
  the library is over the limit. Every removal is announced first ("Leaving soon", with a Keep button), goes through
  Radarr/Sonarr (unmonitor, then delete the file, so it isn't downloaded again; their recycle bin applies), then
  Jellyfin and Seerr are told, and optionally the seeding torrent goes too once no library file uses it.

Everything can be previewed: plan() and run(dry_run=True) say what would go, why, and what it would free.
Nothing is removed while Jellyfin can't tell us what every user has watched.
"""
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import settings

GB = 1024 ** 3
MB = 1024 ** 2
DAY = 86400
HOUR = 3600
STATE_FILE = settings.DIR / "storage.json"   # flags (first seen as leaving), notices, caches
LOG_FILE = settings.DIR / "storage_log.json"
NOTICE_DAYS_UNWATCHED = 3                    # "Leaving soon" shows at least this long before an unwatched title goes
NOTICE_DAYS_MIN = 1                          # ...and at least a day before anything else goes
LOG_MAX = 500
VIDEO = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".webm", ".wmv", ".mpg", ".mpeg"}
MEDIA_COLLECTIONS = {None, "", "movies", "tvshows", "mixed", "homevideos"}

# Fallback sizes in MB per minute (min, typical, max) when the apps don't say, by resolution and source.
DEFAULT_MBMIN = {
    "sd": (2, 7, 20),
    "720-web": (4, 14, 40), "720-bluray": (6, 25, 60),
    "1080-web": (8, 32, 80), "1080-bluray": (15, 70, 160), "1080-remux": (100, 230, 400),
    "2160-web": (25, 110, 250), "2160-bluray": (45, 190, 400), "2160-remux": (187, 400, 800),
}

_lock = threading.RLock()
_mem = {}          # in-memory cache: key -> (time, value)
_state = None
_job = None


# ------------------------------------------------------------------ small helpers
def now():
    return time.time()


def ts(s):
    """ISO date (as the *arr apps and Jellyfin write it) -> epoch seconds, or 0."""
    if not s:
        return 0
    if isinstance(s, (int, float)):
        return float(s)
    try:
        s = re.sub(r"(\.\d{6})\d+", r"\1", s.replace("Z", "+00:00"))
        d = datetime.fromisoformat(s)
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return 0


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z") if t else None


def pathkey(p):
    return os.path.normcase(os.path.normpath(p or "")).replace("/", "\\").rstrip("\\") if p else ""


def under(child, parent):
    c, p = pathkey(child), pathkey(parent)
    return bool(c and p) and (c == p or c.startswith(p + "\\"))


def fmt_size(b):
    if b is None:
        return "?"
    if b < GB:
        return f"{max(1, round(b / MB))} MB" if b >= MB else "0 MB"
    g = b / GB
    if g >= 1024:
        return f"{g / 1024:.1f} TB"
    return f"{g:.1f} GB" if g < 10 else f"{round(g)} GB"


def cached(key, seconds, fn):
    hit = _mem.get(key)
    if hit and now() - hit[0] < seconds:
        return hit[1]
    val = fn()
    _mem[key] = (now(), val)
    return val


def forget(prefix=""):
    for k in [k for k in _mem if k.startswith(prefix)]:
        _mem.pop(k, None)


def _load_state():
    global _state
    if _state is None:
        try:
            _state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _state = {}
        _state.setdefault("flagged", {})
        _state.setdefault("notified", {})
        _state.setdefault("calib", {})
        _state.setdefault("walk", {})
    return _state


def _save_state():
    with _lock:
        st = _load_state()
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)


def deletes_allowed():
    """Development runs point at a real media stack: deletions there need an explicit opt-in."""
    if os.environ.get("MARQUEE_STORAGE_NO_DELETE"):
        return False
    dev = any(os.environ.get(k) for k in ("MARQUEE_DEV_SERVER", "MARQUEE_DEV_SEERR", "MARQUEE_DEV_KEY"))
    return not dev or bool(os.environ.get("MARQUEE_STORAGE_ALLOW_DELETE"))


# ------------------------------------------------------------------ the *arr apps
class Arr:
    """A Radarr or Sonarr (API v3). Writes go through send(), which refuses while `read_only` is set."""

    def __init__(self, kind, base, key, name=None, is4k=False, seerr_id=None, source="settings"):
        self.kind = kind            # "radarr" | "sonarr"
        self.base = base.rstrip("/")
        self.key = key
        self.name = name or kind.title()
        self.is4k = bool(is4k)
        self.seerr_id = seerr_id
        self.source = source
        self.read_only = False
        self.s = requests.Session()
        self.s.headers["X-Api-Key"] = key

    def __repr__(self):
        return f"<Arr {self.kind} {self.name} {self.base}>"

    def get(self, path, timeout=30, **params):
        r = self.s.get(f"{self.base}/api/v3{path}", params=params or None, timeout=timeout)
        r.raise_for_status()
        return r.json() if r.content else None

    def send(self, method, path, json_body=None, params=None, timeout=30):
        if self.read_only:
            raise PermissionError(f"{self.name} is read-only here")
        r = self.s.request(method, f"{self.base}/api/v3{path}", json=json_body, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json() if r.content and "json" in r.headers.get("content-type", "") else None

    def public(self):
        return {"kind": self.kind, "name": self.name, "is4k": self.is4k, "server": self.seerr_id, "source": self.source}


def _seerr(app):
    sr = getattr(app, "seerr", None)
    return sr if sr is not None and sr.ready else None


def arrs(app, kind=None):
    """The Radarr/Sonarr apps we can talk to: from Settings (radarr_url/radarr_key...) or, when signed in to Seerr
    as an admin, the connections Seerr itself uses. Cached for 10 minutes."""
    def find():
        s = settings.load()
        out = []
        for k in ("radarr", "sonarr"):
            if s.get(f"{k}_url") and s.get(f"{k}_key"):
                out.append(Arr(k, s[f"{k}_url"], s[f"{k}_key"], source="settings"))
                continue
            sr = _seerr(app)
            if not sr:
                continue
            try:
                conns = sr.call("GET", f"/settings/{k}", timeout=8) or []
            except Exception:
                conns = []  # not an admin: Settings > Storage asks for the address and key instead
            for c in conns:
                if not c.get("apiKey") or not c.get("hostname"):
                    continue
                base = f"{'https' if c.get('useSsl') else 'http'}://{c['hostname']}:{c.get('port') or (7878 if k == 'radarr' else 8989)}{c.get('baseUrl') or ''}"
                out.append(Arr(k, base, c["apiKey"], c.get("name"), c.get("is4k"), c.get("id"), source="seerr"))
        return out
    found = cached("arrs", 600, find)
    return [a for a in found if kind is None or a.kind == kind]


def arr_for(app, kind, server=None, is4k=None):
    pool = arrs(app, kind)
    if server is not None:
        hit = [a for a in pool if a.seerr_id is not None and str(a.seerr_id) == str(server)]
        if hit:
            return hit[0]
    if is4k is not None:
        hit = [a for a in pool if a.is4k == bool(is4k)]
        if hit:
            return hit[0]
    return pool[0] if pool else None


# ------------------------------------------------------------------ quality definitions and profiles
def _qkind(q):
    """'1080-web' style key for DEFAULT_MBMIN from an *arr quality {name, source, resolution, modifier}."""
    name = (q.get("name") or "").lower()
    res = q.get("resolution") or 0
    if not res:
        m = re.search(r"(2160|1080|720|576|480)p", name)
        res = int(m.group(1)) if m else 0
    if res < 720:
        return "sd"
    tier = "2160" if res >= 2160 else "1080" if res >= 1080 else "720"
    src = (q.get("source") or "").lower()
    if "remux" in name or (q.get("modifier") or "").lower() == "remux":
        kind = "remux"
    elif "bluray" in name or "bluray" in src or "br-disk" in name or "raw-hd" in name:
        kind = "bluray"
    else:
        kind = "web"
    if tier == "720" and kind == "remux":
        kind = "bluray"
    return f"{tier}-{kind}"


def _res(q):
    r = q.get("resolution") or 0
    if not r:
        m = re.search(r"(2160|1080|720|576|480)p", (q.get("name") or "").lower())
        r = int(m.group(1)) if m else 480
    return r


def quality_defs(arr):
    """{quality id: {name, resolution, source, modifier, min, preferred, max}} (MB per minute), cached an hour."""
    def fetch():
        out = {}
        for d in arr.get("/qualitydefinition") or []:
            q = d.get("quality") or {}
            out[q.get("id")] = {"name": q.get("name") or d.get("title"), "resolution": q.get("resolution") or 0,
                                "source": q.get("source"), "modifier": q.get("modifier"),
                                "min": d.get("minSize"), "preferred": d.get("preferredSize"), "max": d.get("maxSize")}
        return out
    return cached(f"qd:{arr.base}", HOUR, fetch)


def quality_profiles(arr):
    """{profile id: {id, name, upgrade, cutoff:[quality ids], allowed:[quality ids in rank order]}}, cached."""
    def fetch():
        out = {}
        for p in arr.get("/qualityprofile") or []:
            allowed, cutoff = [], []
            for it in p.get("items") or []:
                members = [sub["quality"]["id"] for sub in it.get("items") or [] if sub.get("quality")]
                if it.get("quality"):
                    members = [it["quality"]["id"]]
                if it.get("allowed"):
                    allowed += members
                gid = it.get("id") if it.get("items") else (it.get("quality") or {}).get("id")
                if gid == p.get("cutoff"):
                    cutoff = members
            out[p["id"]] = {"id": p["id"], "name": p.get("name"), "upgrade": bool(p.get("upgradeAllowed")),
                            "cutoff": cutoff or allowed[-1:], "allowed": allowed}
        return out
    return cached(f"qp:{arr.base}", 600, fetch)


def _runtime_minutes(text):
    """Radarr/Sonarr mediaInfo runTime "1:58:20" / "22:13" -> minutes."""
    try:
        parts = [float(x) for x in (text or "").split(":")]
    except ValueError:
        return 0
    if len(parts) == 3:
        return parts[0] * 60 + parts[1] + parts[2] / 60
    if len(parts) == 2:
        return parts[0] + parts[1] / 60
    return 0


def calibration(app, arr, force=False):
    """Average MB per minute of the library's own files, per quality name (and per resolution/source key), from
    this app's files. Kept 12 hours in the state file."""
    st = _load_state()
    key = f"{arr.kind}:{arr.base}"
    hit = st["calib"].get(key)
    if hit and not force and now() - hit.get("at", 0) < 12 * HOUR:
        return hit
    samples = []  # (quality dict, mb_per_min)
    try:
        if arr.kind == "radarr":
            for m in arr.get("/movie", timeout=60) or []:
                f = m.get("movieFile")
                if not f or not f.get("size"):
                    continue
                mins = _runtime_minutes((f.get("mediaInfo") or {}).get("runTime")) or m.get("runtime") or 0
                if mins >= 20:
                    samples.append((f["quality"]["quality"], f["size"] / MB / mins))
        else:
            for s in arr.get("/series", timeout=60) or []:
                if not (s.get("statistics") or {}).get("episodeFileCount"):
                    continue
                for f in arr.get("/episodefile", seriesId=s["id"], timeout=60) or []:
                    mins = _runtime_minutes((f.get("mediaInfo") or {}).get("runTime")) or s.get("runtime") or 0
                    if mins >= 5 and f.get("size"):
                        samples.append((f["quality"]["quality"], f["size"] / MB / mins))
    except Exception as e:
        print("storage: calibration failed:", type(e).__name__, e)
        return hit or {"by_name": {}, "by_key": {}, "n": 0, "at": 0}
    by_name, by_key = {}, {}
    for q, v in samples:
        by_name.setdefault(q.get("name"), []).append(v)
        by_key.setdefault(_qkind(q), []).append(v)

    def summary(d):
        out = {}
        for k, vals in d.items():
            vals = sorted(vals)
            mid = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
            out[k] = {"n": len(vals), "mean": sum(vals) / len(vals), "median": mid}
        return out
    res = {"by_name": summary(by_name), "by_key": summary(by_key), "n": len(samples), "at": now()}
    with _lock:
        st["calib"][key] = res
        _save_state()
    return res


def mb_per_min(q, qd, calib):
    """(low, typical, high, calibrated n) MB/min for one quality. Typical: the library's own files when there are
    enough of them (blended with the defaults for 1-2 files), else the app's preferred size when sensible, else the
    built-in default; low/high: the app's min/max when set, else the defaults."""
    key = _qkind(q)
    dmin, dtyp, dmax = DEFAULT_MBMIN[key]
    lo = qd.get("min") if qd and qd.get("min") else dmin
    hi = qd.get("max") if qd and qd.get("max") else dmax
    pref = qd.get("preferred") if qd else None
    typ = pref if pref and lo <= pref <= min(hi, dmax) else dtyp
    n = 0
    c = (calib or {}).get("by_name", {}).get(q.get("name")) or None
    if c is None:
        c = (calib or {}).get("by_key", {}).get(key)
    if c:
        n = c["n"]
        w = min(n, 3)
        typ = (c["median"] * w + typ * max(0, 3 - w)) / 3
    hi = max(hi, typ)
    lo = min(lo, typ)
    return lo, typ, hi, n


def friendly(profile, defs, anime=False):
    """Plain-words label for a quality profile: ("4K (upgrades over time)", tier, resolution)."""
    name = profile.get("name") or ""
    low = name.lower()
    cut = [defs[q] for q in profile.get("cutoff") or [] if q in defs]
    allowed = [defs[q] for q in profile.get("allowed") or [] if q in defs]
    if cut:
        res = max(_res(q) for q in cut)
    else:
        m = re.search(r"(2160|1080|720|480)", low)
        res = int(m.group(1)) if m else (2160 if re.search(r"4k|uhd|ultra", low) else 1080 if "hd" in low else 480)
    grows = profile.get("upgrade") and any(_res(q) < res for q in allowed)
    if low in ("any",):
        label, tier = "Any (whatever is found first)", "any"
    elif res >= 2160:
        label, tier = ("4K (upgrades over time)" if grows else "4K"), "4k"
    elif res >= 1080:
        label, tier = "1080p", "1080"
    elif res >= 720:
        label, tier = "720p / smaller", "720"
    else:
        label, tier = "SD / smallest", "sd"
    if "anime" in low:
        label, tier = f"Anime {label}", "anime-" + tier
    return label, tier, res


def _guess_profile(p):
    """A profile we only know by name (no *arr access): guess its qualities from the name."""
    low = (p.get("name") or "").lower()
    res = 2160 if re.search(r"2160|4k|uhd|ultra", low) else 1080 if "1080" in low else 720 if "720" in low else \
        480 if re.search(r"\bsd\b", low) else 1080
    src = "remux" if "remux" in low else "bluray" if "bluray" in low else "webdl"
    q = {"id": f"g{res}{src}", "name": f"{src}-{res}p", "resolution": res, "source": src,
         "modifier": "remux" if src == "remux" else "none"}
    return q


def estimate_profile(minutes, profile, defs, calib):
    """Size estimate for `minutes` of video in this profile. Returns bytes: start (first grab), typical (where it
    settles: the cutoff), low/high (range), plus how sure we are."""
    if profile.get("_guess"):
        q = _guess_profile(profile)
        lo, typ, hi, n = mb_per_min(q, None, None)
        start = typ
        low, high = lo, hi
        upgrades = False
    else:
        allowed = [dict(defs[q], id=q) for q in profile["allowed"] if q in defs]
        cut = [dict(defs[q], id=q) for q in profile["cutoff"] if q in defs] or allowed[-1:]
        if not allowed:
            return None
        cut_res = max(_res(q) for q in cut)
        stats = {q["id"]: mb_per_min(q, q, calib) for q in allowed + cut}
        typ = sum(stats[q["id"]][1] for q in cut) / len(cut)
        n = sum(stats[q["id"]][3] for q in cut)
        upgrades = profile["upgrade"] and any(_res(q) < cut_res for q in allowed)
        # What gets grabbed first: with upgrades, usually the common WEB 1080p release (else the best allowed
        # quality below the cutoff); without, the cutoff itself.
        if upgrades:
            first = [q for q in allowed if _qkind(q) == "1080-web" and _res(q) <= cut_res] or \
                    [q for q in allowed if 720 <= _res(q) < cut_res] or allowed[:1]
            start = sum(stats[q["id"]][1] for q in first) / len(first)
        else:
            start = typ
        # Range: an upgrading profile starts at its first grab (a smaller release can still turn up first);
        # a fixed one goes no lower than the app's minimum for its qualities, and not under 40% of typical.
        if upgrades:
            low = start * 0.75
        else:
            low = max(min(stats[q["id"]][0] for q in cut), typ * 0.4)
        high = max(stats[q["id"]][2] for q in allowed if _res(q) >= cut_res) if any(
            _res(q) >= cut_res for q in allowed) else max(stats[q["id"]][2] for q in allowed)
        high = min(high, typ * 3.5) if typ else high  # the apps' "no limit" shouldn't read as infinity
    m = max(1.0, float(minutes or 0))
    out = {k: int(v * m * MB) for k, v in (("start", start), ("typical", typ), ("low", min(low, start, typ)),
                                           ("high", max(high, typ)))}
    out["upgrades"] = bool(upgrades and start < typ * 0.8)
    out["calibrated"] = n
    out["text"] = f"≈ {fmt_size(out['typical'])} ({fmt_size(out['low'])}–{fmt_size(out['high'])})"
    out["upgrade_text"] = (f"Starts around {fmt_size(out['start'])}, may grow to ~{fmt_size(out['typical'])} as it "
                           f"upgrades" if out["upgrades"] else "")
    return out


# ------------------------------------------------------------------ what's being requested
def title_info(app, kind, tmdb, seasons="all"):
    """Runtime in minutes (TV: episodes x episode runtime for the chosen seasons), from Seerr's TMDB details."""
    kind = "tv" if kind in ("tv", "anime") else "movie"
    sr = _seerr(app)
    d = {}
    if sr:
        try:
            d = cached(f"tmdb:{kind}:{tmdb}", 6 * HOUR, lambda: sr.call("GET", f"/{kind}/{tmdb}", timeout=10)) or {}
        except Exception:
            d = {}
    genres = [g.get("id") for g in d.get("genres") or []]
    anime = d.get("originalLanguage") == "ja" and 16 in genres
    if kind == "movie":
        mins = d.get("runtime") or 120
        return {"minutes": mins, "episodes": 0, "runtime": mins, "anime": anime, "title": d.get("title"),
                "media_id": (d.get("mediaInfo") or {}).get("id"), "status": (d.get("mediaInfo") or {}).get("status"),
                "seasons": []}
    ep_rt = (d.get("episodeRunTime") or [None])[0] or (d.get("lastEpisodeToAir") or {}).get("runtime")
    if not ep_rt:  # TMDB often leaves it out: Sonarr's figure for a show we have, else season 1's episodes
        try:
            ep_rt = next((x.get("runtime") for x in library(app)["series"]
                          if str(x.get("tmdbId")) == str(tmdb) and x.get("runtime")), None)
        except Exception:
            ep_rt = None
    if not ep_rt and sr:
        try:
            first = min([s["seasonNumber"] for s in d.get("seasons") or [] if s.get("seasonNumber", 0) > 0] or [1])
            sd = cached(f"tmdb:season:{tmdb}:{first}", 24 * HOUR,
                        lambda: sr.call("GET", f"/tv/{tmdb}/season/{first}", timeout=10)) or {}
            rts = sorted(e["runtime"] for e in sd.get("episodes") or [] if e.get("runtime"))
            ep_rt = rts[len(rts) // 2] if rts else None
        except Exception:
            ep_rt = None
    ep_rt = ep_rt or (24 if anime else 45)
    all_seasons = [{"number": s["seasonNumber"], "episodes": s.get("episodeCount") or 0}
                   for s in d.get("seasons") or [] if s.get("seasonNumber", 0) > 0]
    chosen = _pick_seasons(all_seasons, seasons)
    eps = sum(s["episodes"] for s in all_seasons if s["number"] in chosen)
    if not all_seasons:
        eps = 10
    later = [s["episodes"] for s in all_seasons if s["number"] not in chosen and s["number"] > min(chosen or [0])]
    later_eps = (sum(later) / len(later)) if later else 0
    return {"minutes": eps * ep_rt, "episodes": eps, "runtime": ep_rt, "anime": anime, "title": d.get("name"),
            "later_season_minutes": later_eps * ep_rt, "later_seasons": len(later),
            "media_id": (d.get("mediaInfo") or {}).get("id"), "status": (d.get("mediaInfo") or {}).get("status"),
            "seasons": all_seasons, "chosen": sorted(chosen)}


def _pick_seasons(all_seasons, seasons):
    nums = [s["number"] for s in all_seasons]
    if seasons in (None, "", "all"):
        return set(nums)
    if seasons == "latest":
        return set(nums[-1:])
    if seasons == "smart":
        return set(nums[:1])
    if isinstance(seasons, str):
        seasons = [x for x in re.split(r"[,\s]+", seasons) if x.strip()]
    try:
        return {int(x) for x in seasons}
    except (TypeError, ValueError):
        return set(nums)


def request_options(app, kind, tmdb, seasons="all"):
    """Everything the request sheet's quality picker needs: servers (with 4K ones), their profiles in plain words
    with a size estimate each, root folders with free space, the defaults to preselect, and the storage status."""
    info = title_info(app, kind, tmdb, seasons)
    anime = info["anime"] or kind == "anime"
    kind = "tv" if kind in ("tv", "anime") else "movie"
    service = "radarr" if kind == "movie" else "sonarr"
    pref_key = "anime" if anime else kind
    prefs = settings.load().get("request_prefs") or {}
    pref = prefs.get(pref_key) or {}
    sr = _seerr(app)
    servers = []
    raw = []
    if sr:
        try:
            raw = cached(f"svc:{service}", 600, lambda: _seerr_services(sr, service))
        except Exception as e:
            print("storage: Seerr services failed:", type(e).__name__, e)
    for srv in raw:
        arr = arr_for(app, service, server=srv["id"], is4k=srv.get("is4k"))
        defs, profs, calib = {}, {}, None
        try:
            if arr:
                defs, profs = quality_defs(arr), quality_profiles(arr)
                calib = calibration(app, arr)
        except Exception as e:
            print("storage: profile details failed:", type(e).__name__, e)
            defs, profs = {}, {}
        out_profiles = []
        for p in srv["profiles"]:
            full = profs.get(p["id"]) or {"id": p["id"], "name": p["name"], "_guess": True}
            if full.get("_guess"):
                label, tier, res = friendly({"name": p["name"]}, {}, anime)
            else:
                label, tier, res = friendly(full, defs, anime)
            est = estimate_profile(info["minutes"], full, defs, calib)
            if est and info.get("later_season_minutes") and seasons == "smart":
                per = estimate_profile(info["later_season_minutes"], full, defs, calib)
                est["later_each"] = per["typical"]
                est["later_text"] = (f"Season 1 now, then about {fmt_size(per['typical'])} per later season as you "
                                     f"watch ({info['later_seasons']} more)")
            out_profiles.append({"id": p["id"], "name": p["name"], "label": label, "tier": tier, "resolution": res,
                                 "upgrades": bool(full.get("upgrade")), "estimate": est,
                                 "from_defaults": bool(full.get("_guess")) or not (est or {}).get("calibrated")})
        default = srv.get("activeProfileId")
        anime_default = srv.get("activeAnimeProfileId")
        if anime and anime_default is None:  # Radarr has no anime setting in Seerr: an "anime" profile if any
            anime_default = next((p["id"] for p in out_profiles if p["tier"].startswith("anime")), None)
        pick = anime_default if anime and anime_default is not None else default
        _feature(out_profiles, pick, anime)
        roots = srv.get("rootFolders") or []
        default_root = srv.get("activeAnimeDirectory") if anime and srv.get("activeAnimeDirectory") else \
            srv.get("activeDirectory")
        if anime and kind == "movie":
            default_root = next((r["path"] for r in roots if "anime" in r["path"].lower()), default_root)
        servers.append({"id": srv["id"], "name": srv.get("name"), "is4k": bool(srv.get("is4k")),
                        "default": bool(srv.get("isDefault")), "default_profile": pick,
                        "default_root": default_root, "profiles": out_profiles, "arr": bool(arr and profs),
                        "root_folders": [{"path": r["path"], "free": _free_space(r["path"], r.get("freeSpace"))}
                                         for r in roots]})
    # The owner's defaults: movies to a 4K server when there is one, else the default server's profile.
    choice = {}
    if servers:
        want4k = pref.get("is4k") if "is4k" in pref else (kind == "movie" and any(s["is4k"] for s in servers))
        pool = [s for s in servers if s["is4k"] == bool(want4k)] or servers
        srv = next((s for s in pool if pref.get("server") is not None and s["id"] == pref.get("server")), None) or \
            next((s for s in pool if s["default"]), pool[0])
        prof = pref.get("profile") if any(p["id"] == pref.get("profile") for p in srv["profiles"]) else srv["default_profile"]
        if prof is None and srv["profiles"]:
            prof = srv["profiles"][0]["id"]
        root = pref.get("root") if any(r["path"] == pref.get("root") for r in srv["root_folders"]) else srv["default_root"]
        choice = {"server": srv["id"], "profile": prof, "is4k": srv["is4k"], "root": root,
                  "remembered": bool(pref)}
    stat = None
    try:
        stat = usage(app)
    except Exception as e:
        print("storage: usage failed:", type(e).__name__, e)
    return {"kind": kind, "anime": anime, "tmdb": int(tmdb), "pref_key": pref_key, "info": info,
            "servers": servers, "choice": choice, "prefs": prefs, "storage": _slim_usage(stat),
            "arr_available": any(s["arr"] for s in servers),
            "explain": None if any(s["arr"] for s in servers) else
            "Sizes are rough: Atlas can't read Radarr/Sonarr yet (connect them in Settings > Storage)."}


def _seerr_services(sr, service):
    out = []
    for srv in sr.call("GET", f"/service/{service}", timeout=10) or []:
        d = sr.call("GET", f"/service/{service}/{srv['id']}", timeout=10) or {}
        out.append({**srv, "profiles": [{"id": p["id"], "name": p["name"]} for p in d.get("profiles") or []],
                    "rootFolders": [{"path": r["path"], "freeSpace": r.get("freeSpace")} for r in d.get("rootFolders") or []]})
    return out


def _feature(profiles, pick, anime):
    """Mark one profile per friendly tier as featured (the default, then anime/non-anime fit, then upgrades)."""
    best = {}
    for p in profiles:
        p["default"] = p["id"] == pick
        fit = p["tier"].startswith("anime") == bool(anime)
        score = (p["default"], fit, p["upgrades"], -p["id"])
        if p["tier"] not in best or score > best[p["tier"]][0]:
            best[p["tier"]] = (score, p["id"])
    keep = {v[1] for v in best.values()}
    for p in profiles:
        p["featured"] = p["id"] in keep and (p["default"] or not p["tier"].startswith("anime") or anime) and \
            not (p["tier"].startswith("anime") and not anime) and (p["tier"] != "any" or p["default"])
    order = {"4k": 0, "1080": 1, "720": 2, "sd": 3, "any": 4}
    profiles.sort(key=lambda p: (not p["featured"], order.get(p["tier"].replace("anime-", ""), 5), p["name"]))


def _free_space(path, fallback=None):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return fallback


def estimate(app, kind, tmdb, profile=None, seasons="all", server=None, is4k=None):
    """One estimate (GET /api/storage/estimate): the chosen profile's sizes plus whether it fits."""
    opts = request_options(app, kind, tmdb, seasons)
    srv = None
    for s in opts["servers"]:
        if server is not None and str(s["id"]) == str(server):
            srv = s
    if srv is None:
        pool = [s for s in opts["servers"] if is4k is None or s["is4k"] == bool(is4k)] or opts["servers"]
        srv = next((s for s in pool if s["id"] == opts["choice"].get("server")), pool[0] if pool else None)
    prof = None
    if srv:
        want = int(profile) if str(profile or "").isdigit() else srv["default_profile"]
        prof = next((p for p in srv["profiles"] if p["id"] == want), srv["profiles"][0] if srv["profiles"] else None)
    est = (prof or {}).get("estimate")
    if not est:  # no Seerr server at all: defaults for a sensible quality
        q = {"name": "default", "_guess": True}
        q["name"] = "2160p" if opts["kind"] == "movie" else "1080p"
        est = estimate_profile(opts["info"]["minutes"], q, {}, None)
    root = (srv or {}).get("default_root")
    return {"estimate": est, "profile": prof and {k: prof[k] for k in ("id", "name", "label", "tier")},
            "server": srv and {"id": srv["id"], "name": srv["name"], "is4k": srv["is4k"]}, "info": opts["info"],
            "fit": fit_check(app, est, root, opts["storage"]), "explain": opts["explain"]}


def fit_check(app, est, root=None, stat=None):
    """Will this fit? {fits_disk, fits_limit, blocked, message} using the typical size (and the high end for a
    softer warning)."""
    s = settings.load()
    stat = stat or _slim_usage(usage(app))
    typical = (est or {}).get("typical") or 0
    high = (est or {}).get("high") or typical
    free = _free_space(root) if root else None
    if free is None:
        free = (stat or {}).get("disk_free")
    limit = int(float(s.get("storage_limit_gb") or 0) * GB)
    used = (stat or {}).get("projected") or (stat or {}).get("used") or 0
    out = {"free": free, "limit": limit or None, "used": (stat or {}).get("used"), "projected": used,
           "fits_disk": free is None or typical < free, "fits_limit": not limit or used + typical <= limit,
           "message": None, "level": "ok"}
    out["blocked"] = bool(limit and not out["fits_limit"] and s.get("storage_enforce") == "block")
    if not out["fits_disk"]:
        out["level"], out["message"] = "bad", f"Won't fit: about {fmt_size(typical)} needed, {fmt_size(free)} free on that disk."
    elif out["blocked"]:
        out["level"], out["message"] = "bad", (f"Over your {fmt_size(limit)} storage limit: this would take it to "
                                               f"{fmt_size(used + typical)}. Free some space or raise the limit.")
    elif not out["fits_limit"]:
        now_used = (stat or {}).get("used") or 0
        out["level"], out["message"] = "warn", (
            f"This would take your library over its {fmt_size(limit)} limit." if now_used + typical > limit else
            f"With what's already on its way ({fmt_size(used - now_used)}), this would take your library over its "
            f"{fmt_size(limit)} limit.")
    elif free is not None and high >= free:
        out["level"], out["message"] = "warn", f"Tight: at its largest it could need {fmt_size(high)}, and {fmt_size(free)} is free."
    elif limit and used + high > limit:
        out["level"], out["message"] = "warn", "At its largest it could go over your storage limit."
    return out


def send_request(app, body, dry_run=False):
    """Send a request with the picked quality (POST /api/storage/request). Checks the storage rule first, passes
    profileId / serverId / rootFolder / is4k to Seerr, remembers the choice per kind (movie / tv / anime)."""
    kind = body["type"]
    tmdb = int(body["id"])
    seasons = body.get("seasons", "all")
    smart_mode = kind == "tv" and body.get("season_mode") == "smart"
    if smart_mode:  # start with the first season (or the one picked to start with); more come as you watch
        if not isinstance(seasons, list) or not seasons:
            info = title_info(app, "tv", tmdb, "smart")
            seasons = info.get("chosen") or [1]
        body = dict(body, seasons=seasons)
    est = None
    try:
        est = estimate(app, kind, tmdb, body.get("profile"), seasons if kind == "tv" else "all", body.get("server"),
                       body.get("is4k"))
    except Exception as e:
        print("storage: estimate before request failed:", type(e).__name__, e)
    if est and est["fit"].get("blocked"):
        raise PermissionError(est["fit"]["message"])
    payload = app.seerr.request(kind, tmdb, profile=body.get("profile"), server=body.get("server"),
                                seasons=seasons, is4k=bool(body.get("is4k")), root_folder=body.get("root"),
                                dry_run=dry_run)
    if smart_mode and not dry_run:
        from . import smart
        smart.register(app, tmdb, body.get("title"), body, payload if isinstance(payload, dict) else None)
    if not dry_run and body.get("remember", True) is not False:  # the chat's one-off choices aren't remembered
        pref_key = "anime" if body.get("anime") else kind
        prefs = settings.load().get("request_prefs") or {}
        prefs[pref_key] = {k: body.get(k) for k in ("profile", "server", "seasons", "is4k", "season_mode", "root")
                           if k in body}
        settings.save({"request_prefs": prefs})
    if not dry_run:
        forget("usage")
    return {"sent": not dry_run, "payload": payload if dry_run else None, "fit": est and est["fit"],
            "message": "Requested - it'll show up in your library once it downloads" if not dry_run else "Dry run: nothing was sent"}


# ------------------------------------------------------------------ library size
def _jf_folders(app):
    """Jellyfin's film/TV library folders (needs an admin; [] otherwise)."""
    jf = getattr(app, "jf", None)
    if not jf or not jf.configured:
        return []
    try:
        with jf.quick((3, 10)):
            vf = jf.call("GET", "/Library/VirtualFolders") or []
    except Exception:
        return []
    return [{"name": v.get("Name"), "type": v.get("CollectionType"), "path": loc} for v in vf
            if (v.get("CollectionType") or None) in MEDIA_COLLECTIONS for loc in v.get("Locations") or []]


def _walk_size(root):
    """Size of the video files under a folder, each hardlinked file counted once (cached 6 hours)."""
    st = _load_state()
    key = pathkey(root)
    hit = st["walk"].get(key)
    if hit and now() - hit.get("at", 0) < 6 * HOUR:
        return hit
    seen, total, files = set(), 0, 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "$"))]
        for f in filenames:
            p = os.path.join(dirpath, f)
            try:
                s = os.stat(p)
            except OSError:
                continue
            ident = (s.st_dev, s.st_ino) if s.st_ino else p
            if ident in seen:
                continue
            seen.add(ident)
            total += s.st_size
            files += 1
    res = {"size": total, "files": files, "at": now()}
    with _lock:
        st["walk"][key] = res
        _save_state()
    return res


def library(app):
    """What's in the library according to the *arr apps (cached 10 minutes):
    {"movies": [...], "series": [...], "ok": bool, "errors": [...]}"""
    def fetch():
        out = {"movies": [], "series": [], "ok": False, "errors": [], "roots": [], "queue": []}
        for a in arrs(app):
            try:
                roots = a.get("/rootfolder") or []
                out["roots"] += [{"path": r["path"], "free": r.get("freeSpace"), "app": a.kind} for r in roots]
                if a.kind == "radarr":
                    for m in a.get("/movie", timeout=60) or []:
                        m["_arr"] = a
                        out["movies"].append(m)
                else:
                    for s in a.get("/series", timeout=60) or []:
                        s["_arr"] = a
                        out["series"].append(s)
                q = a.get("/queue", pageSize=500, timeout=30) or {}
                out["queue"] += [{"app": a.kind, "title": r.get("title"), "size": r.get("size") or 0,
                                  "left": r.get("sizeleft") or 0, "status": r.get("status"),
                                  "movie": r.get("movieId"), "series": r.get("seriesId")}
                                 for r in q.get("records") or []]
                out["ok"] = True
            except Exception as e:
                out["errors"].append(f"{a.name}: {type(e).__name__}")
        return out
    return cached("library", 600, fetch)


def _classify(path, anime_type=False):
    return "anime" if anime_type or "anime" in (path or "").lower() else None


def usage(app, force=False):
    """The storage dashboard numbers (cached 5 minutes): used / limit / disk, Movies / TV / Anime, largest titles,
    and the projection with what's still on its way."""
    if force:
        forget("usage")
        forget("library")
    return cached("usage", 300, lambda: _usage(app))


def _usage(app):
    s = settings.load()
    lib = library(app)
    limit = int(float(s.get("storage_limit_gb") or 0) * GB)
    by = {"movies": 0, "tv": 0, "anime": 0, "other": 0}
    items = []
    roots = {pathkey(r["path"]): r for r in lib["roots"]}
    for m in lib["movies"]:
        size = m.get("sizeOnDisk") or (m.get("movieFile") or {}).get("size") or 0
        if not size:
            continue
        k = _classify(m.get("path")) or "movies"
        by[k] += size
        items.append({"kind": "movie", "title": m.get("title"), "year": m.get("year"), "size": size, "group": k,
                      "tmdb": m.get("tmdbId"), "path": m.get("path")})
    for se in lib["series"]:
        size = (se.get("statistics") or {}).get("sizeOnDisk") or 0
        if not size:
            continue
        k = _classify(se.get("path"), se.get("seriesType") == "anime") or "tv"
        by[k] += size
        items.append({"kind": "series", "title": se.get("title"), "year": se.get("year"), "size": size, "group": k,
                      "tmdb": se.get("tmdbId"), "path": se.get("path"),
                      "files": (se.get("statistics") or {}).get("episodeFileCount")})
    # Jellyfin library folders that no *arr manages (a hand-kept folder): walked, hardlinks once.
    extra = []
    for f in _jf_folders(app):
        if any(under(f["path"], r) or under(r, f["path"]) for r in roots):
            continue
        if not os.path.isdir(f["path"]):
            continue
        w = _walk_size(f["path"])
        extra.append({"path": f["path"], "name": f["name"], "size": w["size"], "files": w["files"]})
        by["anime" if "anime" in (f["name"] or "").lower() else "other"] += w["size"]
    used = sum(by.values())
    # Disks: the one most of the library is on is the one the bar shows.
    disks = {}
    for r in list(lib["roots"]) + [{"path": e["path"]} for e in extra]:
        try:
            du = shutil.disk_usage(r["path"])
            drive = os.path.splitdrive(os.path.abspath(r["path"]))[0].upper() or r["path"]
            disks[drive] = {"drive": drive, "total": du.total, "free": du.free, "used": du.used}
        except OSError:
            if r.get("free") is not None:
                disks.setdefault(r["path"], {"drive": r["path"], "total": None, "free": r["free"], "used": None})
    weight = {}
    for it in items + extra:
        d = os.path.splitdrive(os.path.abspath(it.get("path") or ""))[0].upper()
        weight[d] = weight.get(d, 0) + it["size"]
    main = max(disks.values(), key=lambda d: weight.get(d["drive"], 0), default=None)
    proj = projection(app, lib)
    items.sort(key=lambda x: -x["size"])
    out = {"used": used, "limit": limit or None, "by": by, "largest": items[:12], "extra_folders": extra,
           "disk": main, "disks": list(disks.values()), "disk_free": main and main["free"],
           "projection": proj, "projected": used + proj["total"],
           "over_limit": bool(limit and used > limit), "projected_over": bool(limit and used + proj["total"] > limit),
           "enforce": s.get("storage_enforce") or "warn", "arr_available": bool(arrs(app)) and lib["ok"],
           "errors": lib["errors"], "computed_at": now(),
           "note": "Seeding copies hardlinked from the downloads folder aren't counted twice."}
    if out["over_limit"] and out["enforce"] == "auto":
        kick()
    return out


def _slim_usage(u):
    if not u:
        return None
    return {k: u.get(k) for k in ("used", "limit", "disk_free", "projected", "over_limit", "projected_over",
                                  "enforce", "arr_available")}


def projection(app, lib=None):
    """What's still coming: monitored titles without a file (what Radarr/Sonarr will download) and requests
    waiting for approval in Seerr. Upgrades of files already here aren't counted."""
    lib = lib or library(app)
    missing_movies = missing_eps = 0
    count_m = count_e = 0
    calib_cache = {}

    def per_min(arr, profile_id):
        k = (arr.base, profile_id)
        if k not in calib_cache:
            try:
                prof = quality_profiles(arr).get(profile_id)
                est = estimate_profile(1, prof, quality_defs(arr), calibration(app, arr)) if prof else None
                calib_cache[k] = est["typical"] if est else 40 * MB
            except Exception:
                calib_cache[k] = 40 * MB
        return calib_cache[k]

    for m in lib["movies"]:
        if m.get("monitored") and not m.get("hasFile") and m.get("isAvailable"):
            missing_movies += per_min(m["_arr"], m.get("qualityProfileId")) * (m.get("runtime") or 120)
            count_m += 1
    for se in lib["series"]:
        st = se.get("statistics") or {}
        miss = max(0, (st.get("episodeCount") or 0) - (st.get("episodeFileCount") or 0))
        if se.get("monitored") and miss:
            missing_eps += per_min(se["_arr"], se.get("qualityProfileId")) * (se.get("runtime") or 30) * miss
            count_e += miss
    pending = 0
    count_p = 0
    sr = _seerr(app)
    if sr:
        try:
            reqs = cached("seerr-pending", 900, lambda: (sr.call("GET", "/request", params={
                "take": 50, "filter": "pending", "sort": "added"}, timeout=10) or {}).get("results") or [])
            for r in reqs[:20]:
                tm = (r.get("media") or {}).get("tmdbId")
                if not tm:
                    continue
                info = title_info(app, r["type"], tm, [s["seasonNumber"] for s in r.get("seasons") or []] or "all")
                pending += info["minutes"] * (110 if r["type"] == "movie" and not r.get("is4k") else 40) * MB
                count_p += 1
        except Exception:
            pass
    downloading = sum(q["left"] for q in lib["queue"])
    return {"missing_movies": int(missing_movies), "missing_movie_count": count_m, "missing_episodes": int(missing_eps),
            "missing_episode_count": count_e, "pending_requests": int(pending), "pending_count": count_p,
            "downloading_left": downloading, "queue_count": len(lib["queue"]),
            "total": int(missing_movies + missing_eps + pending)}


# ------------------------------------------------------------------ who has watched what
def viewing(app, simulate=None):
    """Watch state for every Jellyfin user, by file path:
    {"users": [...], "all_users": bool, "by_path": {pathkey: {user_id: {played, pos, last, fav}}},
     "series_fav": {series jellyfin id}, "ids": {pathkey: {id, series_id, season_id}}, "ok": bool}
    The signed-in user's state comes from the local catalog when it's newer (changes not synced yet).
    `simulate` (testing): {item id or path: {played, position, last_played}} applied to the signed-in user."""
    jf = getattr(app, "jf", None)
    out = {"users": [], "all_users": False, "by_path": {}, "series_fav": set(), "ids": {}, "ok": False,
           "me": getattr(jf, "user", None)}
    if jf and jf.configured:
        try:
            with jf.quick((4, 30)):
                users = jf.call("GET", "/Users") or []
            out["users"] = [{"id": u["Id"], "name": u.get("Name")} for u in users]
            out["all_users"] = True
        except Exception:
            if jf.user:
                out["users"] = [{"id": jf.user, "name": "you"}]
        try:
            for u in out["users"]:
                with jf.quick((4, 60)):
                    data = jf.call("GET", "/Items", params={
                        "userId": u["id"], "Recursive": "true", "IncludeItemTypes": "Movie,Episode",
                        "Fields": "Path", "EnableImages": "false", "EnableTotalRecordCount": "false"}) or {}
                for i in data.get("Items") or []:
                    k = pathkey(i.get("Path"))
                    if not k:
                        continue
                    ud = i.get("UserData") or {}
                    out["by_path"].setdefault(k, {})[u["id"]] = {
                        "played": bool(ud.get("Played")), "pos": (ud.get("PlaybackPositionTicks") or 0) / 1e7,
                        "last": ts(ud.get("LastPlayedDate")), "fav": bool(ud.get("IsFavorite"))}
                    out["ids"].setdefault(k, {"id": i.get("Id"), "series_id": i.get("SeriesId"),
                                              "season_id": i.get("SeasonId"), "name": i.get("Name")})
                with jf.quick((4, 30)):
                    favs = jf.call("GET", "/Items", params={
                        "userId": u["id"], "Recursive": "true", "IncludeItemTypes": "Series",
                        "Filters": "IsFavorite", "EnableImages": "false"}) or {}
                out["series_fav"] |= {i["Id"] for i in favs.get("Items") or []}
            out["ok"] = bool(out["users"])
        except Exception as e:
            print("storage: Jellyfin watch state failed:", type(e).__name__, e)
            out["ok"] = False
    # The local catalog: the signed-in user's newest state (and the only source when Jellyfin is away)
    cat = getattr(app, "catalog", None)
    me = out["me"] or "me"
    if cat is not None:
        with cat.lock:
            for rid, r in cat.items.items():
                if r.get("type") not in ("Movie", "Episode") or not r.get("path"):
                    continue
                k = pathkey(r["path"])
                u = cat.ud.get(rid) or {}
                mine = out["by_path"].setdefault(k, {}).get(me)
                if mine is None or (u.get("changed") or 0) >= (mine.get("last") or 0) or u.get("dirty"):
                    out["by_path"][k][me] = {"played": bool(u.get("played")), "pos": u.get("position") or 0,
                                             "last": u.get("last_played") or (mine or {}).get("last") or 0,
                                             "fav": bool(u.get("favorite")) or bool((mine or {}).get("fav"))}
                out["ids"].setdefault(k, {"id": rid, "series_id": r.get("series_id"), "season_id": r.get("season_id"),
                                          "name": r.get("name")})
            for rid, u in cat.ud.items():
                r = cat.items.get(rid)
                if r and r.get("type") == "Series" and u.get("favorite"):
                    out["series_fav"].add(rid)
    for key, val in (simulate or {}).items():
        k = key if "\\" in key or "/" in key else None
        if k is None and cat is not None:
            r = cat.get(key)
            k = r and r.get("path")
        if not k:
            continue
        k = pathkey(k)
        out["by_path"].setdefault(k, {})[me] = {"played": bool(val.get("played")), "pos": val.get("position") or 0,
                                                "last": val.get("last_played") or now(), "fav": bool(val.get("favorite"))}
    return out


def _mylist(app):
    cat = getattr(app, "catalog", None)
    if cat is None:
        return set()
    try:
        return {k[5:] for k in cat.mylist_keys() if k.startswith("item:")}
    except Exception:
        return set()


def _recent_requests(app, days):
    """tmdb ids ("movie:123" / "tv:456") requested in the last `days` days."""
    sr = _seerr(app)
    if not sr or not days:
        return set()
    try:
        data = cached("seerr-recent", 900, lambda: (sr.call("GET", "/request", params={
            "take": 100, "filter": "all", "sort": "added"}, timeout=10) or {}).get("results") or [])
    except Exception:
        return set()
    cut = now() - days * DAY
    return {f"{r['type']}:{(r.get('media') or {}).get('tmdbId')}" for r in data if ts(r.get("createdAt")) >= cut}


# ------------------------------------------------------------------ the cleanup plan
def _file_cost(path, size):
    """(frees now, frees once the seeding torrent goes, verified): a file with other hardlinks (the downloads
    folder's seeding copy) only frees its space when the last link goes."""
    try:
        st = os.stat(path)
    except OSError:
        return size, 0, False
    links = getattr(st, "st_nlink", 1) or 1
    return (size, 0, True) if links <= 1 else (0, size, True)


def plan(app, s=None, simulate=None, at=None, lib=None):
    """What the cleanup rules would remove, why, when, and what it would free. Nothing is changed.
    Returns {items (would go, by the current settings), could (watched by everyone: could go), skipped, ...}."""
    s = s or settings.load()
    t = at or now()
    lib = lib or library(app)
    view = viewing(app, simulate)
    keep = {str(k) for k in s.get("keep_ids") or []}
    mylist = _mylist(app)
    st = _load_state()
    flagged = st.get("flagged", {})
    unwatched_days = int(s.get("delete_unwatched_days") or 0)
    recent = _recent_requests(app, unwatched_days)
    grace = float(s.get("delete_after_watch_days") or 0) * DAY
    can_delete, why_not = True, None
    if not arrs(app) or not lib["ok"]:
        can_delete, why_not = False, "Radarr/Sonarr aren't connected, so Atlas can't remove anything (Settings > Storage)."
    elif not view["ok"]:
        can_delete, why_not = False, "Jellyfin isn't answering, so Atlas can't check what everyone has watched."
    elif not view["all_users"]:
        can_delete, why_not = False, ("Atlas can only see your own watch history (sign in as a Jellyfin admin), so "
                                      "it can't check whether someone else is still watching.")
    could, items, skipped = [], [], []

    def kept(*ids):
        """Why a title is protected by the user ("Marked Keep" / "In My List"), or None."""
        if any(i and str(i) in keep for i in ids):
            return "Marked Keep"
        if any(i and i in mylist for i in ids):
            return "In My List"
        return None

    def state(path):
        return view["by_path"].get(pathkey(path)) or {}

    # -------- films
    for m in lib["movies"]:
        f = m.get("movieFile")
        if not f or not f.get("path"):
            continue
        p = f["path"]
        ids = view["ids"].get(pathkey(p)) or {}
        jid = ids.get("id")
        base = {"key": jid or f"radarr:{m['id']}", "kind": "movie", "title": m.get("title"), "year": m.get("year"),
                "subtitle": str(m.get("year") or ""), "jellyfin_id": jid, "tmdb": m.get("tmdbId"),
                "arr": m["_arr"].kind, "arr_name": m["_arr"].name, "movie_id": m["id"], "file_ids": [f["id"]],
                "paths": [p], "size": f.get("size") or 0, "added": ts(f.get("dateAdded")) or ts(m.get("added")),
                "keep_keys": [x for x in (jid, f"radarr:{m['id']}", f"movie:{m.get('tmdbId')}") if x]}
        fn, fl, ver = _file_cost(p, base["size"])
        base.update(frees_now=fn, frees_after_torrent=fl, verified=ver)
        users = state(p)
        played = {u: v for u, v in users.items() if v["played"]}
        started = {u: v for u, v in users.items() if not v["played"] and v["pos"] > 0}
        favs = any(v.get("fav") for v in users.values())
        hold = kept(*base["keep_keys"]) or ("A favourite" if favs else None)
        if played and not started:
            w = max(v["last"] or 0 for v in played.values()) or t
            c = dict(base, rule="watched", watched_at=w,
                     reason=f"Watched {_ago(t - w)}" + (f" by {len(played)} people" if len(played) > 1 else ""))
            if hold:
                skipped.append(dict(c, why=hold))
                continue
            could.append(c)
            if s.get("delete_after_watch"):
                items.append(_leave(c, max(w + grace, _first(flagged, c["key"], t) + NOTICE_DAYS_MIN * DAY)))
        elif started:
            if s.get("delete_after_watch") or unwatched_days:
                skipped.append(dict(base, rule="-", why="Still being watched"))
        elif unwatched_days and not users_started(users):
            added = base["added"] or t
            if t - added < unwatched_days * DAY:
                continue
            c = dict(base, rule="unwatched", reason=f"Nobody has started it since it arrived {_ago(t - added)}")
            if hold:
                skipped.append(dict(c, why=hold))
            elif f"movie:{m.get('tmdbId')}" in recent:
                skipped.append(dict(c, why=f"Requested in the last {unwatched_days} days"))
            else:
                items.append(_leave(c, max(added + unwatched_days * DAY,
                                           _first(flagged, c["key"], t) + NOTICE_DAYS_UNWATCHED * DAY)))

    # -------- shows
    for se in lib["series"]:
        if not (se.get("statistics") or {}).get("episodeFileCount"):
            continue
        a = se["_arr"]
        try:
            eps, files = _series_files(a, se["id"])
        except Exception as e:
            skipped.append({"key": f"sonarr:{se['id']}", "kind": "series", "title": se.get("title"),
                            "why": f"Couldn't read its episodes ({type(e).__name__})"})
            continue
        _plan_series(se, a, eps, files, view, s, t, grace, unwatched_days, recent, flagged, kept, could, items,
                     skipped)

    # -------- over the limit, automatic mode: watched titles, oldest-watched first, until it fits
    u = None
    limit = int(float(s.get("storage_limit_gb") or 0) * GB)
    if limit and s.get("storage_enforce") == "auto":
        try:
            u = usage(app)
        except Exception:
            u = None
        over = (u["used"] - limit) if u else 0
        chosen = {i["key"] for i in items}
        freed = sum(i["size"] for i in items if i.get("leave_at", t) <= t + DAY)
        for c in sorted(could, key=lambda c: c.get("watched_at") or 0):
            if freed >= over:
                break
            if c["key"] in chosen:
                continue
            items.append(_leave(dict(c, rule="limit", reason=c["reason"] + "; the library is over its limit"),
                                _first(flagged, c["key"], t) + NOTICE_DAYS_MIN * DAY))
            freed += c["size"]
    for i in items:
        i["due"] = i["leave_at"] <= t
        i["leave_date"] = iso(i["leave_at"])
        i["text"] = _say(i)
    items.sort(key=lambda i: i["leave_at"])
    could.sort(key=lambda c: c.get("watched_at") or 0)
    tot = lambda xs, k: sum(x.get(k) or 0 for x in xs)
    return {"items": items, "could": could, "skipped": skipped[:200], "can_delete": can_delete, "why_not": why_not,
            "all_users": view["all_users"], "users": [u["name"] for u in view["users"]],
            "totals": {"size": tot(items, "size"), "frees_now": tot(items, "frees_now"),
                       "frees_after_torrent": tot(items, "frees_after_torrent"), "count": len(items),
                       "due": sum(1 for i in items if i["due"]), "could_size": tot(could, "size")},
            "options": {k: s.get(k) for k in ("delete_after_watch", "delete_after_watch_days", "delete_whole_seasons",
                                              "delete_unwatched_days", "delete_torrents", "storage_enforce",
                                              "storage_limit_gb")},
            "at": t}


def users_started(users):
    return any(v["played"] or v["pos"] > 0 for v in users.values())


def _first(flagged, key, t):
    return flagged.get(key) or t


def _leave(c, at):
    c = dict(c)
    c["leave_at"] = at
    return c


def _ago(sec):
    d = int(sec // DAY)
    if d <= 0:
        return "today"
    if d == 1:
        return "yesterday"
    if d < 60:
        return f"{d} days ago"
    return f"{d // 30} months ago"


def _say(i):
    frees = i["frees_now"] + (i["frees_after_torrent"] if settings.load().get("delete_torrents") else 0)
    extra = ""
    if i["frees_after_torrent"] and not settings.load().get("delete_torrents"):
        extra = f" ({fmt_size(i['frees_after_torrent'])} more once its torrent stops seeding)"
    return f"would remove {i['title']}{(' ' + i['subtitle']) if i.get('subtitle') else ''}, frees {fmt_size(frees)}{extra}"


def _series_files(arr, series_id):
    def fetch():
        return (arr.get("/episode", seriesId=series_id, timeout=60) or [],
                arr.get("/episodefile", seriesId=series_id, timeout=60) or [])
    return cached(f"eps:{arr.base}:{series_id}", 600, fetch)


def _plan_series(se, a, eps, files, view, s, t, grace, unwatched_days, recent, flagged, kept, could, items, skipped):
    fmap = {f["id"]: f for f in files}
    eps = sorted([e for e in eps if (e.get("seasonNumber") or 0) > 0 or e.get("hasFile")],
                 key=lambda e: (e.get("seasonNumber") or 0, e.get("episodeNumber") or 0))
    with_file = [e for e in eps if e.get("episodeFileId") and e["episodeFileId"] in fmap]
    if not with_file:
        return
    users_of = {}
    for e in with_file:
        users_of[e["id"]] = view["by_path"].get(pathkey(fmap[e["episodeFileId"]].get("path"))) or {}
    # Who is watching this show: anyone who played or started any of its episodes
    watchers = set()
    for us in users_of.values():
        watchers |= {u for u, v in us.items() if v["played"] or v["pos"] > 0}
    ids0 = view["ids"].get(pathkey(fmap[with_file[0]["episodeFileId"]].get("path"))) or {}
    series_jid = ids0.get("series_id")
    keys = [x for x in (series_jid, f"sonarr:{se['id']}", f"tv:{se.get('tmdbId')}") if x]
    fav = series_jid in view["series_fav"] or any(v.get("fav") for us in users_of.values() for v in us.values())
    hold = kept(*keys) or ("A favourite" if fav else None)
    title = se.get("title")
    if not watchers:
        if not unwatched_days:
            return
        newest = max(ts(fmap[e["episodeFileId"]].get("dateAdded")) for e in with_file) or t
        if t - newest < unwatched_days * DAY:
            return
        c = _tv_item(se, a, [e for e in with_file], fmap, view, "series", series_jid, title, "All episodes",
                     keys)
        c.update(rule="unwatched", added=newest,
                 reason=f"Nobody has started it since the newest episode arrived {_ago(t - newest)}")
        if hold:
            skipped.append(dict(c, why=hold))
        elif f"tv:{se.get('tmdbId')}" in recent:
            skipped.append(dict(c, why=f"Requested in the last {unwatched_days} days"))
        else:
            items.append(_leave(c, max(newest + unwatched_days * DAY,
                                       _first(flagged, c["key"], t) + NOTICE_DAYS_UNWATCHED * DAY)))
        return

    def watched_by_all(e):
        us = users_of[e["id"]]
        return all((us.get(u) or {}).get("played") for u in watchers)

    # Protected: each watcher's next-up episode, anything in progress, and the seasons those are in.
    protect_eps, current_seasons = set(), set()
    for u in watchers:
        last = None
        for k, e in enumerate(eps):
            us = view["by_path"].get(pathkey(fmap.get(e.get("episodeFileId"), {}).get("path"))) or {}
            v = us.get(u) or {}
            if v.get("pos", 0) > 0 and not v.get("played"):
                protect_eps.add(e["id"])
                current_seasons.add(e.get("seasonNumber"))
            if v.get("played"):
                last = k
        nxt = None
        if last is not None:
            for e in eps[last + 1:]:
                us = view["by_path"].get(pathkey(fmap.get(e.get("episodeFileId"), {}).get("path"))) or {}
                if not (us.get(u) or {}).get("played"):
                    nxt = e
                    break
        if nxt is not None:
            protect_eps.add(nxt["id"])
            current_seasons.add(nxt.get("seasonNumber"))
    seasons = {}
    for e in with_file:
        seasons.setdefault(e.get("seasonNumber"), []).append(e)
    whole = bool(s.get("delete_whole_seasons"))
    for sn, sep in sorted(seasons.items(), key=lambda kv: kv[0] or 0):
        done = [e for e in sep if watched_by_all(e)]
        if not done:
            continue
        if whole:
            units = [sep] if len(done) == len(sep) else []
            if len(done) != len(sep):
                continue
        else:
            byfile = {}
            for e in done:
                byfile.setdefault(e["episodeFileId"], []).append(e)
            # a file with several episodes goes only when all of them are watched
            units = [es for fid, es in byfile.items()
                     if all(watched_by_all(x) for x in with_file if x["episodeFileId"] == fid)]
        for unit in units:
            w = max((v.get("last") or 0) for e in unit for v in users_of[e["id"]].values() if v.get("played")) or t
            if whole:
                sid = (view["ids"].get(pathkey(fmap[unit[0]["episodeFileId"]].get("path"))) or {}).get("season_id")
                c = _tv_item(se, a, unit, fmap, view, "season", sid, title, f"Season {sn}",
                             keys + [x for x in (sid, f"sonarr:{se['id']}:s{sn}") if x])
            else:
                e0 = unit[0]
                ej = (view["ids"].get(pathkey(fmap[e0["episodeFileId"]].get("path"))) or {})
                sub = f"S{sn:02d}E{e0.get('episodeNumber') or 0:02d}" + (
                    f"-E{unit[-1].get('episodeNumber'):02d}" if len(unit) > 1 else "")
                c = _tv_item(se, a, unit, fmap, view, "episode", ej.get("id"), title, sub,
                             keys + [x for x in (ej.get("id"), ej.get("season_id"), f"sonarr:{se['id']}:s{sn}") if x])
            c.update(rule="watched", watched_at=w, reason=f"Watched {_ago(t - w)}" + (
                f" by all {len(watchers)} people watching it" if len(watchers) > 1 else "") + (
                "; a smart show, so later seasons arrive as you watch" if _is_smart(se.get("tmdbId")) else ""))
            blocked = None
            if any(e["id"] in protect_eps for e in unit):
                blocked = "Next up or in progress"
            elif sn in current_seasons:
                blocked = "The season being watched right now"
            elif hold or kept(*c["keep_keys"]):
                blocked = hold or kept(*c["keep_keys"])
            if blocked:
                skipped.append(dict(c, why=blocked))
                continue
            could.append(c)
            if s.get("delete_after_watch"):
                items.append(_leave(c, max(w + grace, _first(flagged, c["key"], t) + NOTICE_DAYS_MIN * DAY)))


def _is_smart(tmdb):
    try:
        from . import smart
        return bool(tmdb) and smart.is_smart(tmdb)
    except Exception:
        return False


def _tv_item(se, a, unit, fmap, view, kind, jid, title, sub, keys):
    fids = sorted({e["episodeFileId"] for e in unit})
    paths = [fmap[f]["path"] for f in fids if fmap[f].get("path")]
    size = sum(fmap[f].get("size") or 0 for f in fids)
    fn = fl = 0
    ver = True
    for f in fids:
        x, y, v = _file_cost(fmap[f].get("path"), fmap[f].get("size") or 0)
        fn, fl, ver = fn + x, fl + y, ver and v
    key = jid or f"sonarr:{se['id']}:" + (f"f{fids[0]}" if kind == "episode" else
                                           f"s{unit[0].get('seasonNumber')}" if kind == "season" else "all")
    return {"key": key, "kind": kind, "title": title, "subtitle": sub, "year": se.get("year"), "jellyfin_id": jid,
            "series_jellyfin_id": (view["ids"].get(pathkey(paths[0])) or {}).get("series_id") if paths else None,
            "tmdb": se.get("tmdbId"), "arr": a.kind, "arr_name": a.name, "series_id": se["id"],
            "episode_ids": sorted({e["id"] for e in unit} | {x for f in fids for x in (fmap[f].get("_eps") or [])}),
            "file_ids": fids, "paths": paths, "size": size, "frees_now": fn, "frees_after_torrent": fl,
            "verified": ver, "added": max(ts(fmap[f].get("dateAdded")) for f in fids),
            "keep_keys": list(dict.fromkeys([k for k in [key] + list(keys) if k]))}


# ------------------------------------------------------------------ removing
def _qbt(app):
    """(base url, session) for qBittorrent: the qbt_url setting, else the download client the *arr apps use."""
    s = settings.load()
    url = s.get("qbt_url")
    if not url:
        for a in arrs(app):
            try:
                for d in a.get("/downloadclient") or []:
                    if d.get("implementation") == "QBittorrent" and d.get("enable", True):
                        fv = {f["name"]: f.get("value") for f in d.get("fields") or []}
                        url = f"{'https' if fv.get('useSsl') else 'http'}://{fv.get('host')}:{fv.get('port')}{fv.get('urlBase') or ''}"
                        break
            except Exception:
                continue
            if url:
                break
    if not url:
        return None, None
    return url.rstrip("/"), requests.Session()


def _torrent_hashes(arr, item):
    """Torrent hashes the *arr apps grabbed this title's files from (their import history)."""
    out = set()
    try:
        if arr.kind == "radarr":
            hist = arr.get("/history/movie", movieId=item["movie_id"]) or []
        else:
            hist = arr.get("/history/series", seriesId=item["series_id"]) or []
            eps = set(item.get("episode_ids") or [])
            hist = [h for h in hist if h.get("episodeId") in eps]
        for h in hist:
            if h.get("eventType") in ("downloadFolderImported", "grabbed") and h.get("downloadId"):
                out.add(h["downloadId"].lower())
    except Exception:
        pass
    return out


def _torrent_cleanup(app, hashes, ours=frozenset()):
    """Remove each torrent (and its files) once none of its video files still has a library hardlink. `ours`:
    (device, inode) of the files just removed; their remaining link may sit in the *arr recycle bin, which
    doesn't count as the library."""
    base, qs = _qbt(app)
    report = []
    if not base or not hashes:
        return report
    for h in hashes:
        try:
            r = qs.get(f"{base}/api/v2/torrents/info", params={"hashes": h}, timeout=10)
            if r.status_code == 403:
                report.append({"hash": h, "removed": False, "why": "qBittorrent needs a sign-in"})
                continue
            info = (r.json() or [None])[0]
            if not info:
                continue
            files = qs.get(f"{base}/api/v2/torrents/files", params={"hash": h}, timeout=10).json() or []
            save = info.get("save_path") or ""
            shared = []
            for f in files:
                p = os.path.join(save, f.get("name") or "")
                if os.path.splitext(p)[1].lower() not in VIDEO:
                    continue
                try:
                    st = os.stat(p)
                    if (st.st_nlink or 1) > 1 and (st.st_dev, st.st_ino) not in ours:
                        shared.append(f.get("name"))
                except OSError:
                    pass
            if shared:
                report.append({"hash": h, "name": info.get("name"), "removed": False,
                               "why": f"{len(shared)} file(s) still in the library"})
                continue
            size = info.get("size") or 0
            qs.post(f"{base}/api/v2/torrents/delete", data={"hashes": h, "deleteFiles": "true"}, timeout=10)
            report.append({"hash": h, "name": info.get("name"), "removed": True, "freed": size})
        except Exception as e:
            report.append({"hash": h, "removed": False, "why": type(e).__name__})
    return report


def _viewing_cached(app):
    return cached("view", 60, lambda: viewing(app))


def _catalog_row(app, path=None, item_id=None):
    cat = getattr(app, "catalog", None)
    if cat is None:
        return None, None
    with cat.lock:
        rid = cat._resolve(item_id) if item_id else cat.by_path.get(pathkey(path)) if path else None
        r = cat.items.get(rid or "")
        return (dict(r) if r else None), (dict(cat.ud.get(rid) or {}) if rid else None)


def record_history(app, item, view=None, reason=None):
    """Before anything is removed: write a "removed" record per film / episode to the catalog (ids, title,
    artwork, runtime, genres, everyone's played state), so watched stays watched after the file is gone."""
    cat = getattr(app, "catalog", None)
    if cat is None or not hasattr(cat, "record_removed"):
        return []
    view = view or _viewing_cached(app)
    me = view.get("me") or "me"
    keys = []
    for p in item.get("paths") or []:
        row, ud = _catalog_row(app, path=p)
        users = {}
        for uid, v in (view["by_path"].get(pathkey(p)) or {}).items():
            users[uid] = {"played": bool(v.get("played")), "last_played": v.get("last") or 0,
                          "position": v.get("pos") or 0, "favorite": bool(v.get("fav")),
                          "play_count": (ud or {}).get("play_count") if uid == me else (1 if v.get("played") else 0)}
        series = None
        if row and row.get("series_id"):
            series, _ = _catalog_row(app, item_id=row["series_id"])
        prov = (row or {}).get("provider") or {}
        sprov = (series or {}).get("provider") or {}
        kind = "movie" if item["kind"] == "movie" else "episode"
        data = {"tmdb": item.get("tmdb") or (sprov.get("Tmdb") if kind == "episode" else prov.get("Tmdb")),
                "tvdb": sprov.get("Tvdb") if kind == "episode" else prov.get("Tvdb"),
                "imdb": prov.get("Imdb"), "episode_tvdb": prov.get("Tvdb") if kind == "episode" else None,
                "title": (row or {}).get("name") or item["title"], "year": (row or {}).get("year") or item.get("year"),
                "series": item["title"] if kind == "episode" else None,
                "season": (row or {}).get("season_no"), "episode": (row or {}).get("episode_no"),
                "poster": (row or {}).get("poster"), "backdrop": (row or {}).get("backdrop") or (series or {}).get("backdrop"),
                "thumb": (row or {}).get("thumb"), "series_poster": (series or {}).get("poster"),
                "runtime": (row or {}).get("runtime"), "genres": (row or series or {}).get("genres") or [],
                "rating": (row or series or {}).get("rating"), "overview": (row or {}).get("overview"),
                "anime": (row or {}).get("library") == "anime", "favorite": any(u["favorite"] for u in users.values()),
                "users": users, "size": item.get("size"), "path": p,
                "jellyfin_id": (row or {}).get("id"), "series_jellyfin_id": (row or {}).get("series_id")}
        if kind == "episode" and (data["season"] is None or data["episode"] is None):
            m = re.search(r"[Ss](\d{1,3})[Ee](\d{1,4})", os.path.basename(p))
            if m:
                data["season"], data["episode"] = int(m.group(1)), int(m.group(2))
        try:
            keys.append(cat.record_removed(kind, data, reason=reason or item.get("reason")))
        except Exception as e:
            print("storage: couldn't record history:", type(e).__name__, e)
    return keys


def recycle(paths):
    """Send files to the Windows Recycle Bin (never a hard delete). Returns the paths that went."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    done = []
    for p in paths:
        if not os.path.exists(p):
            continue
        op = SHFILEOPSTRUCTW(None, FO_DELETE, os.path.abspath(p) + "\0\0", None,
                             FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if rc == 0 and not op.fAnyOperationsAborted and not os.path.exists(p):
            done.append(p)
    return done


def remove(app, item, s=None, view=None, also_torrent=None, keep_monitored=False, manual=False):
    """Remove one item for real. A "removed" record goes to the catalog first (watch history survives). Then:
    Radarr/Sonarr unmonitor (unless keep_monitored) and delete the files (their recycle bin applies), or, for a
    file no *arr manages, the Windows Recycle Bin; Jellyfin and Seerr are told; optionally the seeding torrent
    goes once no library file uses it. Returns the deletion-log entry."""
    s = s or settings.load()
    if not deletes_allowed():
        raise PermissionError("Removing files is turned off in this development run")
    also_torrent = s.get("delete_torrents") if also_torrent is None else also_torrent
    arr = None
    if item.get("arr"):
        arr = next((a for a in arrs(app, item["arr"]) if a.name == item.get("arr_name")), None) or \
            arr_for(app, item["arr"])
        if arr is None:
            raise RuntimeError("Radarr/Sonarr isn't connected")
    sizes_before, ours = {}, set()
    for p in item.get("paths") or []:
        try:
            st = os.stat(p)
            sizes_before[p] = (st.st_size, st.st_nlink or 1)
            ours.add((st.st_dev, st.st_ino))
        except OSError:
            pass
    record_history(app, item, view, reason=item.get("reason"))
    hashes = _torrent_hashes(arr, item) if (also_torrent and arr) else set()
    steps = []
    if arr is None:  # a file only Atlas knows (a library folder, a provisional download)
        gone = recycle(item.get("paths") or [])
        if len(gone) != len(item.get("paths") or []):
            raise RuntimeError("Windows couldn't move the file to the Recycle Bin")
        steps.append("moved to the Recycle Bin")
        cat = getattr(app, "catalog", None)
        if cat is not None:
            try:
                cat.kick(scan=True)
            except Exception:
                pass
    elif arr.kind == "radarr":
        if not keep_monitored:
            arr.send("PUT", "/movie/editor", {"movieIds": [item["movie_id"]], "monitored": False})
            steps.append("unmonitored")
        for fid in item["file_ids"]:
            arr.send("DELETE", f"/moviefile/{fid}")
        steps.append("file deleted")
    else:
        if not keep_monitored:
            if item.get("episode_ids"):
                arr.send("PUT", "/episode/monitor", {"episodeIds": item["episode_ids"], "monitored": False})
            if item["kind"] == "series":
                arr.send("PUT", "/series/editor", {"seriesIds": [item["series_id"]], "monitored": False})
            steps.append("unmonitored")
        for fid in item["file_ids"]:
            arr.send("DELETE", f"/episodefile/{fid}")
        steps.append(f"{len(item['file_ids'])} file(s) deleted")
    # Jellyfin: tell it the files are gone (it also notices on its own)
    jf = getattr(app, "jf", None)
    if jf and jf.configured and item.get("paths"):
        try:
            with jf.quick((3, 10)):
                jf.call("POST", "/Library/Media/Updated",
                        json={"Updates": [{"Path": p, "UpdateType": "Deleted"} for p in item.get("paths") or []]})
            steps.append("Jellyfin told")
        except Exception:
            pass
    # Seerr: a film (or a whole show) that's gone becomes requestable again
    sr = _seerr(app)
    if sr and arr is not None and item["kind"] in ("movie", "series") and item.get("tmdb"):
        try:
            d = sr.call("GET", f"/{'movie' if item['kind'] == 'movie' else 'tv'}/{item['tmdb']}", timeout=10) or {}
            mid = (d.get("mediaInfo") or {}).get("id")
            if mid:
                sr.call("DELETE", f"/media/{mid}", timeout=10)
                steps.append("Seerr cleared")
        except Exception:
            pass
    torrents = _torrent_cleanup(app, hashes, frozenset(ours)) if hashes else []
    freed = sum(sz for p, (sz, links) in sizes_before.items() if links <= 1)
    if not sizes_before:
        freed = item.get("frees_now") or 0
    freed_t = sum(t.get("freed") or 0 for t in torrents if t.get("removed"))
    shared = sum(sz for p, (sz, links) in sizes_before.items() if links > 1)
    entry = {"at": now(), "title": item["title"], "subtitle": item.get("subtitle"), "kind": item["kind"],
             "size": item["size"], "freed": freed + (min(shared, freed_t) if freed_t else 0),
             "still_seeding": max(0, shared - freed_t), "reason": item.get("reason"),
             "rule": "manual" if manual else item.get("rule"), "steps": steps, "torrents": torrents,
             "key": item["key"], "recycle_bin": _recycle_bin(arr) if arr is not None else "Windows Recycle Bin"}
    if entry["recycle_bin"]:
        entry["note"] = "The space comes back once the recycle bin is emptied (the apps clean theirs on a schedule)."
    _log(entry)
    forget("library")
    forget("usage")
    forget("plan")
    forget("view")
    if arr is not None:
        forget(f"eps:{arr.base}:{item.get('series_id')}")
    return entry


# ------------------------------------------------------------------ deleting by hand
def _recycle_bin(arr):
    if arr is None:
        return "Windows Recycle Bin"
    try:
        mm = cached(f"mm:{arr.base}", HOUR, lambda: arr.get("/config/mediamanagement")) or {}
        return mm.get("recycleBin") or None
    except Exception:
        return None


def manual_item(app, item_id, scope=None):
    """Turn a library id (film, show, season or episode, as the details page knows it) and a scope (movie |
    episode | season | series) into a removable item, in the same shape as the cleanup plan's items."""
    row, _ = _catalog_row(app, item_id=item_id)
    if row is None:
        raise LookupError("That title isn't in the library on this PC")
    typ = row["type"]
    scope = scope or {"Movie": "movie", "Episode": "episode", "Season": "season", "Series": "series"}.get(typ)
    lib = library(app)
    if typ == "Movie":
        p = row.get("path")
        tmdb = (row.get("provider") or {}).get("Tmdb")
        m = next((m for m in lib["movies"] if (m.get("movieFile") or {}).get("path") and
                  pathkey(m["movieFile"]["path"]) == pathkey(p)), None) or \
            next((m for m in lib["movies"] if tmdb and str(m.get("tmdbId")) == str(tmdb) and m.get("movieFile")), None)
        base = {"key": row["id"], "kind": "movie", "title": row.get("name"), "year": row.get("year"),
                "subtitle": str(row.get("year") or ""), "jellyfin_id": row["id"], "tmdb": tmdb,
                "reason": "Deleted by hand"}
        if m:
            f = m["movieFile"]
            base.update(arr=m["_arr"].kind, arr_name=m["_arr"].name, movie_id=m["id"], file_ids=[f["id"]],
                        paths=[f["path"]], size=f.get("size") or 0, tmdb=m.get("tmdbId"))
        else:
            base.update(arr=None, file_ids=[], paths=[p] if p and os.path.exists(p) else [],
                        size=os.path.getsize(p) if p and os.path.exists(p) else 0)
        return _costed(base)
    # TV: find the show in Sonarr by the episode paths (or the show's tmdb/tvdb id)
    series_row = row if typ == "Series" else _catalog_row(app, item_id=row.get("series_id"))[0] or {}
    cat = app.catalog
    with cat.lock:
        eps = list(cat.eps.get(series_row.get("id"), []))
    if typ == "Episode":
        want = [row] if scope == "episode" else [e for e in eps if e.get("season_no") == row.get("season_no")] \
            if scope == "season" else eps
    elif typ == "Season":
        want = [e for e in eps if e.get("season_id") == row["id"]] if scope != "series" else eps
    else:
        want = eps
    want_paths = {pathkey(e.get("path")) for e in want if e.get("path")}
    sprov = series_row.get("provider") or {}
    se = next((x for x in lib["series"] if (sprov.get("Tmdb") and str(x.get("tmdbId")) == str(sprov["Tmdb"])) or
               (sprov.get("Tvdb") and str(x.get("tvdbId")) == str(sprov["Tvdb"]))), None)
    if se is None and want_paths:
        se = next((x for x in lib["series"] if any(under(p, x.get("path")) for p in want_paths)), None)
    sub = {"episode": f"S{row.get('season_no') or 0:02d}E{row.get('episode_no') or 0:02d}" if typ == "Episode" else "",
           "season": f"Season {row.get('season_no') if typ != 'Series' else ''}".strip(),
           "series": "Whole show"}.get(scope, "")
    if scope == "season" and typ == "Season":
        sub = row.get("name") or sub
    base = {"key": f"{row['id']}:{scope}", "kind": "series" if scope == "series" else "episode" if scope == "episode"
            else "season", "title": series_row.get("name") or row.get("series_name"), "subtitle": sub,
            "year": series_row.get("year"), "jellyfin_id": row["id"], "series_jellyfin_id": series_row.get("id"),
            "tmdb": sprov.get("Tmdb"), "reason": "Deleted by hand"}
    if se is not None:
        a = se["_arr"]
        all_eps, files = _series_files(a, se["id"])
        fmap = {f["id"]: f for f in files}
        chosen = [f for f in files if pathkey(f.get("path")) in want_paths]
        if scope == "series":
            chosen = files
        fids = sorted({f["id"] for f in chosen})
        ep_ids = sorted({e["id"] for e in all_eps if e.get("episodeFileId") in fids})
        if scope == "series":
            ep_ids = sorted({e["id"] for e in all_eps})
        base.update(arr=a.kind, arr_name=a.name, series_id=se["id"], tmdb=se.get("tmdbId") or base["tmdb"],
                    file_ids=fids, episode_ids=ep_ids, paths=[fmap[f]["path"] for f in fids],
                    size=sum(fmap[f].get("size") or 0 for f in fids))
    else:
        paths = [e["path"] for e in want if e.get("path") and os.path.exists(e["path"])]
        base.update(arr=None, file_ids=[], episode_ids=[], paths=paths,
                    size=sum(os.path.getsize(p) for p in paths))
    return _costed(base)


def _costed(item):
    fn = fl = 0
    ver = True
    for p in item.get("paths") or []:
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        x, y, v = _file_cost(p, size)
        fn, fl, ver = fn + x, fl + y, ver and v
    if not item.get("paths"):
        fn = 0
    elif not ver and item.get("size"):
        fn = item["size"]
    item.update(frees_now=fn, frees_after_torrent=fl, verified=ver)
    return item


def delete_preview(app, item_id, scope=None, also_torrent=None, keep_monitored=False):
    """GET /api/storage/delete/preview: what deleting would do, before anything happens."""
    s = settings.load()
    also_torrent = bool(s.get("delete_torrents")) if also_torrent is None else bool(also_torrent)
    item = manual_item(app, item_id, scope)
    arr = None
    if item.get("arr"):
        arr = next((a for a in arrs(app, item["arr"]) if a.name == item.get("arr_name")), None)
    view = _viewing_cached(app)
    warnings = []
    me = view.get("me")
    mid, done_other = [], []
    names = {u["id"]: u["name"] for u in view["users"]}
    for p in item.get("paths") or []:
        for uid, v in (view["by_path"].get(pathkey(p)) or {}).items():
            if v.get("pos", 0) > 0 and not v.get("played"):
                mid.append(names.get(uid) or ("You" if uid == me else "Someone"))
    if mid:
        warnings.append(f"{', '.join(sorted(set(mid)))} {'is' if len(set(mid)) == 1 else 'are'} partway through this.")
    cat = getattr(app, "catalog", None)
    if cat is not None and item.get("series_jellyfin_id") and item["kind"] in ("episode", "season"):
        try:
            with cat.lock:
                nxt = cat.next_up_for(item["series_jellyfin_id"], first_if_new=False)
            if nxt and nxt.get("path") and pathkey(nxt["path"]) in {pathkey(p) for p in item.get("paths") or []}:
                warnings.append(f"S{nxt.get('season_no') or 0:02d}E{nxt.get('episode_no') or 0:02d} is next up for you.")
        except Exception:
            pass
    mylist = _mylist(app)
    keepers = {str(k) for k in s.get("keep_ids") or []}
    ids = {item.get("jellyfin_id"), item.get("series_jellyfin_id")}
    if ids & mylist:
        warnings.append("It's in My List.")
    if {str(i) for i in ids if i} & keepers:
        warnings.append("It's marked Keep.")
    if any(v.get("fav") for p in item.get("paths") or [] for v in (view["by_path"].get(pathkey(p)) or {}).values()):
        warnings.append("Someone has it as a favourite.")
    if item["frees_after_torrent"] and not also_torrent:
        warnings.append(f"{fmt_size(item['frees_after_torrent'])} is also held by a seeding torrent, so that space "
                        f"only comes back when the torrent is removed.")
    if not item.get("paths"):
        warnings.append("There's no file on this PC to delete.")
    if not deletes_allowed():
        warnings.append("Deleting is turned off in this development run (preview only).")
    frees = item["frees_now"] + (item["frees_after_torrent"] if also_torrent else 0)
    return {"item": _slim_item(item), "title": item["title"], "subtitle": item.get("subtitle"), "scope": scope or item["kind"],
            "files": [{"path": p, "size": _size(p)} for p in item.get("paths") or []], "size": item["size"],
            "size_freed": frees, "frees_now": item["frees_now"], "frees_after_torrent": item["frees_after_torrent"],
            "torrent_holds": bool(item["frees_after_torrent"]) and not also_torrent,
            "unmonitor": bool(arr) and not keep_monitored, "via": arr.name if arr else "Windows Recycle Bin",
            "recycle_bin": _recycle_bin(arr), "warnings": warnings, "can_delete": bool(item.get("paths")) and
            deletes_allowed(), "scopes": _scopes(app, item_id),
            "text": f"Delete {item['title']}{(' ' + item['subtitle']) if item.get('subtitle') else ''}? Frees {fmt_size(frees)}"}


def _size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return None


def _scopes(app, item_id):
    row, _ = _catalog_row(app, item_id=item_id)
    typ = (row or {}).get("type")
    return {"Movie": ["movie"], "Episode": ["episode", "season", "series"], "Season": ["season", "series"],
            "Series": ["series"]}.get(typ, [])


def delete_now(app, item_id, scope=None, also_torrent=None, keep_monitored=False):
    """POST /api/storage/delete: delete by hand, through the same safe path as the cleanup rules."""
    item = manual_item(app, item_id, scope)
    if not item.get("paths"):
        raise LookupError("There's no file on this PC to delete")
    return remove(app, item, also_torrent=also_torrent, keep_monitored=keep_monitored, manual=True)


def _log(entry):
    with _lock:
        try:
            log = json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            log = []
        log.insert(0, entry)
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LOG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(log[:LOG_MAX], indent=1), encoding="utf-8")
        tmp.replace(LOG_FILE)


def read_log(limit=100):
    try:
        return json.loads(LOG_FILE.read_text(encoding="utf-8"))[:limit]
    except Exception:
        return []


def keep(item_id, on=True):
    """Mark a title "Keep" (never removed by the rules) or undo it. Accepts plan keys and Jellyfin ids."""
    s = settings.load()
    ids = [str(x) for x in s.get("keep_ids") or []]
    item_id = str(item_id)
    if on and item_id not in ids:
        ids.append(item_id)
    if not on:
        ids = [x for x in ids if x != item_id]
    settings.save({"keep_ids": ids})
    with _lock:
        _load_state()["flagged"].pop(item_id, None)
        _save_state()
    return ids


# ------------------------------------------------------------------ the background job
def run(app, dry_run=True, simulate=None):
    """One pass of the cleanup rules. dry_run: say what would happen ("would remove X, frees Y GB"), change
    nothing. A real run remembers when each title was first announced (its "Leaving soon" clock), sends the
    day-before notice, and removes what's due, never while something is playing."""
    s = settings.load()
    p = plan(app, s, simulate)
    t = now()
    st = _load_state()
    actions, notices = [], []
    real = not dry_run and p["can_delete"] and deletes_allowed()
    if not dry_run:
        with _lock:
            live = {i["key"] for i in p["items"]}
            for k in list(st["flagged"]):
                if k not in live:
                    st["flagged"].pop(k)
                    st["notified"].pop(k, None)
            for i in p["items"]:
                st["flagged"].setdefault(i["key"], t)
            _save_state()
    playing = bool(getattr(getattr(app, "player", None), "playing", False))
    for i in p["items"]:
        if i["due"]:
            if not real or playing:
                actions.append({"key": i["key"], "title": i["title"], "subtitle": i.get("subtitle"),
                                "would": True, "text": i["text"], "size": i["size"], "frees_now": i["frees_now"],
                                "frees_after_torrent": i["frees_after_torrent"], "rule": i["rule"],
                                "why": None if dry_run else (p["why_not"] or (
                                    "something is playing" if playing else "deletions are off in this run"))})
                continue
            try:
                e = remove(app, i, s)
                actions.append({"key": i["key"], "title": i["title"], "removed": True, "freed": e["freed"],
                                "still_seeding": e["still_seeding"]})
            except Exception as ex:
                actions.append({"key": i["key"], "title": i["title"], "removed": False, "error": str(ex)})
        elif i["leave_at"] - t <= DAY and not st["notified"].get(i["key"]):
            notices.append(i)
    if notices and not dry_run:
        with _lock:
            for i in notices:
                st["notified"][i["key"]] = t
            _save_state()
        try:
            app.notify_ui("storage-leaving", {
                "message": (f"{notices[0]['title']} leaves tomorrow" if len(notices) == 1 else
                            f"{len(notices)} titles leave tomorrow") + " to save space. Tap Keep to hold on to them.",
                "items": [leaving_card(i) for i in notices]})
        except Exception:
            pass
    return {"dry_run": dry_run or not real, "actions": actions, "notices": [i["key"] for i in notices],
            "plan": {k: p[k] for k in ("totals", "can_delete", "why_not", "options")}, "at": t}


def leaving_card(i):
    """What the Home "Leaving soon" row needs for one title."""
    return {"key": i["key"], "id": i.get("jellyfin_id") or i.get("series_jellyfin_id"), "kind": i["kind"],
            "title": i["title"], "subtitle": i.get("subtitle"), "leave_at": i["leave_at"],
            "leave_date": iso(i["leave_at"]), "reason": i.get("reason"), "rule": i.get("rule"),
            "size": i["size"], "series_id": i.get("series_jellyfin_id"), "keep_key": i["key"]}


def leaving_soon(app, days=7):
    """Titles leaving within `days` (the Home row), soonest first. Empty when no rule is on."""
    s = settings.load()
    if not (s.get("delete_after_watch") or s.get("delete_unwatched_days") or
            (s.get("storage_enforce") == "auto" and s.get("storage_limit_gb"))):
        return []
    p = cached("plan", 300, lambda: plan(app, s))
    t = now()
    return [leaving_card(i) for i in p["items"] if i["leave_at"] - t <= days * DAY]


class Job:
    def __init__(self, app):
        self.app = app
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self.loop, name="storage", daemon=True)
        self.last = None
        self.stop = False

    def loop(self):
        time.sleep(90)  # let the window and the catalog settle first
        try:
            from . import smart
            smart.start(self.app)
        except Exception as e:
            print("smart requests unavailable:", type(e).__name__, e)
        while not self.stop:
            try:
                from . import smart
                forget("view")
                for r in smart.check(self.app):
                    if r["action"] in ("request", "error", "waiting_space"):
                        print("smart requests:", r.get("text"), flush=True)
            except Exception as e:
                print("smart requests failed:", type(e).__name__, e, flush=True)
            try:
                s = settings.load()
                active = s.get("delete_after_watch") or s.get("delete_unwatched_days") or (
                    s.get("storage_enforce") == "auto" and s.get("storage_limit_gb"))
                if active:
                    forget("library")
                    forget("plan")
                    self.last = run(self.app, dry_run=False)
                    done = [a for a in self.last["actions"] if a.get("removed")]
                    if done:
                        print(f"storage: removed {len(done)} title(s)", flush=True)
                        try:
                            self.app.notify_ui("storage-removed", {"items": done})
                        except Exception:
                            pass
            except Exception as e:
                print("storage job failed:", type(e).__name__, e, flush=True)
            self.wake.wait(HOUR)
            self.wake.clear()


def start(app):
    """Start the hourly job (App.started). Safe to call twice."""
    global _job
    if _job is None:
        _job = Job(app)
        _job.thread.start()
        try:  # watched 4K films to 1080p (Settings > Storage)
            from . import shrink
            shrink.start(app)
        except Exception as e:
            print("shrink unavailable:", type(e).__name__, e)
    return _job


def kick():
    """Run the job soon (the library went over its limit, or the settings changed)."""
    if _job is not None:
        _job.wake.set()


def status(app):
    """GET /api/storage: dashboard numbers, the plan summary and the settings in one answer."""
    s = settings.load()
    u = usage(app)
    try:
        p = cached("plan", 300, lambda: plan(app, s))
    except Exception as e:
        p = {"items": [], "could": [], "skipped": [], "can_delete": False, "why_not": f"Couldn't plan: {e}",
             "totals": {}}
    return {"usage": u, "leaving": [leaving_card(i) for i in p["items"]][:30],
            "could": [_slim_item(c) for c in p["could"]][:30],
            "could_size": sum(c["size"] for c in p["could"]), "can_delete": p["can_delete"], "why_not": p["why_not"],
            "totals": p.get("totals"), "settings": settings_view(s), "log": read_log(20),
            "arrs": [a.public() for a in arrs(app)], "deletes_allowed": deletes_allowed(), "shrink": _shrink_status()}


def _shrink_status():
    try:
        from . import shrink
        return shrink.status()
    except Exception:
        return None


def _slim_item(c):
    return {k: c.get(k) for k in ("key", "kind", "title", "subtitle", "size", "frees_now", "frees_after_torrent",
                                  "reason", "rule", "watched_at", "jellyfin_id", "series_jellyfin_id", "leave_at",
                                  "leave_date", "due", "text")}


SETTING_KEYS = ("storage_limit_gb", "storage_enforce", "delete_after_watch", "delete_after_watch_days",
                "delete_whole_seasons", "delete_unwatched_days", "delete_torrents", "radarr_url", "sonarr_url",
                "smart_request", "smart_request_at", "shrink_4k_watched")


def settings_view(s):
    return {**{k: s.get(k) for k in SETTING_KEYS}, "keep_ids": s.get("keep_ids") or [],
            "radarr_key_set": bool(s.get("radarr_key")), "sonarr_key_set": bool(s.get("sonarr_key"))}


def save_settings(changes):
    """POST /api/storage/settings: validated; the *arr keys are set-only (empty = unchanged, never sent back)."""
    out = {}
    for k, v in (changes or {}).items():
        if k == "storage_limit_gb":
            out[k] = max(0.0, float(v or 0))
        elif k == "storage_enforce":
            if v not in ("warn", "block", "auto"):
                raise ValueError("storage_enforce must be warn, block or auto")
            out[k] = v
        elif k == "smart_request_at":
            out[k] = max(10, min(100, int(v or 50)))
        elif k in ("delete_after_watch", "delete_whole_seasons", "delete_torrents", "smart_request", "shrink_4k_watched"):
            out[k] = bool(v)
        elif k == "delete_after_watch_days":
            out[k] = max(0, min(365, int(v or 0)))
        elif k == "delete_unwatched_days":
            out[k] = max(0, min(3650, int(v or 0)))
        elif k in ("radarr_url", "sonarr_url"):
            v = (v or "").strip().rstrip("/")
            if v and not re.match(r"^https?://", v):
                raise ValueError("The address should start with http:// or https://")
            out[k] = v
        elif k in ("radarr_key", "sonarr_key"):
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()
    s = settings.save(out)
    forget()
    kick()
    if out.get("shrink_4k_watched"):
        from . import shrink
        shrink.kick()
    return settings_view(s)
