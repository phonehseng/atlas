"""Home as one catalogue: the library and everything Seerr can request, merged, one card per title.

Every Home row goes through assemble() before the page gets it:

  * one card per title: a title that's in the library is the library card (it plays), anything else is the Seerr
    card (Request), so nothing shows twice because it exists in both worlds;
  * series order: a later part of a film series (TMDB collection) is never suggested while an earlier part is
    unwatched; the earliest unwatched part takes its place ("Next in Harry Potter: Chamber of Secrets"). Watched
    means played here, or played and then removed to save space (catalog.removed_items). Shows only ever offer the
    next unwatched episode;
  * de-duplication: a title is in at most one recommendation row, and on the page at most twice counting Continue
    watching / Next up / My List; a row that mostly repeats an earlier one, or is left with fewer than 4 titles,
    is dropped; only one people row (director / actor) per page, the one with the most variety;
  * a capped, rotating mix (stable for the day) under the personal rows, with more rows paged in as you scroll.

Everything here reads the catalog and its small cache (the kv table) only, so Home answers at once and works
offline (requestable titles simply drop out then). What's missing is fetched from Seerr in the background
(warm()); the page asks again when that's done."""
import datetime as dt
import hashlib
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

DAY = 86400
ROW_LEN = 20            # titles per recommendation row
FIRST_PAGE_ROWS = 6     # recommendation rows on the first screenful (the rest page in on scroll)
MORE_ROWS = 3           # rows per extra page
MIN_TITLES = 4          # a row with fewer distinct titles left after de-duplication is dropped
MAX_OVERLAP = 0.5       # ...and so is one that's mostly the titles of rows above it
PAGE_MAX = 2            # times a title may appear on the whole page
BROWSE_AGE = 6 * 3600   # trending / popular / genre lists are refreshed after this (the old list is used meanwhile)
PERSONAL = ("continue", "nextup", "new")
NOT_FOR_HOME = {10763, 10764, 10767}  # TMDB TV genres: news, reality, talk shows (never "recommendations")

_lock = threading.RLock()
_index = {"at": 0, "data": None}
_seerr = {"ok": None, "at": 0}
_warm = {"thread": None, "needs": set(), "tried": {}, "done_at": 0, "added": 0}
_last = {"rec_keys": set(), "page_keys": {}}


def invalidate():
    """The library changed: rebuild the collection index on the next call."""
    _index["at"] = 0


def _catalog():
    from . import server
    return server.catalog()


def _app():
    from . import server
    return server.APP


# ------------------------------------------------------------------ Seerr reachability
def seerr_ready(app=None):
    sr = getattr(app or _app(), "seerr", None)
    return bool(sr and sr.ready)


def seerr_online(app=None):
    """Seerr is set up and answered lately (checked in the background, never on the page's time)."""
    app = app or _app()
    if not seerr_ready(app):
        return False
    if time.time() - _seerr["at"] > 120:
        _kick(app)
    return _seerr["ok"] is not False


def _ping(app):
    try:
        app.seerr.call("GET", "/status", timeout=(2, 4))
        ok = True
    except Exception:
        ok = False
    _seerr.update(ok=ok, at=time.time())
    return ok


# ------------------------------------------------------------------ keys and cards
def _kv(cat, key, max_age=None):
    try:
        data, age = cat.kv_get(key)
    except Exception:
        return None, None
    return data, age


def lib_key(cat, r):
    """The one key a title has on Home, whichever world its card comes from: "movie:<tmdb>", "tv:<tmdb>"."""
    if r.get("type") in ("Episode", "Season") and r.get("series_id"):
        r = cat.items.get(r["series_id"]) or r
    t = (r.get("provider") or {}).get("Tmdb")
    if t and r.get("type") in ("Movie", "Series"):
        return f"{'movie' if r['type'] == 'Movie' else 'tv'}:{t}"
    return "item:" + str(r.get("id"))


def key_of(cat, card):
    if card.get("type") in ("movie", "tv"):
        return f"{card['type']}:{card['id']}"
    r = cat.items.get(card.get("id")) or card
    return lib_key(cat, r)


def is_req(card):
    return card.get("type") in ("movie", "tv")


def _short_series(name):
    return re.sub(r"\s*\b(collection|saga|series|trilogy|anthology)$", "", (name or "").strip(), flags=re.I).strip()


