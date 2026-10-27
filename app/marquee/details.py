"""The details page's extras, My List, people, "Up next" picks and title previews (a Flask blueprint on the local
UI server).

Local first, like the rest of the server: everything here answers from the catalog and its small cache (the `kv`
table in catalog.db) at once, and only reaches Seerr / Jellyfin to fill in or refresh, with short timeouts. What it
learns online (IMDb / Rotten Tomatoes scores, TMDB cast, trailers, seasons and episodes that aren't in the library,
person biographies) is kept in the cache, so the page looks the same when the servers are away.

Reviews: neither Seerr nor Jellyfin exposes user or critic reviews (Seerr has no reviews route; Jellyfin's
CriticReviews answers 404), so none are shown. Nothing here makes text up."""
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import requests
from flask import Blueprint, Response, jsonify, request, send_file

from . import settings
from .catalog import TICKS, jf_img, tmdb_img

bp = Blueprint("marquee_details", __name__)
TMDB = "https://image.tmdb.org/t/p/"
DAY = 86400
PREVIEW_DIR = settings.DIR / "cache" / "previews"
PREVIEW_LIMIT = 1200 * 2**20
CLIP_SECONDS = 24
_clip_locks = {}
_clip_guard = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="details")


def _server():
    from . import server
    return server


def APP():
    return _server().APP


def cat():
    return _server().catalog()


def ok(**kw):
    return jsonify(ok=True, **kw)


def fail(msg, code=400):
    return jsonify(ok=False, message=str(msg)), code


def seerr_ready():
    sr = getattr(APP(), "seerr", None)
    return bool(sr and sr.ready)


def seerr(path, timeout=(3, 8)):
    return APP().seerr.call("GET", path, timeout=timeout)


def timg(path, size="w500"):
    """A TMDB image path or URL as a local cached /img/tmdb/... URL at this size."""
    if not path:
        return None
    if path.startswith(TMDB):
        path = "/" + path[len(TMDB):].split("/", 1)[1].lstrip("/")
    return tmdb_img(TMDB + size + "/" + path.lstrip("/"))


def cached(key, max_age, fetch, empty_age=DAY):
    """Cache-first: a fresh cached value; else fetch() (stored); else whatever is cached, however old.
    fetch() may return None for "nothing there" (remembered for empty_age so we don't ask on every page)."""
    c = cat()
    data, age = c.kv_get(key)
    fresh_for = empty_age if (data in (None, {}, []) and age is not None) else max_age
    if age is not None and age < fresh_for:
        return data
    try:
        new = fetch()
    except Exception as e:
        if not isinstance(e, (requests.RequestException, PermissionError)):
            print("details:", key, type(e).__name__, e)
        return data
    if new is None and data is not None:
        new = {} if isinstance(data, dict) else data
    c.kv_set(key, new if new is not None else {})
    return new


# ------------------------------------------------------------------ TMDB (through Seerr)
def _cert(kind, d):
    if kind == "movie":
        for r in (d.get("releases") or {}).get("results") or []:
            if r.get("iso_3166_1") == "US":
                for x in r.get("release_dates") or []:
                    if x.get("certification"):
                        return x["certification"]
        return None
    for r in (d.get("contentRatings") or {}).get("results") or []:
        if r.get("iso_3166_1") == "US" and r.get("rating"):
            return r["rating"]
    return None


def _videos(d):
    vids = [v for v in d.get("relatedVideos") or [] if v.get("site") == "YouTube" and v.get("key")]
    order = {"Trailer": 0, "Teaser": 1, "Clip": 2, "Featurette": 3, "Behind the Scenes": 4}
    vids.sort(key=lambda v: order.get(v.get("type"), 5))
    return [{"key": v["key"], "name": v.get("name") or v.get("type") or "Video", "type": v.get("type")} for v in vids[:12]]


def _slim(kind, d):
    info = d.get("mediaInfo") or {}
    date_s = d.get("releaseDate") or d.get("firstAirDate") or ""
    credits = d.get("credits") or {}
    crew = [{"name": c["name"], "job": c.get("job"), "tmdb": c.get("id")} for c in credits.get("crew") or []
            if c.get("job") in ("Director",)][:3]
    crew += [{"name": c["name"], "job": "Creator", "tmdb": c.get("id")} for c in d.get("createdBy") or []][:3]
    return {
        "kind": kind, "tmdb": d.get("id"), "title": d.get("title") or d.get("name"), "year": date_s[:4] or None,
        "date": date_s or None, "overview": d.get("overview") or "", "tagline": d.get("tagline") or None,
        "runtime": (d.get("runtime") or (d.get("episodeRunTime") or [0] or [0])[0] or 0) * 60,
        "genres": [g["name"] for g in d.get("genres") or []], "rating": d.get("voteAverage"),
        "votes": d.get("voteCount"), "age": _cert(kind, d),
        "poster": timg(d.get("posterPath"), "w500"), "backdrop": timg(d.get("backdropPath"), "original"),
        "status": info.get("status"), "jellyfin": info.get("jellyfinMediaId"),
        "imdb": (d.get("externalIds") or {}).get("imdbId") or d.get("imdbId"),
        "anime": kind == "tv" and d.get("originalLanguage") == "ja" and any(g.get("id") == 16 for g in d.get("genres") or []),
        "cast": [{"name": c["name"], "role": c.get("character"), "tmdb": c.get("id"),
                  "photo": timg(c.get("profilePath"), "w185")} for c in (credits.get("cast") or [])[:18]],
        "crew": crew, "videos": _videos(d),
        "collection": {"id": d["collection"]["id"], "name": d["collection"].get("name")} if d.get("collection") else None,
        "seasons": [{"number": s.get("seasonNumber"), "name": s.get("name"), "episodes": s.get("episodeCount"),
                     "overview": s.get("overview") or "", "air": s.get("airDate"),
                     "poster": timg(s.get("posterPath"), "w342")} for s in d.get("seasons") or []],
        "season_count": d.get("numberOfSeasons"), "show_status": d.get("status"),
        "next_air": (d.get("nextEpisodeToAir") or {}).get("airDate"),
        "networks": [n.get("name") for n in d.get("networks") or []][:3],
        "studios": [p.get("name") for p in d.get("productionCompanies") or []][:3],
        "seasons_status": {str(s.get("seasonNumber")): s.get("status") for s in info.get("seasons") or []},
    }


