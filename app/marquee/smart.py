"""Smart requesting for shows: request season 1 now, the next season once someone is about halfway through the
season they're watching, with the same quality profile, root folder and 4K flag.

A show becomes "smart" when it's requested with season_mode "smart" (marquee.storage.send_request). Each show is a
row in smart.json: {tmdb, title, seerr_user, profile, server, root, is4k, anime, requested: [season numbers],
next, state, history}. A check runs when watch progress changes (poke(), debounced) and hourly (sweep()):

- base season: the highest season anyone has watched or started (someone who jumps to S3 counts from S3)
- trigger: watched episodes / episodes in that season >= smart_request_at (default 50%); 30% for 4K or a big season
- next season: the first later season (specials excluded) that has started airing and isn't requested or available
  in Seerr yet; a season still airing is requested too (Sonarr grabs episodes as they air)
- storage: over the limit with "block" enforcement -> no request, state "waiting_space" and a notice instead

Nothing here sends a request in dry-run mode: check(dry_run=True) says what it would do and why.
"""
import json
import threading
import time
from datetime import date

from . import settings

FILE = settings.DIR / "smart.json"
_lock = threading.RLock()
_timer = None
DAY = 86400
BIG_SEASON = 120 * 1024 ** 3  # a season this big is fetched earlier (30%)


def _load():
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data):
    with _lock:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(FILE)


def shows():
    return _load()


def get(tmdb):
    return _load().get(str(tmdb))


def is_smart(tmdb):
    e = get(tmdb)
    return bool(e and e.get("state") not in ("stopped",))


def register(app, tmdb, title, body, request_result=None):
    """A show was requested in smart mode (season 1 or the season picked to start with)."""
    me = None
    try:
        me = (app.seerr.call("GET", "/auth/me", timeout=8) or {}).get("id")
    except Exception:
        pass
    seasons = body.get("seasons") if isinstance(body.get("seasons"), list) else []
    with _lock:
        data = _load()
        e = data.get(str(tmdb)) or {"tmdb": int(tmdb), "created": time.time(), "history": []}
        e.update(title=title or e.get("title"), seerr_user=me, profile=body.get("profile"), server=body.get("server"),
                 root=body.get("root"), is4k=bool(body.get("is4k")), anime=bool(body.get("anime")),
                 requested=sorted(set((e.get("requested") or []) + seasons)), state="watching",
                 request_id=(request_result or {}).get("id"))
        e["next"] = (max(e["requested"]) + 1) if e["requested"] else 2
        e["history"].append({"at": time.time(), "seasons": seasons, "request_id": e.get("request_id"), "why": "first"})
        data[str(tmdb)] = e
        _save(data)
    return e


def stop(tmdb):
    with _lock:
        data = _load()
        if str(tmdb) in data:
            data[str(tmdb)]["state"] = "stopped"
            _save(data)
    return get(tmdb)


# ------------------------------------------------------------------ what Seerr knows about the show
def _seerr_seasons(app, tmdb):
    """[{number, episodes, air_date, aired, status (Seerr: 1 unknown .. 5 available), requested}] (no specials)."""
    d = app.seerr.call("GET", f"/tv/{tmdb}", timeout=10) or {}
    info = d.get("mediaInfo") or {}
    status = {s["seasonNumber"]: s.get("status") for s in info.get("seasons") or []}
    requested = set()
    for r in info.get("requests") or []:
        if r.get("status") in (1, 2, 5):  # pending / approved / completed (not declined or failed)
            requested |= {s["seasonNumber"] for s in r.get("seasons") or []}
    today = date.today().isoformat()
    out = []
    for s in d.get("seasons") or []:
        n = s.get("seasonNumber") or 0
        if n <= 0:
            continue
        air = s.get("airDate") or ""
        out.append({"number": n, "episodes": s.get("episodeCount") or 0, "air_date": air,
                    "aired": bool(air) and air <= today, "status": status.get(n),
                    "requested": n in requested or (status.get(n) or 1) >= 2})
    return {"title": d.get("name"), "seasons": out, "ended": d.get("status") in ("Ended", "Canceled"),
            "media_id": info.get("id")}


# ------------------------------------------------------------------ watch progress
def progress(app, tmdb, view=None):
    """Per season: episodes on disk and how many someone has watched. {season: {have, watched, started}}."""
    from . import storage
    cat = getattr(app, "catalog", None)
    if cat is None:
        return {}, None
    show = cat.by_tmdb("tv", tmdb)
    if not show:
        return {}, None
    view = view or storage._viewing_cached(app)
    out = {}
    with cat.lock:
        eps = list(cat.eps.get(show["id"], []))
    for e in eps:
        n = e.get("season_no") or 0
        if n <= 0:
            continue
        users = view["by_path"].get(storage.pathkey(e.get("path"))) or {} if e.get("path") else {}
        if not users:  # not in Jellyfin's answer: the catalog's own state for this user
            u = cat.ud.get(e["id"]) or {}
            users = {"me": {"played": bool(u.get("played")), "pos": u.get("position") or 0}}
        s = out.setdefault(n, {"have": 0, "watched": 0, "started": 0})
        s["have"] += 1
        if any(v.get("played") for v in users.values()):
            s["watched"] += 1
        elif any((v.get("pos") or 0) > 0 for v in users.values()):
            s["started"] += 1
    return out, show