def part_label(col_name, title):
    """("Harry Potter", "Chamber of Secrets") from "Harry Potter Collection" and the film's title."""
    base = _short_series(col_name)
    sub = (title or "").strip()
    if base and sub.lower().startswith(base.lower()) and len(sub) > len(base) + 2:
        rest = sub[len(base):].strip(" :-–—,.")
        m = re.match(r"(?:and|&)\s+(?:the\s+)?(.+)", rest, re.I)
        sub = (m.group(1) if m else rest).strip() or sub
        sub = sub[0].upper() + sub[1:]
    return base or col_name or "the series", sub


# ------------------------------------------------------------------ film series (TMDB collections)
def _collection_index(cat):
    """{member: {"movie:<tmdb>": cid}, cols: {cid: {"name", "parts"}}, missing: [library cids not cached]}.
    From the library's own TmdbCollection ids, cached title details and cached collections. Kept for 2 minutes."""
    with _lock:
        if _index["data"] and time.time() - _index["at"] < 120:
            return _index["data"]
    from .details import part_order
    member, cols, lib_cols = {}, {}, {}
    with cat.lock:
        for r in cat._of("Movie"):
            p = r.get("provider") or {}
            t, cid = p.get("Tmdb"), p.get("TmdbCollection")
            if t and cid:
                member[f"movie:{t}"] = str(cid)
                lib_cols.setdefault(str(cid), []).append(r)
        try:
            rows = cat.db.execute("SELECT key, json_extract(data, '$.collection.id'), "
                                  "json_extract(data, '$.collection.name') FROM kv "
                                  "WHERE key LIKE 'tmdb:movie:%'").fetchall()
        except Exception:
            rows = []
        try:
            cached = cat.db.execute("SELECT key, data FROM kv WHERE key LIKE 'collection:%'").fetchall()
        except Exception:
            cached = []
    names = {}
    for key, cid, name in rows:
        if cid:
            member.setdefault("movie:" + key.split(":")[2], str(cid))
            names[str(cid)] = name
    for key, data in cached:
        try:
            d = json.loads(data or "{}")
        except ValueError:
            continue
        cid = key.split(":", 1)[1]
        parts = sorted([p for p in d.get("parts") or [] if p.get("id")], key=part_order)
        if not parts:
            continue
        cols[cid] = {"name": d.get("name") or names.get(cid), "parts": parts}
        for p in parts:
            member[f"movie:{p['id']}"] = cid
    missing = []
    for cid, rs in lib_cols.items():
        if cid in cols:
            continue
        missing.append(cid)
        from .scripted import collection_name
        with cat.lock:
            nm = names.get(cid) or collection_name(cat, (rs[0].get("provider") or {}).get("TmdbCollection"))
        parts = [{"id": int((r.get("provider") or {}).get("Tmdb")), "title": r.get("name"),
                  "year": str(r.get("year") or ""),
                  "date": dt.datetime.fromtimestamp(r["premiere"]).strftime("%Y-%m-%d") if r.get("premiere") else None,
                  "poster": None} for r in rs if str((r.get("provider") or {}).get("Tmdb") or "").isdigit()]
        cols[cid] = {"name": nm, "parts": sorted(parts, key=part_order), "library_only": True}
    data = {"member": member, "cols": cols, "missing": missing}
    with _lock:
        _index.update(at=time.time(), data=data)
    return data