def tmdb_detail(kind, tmdb, max_age=3 * DAY):
    if kind not in ("movie", "tv"):
        return None
    return cached(f"tmdb:{kind}:{int(tmdb)}", max_age,
                  lambda: _slim(kind, seerr(f"/{kind}/{int(tmdb)}")) if seerr_ready() else _raise_offline())


def _raise_offline():
    raise requests.ConnectionError("Seerr isn't connected")


def ratings(kind, tmdb, max_age=3 * DAY):
    """{"rt": {critics, critics_rating, audience, audience_rating, url}, "imdb": {score, votes, url}} (parts None)."""
    def fetch():
        if not seerr_ready():
            _raise_offline()
        rt = imdb = None
        if kind == "movie":
            try:
                d = seerr(f"/movie/{int(tmdb)}/ratingscombined")
                rt, imdb = d.get("rt"), d.get("imdb")
            except requests.HTTPError as e:
                if getattr(e.response, "status_code", 0) != 404:
                    raise
                try:
                    rt = seerr(f"/movie/{int(tmdb)}/ratings")
                except requests.HTTPError:
                    rt = None
        else:
            try:
                rt = seerr(f"/tv/{int(tmdb)}/ratings")
            except requests.HTTPError as e:
                if getattr(e.response, "status_code", 0) != 404:
                    raise
        out = {"rt": None, "imdb": None}
        if rt and (rt.get("criticsScore") is not None or rt.get("audienceScore") is not None):
            out["rt"] = {"critics": rt.get("criticsScore"), "critics_rating": rt.get("criticsRating"),
                         "audience": rt.get("audienceScore"), "audience_rating": rt.get("audienceRating"),
                         "url": rt.get("url")}
        if imdb and imdb.get("criticsScore") is not None:
            out["imdb"] = {"score": imdb.get("criticsScore"), "votes": imdb.get("criticsScoreCount"), "url": imdb.get("url")}
        return out
    return cached(f"ratings:{kind}:{int(tmdb)}", max_age, fetch) or {}


FLAG_KEYS = ("removed", "played", "state", "removed_at", "watched_at", "watched", "watched_seasons")


def title_flag(kind, tmdb):
    """Removed / watched state for a requestable card, read now (cached cards carry whatever it was then)."""
    try:
        return cat().title_flag(kind, tmdb) or {}
    except Exception:
        return {}


def tmdb_card(c):
    """A Seerr card with local image URLs (what tmdbCard() in the page draws) and its current watched state."""
    if not c:
        return None
    out = {**c, "poster": tmdb_img(c.get("poster")), "backdrop": tmdb_img(c.get("backdrop"))}
    if c.get("type") in ("movie", "tv") and c.get("id") and c.get("status") not in (4, 5):
        for k in FLAG_KEYS:
            out.pop(k, None)
        out.update(title_flag(c["type"], c["id"]))
    return out


def owned_or_tmdb(kind, c):
    """A library card when we own it (matched by TMDB id here), else the requestable card."""
    r = cat().by_tmdb(kind, c["id"])
    if r:
        card = cat().card(r)
        card["owned"] = True
        return card
    return tmdb_card(c)


def collection(cid, exclude_tmdb=None):
    def fetch():
        if not seerr_ready():
            _raise_offline()
        c = seerr(f"/collection/{int(cid)}")
        parts = []
        for p in c.get("parts") or []:
            card = APP().seerr.card({**p, "mediaType": "movie"})
            if card:
                card["date"] = p.get("releaseDate") or None
                parts.append(card)
        return {"name": c.get("name"), "parts": sorted(parts, key=part_order)}
    data = cached(f"collection:{int(cid)}", 7 * DAY, fetch) or {}
    parts = sorted(data.get("parts") or [], key=part_order)  # older caches were saved in TMDB's own order
    items = [owned_or_tmdb("movie", p) for p in parts if str(p["id"]) != str(exclude_tmdb)]
    return {"name": data.get("name"), "items": items} if items else None


def part_order(p):
    """A film series in watching order: by release date (1, 2, 3...), undated (announced) parts last."""
    return (p.get("date") or (f"{p['year']}-99" if p.get("year") else "9999"), str(p.get("title") or ""))


