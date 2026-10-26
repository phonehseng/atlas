"""Classic mode: everything the AI does, without any AI. Prepared lines (marquee.scripted_lines) and rules over
the local catalog, so it works offline, costs nothing and never contacts a language model.

    welcome(app)                    -> str            the line at the top of Home (same as ai.welcome)
    intro(app, item_id, because)    -> {"text"}       a short "why watch" blurb for a title
    picks(app)                      -> {"items"}      library picks with a reason each (same as ai.picks)
    curate(app, pool)               -> [card+why]     the request row from Seerr candidates (same as ai.curate)
    rows(app)                       -> [row]          extra Home rows ("Because you watched X", "Short tonight", ...)
    search(app, q)                  -> {"answer", "items", "requestable"}   (same as ai.search)
    ending_comment(app, item, picks)-> (comment, {n: why})   (same as the assistant's ending _comment)
    ending(app, item_id)            -> the Up-next payload {item_id, state, thinking, comment, picks, credits_at}

Lines are chosen with a seeded random generator (so a page reload shows the same line in the same hour) and a
small history in the settings folder keeps a welcome line from coming back within two weeks."""
import datetime as dt
import hashlib
import json
import random
import re
import threading
import time

from . import ai, settings
from . import scripted_lines as L
from . import taste as taste_profile

HISTORY = settings.DIR / "classic_history.json"
NO_REPEAT_DAYS = 14
MAX_LEN = 120
_hist_lock = threading.Lock()

# Genre names as Jellyfin/TMDB write them -> one canonical name (TV uses combined names)
SPLIT = {"Action & Adventure": ["Action", "Adventure"], "Sci-Fi & Fantasy": ["Science Fiction", "Fantasy"],
         "War & Politics": ["War"], "Kids": ["Family"], "Soap": ["Drama"], "Sci-Fi": ["Science Fiction"],
         "Suspense": ["Thriller"], "Musical": ["Music"]}
KEY = {"Horror": "horror", "Comedy": "comedy", "Science Fiction": "scifi", "Fantasy": "fantasy", "Action": "action",
       "Adventure": "adventure", "Drama": "drama", "Thriller": "thriller", "Mystery": "mystery", "Crime": "crime",
       "Romance": "romance", "Animation": "animation", "Family": "family", "Documentary": "documentary", "War": "war",
       "Western": "western", "Music": "music", "History": "history"}
WORD = {"horror": "horror", "comedy": "comedy", "scifi": "sci-fi", "fantasy": "fantasy", "action": "action",
        "adventure": "adventure", "drama": "drama", "thriller": "thrillers", "mystery": "mystery", "crime": "crime",
        "romance": "romance", "animation": "animation", "family": "family films", "documentary": "documentaries",
        "war": "war films", "western": "westerns", "music": "music", "history": "history", "anime": "anime"}
# Genres that matter more for a match than the broad ones every second title has
BROAD = {"Drama", "Adventure", "Action", "Comedy", "Animation"}
SEASONAL = {10: ("halloween", {"Horror", "Thriller", "Mystery"}),
            12: ("christmas", {"Family", "Animation", "Comedy"}),
            2: ("valentine", {"Romance"}),
            6: ("summer", {"Action", "Adventure", "Science Fiction"}),
            7: ("summer", {"Action", "Adventure", "Science Fiction"}),
            8: ("summer", {"Action", "Adventure", "Science Fiction"})}


# ------------------------------------------------------------------ small helpers
def norm_genres(genres):
    out = []
    for g in genres or []:
        for x in SPLIT.get(g, [g]):
            if x not in out:
                out.append(x)
    return out


def genre_key(genres, anime=False):
    if anime:
        return "anime"
    gs = norm_genres(genres)
    specific = [g for g in gs if g in KEY and g not in BROAD]
    pick = (specific or [g for g in gs if g in KEY] or [None])[0]
    return KEY.get(pick)


def _cat(app):
    c = getattr(app, "catalog", None)
    if c is None:
        from . import server
        c = server.catalog()
    return c


def _rng(*parts):
    return random.Random(hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest())


def _fill(template, ctx):
    """The line with its placeholders filled, or None when one of them isn't known."""
    for f in re.findall(r"\{(\w+)\}", template):
        if ctx.get(f) in (None, "", 0):
            return None
    try:
        text = template.format(**ctx)
    except (KeyError, IndexError, ValueError):
        return None
    text = text.replace("….", "…").replace("…,", "…")
    return text[0].upper() + text[1:] if text else text


def _hid(template):
    return hashlib.sha1(template.encode()).hexdigest()[:12]


def _history():
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_history(h):
    try:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        tmp = HISTORY.with_suffix(".tmp")
        tmp.write_text(json.dumps(h), encoding="utf-8")
        tmp.replace(HISTORY)
    except OSError as e:
        print("classic history not saved:", e)


def _first_name():
    n = (settings.load().get("user_name") or "").strip()
    if not n or "@" in n:
        return None
    return n.split()[0][:20]


def part_of_day(now):
    h = now.hour
    return "night" if h < 5 else "morning" if h < 12 else "afternoon" if h < 17 else "evening" if h < 22 else "night"


def season_of(now):
    return ["winter", "winter", "spring", "spring", "spring", "summer", "summer", "summer", "autumn", "autumn",
            "autumn", "winter"][now.month - 1]


def _days(n):
    return "today" if n == 0 else "tomorrow" if n == 1 else f"in {n} days"