class Ctx:
    """What one assemble pass knows: the collection index, what's watched/started, owned titles by key."""

    def __init__(self, app, cat):
        self.app, self.cat = app, cat
        self.online = seerr_online(app)
        self.idx = _collection_index(cat)
        self.needs = set()
        self.watched, self.started, self.owned = set(), set(), {}
        with cat.lock:
            for r in cat._of("Movie,Series"):
                k = lib_key(cat, r)
                self.owned[k] = r
                if r["type"] == "Movie":
                    u = cat.ud.get(r["id"]) or {}
                    if u.get("played"):
                        self.watched.add(k)
                    elif u.get("position"):
                        self.started.add(k)
        try:
            for rec in cat.removed_items("movie"):
                if rec.get("played") and rec.get("tmdb"):
                    self.watched.add(f"movie:{rec['tmdb']}")
        except Exception:
            pass
        for cid in self.idx["missing"]:
            self.need("col", cid)

    def need(self, *task):
        if seerr_ready(self.app):
            self.needs.add(task)

    def kv(self, key, task=None, max_age=None):
        data, age = _kv(self.cat, key)
        if task and (age is None or (max_age and age > max_age)):
            self.need(*task)
        return data

    # ---------------------------------------------------------------- cards
    def lib_card(self, r, why=None):
        c = self.cat.card(r)
        if why:
            c["why"] = why
        return c

    def req_card(self, c, why=None, allow_requested=False):
        """A Seerr card as the page shows it, or the library card when it's owned, or None (watched and removed,
        already requested, offline)."""
        if not c or c.get("type") not in ("movie", "tv") or not c.get("id"):
            return None
        k = f"{c['type']}:{c['id']}"
        r = self.owned.get(k)
        if r:
            return self.lib_card(r, why)
        if c.get("status") == 5 or not self.online:
            return None
        if c.get("status") in (2, 3, 4) and not allow_requested:
            return None
        if set(c.get("genre_ids") or []) & NOT_FOR_HOME:
            return None
        if k in self.watched:
            return None
        from .details import tmdb_card
        out = tmdb_card({kk: v for kk, v in c.items() if kk not in ("because", "role")})
        flag = None
        try:
            flag = self.cat.removed_flag(c["type"], c["id"])
        except Exception:
            pass
        for kk in ("removed", "played", "state", "removed_at", "watched_at"):
            out.pop(kk, None)
        if flag:
            if flag.get("played"):
                return None
            out.update(flag)
        if why:
            out["why"] = why
        if c["type"] == "movie" and k not in self.idx["member"]:
            self.need("detail", int(c["id"]))  # its collection (if any) isn't known yet
        return out

    # ---------------------------------------------------------------- series order
    def _place(self, k):
        cid = self.idx["member"].get(k)
        col = self.idx["cols"].get(cid) if cid else None
        if not col:
            return None, None, None
        ids = [f"movie:{p['id']}" for p in col["parts"]]
        return (cid, col, ids.index(k)) if k in ids else (None, None, None)

    def next_part(self, cid):
        col = self.idx["cols"].get(cid)
        for p in (col or {}).get("parts") or []:
            if f"movie:{p['id']}" not in self.watched:
                return p
        return None

    def _part_card(self, col, p, label):
        k = f"movie:{p['id']}"
        r = self.owned.get(k)
        if r:
            return self.lib_card(r, label)
        return self.req_card({**p, "type": "movie"}, label, allow_requested=True)

    def rule(self, card, swap=True):
        """The card as it may be recommended: itself (maybe with a "Next in ..." label), the earlier part of its
        series that comes first (swap=False: None instead, for lists like Trending where an old film would be
        out of place), or None."""
        cat = self.cat
        if not is_req(card):
            r = cat.items.get(card.get("id"))
            if not r:
                return card
            if r["type"] == "Episode":
                nxt = cat.next_up_for(r.get("series_id")) if r.get("series_id") else None
                return card if nxt and nxt["id"] == r["id"] else None
            if r["type"] == "Season":
                return None
            if r["type"] == "Series":
                with cat.lock:
                    eps = cat.eps.get(r["id"]) or []
                    started = any((cat.ud.get(e["id"]) or {}).get("played") for e in eps)
                    nxt = cat.next_up_for(r["id"]) if started else None
                if nxt and nxt.get("season_no") is not None and not card.get("why"):
                    card = {**card, "why": f"Up next: S{nxt['season_no']} E{nxt.get('episode_no') or 1}"}
                return card
            if r["type"] != "Movie":
                return card
        k = key_of(cat, card)
        cid, col, i = self._place(k)
        if col is None:
            return card
        earlier = [p for p in col["parts"][:i] if f"movie:{p['id']}" not in self.watched]
        name = col.get("name")
        if not earlier or k in self.started or k in self.watched:  # what you've seen or begun isn't a suggestion
            if i > 0 and not earlier and k not in self.watched and not (card.get("why") or "").startswith("Next in"):
                base, sub = part_label(name, card.get("name") or card.get("title"))
                card = {**card, "why": f"Next in {base}: {sub}", "next_part": True}
            return card
        if not swap:
            return None
        first = earlier[0]
        base, sub = part_label(name, first.get("title"))
        j = [p["id"] for p in col["parts"]].index(first["id"])
        label = f"Next in {base}: {sub}" if j > 0 else None  # the first part needs no explaining
        swapped = self._part_card(col, first, label)
        if swapped:
            swapped["next_part"] = True
        return swapped


# ------------------------------------------------------------------ sources (cache only; misses are warmed)
def _recs(ctx, kind, tmdb):
    return ctx.kv(f"recs:{kind}:{int(tmdb)}", ("recs", kind, int(tmdb)), 2 * DAY) or []


def _browse(ctx, path, **params):
    key = "homemix:browse:" + path + ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else "")
    data = ctx.kv(key, ("browse", path, tuple(sorted(params.items()))), BROWSE_AGE)
    return data if isinstance(data, list) else []


def _genre_ids(genres, kind):
    from .ai import TMDB_MOVIE, TMDB_TV
    from .scripted import norm_genres
    table = TMDB_MOVIE if kind == "movie" else TMDB_TV
    return sorted({str(table[g]) for g in norm_genres(genres) if g in table})