def recommended(kind, tmdb):
    def fetch():
        if not seerr_ready():
            _raise_offline()
        return APP().seerr.recommended(kind, tmdb, limit=20)
    items = cached(f"recs:{kind}:{int(tmdb)}", 2 * DAY, fetch) or []
    out = []
    for c in items if isinstance(items, list) else []:
        if not cat().by_tmdb(kind, c["id"]):
            out.append(tmdb_card(c))
    return out[:12]


def tmdb_season(tmdb, number):
    """TMDB's episode list for one season (also the ones not in the library, and not aired yet)."""
    def fetch():
        if not seerr_ready():
            _raise_offline()
        d = seerr(f"/tv/{int(tmdb)}/season/{int(number)}")
        return {"overview": d.get("overview") or "", "air": d.get("airDate"), "name": d.get("name"),
                "episodes": [{"season": e.get("seasonNumber"), "episode": e.get("episodeNumber"), "name": e.get("name"),
                              "overview": e.get("overview") or "", "air": e.get("airDate"),
                              "still": timg(e.get("stillPath"), "w300"), "vote": e.get("voteAverage")}
                             for e in d.get("episodes") or []]}
    return cached(f"tmdbseason:{int(tmdb)}:{int(number)}", DAY, fetch) or {}


# ------------------------------------------------------------------ details extras
def _people_merge(item, d):
    """Cast from Jellyfin (cached headshots); TMDB's credits when Jellyfin has none."""
    people = [p for p in item.get("people") or [] if p.get("type") in ("Actor", "GuestStar")]
    cast = [{"name": p["name"], "role": p.get("role"), "photo": p.get("photo"), "id": p.get("id")} for p in people]
    if not cast and d:
        cast = [{**c, "id": f"tmdb-{c['tmdb']}" if c.get("tmdb") else None} for c in d.get("cast") or []]
    crew = [{"name": p["name"], "job": p.get("type"), "id": p.get("id")} for p in item.get("people") or []
            if p.get("type") in ("Director", "Creator")]
    if not crew and d:
        crew = [{"name": c["name"], "job": c.get("job"), "id": f"tmdb-{c['tmdb']}" if c.get("tmdb") else None}
                for c in d.get("crew") or []]
    return cast, crew


@bp.get("/api/item/<item_id>/extras")
def item_extras(item_id):
    """Online-enriched parts of a library title's page (cache-first; works offline from the cache)."""
    c = cat()
    item = c.detail(item_id)
    if item is None:
        return fail("That title isn't in the library on this PC", 404)
    kind = {"Movie": "movie", "Series": "tv"}.get(item["type"])
    tmdb = item.get("tmdb")
    d = tmdb_detail(kind, tmdb) if kind and tmdb and str(tmdb).isdigit() else None
    rt = ratings(kind, tmdb) if kind and tmdb and str(tmdb).isdigit() else {}
    cast, crew = _people_merge(item, d)
    trailers = [{"key": _yt_key(t["url"]), "name": t["name"], "type": "Trailer"} for t in item.get("trailers") or []]
    trailers = [t for t in trailers if t["key"]]
    seen = {t["key"] for t in trailers}
    for v in (d or {}).get("videos") or []:
        if v["key"] not in seen:
            trailers.append(v)
            seen.add(v["key"])
    out = {"ratings": {**rt, "community": item.get("rating"), "critic": item.get("critic")},
           "cast": cast, "crew": crew, "videos": trailers[:12], "reviews": [],
           "age": item.get("age") or (d or {}).get("age"), "tagline": item.get("tagline") or (d or {}).get("tagline"),
           "overview": item.get("overview") or (d or {}).get("overview"), "seerr": seerr_ready(),
           "imdb": item.get("imdb") or (d or {}).get("imdb"), "collection": None, "recommended": [],
           "tmdb_seasons": [], "status": (d or {}).get("status"), "show_status": (d or {}).get("show_status")}
    if d:
        if kind == "movie" and d.get("collection"):
            out["collection"] = collection(d["collection"]["id"], exclude_tmdb=tmdb)
        out["recommended"] = recommended(kind, tmdb)
        if kind == "tv":
            out["tmdb_seasons"] = d.get("seasons") or []
            out["seasons_status"] = d.get("seasons_status") or {}
    return ok(**out)


def _yt_key(url):
    m = re.search(r"(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{6,15})", url or "")
    return m.group(1) if m else None


@bp.get("/api/tmdb/<kind>/<int:tmdb>")
def tmdb_page(kind, tmdb):
    """A title that isn't in the library: the same details page, from TMDB through Seerr (cached)."""
    if kind not in ("movie", "tv"):
        return fail("unknown kind", 404)
    owned = cat().by_tmdb(kind, tmdb)
    if owned:
        return ok(owned_id=owned["id"])
    d = tmdb_detail(kind, tmdb)
    if not d:
        return fail("Couldn't load this title (Seerr isn't reachable and it isn't saved here)", 503)
    out = dict(d)
    out["ratings"] = ratings(kind, tmdb)
    out["recommended"] = recommended(kind, tmdb)
    out["collection"] = collection(d["collection"]["id"], exclude_tmdb=tmdb) if d.get("collection") else None
    out["cast"] = [{**c, "id": f"tmdb-{c['tmdb']}" if c.get("tmdb") else None} for c in d.get("cast") or []]
    out["crew"] = [{**c, "id": f"tmdb-{c['tmdb']}" if c.get("tmdb") else None} for c in d.get("crew") or []]
    out["mylist"] = f"{kind}:{tmdb}" in set(cat().mylist_keys())
    out["seerr"] = seerr_ready()
    out["watched"] = cat().watched_flag(kind, tmdb)  # marked watched here (not in the library)
    return ok(item=out)