def decide(app, entry, view=None, s=None):
    """What to do for one smart show now: {action: request|wait|waiting_space|done|none, season, why, ...}."""
    from . import storage
    s = s or settings.load()
    tmdb = entry["tmdb"]
    prog, show = progress(app, tmdb, view)
    try:
        sd = storage.cached(f"smart-seerr:{tmdb}", 900, lambda: _seerr_seasons(app, tmdb))
    except Exception as e:
        return {"action": "none", "why": f"Seerr didn't answer ({type(e).__name__})"}
    seasons = {x["number"]: x for x in sd["seasons"]}
    title = entry.get("title") or sd["title"]
    active = [n for n, p in prog.items() if p["watched"] or p["started"]]
    if not active:
        return {"action": "wait", "why": f"Nobody has started {title} yet", "title": title}
    base = max(active)
    p = prog[base]
    total = (seasons.get(base) or {}).get("episodes") or p["have"] or 1
    frac = p["watched"] / total
    later = [x for n, x in sorted(seasons.items()) if n > base]
    nxt = next((x for x in later if not x["requested"]), None)
    if nxt is None:
        future = [x for x in later if not x["aired"]]
        return {"action": "done" if sd["ended"] and not future else "wait", "title": title, "base": base,
                "why": "Every later season is already requested or here" if later else
                ("That was the last season" if sd["ended"] else "Waiting for a new season to be announced")}
    if not nxt["aired"]:
        return {"action": "wait", "title": title, "base": base, "season": nxt["number"],
                "why": f"Season {nxt['number']} hasn't started airing yet ({nxt['air_date'] or 'no date'})"}
    at = float(s.get("smart_request_at") or 50) / 100
    est = None
    try:
        est = storage.estimate(app, "tv", tmdb, entry.get("profile"), [nxt["number"]], entry.get("server"),
                               entry.get("is4k"))
    except Exception:
        pass
    size = ((est or {}).get("estimate") or {}).get("typical") or 0
    if entry.get("is4k") or size >= BIG_SEASON:
        at = min(at, 0.30)  # big or slow releases: start earlier
    why = (f"{p['watched']}/{total} episodes of season {base} are watched "
           f"({round(frac * 100)}%, fetching at {round(at * 100)}%)")
    out = {"title": title, "base": base, "season": nxt["number"], "fraction": frac, "threshold": at,
           "size": size, "size_text": storage.fmt_size(size) if size else None, "why": why}
    if frac < at:
        return dict(out, action="wait")
    fit = (est or {}).get("fit") or {}
    if fit.get("blocked"):
        return dict(out, action="waiting_space", why=why + "; " + (fit.get("message") or "over the storage limit"))
    return dict(out, action="request")


def _request(app, entry, season, dry_run):
    body = app.seerr.request("tv", entry["tmdb"], profile=entry.get("profile"), server=entry.get("server"),
                             seasons=[season], is4k=bool(entry.get("is4k")), root_folder=entry.get("root"),
                             dry_run=dry_run)
    return body


def check(app, tmdb=None, dry_run=False):
    """Run the decision for every smart show (or one). Real runs request seasons and tell the UI."""
    s = settings.load()
    if not s.get("smart_request", True) and not dry_run:
        return []
    from . import storage
    view = None
    try:
        view = storage._viewing_cached(app)
    except Exception:
        pass
    results = []
    data = _load()
    for key, entry in list(data.items()):
        if tmdb is not None and str(tmdb) != key:
            continue
        if entry.get("state") in ("stopped", "done"):
            continue
        d = decide(app, entry, view, s)
        d["tmdb"] = entry["tmdb"]
        if d["action"] == "request":
            if dry_run:
                d["payload"] = _request(app, entry, d["season"], True)["body"]
                d["text"] = f"would request S{d['season']} of {d['title']} because {d['why']}"
            else:
                try:
                    res = _request(app, entry, d["season"], False) or {}
                    with _lock:
                        cur = _load()
                        e = cur.get(key) or entry
                        e["requested"] = sorted(set((e.get("requested") or []) + [d["season"]]))
                        e["next"] = d["season"] + 1
                        e["state"] = "watching"
                        e.setdefault("history", []).append({"at": time.time(), "seasons": [d["season"]],
                                                            "request_id": res.get("id"), "why": d["why"]})
                        cur[key] = e
                        _save(cur)
                    storage.forget(f"smart-seerr:{entry['tmdb']}")
                    d["text"] = (f"Season {d['season']} of {d['title']} requested - it'll be ready before you finish "
                                 f"season {d['base']}")
                    app.notify_ui("smart-request", {"tmdb": entry["tmdb"], "title": d["title"], "season": d["season"],
                                                    "message": d["text"], "state": "requested"})
                except Exception as ex:
                    d["action"], d["error"] = "error", str(ex)
        elif d["action"] == "waiting_space":
            d["text"] = f"Season {d['season']} of {d['title']} is waiting for space"
            if not dry_run and entry.get("state") != "waiting_space":
                _set_state(key, "waiting_space")
                app.notify_ui("smart-request", {"tmdb": entry["tmdb"], "title": d["title"], "season": d["season"],
                                                "message": d["text"], "state": "waiting_space"})
        elif d["action"] == "done" and not dry_run:
            _set_state(key, "done")
        elif not dry_run and entry.get("state") == "waiting_space" and d["action"] == "wait":
            _set_state(key, "watching")
        d.setdefault("text", f"{d.get('title') or key}: {d['why']}")
        results.append(d)
    return results