def _by_genre(ctx, genres, kind="movie"):
    ids = _genre_ids(genres, kind)
    if not ids:
        return []
    return _browse(ctx, "/discover/movies" if kind == "movie" else "/discover/tv", genre=",".join(ids))


def _person_credits(ctx, person):
    """Seerr cards for a person's films/shows (as cast, or as director for directors), most known first."""
    jf_id = person.get("id")
    if not jf_id:
        return []
    info = ctx.kv(f"person:jf:{jf_id}", ("pjf", jf_id), 14 * DAY) or {}
    tmdb = info.get("tmdb")
    if not tmdb:
        return []
    data = ctx.kv(f"homemix:credits:{tmdb}", ("credits", str(tmdb)), 14 * DAY) or {}
    # their films and shows, not the making-of specials and reunions about them
    return [c for c in data.get("crew" if person.get("type") == "Director" else "cast") or []
            if 99 not in (c.get("genre_ids") or []) and (c.get("votes") or 0) >= 40]


# ------------------------------------------------------------------ row building
def _weave(owned, reqs, n=ROW_LEN):
    """Owned titles that fit well lead and recur every third place; requestable titles fill the rest."""
    o, r, out = list(owned), list(reqs), []
    while len(out) < n and (o or r):
        take_owned = o and (not r or len(out) % 3 == 0)
        out.append(o.pop(0) if take_owned else r.pop(0))
    return out


def _uniq(ctx, cards):
    seen, out = set(), []
    for c in cards:
        if not c:
            continue
        k = key_of(ctx.cat, c)
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


def _ruled(ctx, cards, swap=True):
    return _uniq(ctx, [ctx.rule(c, swap) for c in cards if c])


