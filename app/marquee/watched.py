"""Watched, for any title: films and shows you've seen, whether or not they're on this PC.

  * A library title is marked through the catalog's own played state (synced to Jellyfin, like the title page's
    check button always did). A title that isn't here (requestable in Seerr, or not even that) is kept in the
    catalog's `watched` table; when it's downloaded later its played state is carried over to Jellyfin
    (Catalog.restore_watched, the same way removed titles get theirs back).
  * Either way the taste profile learns from it like a finished title (taste.watched_signal: the finish signal once,
    and the remembered title), with genres and people from the library or from TMDB through Seerr (cached). A star
    rating goes to taste.rate. Unmarking drops the record and the remembered title; what it taught stays.
  * GET /api/watched lists everything watched: library titles played (catalog userdata), titles marked here, and
    titles watched and then removed to save space, one entry per title (by TMDB id), newest first.

The /api/watched routes are part of the local API (marquee.localapi): other programs on this PC (Atlas) call them with
the X-Marquee-Token header from local-api.json; see docs/local-api.md."""
import re
import time
from datetime import datetime

from flask import Blueprint, jsonify, request

from . import taste
from .catalog import iso, tmdb_img
from .localapi import caller, local_api

bp = Blueprint("marquee_watched", __name__)
SOURCES = ("manual", "atlas", "chat", "letterboxd")
KINDS = {"movie": "movie", "film": "movie", "movies": "movie", "tv": "tv", "show": "tv", "series": "tv", "shows": "tv"}


class Ambiguous(Exception):
    def __init__(self, message, candidates):
        super().__init__(message)
        self.candidates = candidates


class NotFound(LookupError):
    pass


def _server():
    from . import server
    return server


def _cat():
    return _server().catalog()


def _app():
    return _server().APP


def ok(**kw):
    return jsonify(ok=True, **kw)


def fail(msg, code=400, **kw):
    return jsonify(ok=False, message=str(msg), **kw), code


# ------------------------------------------------------------------ small readers
def _kind(v):
    return KINDS.get(str(v or "").strip().lower())


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _seasons(b):
    """[season numbers] from "season": 1 or "seasons": [1, 2], or None for the whole title."""
    raw = b.get("seasons", b.get("season"))
    if raw in (None, "", "all", []):
        return None
    vals = raw if isinstance(raw, list) else [raw]
    out = sorted({n for n in (_int(v) for v in vals) if n is not None and n >= 0})
    if not out:
        raise ValueError("season must be a number")
    return out