@bp.get("/api/tmdb/tv/<int:tmdb>/season/<int:number>")
def tmdb_season_page(tmdb, number):
    return ok(removed=removed_episodes(tmdb, number), **tmdb_season(tmdb, number))


@bp.get("/api/episodes/<series>/<season>/tmdb")
def episodes_tmdb(series, season):
    """TMDB's view of a library show's season: fills in descriptions/stills the library lacks and lists episodes
    that are missing or not aired yet. The page first draws the catalog's episodes, then merges this."""
    c = cat()
    r = c.get(series)
    tmdb = ((r or {}).get("provider") or {}).get("Tmdb")
    if not r or not tmdb:
        return ok(episodes=[], tmdb=None)
    number = season
    srow = c.get(season)
    if srow and srow.get("type") == "Season":
        number = srow.get("season_no")
    try:
        number = int(str(number).replace("tmdb-", ""))
    except ValueError:
        return ok(episodes=[], tmdb=tmdb)
    data = tmdb_season(tmdb, number)
    return ok(tmdb=tmdb, season=number, today=date.today().isoformat(), removed=removed_episodes(tmdb, number),
              **(data or {"episodes": []}))


def removed_episodes(tmdb, season):
    """Episodes of this season deleted to save space (storage): [{season, episode, played, removed_at}]."""
    c = cat()
    if not hasattr(c, "removed_items"):
        return []
    me = getattr(APP().jf, "user", None) or ""
    out = []
    try:
        for r in c.removed_items("episode"):
            if str(r.get("tmdb")) != str(tmdb) or str(r.get("season")) != str(season):
                continue
            mine = (r.get("users") or {}).get(me) or {}
            out.append({"season": r.get("season"), "episode": r.get("episode"),
                        "played": bool(mine.get("played", r.get("played"))), "removed_at": r.get("removed_at")})
    except Exception as e:
        print("removed episodes:", e)
    return out


# ------------------------------------------------------------------ people
def _short_bio(text, limit=700):
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:end + 1] if end > 200 else cut.rstrip() + "…")


def person_jf(jf_id):
    """A Jellyfin person (name, bio, TMDB id, photo), cache-first."""
    def fetch_jf():
        jf = APP().jf
        if not jf.online:
            _raise_offline()
        with jf.quick():
            p = jf.get(f"/Items/{jf_id}", userId=jf.user)
        return {"name": p.get("Name"), "bio": _short_bio(p.get("Overview")),
                "tmdb": (p.get("ProviderIds") or {}).get("Tmdb"),
                "born": (p.get("PremiereDate") or "")[:10] or None,
                "place": (p.get("ProductionLocations") or [None])[0],
                "photo": jf_img(jf_id, "Primary", "person") if (p.get("ImageTags") or {}).get("Primary") else None}
    return cached(f"person:jf:{jf_id}", 14 * DAY, fetch_jf) or {}


@bp.get("/api/person/<path:pid>")
def person(pid):
    """A person sheet: photo, short bio, what's in the library with them, and what they're known for."""
    c = cat()
    name = request.args.get("name") or ""
    tmdb = pid[5:] if pid.startswith("tmdb-") else None
    jf_id = None if tmdb else pid
    info = {}
    if jf_id and re.fullmatch(r"[0-9a-f]{32}", jf_id):
        info = person_jf(jf_id)
        tmdb = info.get("tmdb") or tmdb
    known, t = [], {}
    if tmdb and str(tmdb).isdigit():
        def fetch_tmdb():
            if not seerr_ready():
                _raise_offline()
            p = seerr(f"/person/{int(tmdb)}")
            cr = seerr(f"/person/{int(tmdb)}/combined_credits")
            seen, kf = set(), []
            for x in sorted(cr.get("cast") or [], key=lambda x: -((x.get("voteCount") or 0) + (x.get("popularity") or 0) * 20)):
                if x.get("mediaType") not in ("movie", "tv") or x["id"] in seen or (x.get("genreIds") or []) == [10767]:
                    continue
                seen.add(x["id"])
                card = APP().seerr.card(x)
                if card:
                    card["role"] = x.get("character")
                    kf.append(card)
                if len(kf) >= 24:
                    break
            return {"name": p.get("name"), "bio": _short_bio(p.get("biography")), "born": p.get("birthday"),
                    "place": p.get("placeOfBirth"), "dept": p.get("knownForDepartment"),
                    "photo": timg(p.get("profilePath"), "w342"), "known": kf}
        t = cached(f"person:tmdb:{int(tmdb)}", 14 * DAY, fetch_tmdb) or {}
        for k in t.get("known") or []:
            card = owned_or_tmdb(k["type"], k)
            if card and not card.get("owned"):
                card["role"] = k.get("role")
            known.append(card)
    out = {"id": pid, "tmdb": tmdb, "name": info.get("name") or t.get("name") or name,
           "bio": info.get("bio") or t.get("bio") or "", "born": info.get("born") or t.get("born"),
           "place": info.get("place") or t.get("place"), "dept": t.get("dept"),
           "photo": info.get("photo") or t.get("photo")}
    out["library"] = c.with_person(out["name"], jf_id)
    lib_ids = {x["id"] for x in out["library"]}
    out["known"] = [k for k in known if not (k.get("owned") and k["id"] in lib_ids)]
    return ok(person=out)