def _candidates(ctx, rows_in, taste):
    """Every recommendation row Home could show today: [{key, title, kind, prio, owned, reqs, mode, ...}]."""
    cat, out = ctx.cat, []
    rng = random.Random(hashlib.sha1(f"{dt.date.today()}|home".encode()).hexdigest())

    def add(key, title, kind, prio, owned=(), reqs=(), mode="weave", **extra):
        out.append({"key": key, "title": title, "kind": kind, "prio": prio + rng.random() * 2.5,
                    "owned": [c for c in owned if c], "reqs": [c for c in reqs if c], "mode": mode, **extra})

    with cat.lock:
        recent = [r for r in cat.recently_watched(8) if r["type"] in ("Movie", "Series")]
        unplayed = [r for r in cat._of("Movie,Series") if not cat._played(r)]
    # 1. the next part of every film series you've started (owned or to request), most recent first
    started_cols = [ctx.idx["member"].get(lib_key(cat, r)) for r in recent]
    try:  # watched, then removed to save space: their series still count
        started_cols += [ctx.idx["member"].get(f"movie:{rec.get('tmdb')}") for rec in cat.removed_items("movie")
                         if rec.get("played")]
    except Exception:
        pass
    nexts, seen_cols = [], set()
    for cid in started_cols:
        if not cid or cid in seen_cols:
            continue
        seen_cols.add(cid)
        p = ctx.next_part(cid)
        card = ctx._part_card(None, p, None) if p else None
        card = ctx.rule(card) if card else None
        if card and card.get("id"):
            nexts.append(card)
    if nexts:
        add("series", "Next in your series", "series", 100, owned=[c for c in nexts if not is_req(c)],
            reqs=[c for c in nexts if is_req(c)], mode="natural", min_titles=1,
            order=[key_of(cat, c) for c in nexts])
    # 2. because you watched X: Seerr's recommendations for it, with the library's own similar titles
    for n, base in enumerate(recent[:4]):
        tmdb = (base.get("provider") or {}).get("Tmdb")
        kind = "tv" if base["type"] == "Series" else "movie"
        sims = [cat.items.get(i) for i in (base.get("extra") or {}).get("similar") or []]
        sims = [s for s in sims if s] or cat.similar_local(base, 12)
        lib = [ctx.lib_card(s) for s in sims if s and not cat._played(s)]
        reqs = [ctx.req_card(c) for c in _recs(ctx, kind, tmdb)] if tmdb else []
        add(f"because-{base['id']}", f"Because you watched {base.get('name')}", "because", 9 - n * 1.5,
            owned=lib, reqs=reqs, seed=base["id"])
    # 3. what everyone's watching
    add("trending", "Trending this week", "browse", 6, reqs=[ctx.req_card(c) for c in _browse(ctx, "/discover/trending")],
        mode="natural")
    add("popular-movies", "Popular films", "browse", 4.5,
        reqs=[ctx.req_card(c) for c in _browse(ctx, "/discover/movies")], mode="natural")
    add("popular-tv", "Popular shows", "browse", 4.5,
        reqs=[ctx.req_card(c) for c in _browse(ctx, "/discover/tv")], mode="natural")
    add("upcoming", "Coming soon to cinemas", "browse", 3.5,
        reqs=[ctx.req_card(c) for c in _browse(ctx, "/discover/movies/upcoming")], mode="natural")
    # 4. taste: the profile's best library fits, with requestable titles in its top genres ranked by it
    from . import taste as taste_profile
    top_genres = []
    if taste:
        top_genres = taste.get("genres") or []
        reqs = []
        for g in top_genres[:2]:
            reqs += _by_genre(ctx, [g], "movie")
        reqs = sorted(_uniq(ctx, [ctx.req_card(c) for c in reqs]), key=lambda c: -taste_profile.score(c)["score"])
        add("taste", taste["title"], "taste", 7.5, owned=taste["items"], reqs=reqs)
    # 5. one people row: a director or actor from what you watched (Jellyfin's suggestions), most varied wins
    people = []
    for rec in cat.meta.get("recommendations") or []:
        t = rec.get("type") or ""
        if "Director" not in t and "Actor" not in t:
            continue
        base = rec.get("base")
        person = None
        with cat.lock:
            for r in cat._of("Movie,Series"):
                person = next((p for p in (r.get("extra") or {}).get("people") or [] if p.get("name") == base), None)
                if person:
                    break
        lib = [ctx.lib_card(cat.items[i]) for i in rec.get("ids") or [] if i in cat.items and not cat._played(cat.items[i])]
        reqs = [ctx.req_card(c) for c in _person_credits(ctx, person or {})]
        title = f"Directed by {base}" if "Director" in t else f"Starring {base}"
        people.append(dict(key="person-" + re.sub(r"\W+", "-", base or "").strip("-").lower(), title=title,
                           owned=lib, reqs=reqs))
    if people:
        def variety(p):
            cards = _ruled(ctx, p["owned"] + p["reqs"])
            groups = {ctx.idx["member"].get(key_of(cat, c)) or key_of(cat, c) for c in cards}
            return len(groups)
        best = max(people, key=variety)
        add(best["key"], best["title"], "people", 5.5, owned=best["owned"], reqs=best["reqs"])
    # 6. genres: today's few (your top ones weigh more), library titles plus Seerr's popular ones in that genre
    from .scripted import Taste, SEASONAL, norm_genres
    with cat.lock:
        weights = Taste(cat).genres
    pool = ["Action", "Comedy", "Science Fiction", "Animation", "Drama", "Adventure", "Thriller", "Fantasy",
            "Horror", "Mystery", "Crime", "Family", "Romance"]
    season = SEASONAL.get(dt.date.today().month)
    for g in pool:
        lib = sorted([r for r in unplayed if g in norm_genres(r.get("genres"))],
                     key=lambda r: -((r.get("rating") or 0) + rng.random()))
        prio = 2.5 + 4 * weights.get(g, 0) + (2 if season and g in season[1] else 0)
        add("genre-" + g, g if g != "Science Fiction" else "Sci-fi", "genre", prio,
            owned=[ctx.lib_card(r) for r in lib], reqs=[ctx.req_card(c) for c in _by_genre(ctx, [g], "movie")])
    if any(r.get("library") == "anime" for r in cat.items.values()):
        lib = [ctx.lib_card(r) for r in unplayed if r.get("library") == "anime"]
        add("anime", "Anime", "genre", 3.5, owned=lib,
            reqs=[ctx.req_card(c) for c in _browse(ctx, "/discover/tv", genre="16", language="ja")])
    # 7. rows made elsewhere (rules, taste, AI): they go through the same checks
    # (the catalog's own library rows, "because", "finish", people and taste rows are rebuilt above)
    for row in rows_in:
        k = row["key"]
        if k not in ("ai-picks", "ai-discover", "classic-top-unseen", "classic-seasonal", "classic-short"):
            continue
        kind = "ai" if k.startswith("ai-") else "library"
        prio = {"ai-picks": 6, "ai-discover": 6.5, "classic-top-unseen": 3.5, "classic-seasonal": 4.5,
                "classic-short": 2}.get(k, 2.5)
        lib = [c for c in row["items"] if not is_req(c)]
        reqs = [ctx.req_card(c) for c in row["items"] if is_req(c)]
        if k == "classic-top-unseen":
            reqs += [ctx.req_card(c) for c in _browse(ctx, "/discover/movies", sortBy="vote_average.desc",
                                                     voteCountGte="2000")]
        add(k, row["title"], kind, prio, owned=lib, reqs=reqs, mode="natural" if kind == "ai" else "weave",
            ai=row.get("ai"))
    return out