def _title_short(name, limit=44):
    """A title that fits in a line: the part before a subtitle colon if that's enough, else cut at a word."""
    name = (name or "").strip()
    if len(name) <= limit:
        return name
    head = name.split(":")[0].strip()
    if 8 <= len(head) <= limit:
        return head
    return name[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:-&") + "…"


def _sentence(text):
    return text if not text or text[-1] in ".!?…" else text + "."


def _played_at(cat, rid):
    return (cat.ud.get(rid) or {}).get("last_played") or 0


# ------------------------------------------------------------------ welcome
def _welcome_context(app, now):
    cat = _cat(app)
    ctx = {"name": _first_name(), "part": part_of_day(now), "weekday": f"{now:%A}", "season": season_of(now)}
    hol = ai.upcoming_holiday(now)
    if hol:
        ctx["holiday"], ctx["days"], ctx["_hdays"] = hol[0], _days(hol[1]), hol[1]
        ctx["daysn"] = None if hol[1] == 0 else "1 day" if hol[1] == 1 else f"{hol[1]} days"
    with cat.lock:
        nxt = cat.next_up(5)
        if nxt:
            e = nxt[0]
            ctx["show"] = _title_short(e.get("series_name"))
            if e.get("season_no") is not None and e.get("episode_no") is not None:
                ctx["se"] = f"S{e['season_no']}E{e['episode_no']}"
            if e.get("name") and not re.fullmatch(r"(episode|ep\.?)\s*\d+", e["name"], re.I):
                ctx["ep"] = _title_short(e["name"], 36)
        for r in cat.resume(5):
            if r["type"] == "Movie" and r.get("runtime"):
                pos = (cat.ud.get(r["id"]) or {}).get("position") or 0
                ctx["movie"], ctx["pct"] = _title_short(r.get("name")), f"{max(1, int(pos / r['runtime'] * 100))}%"
                break
        week = time.time() - 7 * 86400
        fresh = time.time() - 4 * 86400
        new_eps = {}
        for e in cat._of("Episode"):
            if (e.get("added") or 0) > fresh and not (cat.ud.get(e["id"]) or {}).get("played"):
                new_eps.setdefault(e.get("series_name"), []).append(e)
        if new_eps:
            show, eps = max(new_eps.items(), key=lambda kv: len(kv[1]))
            ctx["newshow"], ctx["_neweps"] = _title_short(show), len(eps)
            srow = cat.get(eps[0].get("series_id")) if eps[0].get("series_id") else None
            # the whole show is new (not new episodes of a show you had): say so
            ctx["_newshow_whole"] = bool(srow and (srow.get("added") or 0) > fresh
                                         and len(eps) >= len(cat.eps.get(srow["id"], [])) * 0.8)
        movies = sorted([r for r in cat._of("Movie") if (r.get("added") or 0) > week and not cat._played(r)],
                        key=lambda r: -(r.get("added") or 0))
        if movies:
            ctx["newmovie"] = _title_short(movies[0].get("name"))
        new_titles = len(movies) + len([s for s in cat._of("Series") if (s.get("added") or 0) > week]) + len(new_eps)
        ctx["_newmany"] = new_titles
        recent = cat.recently_watched(8)
        if recent:
            ctx["recent"] = _title_short(recent[0].get("name"))
        stamps = [u.get("last_played") for u in cat.ud.values() if u.get("last_played")]
        days = {dt.date.fromtimestamp(t) for t in stamps}
        today = now.date()
        streak, d = 0, today if today in days else today - dt.timedelta(days=1)
        while d in days:
            streak += 1
            d -= dt.timedelta(days=1)
        ctx["_streak"] = streak
        ctx["_last"] = max(stamps) if stamps else 0
        today_eps = [e for e in cat._of("Episode") if (cat.ud.get(e["id"]) or {}).get("played")
                     and dt.date.fromtimestamp(_played_at(cat, e["id"]) or 0) == today]
        if today_eps:
            by_show = {}
            for e in today_eps:
                by_show.setdefault(e.get("series_name"), []).append(e)
            show, eps = max(by_show.items(), key=lambda kv: len(kv[1]))
            ctx["_binge"], ctx["_binge_show"] = len(eps), _title_short(show)
        pool = [r for r in cat._of("Movie,Series") if not cat._played(r) and (r.get("rating") or 0) >= 7.5]
        if pool:
            pool.sort(key=lambda r: -(r.get("rating") or 0))
            ctx["pick"] = _title_short(_rng(now.date(), "pick").choice(pool[:12]).get("name"))
        weights = {}
        for k, r in enumerate(recent):
            for g in norm_genres(r.get("genres")):
                if g in KEY:
                    weights[g] = weights.get(g, 0) + 0.8 ** k * (0.5 if g in BROAD else 1)
        if weights:
            ctx["genre"] = WORD[KEY[max(weights, key=weights.get)]]
        anime = [r for r in recent[:4] if r.get("library") == "anime"]
        ctx["_anime"] = len(anime) >= 2
        ctx["_played_any"] = bool(stamps)
    return ctx


HOLIDAY_KEYS = {"Halloween": "halloween", "Christmas": "christmas", "New Year's Eve": "newyear",
                "New Year's Day": "newyear", "Valentine's Day": "valentine", "Thanksgiving": "thanksgiving"}


def _welcome_categories(ctx, now):
    """[(weight, pool name, extra placeholders)] for what's true right now."""
    cats = []
    late = ctx["part"] == "night"
    if not ctx["_played_any"]:
        cats.append((6, "first_time", {}))
    elif ctx["_last"] and time.time() - ctx["_last"] > 7 * 86400:
        cats.append((5, "back", {}))
    if ctx.get("holiday"):
        key = HOLIDAY_KEYS.get(ctx["holiday"], "holiday")
        if ctx["_hdays"] == 0:
            cats.append((7, key + "_today" if key + "_today" in L.WELCOME else "holiday_today", {}))
        else:
            cats.append((3.5 if ctx["_hdays"] <= 7 else 2, key if key in L.WELCOME else "holiday", {}))
    if ctx.get("show") and ctx.get("se"):
        cats.append((4, "next_up_late" if late else "next_up", {}))
    if ctx.get("movie"):
        cats.append((2.5, "resume", {}))
    if ctx.get("newshow"):
        n = ctx["_neweps"]
        name = "new_show" if ctx.get("_newshow_whole") and n > 1 else "new_episodes" if n > 1 else "new_episode"
        cats.append((3.5, name, {"count": n if n > 1 else None}))
    if ctx.get("newmovie"):
        cats.append((2.5, "new_movie", {}))
    if ctx["_newmany"] >= 3:
        cats.append((1.2, "new_many", {"count": ctx["_newmany"]}))
    if ctx["_streak"] >= 3:
        cats.append((1.5, "streak", {"streak": ctx["_streak"]}))
    if ctx.get("_binge", 0) >= 3:
        cats.append((2, "binge", {"binge": ctx["_binge"], "show": ctx["_binge_show"]}))
    cats.append((2, ctx["part"], {}))
    wd = now.weekday()
    if wd >= 5 or (wd == 4 and now.hour >= 17):
        cats.append((1.6, "friday" if wd == 4 else "weekend", {}))
        if wd == 6:
            cats.append((0.8, "sunday", {}))
    elif wd == 0:
        cats.append((1.2, "monday", {}))
    elif wd in (1, 2, 3):
        cats.append((0.6, "midweek", {}))
    cats.append((1, ctx["season"], {}))
    if ctx.get("recent"):
        cats.append((1, "recent", {}))
    if ctx.get("pick"):
        cats.append((2, "pick", {}))
    if ctx.get("genre"):
        cats.append((1, "genre", {}))
    if ctx["_anime"]:
        cats.append((0.8, "anime", {}))
    cats.append((1.2, "generic", {}))
    return cats


def welcome(app, now=None, remember=True):
    """One greeting line. Same answer for the same hour (unless what it talks about changed); a line is not
    reused within NO_REPEAT_DAYS."""
    now = now or dt.datetime.now()
    try:
        ctx = _welcome_context(app, now)
    except Exception as e:
        print("classic welcome context failed:", e)
        ctx = {"name": _first_name(), "part": part_of_day(now), "weekday": f"{now:%A}", "season": season_of(now),
               "_streak": 0, "_last": 0, "_newmany": 0, "_anime": False, "_played_any": True}
    slot = f"{now:%Y-%m-%d %H}"
    with _hist_lock:
        hist = _history()
        used = {k: v for k, v in (hist.get("used") or {}).items() if time.time() - v < NO_REPEAT_DAYS * 86400}
        last = hist.get("last") or {}
        fingerprint = hashlib.sha1(json.dumps({k: v for k, v in ctx.items() if not k.startswith("_")},
                                              sort_keys=True, default=str).encode()).hexdigest()[:10]
        if remember and last.get("slot") == slot and last.get("fp") == fingerprint and last.get("text"):
            return last["text"]
        rng = _rng(slot, fingerprint, len(used))
        recent_cats = (hist.get("cats") or [])[-3:]  # the last few kinds of line get a rest
        cats = [(w * (0.3 if n in recent_cats else 1), n, x) for w, n, x in _welcome_categories(ctx, now)]
        text, tid, chosen = None, None, None
        for _ in range(len(cats)):
            total = sum(w for w, _, _ in cats)
            x, acc = rng.random() * total, 0
            for k, (w, name, extra) in enumerate(cats):
                acc += w
                if x <= acc:
                    break
            _, name, extra = cats.pop(k)
            local = {**ctx, **extra}
            lines = list(L.WELCOME.get(name) or [])
            rng.shuffle(lines)
            fits = [(t, _fill(t, local)) for t in lines]
            fits = [(t, s) for t, s in fits if s and len(s) <= MAX_LEN]
            fresh = [(t, s) for t, s in fits if _hid(t) not in used]
            if fresh:
                tid, text, chosen = fresh[0][0], fresh[0][1], name
                break
            if fits and not text:
                (tid, text), chosen = fits[0], name  # all used lately: keep looking, fall back to this
        if not text:
            text = "Welcome back. What are we watching?"
        if remember:
            if tid:
                used[_hid(tid)] = time.time()
            hist["used"] = used
            hist["cats"] = ((hist.get("cats") or []) + [chosen])[-6:]
            hist["last"] = {"slot": slot, "fp": fingerprint, "text": text}
            _save_history(hist)
    return text


# ------------------------------------------------------------------ taste and scoring
def _people(r, kind=None):
    ppl = (r.get("extra") or {}).get("people") or []
    return [p for p in ppl if p.get("name") and (kind is None or p.get("type") == kind)]


def _collection(r):
    return (r.get("provider") or {}).get("TmdbCollection")


def collection_name(cat, col):
    """A readable name for a film collection from its members' titles: "Harry Potter", "Dune"."""
    names = [r.get("name") or "" for r in cat._of("Movie") if _collection(r) == col]
    if not names:
        return None
    words = [re.split(r"\s+", re.sub(r"[:\-–]", " ", n).strip()) for n in names]
    common = []
    for parts in zip(*words):
        if len(set(p.lower() for p in parts)) == 1:
            common.append(parts[0])
        else:
            break
    while common and common[-1].lower() in ("and", "the", "of", "part", "a", "chapter"):
        common.pop()
    return " ".join(common) if common else None


def _is_anime(r):
    return r.get("library") == "anime" or "\\anime" in (r.get("path") or "").lower()


class Taste:
    """What the viewer watched lately, weighted by recency: genres, cast, directors, studios, collections."""

    def __init__(self, cat, now=None):
        self.cat = cat
        self.now = now or dt.datetime.now()
        self.recent = cat.recently_watched(15)
        self.w = {r["id"]: 0.85 ** k for k, r in enumerate(self.recent)}
        self.genres, self.cast, self.directors, self.studios, self.similar, self.cols = {}, {}, {}, {}, {}, {}
        years = []
        for r in self.recent:
            w = self.w[r["id"]]
            for g in norm_genres(r.get("genres")):
                self.genres[g] = self.genres.get(g, 0) + w * (0.6 if g in BROAD else 1)
            for p in _people(r, "Actor")[:6]:
                self._add(self.cast, p["name"], w, r)
            for p in _people(r, "Director"):
                self._add(self.directors, p["name"], w, r)
            for s in ((r.get("extra") or {}).get("studios") or [])[:3]:
                self._add(self.studios, s, w, r)
            for i in (r.get("extra") or {}).get("similar") or []:
                self._add(self.similar, i, w, r)
            if _collection(r):
                self.cols.setdefault(_collection(r), []).append(r)
            if r.get("year"):
                years.append(r["year"])
        self.year = sum(years) / len(years) if years else None
        self.anime = sum(1 for r in self.recent[:6] if _is_anime(r)) / max(1, min(6, len(self.recent)))
        self.jf_recs = {}
        for rec in cat.meta.get("recommendations") or []:
            for i in rec.get("ids") or []:
                self.jf_recs.setdefault(i, (rec.get("type"), rec.get("base")))
        top = sum(self.genres.values()) or 1
        self.genres = {g: v / top for g, v in self.genres.items()}
        self.season = SEASONAL.get(self.now.month)

    @staticmethod
    def _add(d, key, w, r):
        cur = d.get(key)
        if not cur or w > cur[0]:
            d[key] = (w if not cur else cur[0] + w * 0.3, r)
        else:
            d[key] = (cur[0] + w * 0.3, cur[1])

    def next_in_collection(self, r):
        """Is r the next unwatched film after the latest one watched in its collection?"""
        col = _collection(r)
        if not col or col not in self.cols:
            return False
        parts = sorted([m for m in self.cat._of("Movie") if _collection(m) == col], key=lambda m: m.get("year") or 0)
        watched = [k for k, m in enumerate(parts) if self.cat._played(m)]
        if not watched:
            return False
        for m in parts[max(watched) + 1:]:
            if not self.cat._played(m):
                return m["id"] == r["id"]
        return False

    def score(self, r, rng=None):
        """(score, reason) for one library title."""
        parts = []  # (points, reason)
        name = lambda x: _title_short(x.get("name"))
        col = _collection(r)
        if self.next_in_collection(r):
            parts.append((7, L.WHY["sequel"].format(col=collection_name(self.cat, col) or "the")))
        elif col in self.cols:
            parts.append((2, L.WHY["sequel"].format(col=collection_name(self.cat, col) or "the")))
        sim = self.similar.get(r["id"])
        if sim:
            parts.append((2.5 * sim[0] + 0.5, L.WHY["similar"].format(title=name(sim[1]))))
        rec = self.jf_recs.get(r["id"])
        if rec and rec[1]:
            kind, base = rec
            if kind == "HasDirectorFromRecentlyPlayed":
                parts.append((2.5, L.WHY["director_name"].format(director=base)))
            elif kind == "HasActorFromRecentlyPlayed":
                parts.append((1.5, f"With {base}"))
            else:
                parts.append((1.5, L.WHY["similar"].format(title=_title_short(base))))
        for p in _people(r, "Director"):
            d = self.directors.get(p["name"])
            if d:
                parts.append((3 * d[0] + 1, L.WHY["director"].format(title=name(d[1]))))
        shared = [(self.cast[p["name"]], p["name"]) for p in _people(r, "Actor")[:8] if p["name"] in self.cast]
        if shared:
            (w, src), actor = max(shared, key=lambda x: x[0][0])
            if src["id"] != r["id"]:
                parts.append((min(2.4, 1.2 * sum(x[0][0] for x in shared)) + 0.3,
                              L.WHY["cast"].format(actor=actor, title=name(src))))
        for s in ((r.get("extra") or {}).get("studios") or [])[:3]:
            st = self.studios.get(s)
            if st and st[1]["id"] != r["id"]:
                parts.append((0.6 * st[0], L.WHY["studio"].format(studio=s, title=name(st[1]))))
                break
        gs = norm_genres(r.get("genres"))
        overlap = [(self.genres.get(g, 0), g) for g in gs if g in self.genres]
        if overlap:
            pts = 3 * sum(v for v, _ in overlap)
            best_g = max(overlap)[1]
            src = next((x for x in self.recent if best_g in norm_genres(x.get("genres")) and x["id"] != r["id"]), None)
            word = WORD.get(KEY.get(best_g), best_g.lower())
            parts.append((pts, L.WHY["genre"].format(genre=word, title=name(src)) if src else
                          L.WHY["genre_plain"].format(genre=word)))
        rating = r.get("rating") or 0
        if rating:
            parts.append(((rating - 6.5) * 0.5, L.WHY["rating"].format(rating=f"{rating:.1f}") if rating >= 7.8 else None))
        if self.year and r.get("year") and abs(r["year"] - self.year) <= 5:
            parts.append((0.3, None))
        if _is_anime(r):
            parts.append((1.2 if self.anime >= 0.3 else -1.0, L.WHY["anime"] if self.anime >= 0.3 else None))
        elif self.anime > 0.8:
            parts.append((-0.4, None))
        if self.season and set(gs) & self.season[1]:
            parts.append((1.3, L.WHY["seasonal_" + self.season[0]]))
        if not self.cat._played(r):
            parts.append((2, None))
        if rng:
            parts.append((rng.random() * 0.4, None))
        total = sum(p for p, _ in parts)
        reasons = [(p, why) for p, why in parts if why and p > 0]
        why = max(reasons, key=lambda x: x[0])[1] if reasons else "Picked for you"
        return total, why


def _candidates(cat, in_progress_ok=False):
    """Films and shows worth suggesting: not fully watched, and not already on Continue / Next up."""
    busy = set()
    if not in_progress_ok:
        busy |= {r["id"] for r in cat.resume(100)}
        busy |= {e.get("series_id") for e in cat.next_up(100)}
        busy |= {r.get("series_id") for r in cat.resume(100)}
    return [r for r in cat._of("Movie,Series") if not cat._played(r) and r["id"] not in busy]


def ranked(app, limit=12, now=None):
    """[(score, row, why)] best first."""
    cat = _cat(app)
    now = now or dt.datetime.now()
    rng = _rng(now.date(), "ranked")
    with cat.lock:
        taste = Taste(cat, now)
        out = [(*taste.score(r, rng), r) for r in _candidates(cat)]
        if not taste_profile.is_empty():
            out = [(s + (taste_profile.score({**cat.card(r), "people": (r.get("extra") or {}).get("people") or []})["score"] - 50) / 20, why, r)
                   for s, why, r in out]
    out.sort(key=lambda x: -x[0])
    picked, per_col = [], {}
    for s, why, r in out:
        col = _collection(r)
        if col:
            per_col[col] = per_col.get(col, 0) + 1
            if per_col[col] > 2:
                continue
        picked.append((s, r, why))
        if len(picked) >= limit:
            break
    return picked


def picks(app):
    """Same shape as ai.picks(): {"items": [card + "why"]} (6 titles)."""
    try:
        cat = _cat(app)
        with cat.lock:
            return {"items": [{**cat.card(r), "why": why} for _, r, why in ranked(app, 6)]}
    except Exception as e:
        print("classic picks failed:", e)
        return {"items": []}


def curate(app, pool):
    """Same shape as ai.curate(): up to 8 Seerr cards ({..., "because"}) with a rule-made "why"."""
    try:
        cat = _cat(app)
        now = dt.datetime.now()
        with cat.lock:
            taste = Taste(cat, now)
            weights = {r.get("name"): taste.w[r["id"]] for r in taste.recent}
            anime_taste = taste.anime
        scored, seen = [], set()
        for c in pool:
            k = (c.get("type"), c.get("id"))
            if k in seen or c.get("status") == 5:
                continue
            seen.add(k)
            because = c.get("because") or ""
            s, why = 0.0, None
            m = re.match(r"the (.+)", because)
            if because.startswith("people who liked "):
                src = because[len("people who liked "):]
                s += 3 * weights.get(src, 0.3) + 1
                why = L.WHY["liked_by_fans"].format(title=_title_short(src))
            elif m:
                s += 4
                why = L.WHY["sequel_request"].format(col=m.group(1))
                try:  # the earliest missing part of a collection first
                    s += max(0, 2030 - int(c.get("year") or 2030)) * 0.02
                except ValueError:
                    pass
            else:
                s += 0.8
                why = L.WHY["trending"]
            s += ((c.get("rating") or 6.5) - 6.5) * 0.4
            if c.get("anime"):
                s += 0.8 if anime_taste >= 0.3 else -0.8
            if taste.year and str(c.get("year") or "").isdigit() and abs(int(c["year"]) - taste.year) <= 6:
                s += 0.2
            s += _rng(now.date(), c.get("id")).random() * 0.3
            scored.append((s, {**c, "why": why}))
        scored = [(s + (taste_profile.score(c)["score"] - 50) / 25, c) for s, c in scored]
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:8]]
    except Exception as e:
        print("classic curate failed:", e)
        return []