def _when(v):
    """watched_at: ISO 8601 or epoch seconds -> epoch seconds (never in the future), or None."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        t = float(v) / (1000 if v > 1e12 else 1)
    else:
        s = str(v).strip()
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00") if len(s) > 10 else s + "T12:00:00")
        except ValueError:
            raise ValueError("watched_at must be an ISO date (2024-05-01 or 2024-05-01T20:00:00Z) or epoch seconds")
        if d.tzinfo is None:
            d = d.astimezone()
        t = d.timestamp()
    return min(t, time.time())


def _rating(v):
    if v in (None, "", 0, "0"):
        return None
    r = _int(v)
    if r is None or not 1 <= r <= 5:
        raise ValueError("rating must be 1-5")
    return r


def _source(b):
    s = str(b.get("source") or "").strip().lower()
    if s in SOURCES:
        return s
    return "atlas" if caller() == "token" else "manual"


def _year(v):
    m = re.match(r"\d{4}", str(v or ""))
    return int(m.group(0)) if m else None


def _detail(kind, tmdb):
    """TMDB facts for a title (cached by the details page; asked of Seerr when missing). {} when unknown."""
    try:
        from . import details
        d = details.tmdb_detail(kind, tmdb) or {}
    except Exception:
        d = {}
    if not d:
        try:
            app = _app()
            if app.seerr.ready:
                d = app.seerr.details(kind, int(tmdb)) or {}
        except Exception:
            d = {}
    return d


def _info_tmdb(kind, tmdb, d):
    """What the taste profile reads, from a TMDB detail (details._slim shape, or a Seerr card)."""
    people = [{"name": c["name"], "role": "Director"} for c in d.get("crew") or []
              if c.get("job") in ("Director", "Creator") and c.get("name")][:2]
    people += [{"name": c["name"], "role": "Actor"} for c in (d.get("cast") or [])[:3] if c.get("name")]
    return {"title": d.get("title"), "year": _year(d.get("year")), "kind": kind, "genres": d.get("genres") or [],
            "people": people, "runtime": d.get("runtime") or 0, "anime": bool(d.get("anime")), "tmdb": int(tmdb)}


def _tmdb_of(r):
    t = (r.get("provider") or {}).get("Tmdb")
    return int(t) if str(t or "").isdigit() else None


def _changed(event_data):
    """Home, cards and the page catch up (the window hears about marks made by Atlas too)."""
    srv = _server()
    srv._cache.clear()
    try:
        from . import homemix
        homemix.invalidate()
    except Exception:
        pass
    try:
        srv.APP.notify_ui("watched-changed", event_data)
    except Exception:
        pass


# ------------------------------------------------------------------ finding the title
def _lib_candidate(r):
    cat = _cat()
    c = cat.card(r)
    return {"kind": "tv" if r["type"] == "Series" else "movie", "tmdb": _tmdb_of(r), "item_id": r["id"],
            "title": r.get("name"), "year": r.get("year"), "poster": c.get("poster"), "owned": True}


def _seerr_candidate(c):
    return {"kind": c["type"], "tmdb": int(c["id"]), "item_id": None, "title": c.get("title"), "year": _year(c.get("year")),
            "poster": tmdb_img(c.get("poster")) if c.get("poster") else None, "owned": False,
            "overview": (c.get("overview") or "")[:240]}


def find(title, year=None, kind=None):
    """The one title meant by a name (and year / kind): the library first, then Seerr's search. Returns a candidate
    dict; raises Ambiguous (with candidates) or NotFound."""
    from . import chat_actions as ca
    title = (title or "").strip()
    if not title:
        raise ValueError("title is empty")
    year = _year(year)
    cands = [_lib_candidate(r) for r in ca._lib_matches(title, year, kind)]
    exact, close = ca._seerr_matches(title, year, kind)
    have = {(c["kind"], c["tmdb"]) for c in cands if c["tmdb"]}
    cat = _cat()
    for c in exact:
        if (c["type"], int(c["id"])) in have:
            continue
        owned = cat.by_tmdb(c["type"], c["id"])
        cands.append(_lib_candidate(owned) if owned else _seerr_candidate(c))
        have.add((c["type"], int(c["id"])))
    if year and len(cands) > 1:
        same = [c for c in cands if c.get("year") == year]
        if len(same) == 1:
            return same[0]
    if len(cands) == 1:
        return cands[0]
    if cands:
        raise Ambiguous(f"More than one title is called “{title}”: send its tmdb and kind", cands[:8])
    if close:
        raise Ambiguous(f"Nothing is called exactly “{title}”. Did you mean one of these?",
                        [_lib_candidate(cat.by_tmdb(c["type"], c["id"])) if cat.by_tmdb(c["type"], c["id"])
                         else _seerr_candidate(c) for c in close[:6]])
    raise NotFound(f"Couldn't find “{title}”" + ("" if _app().seerr.ready else " (Seerr isn't connected)"))


def resolve(b):
    """("library", row) | ("tmdb", kind, tmdb) from a request body: {item_id} | {tmdb, kind} | {title, year, kind}
    | {key: "movie:123" | "item:<id>"}."""
    cat = _cat()
    key = str(b.get("key") or "")
    if key.startswith("item:"):
        b = {**b, "item_id": key[5:]}
    elif re.fullmatch(r"(movie|tv):\d+", key):
        b = {**b, "kind": key.split(":")[0], "tmdb": key.split(":")[1]}
    if b.get("item_id"):
        r = cat.get(str(b["item_id"]))
        if not r:
            raise NotFound("That title isn't in the library on this PC")
        return "library", r
    kind = _kind(b.get("kind") or b.get("type"))
    if b.get("tmdb") not in (None, ""):
        tmdb = _int(b.get("tmdb"))
        if tmdb is None or tmdb <= 0:
            raise ValueError("tmdb must be a number")
        if not kind:
            raise ValueError("kind is needed with tmdb: movie or tv")
        owned = cat.by_tmdb(kind, tmdb)
        return ("library", owned) if owned else ("tmdb", kind, tmdb)
    if b.get("title"):
        c = find(b["title"], b.get("year"), kind)
        if c.get("item_id"):
            return "library", cat.get(c["item_id"])
        return "tmdb", c["kind"], c["tmdb"]
    raise ValueError("send item_id, or tmdb and kind, or title (with year and kind if you know them)")


# ------------------------------------------------------------------ marking
def _rate(kind, tmdb, item_id, stars):
    from .taste_api import _facts_for
    facts, _ = _facts_for(f"{kind}:{tmdb}" if tmdb else item_id)
    if facts:
        return taste.rate(facts, stars)
    return None


def _library_targets(cat, r, seasons):
    """(the film/show row taste reads, the playable ids this mark covers)."""
    with cat.lock:
        if r["type"] in ("Episode", "Season") and r.get("series_id"):
            show = cat.items.get(r["series_id"]) or r
            ids = [r["id"]] if r["type"] == "Episode" else [e["id"] for e in cat.eps_by_season.get(r["id"], [])]
            return show, ids
        if r["type"] == "Series":
            eps = cat.eps.get(r["id"], [])
            eps = [e for e in eps if e.get("season_no") in set(seasons)] if seasons else \
                [e for e in eps if (e.get("season_no") or 0) > 0] or eps
            return r, [e["id"] for e in eps]
        return r, [r["id"]]


def mark(b):
    """Mark a title watched. Returns {"entry": ..., "moved": {genre: [before, after]}, "rating": ...}."""
    cat = _cat()
    seasons = _seasons(b)
    at = _when(b.get("watched_at"))
    stars = _rating(b.get("rating"))
    source = _source(b)
    how = resolve(b)
    if how[0] == "library":
        r = how[1]
        show, ids = _library_targets(cat, r, seasons)
        if show["type"] == "Series" and not ids:
            raise NotFound("No episodes of " + str(show.get("name")) + (" in that season" if seasons else "") +
                           " are in the library")
        with cat.lock:
            fresh = [i for i in ids if not (cat.ud.get(i) or {}).get("played")]
        if r["type"] == "Episode" or r["type"] == "Season":
            cat.set_played(r["id"], True, at=at)
        else:
            cat.set_played(r["id"], True, at=at, seasons=seasons)
        if r["type"] in ("Episode", "Season"):  # a few episodes: the history job weighs them like plays
            moved = (taste.update_from_history(cat) or {}).get("moved") or {}
        else:
            info = taste._info_from_row(cat, show)
            moved = taste.watched_signal(info, fresh or ids, at, source=_taste_source(source))
        kind, tmdb = ("tv" if show["type"] == "Series" else "movie"), _tmdb_of(show)
        rating = _rate(kind, tmdb, show["id"], stars) if stars else None
        entry = entry_for(kind, tmdb, show["id"])
        _changed({"kind": kind, "tmdb": tmdb, "item_id": show["id"], "watched": True})
        return {"entry": entry, "moved": moved, "rating": rating, "library": True}
    _, kind, tmdb = how
    if kind == "movie" and seasons:
        seasons = None
    d = _detail(kind, tmdb)
    if not d and not b.get("title"):
        raise NotFound("Couldn't look this title up (Seerr isn't reachable and it isn't saved here)")
    d = d or {"title": b.get("title"), "year": b.get("year")}
    data = {"title": d.get("title") or b.get("title"), "year": _year(d.get("year") or b.get("year")),
            "poster": tmdb_img(d.get("poster")), "backdrop": tmdb_img(d.get("backdrop")), "genres": d.get("genres") or [],
            "runtime": d.get("runtime") or 0, "anime": bool(d.get("anime")), "overview": (d.get("overview") or "")[:600]}
    keys = [cat.record_watched(kind, tmdb, data, source, at, season=s) for s in (seasons or [None])]
    info = _info_tmdb(kind, tmdb, {**d, **data})
    moved = taste.watched_signal(info, ["watched:" + k for k in keys], at, source=_taste_source(source))
    rating = _rate(kind, tmdb, None, stars) if stars else None
    try:  # downloaded already but the catalog hasn't caught up? carry it over at once when it has
        cat.restore_watched()
    except Exception as e:
        print("watched carry-over failed:", e)
    _changed({"kind": kind, "tmdb": tmdb, "watched": True})
    return {"entry": entry_for(kind, tmdb), "moved": moved, "rating": rating, "library": False}


def _taste_source(source):
    return {"atlas": "Atlas", "chat": "chat", "letterboxd": "letterboxd"}.get(source, "marked watched")


def unmark(b):
    """Unmark: library titles become unplayed (synced), records here are dropped, a removed title's watched state
    is cleared, and the remembered title is forgotten (the genres and people it taught stay)."""
    cat = _cat()
    seasons = _seasons(b)
    how = resolve(b)
    dropped = []
    if how[0] == "library":
        r = how[1]
        show, _ = _library_targets(cat, r, seasons)
        if r["type"] in ("Episode", "Season"):
            cat.set_played(r["id"], False)
        else:
            cat.set_played(r["id"], False, seasons=seasons)
        kind, tmdb, title, year = ("tv" if show["type"] == "Series" else "movie"), _tmdb_of(show), show.get("name"), \
            show.get("year")
        item_id = show["id"]
    else:
        _, kind, tmdb = how
        recs = cat.watched_records(kind, tmdb)
        title = (recs[0] if recs else {}).get("title")
        year = (recs[0] if recs else {}).get("year")
        item_id = None
    if tmdb:
        for s in (seasons or [None]):
            dropped += cat.forget_watched(kind, tmdb, season=s)
        if not seasons:
            dropped += _clear_removed(cat, kind, tmdb)
    whole_left = bool(tmdb and cat.watched_records(kind, tmdb))
    if not seasons or not whole_left:
        taste.forget_title(title, year, kind, tmdb)
    _changed({"kind": kind, "tmdb": tmdb, "item_id": item_id, "watched": False})
    return {"kind": kind, "tmdb": tmdb, "item_id": item_id, "dropped": dropped}


def _clear_removed(cat, kind, tmdb):
    """A title watched and then removed: forget that this account watched it (the record itself stays)."""
    import json
    me = getattr(_app().jf, "user", None) or "me"
    keys = []
    with cat.lock:
        rows = cat.db.execute("SELECT key, kind, data FROM removed WHERE tmdb=? AND restored_at IS NULL",
                              (str(tmdb),)).fetchall()
        for key, k, data in rows:
            if (k == "movie") != (kind == "movie"):
                continue
            try:
                d = json.loads(data or "{}")
            except ValueError:
                continue
            if not d.get("played"):
                continue
            for uid, st in (d.get("users") or {}).items():
                if uid in (me, "me"):
                    st["played"] = False  # other accounts' history stays as it was
            d["played"] = False
            cat.db.execute("UPDATE removed SET data=? WHERE key=?", (json.dumps(d, ensure_ascii=False), key))
            keys.append(key)
    return keys


# ------------------------------------------------------------------ the list
def _lib_entry(cat, r):
    """An entry for a library film/show with something played, or None."""
    card = cat.card(r)
    tmdb = _tmdb_of(r)
    if r["type"] == "Movie":
        u = cat.ud.get(r["id"]) or {}
        if not u.get("played"):
            return None
        at, eps, seasons, complete = u.get("last_played") or 0, None, [], True
    else:
        all_eps = cat.eps.get(r["id"], [])
        regular = [e for e in all_eps if (e.get("season_no") or 0) > 0] or all_eps
        played = [e for e in regular if (cat.ud.get(e["id"]) or {}).get("played")]
        if not played:
            return None
        at = max((cat.ud.get(e["id"]) or {}).get("last_played") or 0 for e in played)
        by_season = {}
        for e in regular:
            by_season.setdefault(e.get("season_no"), []).append(e)
        seasons = sorted(s for s, es in by_season.items() if s is not None and
                         all((cat.ud.get(e["id"]) or {}).get("played") for e in es))
        eps = {"watched": len(played), "total": len(regular)}
        complete = len(played) == len(regular)
    return {"key": f"{'tv' if r['type'] == 'Series' else 'movie'}:{tmdb}" if tmdb else f"item:{r['id']}",
            "kind": "tv" if r["type"] == "Series" else "movie", "tmdb": tmdb, "item_id": r["id"],
            "title": r.get("name"), "year": r.get("year"), "poster": card.get("poster"), "backdrop": card.get("backdrop"),
            "genres": r.get("genres") or [], "runtime": r.get("runtime") or 0, "anime": card.get("anime"),
            "watched_ts": at, "source": "library", "sources": ["library"], "owned": True, "removed": False,
            "complete": complete, "seasons": seasons, "episodes": eps, "card": card}


def _rec_entries(cat):
    out = {}
    for rec in cat.watched_records():
        k = f"{rec['kind']}:{rec['tmdb']}"
        e = out.get(k)
        if e is None:
            e = out[k] = {"key": k, "kind": rec["kind"], "tmdb": _int(rec["tmdb"]), "item_id": None,
                          "title": rec.get("title"), "year": rec.get("year"), "poster": rec.get("poster"),
                          "backdrop": rec.get("backdrop"), "genres": rec.get("genres") or [],
                          "runtime": rec.get("runtime") or 0, "anime": bool(rec.get("anime")), "watched_ts": 0,
                          "source": rec.get("source") or "manual", "sources": [], "owned": False, "removed": False,
                          "complete": False, "seasons": [], "episodes": None}
        if rec.get("season") is None:
            e["complete"] = True
        elif rec.get("episode") is None:
            e["seasons"] = sorted(set(e["seasons"]) | {rec["season"]})
        if (rec.get("source") or "manual") not in e["sources"]:
            e["sources"].append(rec.get("source") or "manual")
        e["watched_ts"] = max(e["watched_ts"], rec.get("watched_at") or 0)
    return out


def _removed_entries(cat):
    out = {}
    for c in cat.removed_cards():
        if not c.get("played"):
            continue
        k = f"{c['type']}:{c['id']}"
        ts = 0
        for v in (c.get("watched_at"), c.get("removed_at")):
            try:
                ts = datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() if v else 0
            except ValueError:
                ts = 0
            if ts:
                break
        out[k] = {"key": k, "kind": c["type"], "tmdb": _int(c["id"]), "item_id": None, "title": c.get("title"),
                  "year": _year(c.get("year")), "poster": c.get("poster"), "backdrop": c.get("backdrop"),
                  "genres": c.get("genres") or [], "runtime": 0, "anime": bool(c.get("anime")), "watched_ts": ts,
                  "source": "removed", "sources": ["removed"], "owned": False, "removed": True,
                  "removed_at": c.get("removed_at"), "complete": c.get("state") == "removed_watched",
                  "seasons": [], "episodes": None}
    return out


def listing(kind=None, q=None):
    """Everything watched, one entry per title, newest first."""
    cat = _cat()
    entries = {}
    with cat.lock:
        rows = cat._of("Movie,Series")
        lib = [e for e in (_lib_entry(cat, r) for r in rows) if e]
    for e in lib:
        entries[e["key"]] = e
    for src in (_rec_entries(cat), _removed_entries(cat)):
        for k, e in src.items():
            have = entries.get(k)
            if have is None:
                entries[k] = e
                continue
            for s in e["sources"]:
                if s not in have["sources"]:
                    have["sources"].append(s)
            if not have["owned"]:  # a title marked here and also watched before it was removed
                have["complete"] = have["complete"] or e["complete"]
                have["seasons"] = sorted(set(have["seasons"]) | set(e["seasons"]))
                have["removed"] = have["removed"] or e["removed"]
            if have["owned"]:
                have["watched_ts"] = have["watched_ts"] or e["watched_ts"]
            else:
                have["watched_ts"] = max(have["watched_ts"], e["watched_ts"])
            for f in ("poster", "backdrop", "year", "title"):
                have[f] = have.get(f) or e.get(f)
    ratings = (taste.profile().get("ratings") or {})
    out = []
    want = taste.norm(q) if q else ""
    for e in entries.values():
        if kind and e["kind"] != kind:
            continue
        if want and want not in taste.norm(e.get("title")):
            continue
        e["rating"] = (ratings.get(e["key"]) or {}).get("stars") or 0 if e.get("tmdb") else 0
        e["watched_at"] = iso(e.pop("watched_ts")) if e.get("watched_ts") else None
        out.append(e)
    out.sort(key=lambda e: e.get("watched_at") or "", reverse=True)
    return out


def entry_for(kind, tmdb, item_id=None):
    """The list entry of one title (after a change), or None when it isn't watched."""
    for e in listing(kind):
        if (tmdb and e.get("tmdb") == int(tmdb)) or (item_id and e.get("item_id") == item_id):
            return e
    return None