def _personal(ctx, rows_in):
    """Continue watching, Next up and what's new on this PC (My List and arrivals are drawn by the page)."""
    by = {r["key"]: r for r in rows_in}
    out = []
    for key in ("continue", "nextup"):
        if by.get(key) and by[key]["items"]:
            out.append(by[key])
    new = []
    for key in ("new-movies", "new-shows"):
        new += (by.get(key) or {}).get("items") or []
    new = sorted(new, key=lambda c: c.get("added") or "", reverse=True)
    new = [c for c in _ruled(ctx, new, swap=False) if not is_req(c)][:24]  # a list of what's here: never swapped
    if new:
        out.append({"key": "new", "title": "New on this PC", "items": new, "style": "poster"})
    return out


def assemble(app, rows_in, mylist=(), page=1, taste=None):
    """The rows Home shows: personal rows, then the rotating mix. page 1: the first screenful; page N > 1: the next
    few rows for infinite scroll. Returns {"rows", "more", "warming", "seerr"}."""
    cat = _catalog()
    ctx = Ctx(app, cat)
    personal = _personal(ctx, rows_in)
    count = {}
    for row in personal:
        for c in row["items"]:
            k = key_of(cat, c)
            count[k] = count.get(k, 0) + 1
    for k in mylist:
        count[k] = count.get(k, 0) + 1
    cands = sorted(_candidates(ctx, rows_in, taste), key=lambda r: -r["prio"])
    used, out_rows = set(), []
    for c in cands:
        # nothing you've already watched is recommended (a Seerr title that's owned and watched included)
        swap = c["kind"] != "browse"
        owned = [x for x in _ruled(ctx, c["owned"], swap) if not x.get("played")]
        reqs = [x for x in _ruled(ctx, c["reqs"], swap) if not x.get("played")]
        keys = {key_of(cat, x) for x in owned + reqs}
        if not keys:
            continue
        if len(keys & used) / len(keys) > MAX_OVERLAP:
            continue
        ok = lambda x: (key_of(cat, x) not in used and count.get(key_of(cat, x), 0) < PAGE_MAX)
        owned = [x for x in owned if ok(x)]
        reqs = [x for x in reqs if ok(x)]
        if c["mode"] == "natural":
            items = _uniq(ctx, owned + reqs)
            if c.get("order"):
                pos = {k: n for n, k in enumerate(c["order"])}
                items.sort(key=lambda x: pos.get(key_of(cat, x), 999))
            elif c["kind"] == "browse":
                order = {key_of(cat, x): n for n, x in enumerate(c["reqs"])}
                items.sort(key=lambda x: order.get(key_of(cat, x), 999))
            items = items[:ROW_LEN]
        else:
            # strong library matches stay (the next part of a series first); requestable titles are most of it
            lead = [x for x in owned if x.get("next_part")]
            rest = [x for x in owned if not x.get("next_part")]
            items = _uniq(ctx, _weave(lead + rest[:8], reqs))
        if len(items) < c.get("min_titles", MIN_TITLES):
            continue
        for x in items:
            k = key_of(cat, x)
            used.add(k)
            count[k] = count.get(k, 0) + 1
        out_rows.append({"key": c["key"], "title": c["title"], "items": items, "style": "poster",
                         "kind": c["kind"], **({"ai": c["ai"]} if c.get("ai") else {})})
    out_rows = _mix_order(out_rows)
    with _lock:
        _last["rec_keys"] = set(used)
        _last["count"] = dict(count)
    first = out_rows[:FIRST_PAGE_ROWS]
    if page <= 1:
        rows = personal + first
    else:
        start = FIRST_PAGE_ROWS + (page - 2) * MORE_ROWS
        rows = out_rows[start:start + MORE_ROWS]
    more = len(out_rows) > FIRST_PAGE_ROWS + max(0, page - 1) * MORE_ROWS
    warming = _queue(app, ctx.needs)
    return {"rows": rows, "more": more, "warming": warming,
            "seerr": {"ready": seerr_ready(app), "online": ctx.online}}


LIBRARY_VIEW = ("unwatched", "top", "anime", "recent")