# ------------------------------------------------------------------ extra Home rows
def rows(app, now=None, min_items=3):
    """Rule-made rows for Home: [{"key", "title", "items", "style"}] (only rows with enough titles)."""
    cat = _cat(app)
    now = now or dt.datetime.now()
    out = []
    rng = _rng(now.date(), "rows")

    def add(key, title, items):
        items = [r for r in items if r]
        if len(items) >= min_items:
            out.append({"key": "classic-" + key, "title": title, "items": cat.cards(items[:24]), "style": "poster",
                        "classic": True})

    with cat.lock:
        taste = Taste(cat, now)
        cands = _candidates(cat)
        recent = [r for r in taste.recent if r["type"] in ("Movie", "Series")]
        if recent:
            base = recent[0]
            one = Taste.__new__(Taste)
            one.__dict__.update(taste.__dict__)
            one.recent, one.w = [base], {base["id"]: 1.0}
            sims = {i for i in (base.get("extra") or {}).get("similar") or []}
            local = [r["id"] for r in cat.similar_local(base, 24)]
            pool = [r for r in cands if r["id"] in sims or r["id"] in local]
            scored = sorted(pool, key=lambda r: -((r["id"] in sims) * 2 + local.index(r["id"]) * -0.1
                                                  if r["id"] in local else (r["id"] in sims) * 2))
            add("because", L.ROWS["because"].format(title=_title_short(base.get("name"), 40)), scored)
        short = [r for r in cat._of("Movie") if 20 * 60 <= (r.get("runtime") or 0) <= 100 * 60]
        short.sort(key=lambda r: (cat._played(r), -(r.get("rating") or 0)))
        add("short", L.ROWS["short"], short)
        top = [r for r in cat._of("Movie,Series") if not cat._played(r) and (r.get("rating") or 0) >= 7.5]
        top.sort(key=lambda r: -(r.get("rating") or 0))
        add("top-unseen", L.ROWS["top_unseen"], top)
        finish = []
        for s in cat._of("Series"):
            eps = [e for e in cat.eps.get(s["id"], []) if (e.get("season_no") or 0) > 0]
            done = sum(1 for e in eps if (cat.ud.get(e["id"]) or {}).get("played"))
            if eps and 0 < done < len(eps):
                finish.append((cat._series_last(s["id"]), s))
        for col in taste.cols:
            nxt = next((m for m in cat._of("Movie") if _collection(m) == col and taste.next_in_collection(m)), None)
            if nxt:
                finish.append((max(_played_at(cat, m["id"]) for m in taste.cols[col]), nxt))
        finish.sort(key=lambda x: -x[0])
        add("finish", L.ROWS["finish"], [r for _, r in finish])
        if taste.season:
            key, genres = taste.season
            hits = [r for r in cat._of("Movie,Series") if set(norm_genres(r.get("genres"))) & genres]
            if key == "christmas":
                hits += [r for r in cat._of("Movie,Series") if re.search(r"christmas|holiday|santa|xmas",
                                                                         (r.get("name") or "") + " " + (r.get("overview") or ""), re.I)]
            hits = list({r["id"]: r for r in hits}.values())
            hits.sort(key=lambda r: (cat._played(r), -(r.get("rating") or 0) - rng.random()))
            add("seasonal", L.ROWS[key], hits)
    return out