# ------------------------------------------------------------------ routes (local API)
def _body():
    b = request.get_json(silent=True) if request.data else None
    if b is None:
        b = {k: v for k, v in request.args.items()}
    if not isinstance(b, dict):
        raise ValueError("send a JSON object")
    return b


@bp.get("/api/watched")
@local_api("GET /api/watched")
def route_list():
    kind = _kind(request.args.get("kind")) if request.args.get("kind") else None
    if request.args.get("kind") and not kind:
        return fail("kind must be movie or tv")
    items = listing(kind, (request.args.get("q") or "").strip() or None)
    counts = {"movie": sum(1 for e in items if e["kind"] == "movie"), "tv": sum(1 for e in items if e["kind"] == "tv")}
    return ok(items=items, count=len(items), counts=counts)


@bp.post("/api/watched")
@local_api("POST /api/watched")
def route_mark():
    try:
        res = mark(_body())
    except Ambiguous as e:
        return fail(e, 409, ambiguous=True, candidates=e.candidates)
    except NotFound as e:
        return fail(e, 404)
    except ValueError as e:
        return fail(e, 400)
    return ok(watched=True, item=res["entry"], taste={"moved": res["moved"]}, rating=res["rating"])


@bp.delete("/api/watched")
@local_api("DELETE /api/watched")
def route_unmark():
    try:
        res = unmark(_body())
    except Ambiguous as e:
        return fail(e, 409, ambiguous=True, candidates=e.candidates)
    except NotFound as e:
        return fail(e, 404)
    except ValueError as e:
        return fail(e, 400)
    return ok(watched=False, **res)