def library_view(app, rows_in):
    """Home's "On this PC" view: the library's own rows (haven't watched yet, highest rated, a few genres,
    recently watched), shown instead of the mix when that filter is on. Later parts of a series are left out
    of the suggestion rows here too."""
    cat = _catalog()
    ctx = Ctx(app, cat)
    out = []
    for row in rows_in:
        k = row["key"]
        if k not in LIBRARY_VIEW and not k.startswith("genre-"):
            continue
        items = row["items"] if k == "recent" else [c for c in (ctx.rule(c, swap=False) for c in row["items"]) if c]
        if len(items) >= 3:
            out.append({**row, "items": items, "libview": True})
    order = {"unwatched": 0, "top": 2, "anime": 3, "recent": 9}
    return sorted(out, key=lambda r: order.get(r["key"], 5))


def _mix_order(rows):
    """Keep the strongest first, but never two rows of the same kind back to back when it can be helped."""
    if not rows:
        return rows
    out, left = [rows[0]], rows[1:]
    while left:
        k = next((n for n, r in enumerate(left) if r["kind"] != out[-1]["kind"] or r["kind"] == "series"), 0)
        out.append(left.pop(k))
    return out


def filter_late(app, items, min_titles=MIN_TITLES):
    """Rows that arrive after Home was drawn (the assistant's picks): same series rule and de-duplication against
    what Home already shows. Returns the items to show, or [] when too few are left."""
    cat = _catalog()
    ctx = Ctx(app, cat)
    with _lock:
        used = set(_last["rec_keys"])
        count = dict(_last.get("count") or {})
    out = []
    for c in items or []:
        c = ctx.rule(c) if not is_req(c) else ctx.rule(ctx.req_card(c, c.get("why")) or {})
        if not c or not c.get("id"):
            continue
        k = key_of(cat, c)
        if k in used or count.get(k, 0) >= PAGE_MAX or any(key_of(cat, x) == k for x in out):
            continue
        out.append(c)
    _queue(app, ctx.needs)
    return out if len(out) >= min_titles else []


# ------------------------------------------------------------------ library pages: the same merge
def library_extra(app, kind, rows):
    """A library page's genre rows with Seerr's popular titles in that genre mixed in (owned ones stay library
    cards), from the cache. rows: catalog.library_rows(kind)."""
    cat = _catalog()
    ctx = Ctx(app, cat)
    tkind = "movie" if kind == "movies" else "tv"
    out = []
    for row in rows:
        if not row["key"].startswith("g-"):
            out.append(row)
            continue
        g = row["key"][2:]
        if kind == "anime":
            src = _browse(ctx, "/discover/tv", genre="16", language="ja")
        else:
            src = _by_genre(ctx, [g], tkind)
        reqs = _uniq(ctx, [ctx.req_card(c) for c in src])
        owned_keys = {key_of(cat, c) for c in row["items"]}
        reqs = [c for c in reqs if key_of(cat, c) not in owned_keys]
        out.append({**row, "items": _weave(row["items"], reqs, n=max(ROW_LEN, len(row["items"]) + 8))})
    _queue(app, ctx.needs)
    return out


def discover_page(app, kind, page=1, sort="popular", genre=None):
    """Requestable titles for a library page's grid (Movies / Shows / Anime), one Seerr page at a time, without
    the ones you own (the grid already has those). Sorted like the grid where TMDB can (Newest, Top rated); A-Z
    stays by popularity (TMDB's A-Z list is mostly symbols). Cached for a few hours; empty offline."""
    cat = _catalog()
    if not seerr_online(app):
        return {"items": [], "more": False, "online": False}
    movies = kind == "movies"
    path = "/discover/movies" if movies else "/discover/tv"
    today = dt.date.today().isoformat()
    if sort == "new":
        params = ({"sortBy": "primary_release_date.desc", "primaryReleaseDateLte": today} if movies
                  else {"sortBy": "first_air_date.desc", "firstAirDateLte": today})
        params["voteCountGte"] = "30"
    elif sort == "rating":
        params = {"sortBy": "vote_average.desc", "voteCountGte": "1500" if movies else "400"}
    else:
        params = {}
    if kind == "anime":
        params.update(genre="16", language="ja")
    elif genre:
        ids = _genre_ids([genre], "movie" if kind == "movies" else "tv")
        if ids:
            params["genre"] = ",".join(ids)
    key = f"homemix:disc:{path}:{json.dumps(params, sort_keys=True)}:{int(page)}"
    data, age = _kv(cat, key)
    if data is None or age > BROWSE_AGE:
        try:
            res = app.seerr.call("GET", path, params={**params, "page": int(page)}, timeout=(3, 10)) or {}
            data = {"items": [c for c in map(app.seerr.card, res.get("results") or []) if c],
                    "pages": res.get("totalPages") or 1}
            cat.kv_set(key, data)
        except Exception as e:
            print("discover page:", type(e).__name__, e)
            if data is None:
                return {"items": [], "more": False, "online": False}
    ctx = Ctx(app, cat)
    items = []
    for c in data.get("items") or []:
        if f"{c['type']}:{c['id']}" in ctx.owned:
            continue
        card = ctx.req_card(c, allow_requested=True)
        if card and is_req(card):
            items.append(card)
    return {"items": items, "more": int(page) < min(int(data.get("pages") or 1), 50), "online": True}