# ------------------------------------------------------------------ per-title blurb
def intro(app, item_id, because=None, now=None):
    """{"text": "..."}: one or two short fragments on why to watch this title."""
    cat = _cat(app)
    now = now or dt.datetime.now()
    with cat.lock:
        r = cat.get(item_id)
        if not r:
            return {"text": None}
        if r["type"] in ("Episode", "Season") and r.get("series_id"):
            r = cat.get(r["series_id"]) or r
        rng = _rng(r["id"], now.date())
        taste = Taste(cat, now)
        first = None
        if because:
            first = rng.choice(L.INTRO["because"]).format(because=_title_short(because))
        elif taste.next_in_collection(r):
            prev = max(taste.cols[_collection(r)], key=lambda m: m.get("year") or 0)
            first = rng.choice(L.INTRO["sequel"]).format(because=_title_short(prev.get("name")))
        else:
            _, why = taste.score(r)
            if why and why != "Picked for you" and not why.startswith("Rated"):
                first = _sentence(why)
        facts = []
        gk = genre_key(r.get("genres"), _is_anime(r))
        if gk and gk in L.INTRO["genre"]:
            facts.append(rng.choice(L.INTRO["genre"][gk]))
        mins = (r.get("runtime") or 0) / 60
        if r["type"] == "Movie" and 20 <= mins <= 95:
            facts.append(rng.choice(L.INTRO["runtime_short"]).format(mins=int(mins)))
        elif r["type"] == "Movie" and mins >= 150:
            facts.append(rng.choice(L.INTRO["runtime_long"]).format(hours=f"{int(mins // 60)}h {int(mins % 60)}m"))
        rating = r.get("rating") or 0
        if rating >= 8:
            facts.append(rng.choice(L.INTRO["rating_high"]).format(rating=f"{rating:.1f}"))
        elif rating >= 7:
            facts.append(rng.choice(L.INTRO["rating_good"]).format(rating=f"{rating:.1f}"))
        y = r.get("year") or 0
        if y >= now.year - 1:
            facts.append(rng.choice(L.INTRO["year_new"]).format(year=y))
        elif 0 < y < 1990:
            facts.append(rng.choice(L.INTRO["year_classic"]).format(year=y))
        elif 1990 <= y < 2000:
            facts.append(rng.choice(L.INTRO["year_90s"]).format(year=y))
        actors = _people(r, "Actor")
        if actors and actors[0]["name"] not in (first or ""):
            facts.append(rng.choice(L.INTRO["cast"]).format(actor=actors[0]["name"]))
        if r["type"] == "Series":
            n = len([s for s in cat.seasons.get(r["id"], []) if (s.get("season_no") or 0) > 0])
            if n > 1:
                facts.append(rng.choice(L.INTRO["seasons"]).format(seasons=n))
            elif n == 1:
                facts.append(rng.choice(L.INTRO["season_one"]))
        u = cat.ud.get(r["id"]) or {}
        if r["type"] == "Movie" and u.get("position") and r.get("runtime") and not u.get("played"):
            facts.insert(0, rng.choice(L.INTRO["progress"]).format(pct=f"{int(u['position'] / r['runtime'] * 100)}%"))
        elif cat._played(r):
            facts.append(rng.choice(L.INTRO["rewatch"]))
    rng.shuffle(facts)
    if facts and facts[0].startswith(("You're", "Already")) is False:
        facts.sort(key=lambda f: 0 if f.startswith(("You're", "Already")) else 1)
    text = first or ""
    for f in facts:
        cand = (text + " " + f).strip()
        if len(cand) <= 110:
            text = cand
        if text.count(".") + text.count("!") + text.count("?") >= 2:
            break
    return {"text": text.replace("….", "…") or None}