# ------------------------------------------------------------------ My List
def _ml_tmdb_card(key, data):
    kind, _, tmdb = key.partition(":")
    d, _ = cat().kv_get(f"tmdb:{kind}:{tmdb}")
    base = {"type": kind, "id": int(tmdb), "title": data.get("title"), "year": data.get("year"),
            "poster": data.get("poster"), "backdrop": data.get("backdrop"), "overview": data.get("overview") or "",
            "status": data.get("status"), "anime": data.get("anime")}
    if d:
        base.update({k: d.get(k) for k in ("title", "year", "poster", "backdrop", "overview", "status", "anime") if d.get(k) is not None})
    base["poster"] = tmdb_img(base.get("poster"))
    base["backdrop"] = tmdb_img(base.get("backdrop"))
    if base.get("status") not in (4, 5):
        base.update(title_flag(kind, tmdb))
    return base


def _refresh_statuses(keys):
    for k in keys:
        kind, _, tmdb = k.partition(":")
        try:
            tmdb_detail(kind, tmdb, max_age=3600)
        except Exception:
            pass


@bp.get("/api/mylist")
def mylist_get():
    c = cat()
    items, stale = [], []
    for key, added, card, data in c.mylist():
        if card:
            items.append({"key": key, "added": added, "owned": True, "card": card})
        elif not key.startswith("item:"):
            items.append({"key": key, "added": added, "owned": False, "card": _ml_tmdb_card(key, data)})
            stale.append(key)
    if stale and seerr_ready():
        _pool.submit(_refresh_statuses, stale[:30])
    return ok(items=items, keys=[i["key"] for i in items], sync=c._sync_favs())


def _key_from(b):
    c = cat()
    if b.get("key"):
        return b["key"]
    return c.mylist_key(item_id=b.get("id"), kind=b.get("type"), tmdb=b.get("tmdb"))


@bp.post("/api/mylist")
def mylist_add():
    b = request.get_json(force=True, silent=True) or {}
    key = _key_from(b)
    if not key or (key.startswith("item:") and not cat().get(key[5:])):
        return fail("Nothing to add", 400)
    data = {k: b.get(k) for k in ("title", "year", "poster", "backdrop", "overview", "status", "anime") if b.get(k) is not None}
    cat().mylist_add(key, data)
    if not key.startswith("item:"):
        _pool.submit(_refresh_statuses, [key])
    return ok(key=key, keys=cat().mylist_keys())


@bp.delete("/api/mylist")
def mylist_remove():
    b = request.get_json(force=True, silent=True) or dict(request.args)
    key = _key_from(b)
    if not key:
        return fail("Nothing to remove", 400)
    cat().mylist_remove(key)
    return ok(key=key, keys=cat().mylist_keys())


# ------------------------------------------------------------------ Up next (credits page)
@bp.get("/api/upnext/<path:item_id>")
def upnext(item_id):
    """What to offer when the credits roll, from the library alone (instant, offline): the next episode, then
    similar titles with a plain reason. The assistant's comment and picks (/api/assistant/ending) come on top."""
    c = cat()
    r = c.get(item_id)
    if not r:
        return ok(next=None, picks=[])
    picks, nxt = [], None
    if r["type"] == "Episode":
        nid = c.next_episode(r["id"])
        if nid:
            nxt = c.card(c.items[nid])
            picks.append({"card": nxt, "why": "Next episode", "kind": "next"})
        base = c.get(r.get("series_id")) or r
    else:
        base = r
    genres = set(base.get("genres") or [])
    cast = {p.get("name") for p in (base.get("extra") or {}).get("people") or [] if p.get("type") == "Actor"}
    with c.lock:
        sims = c.similar_local(base, limit=12)
        for o in sims:
            if c._played(o):
                continue
            shared_cast = [p.get("name") for p in (o.get("extra") or {}).get("people") or [] if p.get("name") in cast]
            g = [x for x in o.get("genres") or [] if x in genres]
            why = f"Also with {shared_cast[0]}" if shared_cast else (f"More {g[0].lower()}" if g else "You might like it")
            target = o
            if o["type"] == "Series":
                target = c.next_up_for(o["id"]) or o
            card = c.card(target)
            if o["type"] == "Series":
                card = {**c.card(o), "play_id": target["id"]}
            picks.append({"card": card, "why": why, "kind": "similar"})
            if len(picks) >= 7:
                break
    if not nxt and picks:
        nxt = picks[0]["card"]
    return ok(next=nxt, picks=picks, base=c.card(base))


# ------------------------------------------------------------------ previews
def _preview_target(item_id):
    c = cat()
    r = c.get(item_id)
    if r and r["type"] == "Series":
        return c.first_episode(r["id"]), r
    if r and r["type"] == "Season":
        eps = c.eps_by_season.get(r["id"]) or []
        return (eps[0] if eps else None), r
    return r, r