# ------------------------------------------------------------------ warming the cache (background)
def _queue(app, needs):
    """Fetch what assemble found missing, in the background. True while there's something to wait for."""
    if not seerr_ready(app):
        return False
    now = time.time()
    with _lock:
        fresh = {n for n in needs if now - _warm["tried"].get(n, 0) > 1800}
        _warm["needs"] |= fresh
        busy = _warm["thread"] is not None and _warm["thread"].is_alive()
        if (fresh or time.time() - _seerr["at"] > 120) and not busy:
            _start(app)
            busy = True
    return busy and _seerr["ok"] is not False


def _kick(app):
    with _lock:
        if not (_warm["thread"] and _warm["thread"].is_alive()):
            _start(app)


def _start(app):
    t = threading.Thread(target=_warm_run, args=(app,), daemon=True, name="home-warm")
    _warm["thread"] = t
    t.start()


def warming():
    t = _warm["thread"]
    return bool(t and t.is_alive())


def _do(app, task):
    """One fetch into the cache. Returns follow-up tasks."""
    from . import details
    cat = _catalog()
    kind = task[0]
    if kind == "recs":
        details.recommended(task[1], task[2])
    elif kind == "detail":
        d = details.tmdb_detail("movie", task[1]) or {}
        col = d.get("collection")
        if col and _kv(cat, f"collection:{int(col['id'])}")[1] is None:
            return [("col", str(col["id"]))]
    elif kind == "col":
        if str(task[1]).isdigit():
            details.collection(int(task[1]))
    elif kind == "pjf":
        info = details.person_jf(task[1])
        if info.get("tmdb") and _kv(cat, f"homemix:credits:{info['tmdb']}")[1] is None:
            return [("credits", str(info["tmdb"]))]
    elif kind == "credits":
        cr = app.seerr.call("GET", f"/person/{int(task[1])}/combined_credits", timeout=(3, 10)) or {}

        def cards(lst, director=False):
            seen, out = set(), []
            for x in sorted(lst or [], key=lambda x: -(x.get("voteCount") or 0)):
                if director and x.get("job") != "Director":
                    continue
                if x.get("mediaType") == "tv" and not director and (x.get("episodeCount") or 0) < 5:
                    continue  # a cameo or a talk-show visit isn't "starring"
                if x.get("mediaType") not in ("movie", "tv") or x["id"] in seen or (x.get("genreIds") or []) in ([10767], [10763]):
                    continue
                seen.add(x["id"])
                c = app.seerr.card(x)
                if c:
                    c["votes"] = x.get("voteCount") or 0
                    out.append(c)
            return out[:30]
        cat.kv_set(f"homemix:credits:{task[1]}", {"cast": cards(cr.get("cast")), "crew": cards(cr.get("crew"), True)})
    elif kind == "browse":
        path, params = task[1], dict(task[2])
        items = app.seerr.cards(path, **params)
        if path in ("/discover/movies", "/discover/tv") and not params.get("sortBy"):
            try:
                items += app.seerr.cards(path, page=2, **params)
            except Exception:
                pass
        key = "homemix:browse:" + path + ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else "")
        cat.kv_set(key, items)
    return []


def _warm_run(app):
    try:
        if not _ping(app):
            return
        for _ in range(4):  # follow-ups: a title's details name its collection, a person's id leads to credits
            with _lock:
                todo = list(_warm["needs"])[:160]
                _warm["needs"] -= set(todo)
                now = time.time()
                for t in todo:
                    _warm["tried"][t] = now
            if not todo:
                break
            follow = []
            with ThreadPoolExecutor(max_workers=8, thread_name_prefix="home-warm") as pool:
                for res in pool.map(lambda t: _safe(app, t), todo):
                    follow += res
            with _lock:
                _warm["needs"] |= {f for f in follow if f not in _warm["tried"]}
                _warm["added"] += len(todo)
            invalidate()
    finally:
        _warm["done_at"] = time.time()


def _safe(app, task):
    try:
        return _do(app, task) or []
    except Exception as e:
        if "Seerr" not in str(e):
            print("home warm:", task[0], type(e).__name__, e)
        return []