# ------------------------------------------------------------------ search (no AI)
def search(app, q):
    """Same shape as ai.search(): the library re-sorted by rules for a request in words, plus one prepared line."""
    try:
        intent = ai.read_query(q)
        cat = _cat(app)
        want = set(norm_genres(intent["genres"]))
        types = {"movie": "Movie", "tv": "Series"}.get(intent["kind"], "Movie,Series")
        with cat.lock:
            taste = Taste(cat)
            scored = []
            for r in cat._of(types):
                gs = set(norm_genres(r.get("genres")))
                hit = len(gs & want)
                if want and not hit:
                    continue
                if intent.get("anime") and not _is_anime(r):
                    continue
                y = r.get("year") or 0
                if intent.get("years") and not (intent["years"][0] <= y <= intent["years"][1]):
                    continue
                mins = (r.get("runtime") or 0) / 60
                if intent.get("max_minutes") and (r["type"] != "Movie" or not mins or mins > intent["max_minutes"]):
                    continue
                played = cat._played(r)
                if intent.get("unwatched") and played:
                    continue
                s = hit * 2 + (r.get("rating") or 6) / 4 + (0 if played else 1)
                s += sum(taste.genres.get(g, 0) for g in gs) * 0.8
                scored.append((s, r))
            scored.sort(key=lambda x: -x[0])
            items = cat.cards([r for _, r in scored[:24]])
        rng = _rng(q.lower().strip(), dt.date.today())
        n = len(items)
        if not n:
            answer = rng.choice(L.SEARCH["none"])
        elif intent.get("moods") and intent["moods"][0] in L.SEARCH["mood"]:
            answer = rng.choice(L.SEARCH["mood"][intent["moods"][0]])
        elif intent.get("anime"):
            answer = rng.choice(L.SEARCH["genre"]["anime"])
        elif want and genre_key(list(want)) in L.SEARCH["genre"]:
            answer = rng.choice(L.SEARCH["genre"][genre_key(intent["genres"])])
        elif intent.get("max_minutes"):
            answer = rng.choice(L.SEARCH["short"])
        elif intent.get("years"):
            y0 = intent["years"][0]
            era = f"{str(y0)[2:]}s" if y0 >= 1950 and intent["years"][1] - y0 == 9 else "classics"
            answer = rng.choice(L.SEARCH["era"]).replace("{era}", era)
        else:
            answer = rng.choice(L.SEARCH["found"])
        answer = answer.replace("{n}", str(n)).replace("{mins}", str(intent.get("max_minutes") or 100))
        if n == 1:
            answer = answer.replace("1 matches", "1 match")
        req = []
        seerr = getattr(app, "seerr", None)
        if seerr is not None and getattr(seerr, "ready", False) and intent["genres"]:
            try:
                req = seerr.by_genre([g for g in intent["genres"] if g in ai.TMDB_MOVIE or g in ai.TMDB_TV],
                                     intent["kind"])
            except Exception:
                req = []
        return {"answer": answer, "items": items, "requestable": req}
    except Exception as e:
        print("classic search failed:", e)
        return {"answer": None, "items": [], "requestable": []}