def _index_intro_end(item_id):
    """The media index's intro guess, only when that title is already fully indexed (never starts work)."""
    try:
        from .index import deps, store
        if not deps.availability()["available"]:
            return None
        key = item_id.lower().replace("-", "")
        d = store.load(key)
        if d is None or not d["meta"].get("complete"):
            return None
        if not APP().jf.online:
            return None
        from .index.api import intro
        with _server().app.test_request_context():
            j = intro(key).get_json(silent=True) or {}
        return (j.get("intro") or {}).get("end")
    except Exception:
        return None


def preview_start(target):
    c = cat()
    runtime = target.get("runtime") or 0
    start = None
    for s in c.segments.get(target["id"]) or []:
        if s.get("type") in ("Intro", "Recap") and s["end"] < (runtime or 1e9) * 0.5:
            start = max(start or 0, s["end"] + 2)
    if start is None and target["type"] == "Episode":
        end = _index_intro_end(target["id"])
        if end:
            start = end + 2
    if start is None:  # past studio logos and black frames
        start = min(max(runtime * 0.06, 45), 300) if runtime > 600 else max(runtime * 0.1, 5)
    return round(start, 1)


def _browser_native(target):
    m = (target.get("extra") or {}).get("media") or {}
    ext = os.path.splitext(target.get("path") or "")[1].lower()
    return ext in (".mp4", ".m4v", ".webm") and m.get("codec") in ("h264", "vp9", "vp8", "av1") and not m.get("hdr")


def _ffmpeg():
    try:
        from .index import deps
        return deps.find_ffmpeg()
    except Exception:
        return shutil.which("ffmpeg")


# ------------------------------------------------------------------ preview subtitles
# Previews start muted, so they show subtitles: the cues of just the preview's window, as WebVTT. A subtitle file
# next to the video comes first, then the file's own text tracks (image tracks like PGS are skipped).
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
SUB_WINDOW = 40


def _sub_langs():
    return [x.strip().lower() for x in (settings.load().get("sub_lang") or "eng").split(",") if x.strip()] + ["eng", "en"]


def _vtt_time(t):
    t = max(0.0, t)
    return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{t % 60:06.3f}"


def _parse_cues(text):
    """SRT, WebVTT or ASS/SSA text -> [(start, end, text)] in seconds."""
    def ts(s):
        s = s.strip().replace(",", ".")
        parts = s.split(":")
        parts = ["0"] * (3 - len(parts)) + parts
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    cues = []
    if "[Events]" in text or text.lstrip().startswith("[Script Info]"):
        for line in text.splitlines():
            if line.startswith("Dialogue:"):
                p = line.split(",", 9)
                if len(p) == 10:
                    body = re.sub(r"\{[^}]*\}", "", p[9]).replace("\\N", "\n").replace("\\n", "\n").strip()
                    if body:
                        try:
                            cues.append((ts(p[1]), ts(p[2]), body))
                        except ValueError:
                            pass
        return cues
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = [l for l in block.split("\n") if l.strip()]
        for i, l in enumerate(lines):
            m = re.match(r"\s*([\d:.,]+)\s*-->\s*([\d:.,]+)", l)
            if m:
                body = re.sub(r"<[^>]+>|\{[^}]*\}", "", "\n".join(lines[i + 1:])).strip()
                if body:
                    try:
                        cues.append((ts(m.group(1)), ts(m.group(2)), body))
                    except ValueError:
                        pass
                break
    return cues


def _sidecar_subs(path):
    p = __import__("pathlib").Path(path)
    langs = _sub_langs()
    cands = [x for x in p.parent.glob(glob_escape(p.stem) + "*") if x.suffix.lower() in (".srt", ".ass", ".ssa", ".vtt")]

    def rank(x):
        name = x.name.lower()
        lang = next((i for i, l in enumerate(langs) if re.search(rf"[._ -]{re.escape(l)}\b", name)), len(langs))
        return (lang, 1 if re.search(r"forced|sdh|\bcc\b", name) else 0)
    cands.sort(key=rank)
    return cands[0] if cands else None


def glob_escape(s):
    return re.sub(r"([\[\]*?])", r"[\1]", s)


def _embedded_sub_stream(ff, path):
    """Index of the best text subtitle stream in the file, from ffmpeg's own stream listing (no ffprobe needed)."""
    try:
        p = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = []
    for m in re.finditer(r"Stream #0:(\d+)(?:\[\w+\])?(?:\((\w+)\))?: Subtitle: (\w+)([^\n]*)", p.stderr.decode("utf-8", "replace")):
        idx, lang, codec, rest = int(m.group(1)), (m.group(2) or "").lower(), m.group(3).lower(), m.group(4).lower()
        if codec in TEXT_SUB_CODECS:
            found.append((idx, lang, "forced" in rest))
    if not found:
        return None
    langs = _sub_langs()
    found.sort(key=lambda s: (next((i for i, l in enumerate(langs) if s[1].startswith(l[:2])), len(langs)), s[2]))
    return found[0][0]