def _set_state(key, state):
    with _lock:
        cur = _load()
        if key in cur:
            cur[key]["state"] = state
            _save(cur)


def request_all(app, tmdb, dry_run=False):
    """"Get all remaining seasons now": every aired season not requested yet, in one request; smart stops."""
    entry = get(tmdb)
    if not entry:
        raise LookupError("That show isn't being requested smartly")
    sd = _seerr_seasons(app, tmdb)
    rest = [x["number"] for x in sd["seasons"] if not x["requested"] and x["aired"]]
    if not rest:
        return {"seasons": [], "message": "Every aired season is already requested"}
    res = app.seerr.request("tv", tmdb, profile=entry.get("profile"), server=entry.get("server"), seasons=rest,
                            is4k=bool(entry.get("is4k")), root_folder=entry.get("root"), dry_run=dry_run)
    if not dry_run:
        with _lock:
            cur = _load()
            e = cur[str(tmdb)]
            e["requested"] = sorted(set(e.get("requested") or []) | set(rest))
            e["state"] = "done"
            e.setdefault("history", []).append({"at": time.time(), "seasons": rest, "why": "all remaining"})
            _save(cur)
    return {"seasons": rest, "payload": res.get("body") if dry_run else None,
            "message": f"Requested season{'s' if len(rest) > 1 else ''} {', '.join(map(str, rest))}"}


def status_line(app, tmdb):
    """For the details page: {smart, state, text}, e.g. "Smart: S2 requested · S3 when you're halfway through S2"."""
    entry = get(tmdb)
    if not entry:
        return {"smart": False}
    req = entry.get("requested") or []
    last = max(req) if req else None
    at = int(settings.load().get("smart_request_at") or 50)
    pct = "halfway through" if at == 50 else f"{at}% through"
    st = entry.get("state")
    if st == "stopped":
        text = f"Smart requesting is off for this show (S{', S'.join(map(str, req))} requested)" if req else \
            "Smart requesting is off for this show"
    elif st == "done":
        text = f"Smart: S{last} requested · no more seasons to fetch" if last else "Smart: all caught up"
    elif st == "waiting_space":
        text = f"Smart: Season {entry.get('next')} is waiting for space"
    else:
        text = f"Smart: S{last} requested · S{last + 1} when you're {pct} S{last}" if last else "Smart requesting"
    return {"smart": True, "state": st, "text": text, "requested": req, "next": entry.get("next"),
            "profile": entry.get("profile"), "is4k": entry.get("is4k")}


# ------------------------------------------------------------------ triggers
def poke(app, delay=20):
    """Watch progress changed: check soon (debounced)."""
    global _timer
    if not settings.load().get("smart_request", True) or not _load():
        return
    with _lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(delay, _safe_check, args=(app,))
        _timer.daemon = True
        _timer.start()


def _safe_check(app):
    try:
        from . import storage
        storage.forget("view")
        for r in check(app):
            if r["action"] in ("request", "error", "waiting_space"):
                print("smart requests:", r.get("text") or r, flush=True)
    except Exception as e:
        print("smart requests failed:", type(e).__name__, e, flush=True)


def start(app):
    """Hook the catalog's position saves (so finishing an episode triggers a check). The hourly sweep runs in
    marquee.storage's job."""
    cat = getattr(app, "catalog", None)
    if cat is None or getattr(cat, "_smart_hooked", False):
        return
    orig = cat.save_position

    def save_position(item_id, seconds, played=None, at=None):
        ok = orig(item_id, seconds, played=played, at=at)
        try:
            r = cat.get(item_id)
            if r and r.get("type") == "Episode":
                poke(app)
        except Exception:
            pass
        return ok
    cat.save_position = save_position
    cat._smart_hooked = True