# ------------------------------------------------------------------ endings (the Up-next screen)
def _g(item, key, row_key=None):
    """A field from a Jellyfin item or a catalog row."""
    v = item.get(key)
    return v if v is not None else item.get(row_key) if row_key else None


def _series_status(app, series_id):
    try:
        jf = getattr(app, "jf", None)
        if jf is not None and getattr(jf, "online", False):
            with jf.quick():
                return jf.item(series_id).get("Status")
    except Exception:
        pass
    return None


def ending_comment(app, item, picks_=(), now=None):
    """(comment, {pick number: why}) for a title that just ended. item: a Jellyfin item (Type, Name, SeriesName,
    ParentIndexNumber, IndexNumber, Genres, SeriesId, Id) or a catalog row. Spoiler-free: never says how it ended."""
    now = now or dt.datetime.now()
    cat = _cat(app)
    kind = _g(item, "Type", "type")
    iid = _g(item, "Id", "id")
    rng = _rng(iid, now.date(), "ending")
    late = now.hour < 5 or now.hour >= 23
    genres = list(_g(item, "Genres", "genres") or [])
    ctx = {"title": _title_short(_g(item, "Name", "name"), 40)}
    pools = []  # (weight, list of templates)
    with cat.lock:
        row = cat.get(iid) if iid else None
        anime = bool(row and _is_anime(row))
        if kind == "Episode":
            sid = _g(item, "SeriesId", "series_id")
            series = cat.get(sid) if sid else None
            if series:
                genres = genres or series.get("genres") or []
                anime = anime or _is_anime(series)
            season = _g(item, "ParentIndexNumber", "season_no")
            ep = _g(item, "IndexNumber", "episode_no")
            ctx.update(series=_title_short(_g(item, "SeriesName", "series_name") or (series or {}).get("name"), 36),
                       season=season, se=f"S{season}E{ep}" if season is not None and ep is not None else None)
            eps = [e for e in cat.eps.get(sid, []) if (e.get("season_no") or 0) > 0] if sid else []
            later = [e for e in eps if ((e.get("season_no") or 0), e.get("episode_no") or 0) > ((season or 0), ep or 0)]
            this_season = [e for e in eps if e.get("season_no") == season]
            last_of_season = bool(this_season) and ep is not None and ep >= max(e.get("episode_no") or 0 for e in this_season)
            if season and last_of_season and later:
                pools.append((10, L.ENDING["season_finale"]))
            elif season and last_of_season and not later and eps:
                status = (series or {}).get("status") or _series_status(app, sid)
                if status == "Ended":
                    pools.append((10, L.ENDING["series_finale"]))
                else:
                    pools.append((10, ["You're all caught up on {series}.", "That's every episode of {series} you have. Caught up!"]))
            else:
                pools.append((3, L.ENDING["episode"]))
                if late:
                    pools.append((2, L.ENDING["episode_late"]))
        else:
            sequel = next((p for p in picks_ if p.get("kind") == "sequel"), None)
            if sequel:
                ctx["next"] = _title_short(sequel.get("plain_title") or sequel.get("title"), 40)
                pools.append((10, L.ENDING["sequel"]))
            elif row and _collection(row):
                parts = sorted([m for m in cat._of("Movie") if _collection(m) == _collection(row)],
                               key=lambda m: m.get("year") or 0)
                seerr = getattr(app, "seerr", None)
                if len(parts) >= 3 and parts[-1]["id"] == row["id"] and getattr(seerr, "ready", False):
                    ctx["series"] = collection_name(cat, _collection(row))
                    pools.append((8, L.ENDING["collection_end"]))
            pools.append((2, L.ENDING["generic"]))
        gk = genre_key(genres, anime)
        if gk in L.ENDING["genre"]:
            pools.append((2.5 if kind != "Episode" else 1.5, L.ENDING["genre"][gk]))
        if late and not any(w >= 8 for w, _ in pools):
            pools.append((0.6, L.ENDING["late"]))
        # reasons for the similar picks, from what they share with this title
        whys = {}
        base_people = {p["name"] for p in _people(row or {}, "Actor")[:8]}
        base_genres = set(norm_genres(genres))
        about = ctx.get("series") if kind == "Episode" and ctx.get("series") else ctx["title"]
        for p in picks_:
            if p.get("kind") != "similar":
                continue
            other = cat.get(p.get("id")) if p.get("id") else None
            og = set(norm_genres((other or {}).get("genres") or p.get("genres") or []))
            shared_cast = [x["name"] for x in _people(other or {}, "Actor")[:8] if x["name"] in base_people]
            if shared_cast:
                whys[str(p.get("n"))] = L.WHY["cast"].format(actor=shared_cast[0], title=about)
            elif og & base_genres:
                g = sorted(og & base_genres, key=lambda x: x in BROAD)[0]
                whys[str(p.get("n"))] = L.WHY["genre"].format(genre=WORD.get(KEY.get(g), g.lower()),
                                                              title=about)
    pools = [(w, ls) for w, ls in pools if ls]
    if any(w >= 8 for w, _ in pools):  # a finale, a sequel waiting: that's what to talk about
        pools = [(w, ls) for w, ls in pools if w >= 8]
    comment = None
    for _ in range(len(pools)):
        total = sum(w for w, _ in pools)
        x, acc = rng.random() * total, 0
        for k, (w, ls) in enumerate(pools):
            acc += w
            if x <= acc:
                break
        _, ls = pools.pop(k)
        lines = list(ls)
        rng.shuffle(lines)
        comment = next((s for s in (_fill(t, ctx) for t in lines) if s and len(s) <= MAX_LEN), None)
        if comment:
            break
    return comment or f"That's a wrap on {ctx['title']}.", whys