def preview_cues(target, start, length=SUB_WINDOW):
    """Cues inside [start, start + length], times relative to `start`; [] when the title has no text subtitles."""
    path = target.get("path")
    if not path or not os.path.exists(path):
        return []
    side = _sidecar_subs(path)
    if side:
        raw = side.read_bytes()
        text = raw.decode("utf-8-sig", "replace") if raw[:3] == b"\xef\xbb\xbf" or b"\x00" not in raw[:200] \
            else raw.decode("utf-16", "replace")
        return [(max(0, a - start), b - start, t) for a, b, t in _parse_cues(text) if b > start and a < start + length]
    ff = _ffmpeg()
    idx = _embedded_sub_stream(ff, path) if ff else None
    if idx is None:
        return []
    try:  # -ss before -i: fast seek, and the output's clock starts at 0 = `start`
        p = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", path, "-t", f"{length:.1f}",
                            "-map", f"0:{idx}", "-f", "srt", "pipe:1"], capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [c for c in _parse_cues(p.stdout.decode("utf-8", "replace")) if c[1] > 0] if p.returncode == 0 else []


@bp.get("/api/preview/subs/<item_id>")
def preview_subs(item_id):
    """WebVTT for a preview. ?start= is where the preview begins in the title; ?absolute=1 keeps the title's own
    clock (a whole file seeked to `start`), otherwise cues start at 0 (a cut clip or a transcode)."""
    target, _ = _preview_target(item_id)
    try:
        start = float(request.args.get("start", 0))
    except ValueError:
        start = 0.0
    shift = start if request.args.get("absolute") else 0.0
    key = re.sub(r"[^A-Za-z0-9_-]", "_", (target or {}).get("id", item_id))[:60]
    cache = PREVIEW_DIR / f"{key}_{int(start)}_{settings.load().get('sub_lang') or 'eng'}.cues.json"
    cues = None
    if cache.exists():
        try:
            cues = __import__("json").loads(cache.read_text("utf-8"))
        except ValueError:
            cues = None
    if cues is None:
        cues = preview_cues(target, start) if target else []
        try:
            PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(__import__("json").dumps(cues), "utf-8")
        except OSError:
            pass
    body = "WEBVTT\n\n" + "".join(f"{_vtt_time(a + shift)} --> {_vtt_time(b + shift)}\n{t}\n\n" for a, b, t in cues)
    r = Response(body, mimetype="text/vtt")
    r.headers["Cache-Control"] = "private, max-age=3600"
    return r


def _subs_url(tid, start, absolute=False):
    # Previews always show subtitles (they play muted); there's deliberately no setting for it.
    return f"/api/preview/subs/{tid}?start={start}" + ("&absolute=1" if absolute else "")


@bp.get("/api/preview/<item_id>")
def preview(item_id):
    """A muted clip of the title itself for hover cards and the details page, starting after the intro (or past
    studio logos and black frames). The file on this PC first: played as is when the browser can, else a short
    clip cut locally with ffmpeg (kept, so it plays offline next time); otherwise Jellyfin transcodes one."""
    target, _ = _preview_target(item_id)
    if not target:
        return ok(available=False, url=None)
    start = preview_start(target)
    path = target.get("path")
    exists = bool(path and os.path.exists(path))
    tid = target["id"]
    if exists and _browser_native(target):
        return ok(available=True, url=f"/api/preview/file/{tid}", start=start, seek=True, kind="file",
                  subs=_subs_url(tid, start, absolute=True))
    if exists and (_clip_file(tid, start).exists() or _ffmpeg()):
        return ok(available=True, url=f"/api/preview/clip/{tid}?start={start}", start=start, seek=False, kind="clip",
                  subs=_subs_url(tid, start))
    if target.get("source") == "jellyfin" and APP().jf.fresh_online(45):
        return ok(available=True, url=f"/api/preview/stream/{tid}?start={start}", start=start, seek=False, kind="stream",
                  subs=_subs_url(tid, start) if exists else None)
    return ok(available=False, url=None)


@bp.get("/api/preview/file/<item_id>")
def preview_file(item_id):
    r = cat().get(item_id)
    if not r or not r.get("path") or not os.path.exists(r["path"]) or not _browser_native(r):
        return Response(status=404)
    return send_file(r["path"], conditional=True, max_age=0)


def _clip_file(item_id, start):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", item_id)[:60]
    return PREVIEW_DIR / f"{safe}_{int(start)}_1080s.mp4"


def _image_sub_stream(ff, path):
    """Index of the file's best picture subtitle track (PGS / VobSub), for burning into a preview clip when the title
    has no text subtitles to send alongside."""
    try:
        p = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = []
    for m in re.finditer(r"Stream #0:(\d+)(?:\[\w+\])?(?:\((\w+)\))?: Subtitle: (\w+)([^\n]*)", p.stderr.decode("utf-8", "replace")):
        if m.group(3).lower() in ("hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvb_subtitle"):
            found.append((int(m.group(1)), (m.group(2) or "").lower(), "forced" in m.group(4).lower()))
    if not found:
        return None
    langs = _sub_langs()
    found.sort(key=lambda s: (next((i for i, l in enumerate(langs) if s[1].startswith(l[:2])), len(langs)), s[2]))
    return found[0][0] if any(s[1][:2] in [l[:2] for l in langs] for s in found[:1]) else None