def ending_picks(app, item_id):
    """The Up-next picks from the local catalog: next episode, next film of the collection, similar titles."""
    cat = _cat(app)
    out = []
    with cat.lock:
        r = cat.get(item_id)
        if not r:
            return []
        if r["type"] == "Episode":
            nid = cat.next_episode(r["id"])
            n = cat.get(nid) if nid else None
            if n:
                out.append({"id": n["id"], "kind": "next", "type": "Episode",
                            "title": f"S{n.get('season_no', 0)}:E{n.get('episode_no', 0)} · {n.get('name', '')}",
                            "plain_title": n.get("name"), "why": L.WHY["next_up"], "card": cat.card(n)})
            base = cat.get(r.get("series_id")) or r
        else:
            base = r
            col = _collection(r)
            if col:
                parts = sorted([m for m in cat._of("Movie") if _collection(m) == col], key=lambda m: m.get("year") or 0)
                ids = [m["id"] for m in parts]
                if r["id"] in ids and ids.index(r["id"]) + 1 < len(ids):
                    n = parts[ids.index(r["id"]) + 1]
                    out.append({"id": n["id"], "kind": "sequel", "type": "Movie", "title": n.get("name"),
                                "year": n.get("year"), "card": cat.card(n),
                                "why": L.WHY["sequel"].format(col=collection_name(cat, col) or "the"),
                                "later_titles": [m.get("name") for m in parts[ids.index(r["id"]) + 1:]]})
        sims = [cat.get(i) for i in (base.get("extra") or {}).get("similar") or []] or cat.similar_local(base, 16)
        sims = sorted([s for s in sims if s and s["id"] not in {p.get("id") for p in out}],
                      key=lambda s: (cat._played(s), -(s.get("rating") or 0)))
        for s in sims[:4]:
            out.append({"id": s["id"], "kind": "similar", "type": s["type"], "title": s.get("name"),
                        "year": s.get("year"), "why": "A similar feel", "genres": (s.get("genres") or [])[:3],
                        "card": cat.card(s)})
    for n, p in enumerate(out, 1):
        p["n"] = n
    return out


def ending(app, item_id, credits_at=None):
    """The whole Up-next payload, instantly, in the same shape as GET /api/assistant/ending/<id>."""
    picks_ = ending_picks(app, item_id)
    cat = _cat(app)
    with cat.lock:
        row = cat.get(item_id) or {"id": item_id, "type": "Movie", "name": ""}
    comment, whys = ending_comment(app, row, picks_)
    for p in picks_:
        if whys.get(str(p["n"])) and p["kind"] == "similar":
            p["why"] = whys[str(p["n"])]
    return {"item_id": item_id, "state": "ready", "thinking": False, "comment": comment, "picks": picks_,
            "credits_at": credits_at}