def _encoders(ff):
    """Video encoders this ffmpeg can actually open on this PC (NVENC first: a 1080p clip in a couple of seconds)."""
    global _ENCODERS
    if _ENCODERS is None:
        out = []
        for enc in ("h264_nvenc", "h264_qsv", "h264_amf"):
            try:
                p = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=black:s=256x144:d=0.2",
                                    "-c:v", enc, "-f", "null", "-"], capture_output=True, timeout=20,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if p.returncode == 0:
                    out.append(enc)
            except (OSError, subprocess.TimeoutExpired):
                pass
        _ENCODERS = out + ["libx264"]
    return _ENCODERS


_ENCODERS = None
ENC_ARGS = {"h264_nvenc": ["-preset", "p4", "-cq", "23", "-b:v", "0"], "h264_qsv": ["-global_quality", "24"],
            "h264_amf": ["-quality", "balanced", "-rc", "cqp", "-qp_i", "22", "-qp_p", "24"],
            "libx264": ["-preset", "veryfast", "-crf", "23"]}


def _prune_previews():
    try:
        files = sorted(PREVIEW_DIR.glob("*.mp4"), key=lambda f: f.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
        for f in files:
            if total <= PREVIEW_LIMIT * 0.8:
                break
            total -= f.stat().st_size
            f.unlink(missing_ok=True)
    except OSError:
        pass


def make_clip(item_id, start):
    """Cut (once) a short 1080p H.264 clip (stereo AAC; the page starts it muted) from the local file; HDR is
    tone-mapped and picture subtitles are burned in. Returns its path."""
    out = _clip_file(item_id, start)
    if out.exists():
        return out
    r = cat().get(item_id)
    ff = _ffmpeg()
    if not r or not ff or not r.get("path") or not os.path.exists(r["path"]):
        return None
    with _clip_guard:
        lock = _clip_locks.setdefault(out.name, threading.Lock())
    with lock:
        if out.exists():
            return out
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        hdr = ((r.get("extra") or {}).get("media") or {}).get("hdr")
        part = out.with_suffix(".part.mp4")
        # 1080p (upscaled when the source is smaller), tone-mapped from HDR; picture subtitles burned in when the title
        # has no text ones (those go alongside as WebVTT instead).
        burn = None
        if not _sidecar_subs(r["path"]) and _embedded_sub_stream(ff, r["path"]) is None:
            burn = _image_sub_stream(ff, r["path"])
        scale = "scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=ceil(iw/2)*2:ceil(ih/2)*2"
        tones = ["tonemapx=tonemap=bt2390:t=bt709:m=bt709:p=bt709:format=yuv420p", "format=yuv420p"] if hdr else ["format=yuv420p"]
        for enc in _encoders(ff):
            for tone in tones:
                for with_subs in ([True, False] if burn is not None else [False]):
                    if with_subs:  # the subtitle canvas is scaled to the picture, then laid over it
                        fc = (f"[0:v:0]{scale},{tone}[v];[0:{burn}][v]scale2ref=w=iw:h=ih[s][v2];"
                              f"[v2][s]overlay=0:0:format=auto,format=yuv420p[out]")
                        vmap = ["-filter_complex", fc, "-map", "[out]"]
                    else:
                        vmap = ["-map", "0:v:0", "-vf", f"{scale},{tone}"]
                    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start), "-i", r["path"], "-t", str(CLIP_SECONDS),
                           *vmap, "-map", "0:a:0?", "-map_chapters", "-1", "-sn", "-dn", "-c:v", enc, *ENC_ARGS[enc],
                           "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart", str(part)]
                    try:
                        p = subprocess.run(cmd, capture_output=True, timeout=120,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    except (OSError, subprocess.TimeoutExpired) as e:
                        print("preview clip failed:", e)
                        continue
                    if p.returncode == 0 and part.exists() and part.stat().st_size > 10_000:
                        part.replace(out)
                        _prune_previews()
                        return out
        part.unlink(missing_ok=True)
    return None


@bp.get("/api/preview/clip/<item_id>")
def preview_clip(item_id):
    try:
        start = float(request.args.get("start", 0))
    except ValueError:
        start = 0
    f = make_clip(item_id, start)
    if not f:
        return Response(status=404)
    r = send_file(f, mimetype="video/mp4", conditional=True, max_age=0)
    r.headers["Cache-Control"] = "private, max-age=3600"
    return r


@bp.get("/api/preview/stream/<item_id>")
def preview_stream(item_id):
    """Jellyfin's transcoded preview, passed through so the page never sees the server address or token."""
    jf = APP().jf
    if not jf.fresh_online(45):
        return Response(status=503)
    try:
        start = float(request.args.get("start", 0))
    except ValueError:
        start = 0
    try:
        up = requests.get(jf.preview_url(item_id, int(start * TICKS)), stream=True, timeout=(3, 15))
    except requests.RequestException:
        return Response(status=502)
    if up.status_code != 200:
        up.close()
        return Response(status=up.status_code)

    def gen(limit=60 * 2**20):
        sent = 0
        try:
            for chunk in up.iter_content(64 * 1024):
                yield chunk
                sent += len(chunk)
                if sent > limit:
                    break
        finally:
            up.close()
    return Response(gen(), mimetype=up.headers.get("Content-Type", "video/mp4"), direct_passthrough=True,
                    headers={"Cache-Control": "no-store"})
