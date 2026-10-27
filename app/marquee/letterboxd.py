"""Letterboxd import for the taste profile. Two ways in, no login and no page scraping:

    rss   the public diary feed letterboxd.com/<username>/rss/ (recent entries: rating, like, rewatch, review text;
          the feed carries each film's TMDB id)
    zip   the official data export (Letterboxd > Settings > Data > Export your data): ratings.csv, reviews.csv,
          diary.csv, watched.csv and likes/films.csv, read in memory (nothing is unpacked to disk)

Films are matched to TMDB (the feed's id, else a Seerr search by title + year), their genres and directors come from
the cached TMDB details, ratings become liked / disliked weights, and review text becomes a few memory notes (AI
summary when an AI is on; keyword rules in Classic mode)."""
import csv
import io
import json
import re
import threading
import time
import zipfile
import xml.etree.ElementTree as ET

import requests

from . import taste

USER_RX = re.compile(r"^[A-Za-z0-9_]{1,40}$")
MAX_FEED = 3 * 2**20
MAX_CSV = 25 * 2**20
MAX_MATCH = 80            # films looked up per import (the most telling ones first)
RATE = .15                # one Letterboxd rating: a bit more than a stated chip, less than a rewatch here
_status = {"state": "idle", "done": 0, "total": 0, "matched": 0, "message": "", "source": None, "at": 0}
_lock = threading.Lock()


def status():
    return dict(_status)


def _set(**kw):
    _status.update(kw, at=time.time())


# ------------------------------------------------------------------ parsing
def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _strip_html(s):
    s = re.sub(r"<img[^>]*>", " ", s or "")
    s = re.sub(r"<br\s*/?>|</p>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"&nbsp;", " ", s)
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
         .replace("&#039;", "'").replace("&#39;", "'"))
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def parse_rss(xml_text):
    """A Letterboxd RSS feed -> [{title, year, rating, liked, rewatch, review, tmdb, date}] (diary entries only)."""
    if len(xml_text) > MAX_FEED:
        raise ValueError("That feed is unexpectedly large")
    if re.search(r"<!DOCTYPE|<!ENTITY", xml_text[:4000], re.I):
        raise ValueError("Unexpected feed format")
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        f = {_local(c.tag): (c.text or "").strip() for c in item}
        title = f.get("filmTitle")
        if not title:
            continue  # lists and other posts
        review = _strip_html(f.get("description", ""))
        review = re.sub(r"^Watched on \w+ \w+ \d+, \d{4}\.?$", "", review).strip()
        rating = f.get("memberRating")
        out.append({"title": title, "year": int(f["filmYear"]) if f.get("filmYear", "").isdigit() else None,
                    "rating": float(rating) if rating and re.fullmatch(r"\d(\.\d)?", rating) else None,
                    "liked": f.get("memberLike", "").lower() == "yes",
                    "rewatch": f.get("rewatch", "").lower() == "yes",
                    "review": review if len(review) > 12 else "",
                    "tmdb": int(f["movieId"]) if f.get("movieId", "").isdigit() else None,
                    "date": f.get("watchedDate") or f.get("pubDate")})
    return out


def _csv(z, name):
    try:
        info = z.getinfo(name)
    except KeyError:
        hits = [i for i in z.infolist() if i.filename.lower().endswith("/" + name.lower()) or i.filename.lower() == name.lower()]
        if not hits:
            return []
        info = hits[0]
    if info.file_size > MAX_CSV:
        raise ValueError(f"{name} is too large")
    text = z.read(info).decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def parse_zip(data):
    """The export ZIP (bytes) -> the same film records as parse_rss, merged per film."""
    if len(data) > 60 * 2**20:
        raise ValueError("That file is too large to be a Letterboxd export")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("That isn't a ZIP file. Use the export from Letterboxd > Settings > Data.")
    names = {i.filename.lower().rsplit("/", 1)[-1] for i in z.infolist()}
    if not ({"ratings.csv", "reviews.csv", "watched.csv", "diary.csv"} & names):
        raise ValueError("No Letterboxd data in that ZIP (expected ratings.csv, reviews.csv or watched.csv)")
    films = {}

    def rec(row):
        title = (row.get("Name") or "").strip()
        if not title:
            return None
        year = int(row["Year"]) if (row.get("Year") or "").strip().isdigit() else None
        key = (taste.norm(title), year)
        return films.setdefault(key, {"title": title, "year": year, "rating": None, "liked": False,
                                      "rewatch": False, "review": "", "tmdb": None, "date": row.get("Date")})

    def rating(v):
        try:
            return float(v) if v not in (None, "") else None
        except ValueError:
            return None
    for row in _csv(z, "watched.csv"):
        rec(row)
    for row in _csv(z, "diary.csv"):
        r = rec(row)
        if r:
            r["rating"] = rating(row.get("Rating")) or r["rating"]
            r["rewatch"] = r["rewatch"] or (row.get("Rewatch") or "").lower() == "yes"
    for row in _csv(z, "ratings.csv"):
        r = rec(row)
        if r and rating(row.get("Rating")) is not None:
            r["rating"] = rating(row.get("Rating"))
    for row in _csv(z, "reviews.csv"):
        r = rec(row)
        if r:
            r["review"] = (r["review"] + "\n" if r["review"] else "") + (row.get("Review") or "").strip()
            r["rating"] = rating(row.get("Rating")) or r["rating"]
    for name in ("likes/films.csv", "films.csv"):
        for row in _csv(z, name):
            r = rec(row)
            if r:
                r["liked"] = True
        if any(i.filename.lower().endswith(name) for i in z.infolist()):
            break
    return list(films.values())


# ------------------------------------------------------------------ matching (TMDB through Seerr)
def _cat():
    return taste._catalog()


def match(film, search=None):
    """TMDB id for a film: the feed's own, a cached earlier match, or a Seerr search (title + year)."""
    if film.get("tmdb"):
        return film["tmdb"]
    key = f"lbx:{taste.norm(film['title'])}:{film.get('year') or ''}"
    cat = _cat()
    if cat is not None:
        hit, _ = cat.kv_get(key)
        if hit:
            return hit.get("tmdb")
    if search is None:
        from . import server
        if not (server.APP and server.APP.seerr.ready):
            return None
        search = server.APP.seerr.search
    try:
        results = [c for c in search(film["title"]) if c.get("type") == "movie"]
    except Exception:
        return None
    want = taste.norm(film["title"])
    year = film.get("year")

    def good(c, slack):
        return taste.norm(c.get("title")) == want and (not year or not c.get("year") or
                                                        abs(int(c["year"]) - year) <= slack)
    pick = next((c for c in results if good(c, 0)), None) or next((c for c in results if good(c, 1)), None) or \
        next((c for c in results if year and str(c.get("year")) == str(year)), None)
    tmdb = pick["id"] if pick else None
    if cat is not None and tmdb:
        cat.kv_set(key, {"tmdb": tmdb})
    return tmdb


def _detail(tmdb):
    try:
        from . import details
        return details.tmdb_detail("movie", tmdb) or {}
    except Exception:
        return {}


# ------------------------------------------------------------------ folding into the profile
def informative(films):
    """The most telling films first: strong ratings, likes and reviews; recent before old."""
    def weight(f):
        r = f.get("rating")
        return (abs(r - 3) if r is not None else 0) + (1.2 if f.get("liked") else 0) + (.8 if f.get("review") else 0) + \
            (.3 if f.get("rewatch") else 0)
    pool = [f for f in films if f.get("rating") is not None or f.get("liked") or f.get("review")]
    pool.sort(key=lambda f: -weight(f))
    return pool


def target_for(f):
    r = f.get("rating")
    if r is not None:
        if r >= 4.5:
            return 1.0
        if r >= 4:
            return .8
        if r <= 1.5:
            return -.8
        if r <= 2.5:
            return -.5
        return .3 if f.get("liked") else None
    return .7 if f.get("liked") else None


def ingest(films, source_label, search=None, detail=None, summarise=None):
    """Match, look up and fold films into the taste profile. Returns a short report dict."""
    detail = detail or _detail
    pool = informative(films)[:MAX_MATCH]
    _set(state="running", done=0, total=len(pool), matched=0, message="Matching films…", source=source_label)
    infos, matched = [], 0
    for n, f in enumerate(pool):
        tmdb = match(f, search)
        d = detail(tmdb) if tmdb else {}
        if tmdb:
            matched += 1
        infos.append((f, tmdb, d))
        _set(done=n + 1, matched=matched, message=f"Matched {matched} of {n + 1}")

    def go(p):
        liked = disliked = 0
        for f, tmdb, d in infos:
            t = target_for(f)
            if t is None:
                continue
            info = {"title": d.get("title") or f["title"], "year": f.get("year") or d.get("year"), "kind": "movie",
                    "genres": d.get("genres") or [], "people": [{"name": c["name"], "role": "Director"}
                                                                for c in d.get("crew") or [] if c.get("job") == "Director"],
                    "runtime": d.get("runtime") or 0, "anime": False, "tmdb": tmdb, "rating": f.get("rating")}
            taste.apply_signal(p, info, t, RATE * (1.5 if f.get("rewatch") else 1), source="letterboxd", fade=False,
                               note_title=False)
            taste.remember_title(p, info["title"], info["year"], "movie", tmdb, t > 0, "letterboxd", f.get("rating"),
                                 strength=round(abs(t) * 1.5, 2))
            liked += t > 0
            disliked += t < 0
        p["letterboxd"] = {**(p.get("letterboxd") or {}), "imported": time.time(), "source": source_label,
                           "films": len(films), "used": len(infos), "matched": matched, "liked": liked,
                           "disliked": disliked}
        return liked, disliked
    liked, disliked = taste._edit(go)
    _record_watched(infos)
    _set(message="Reading your reviews…")
    notes = review_notes(films, summarise)
    for text in notes:
        try:
            taste.add_note(text, source="letterboxd", confidence=.6)
        except ValueError:
            pass
    report = {"films": len(films), "used": len(infos), "matched": matched, "liked": liked, "disliked": disliked,
              "notes": notes}
    _set(state="done", message=f"Imported {len(films)} films: {liked} liked, {disliked} not for you, "
                               f"{len(notes)} notes from reviews", report=report)
    return report


def _record_watched(infos):
    """Diary films that aren't in the library join the Watched list (source "letterboxd"); the profile already
    learned from them above, so they're noted as counted."""
    cat = _cat()
    if cat is None or not hasattr(cat, "record_watched"):
        return
    from .catalog import tmdb_img
    seen = []
    for f, tmdb, d in infos:
        if not tmdb or cat.by_tmdb("movie", tmdb):
            continue
        at = None
        try:
            from .watched import _when
            at = _when(f.get("date")) if f.get("date") else None
        except Exception:
            at = None
        try:
            key = cat.record_watched("movie", tmdb, {
                "title": d.get("title") or f["title"], "year": f.get("year") or d.get("year"),
                "poster": tmdb_img(d.get("poster")), "backdrop": tmdb_img(d.get("backdrop")),
                "genres": d.get("genres") or [], "runtime": d.get("runtime") or 0}, "letterboxd", at)
            seen.append("watched:" + key)
        except Exception as e:
            print("letterboxd: couldn't keep", f.get("title"), "as watched:", e)
    if seen:
        taste.note_seen(seen)


# ------------------------------------------------------------------ reviews -> memory notes
def review_notes(films, summarise=None):
    reviews = [f for f in films if f.get("review")]
    if not reviews:
        return []
    reviews.sort(key=lambda f: -abs((f.get("rating") or 3) - 3))
    if summarise is None:
        summarise = _ai_summarise
    try:
        notes = summarise(reviews[:14])
        if notes is not None:
            return notes
    except Exception as e:
        print("letterboxd: review summary failed:", type(e).__name__, e)
    return rule_notes(reviews)


def _ai_summarise(reviews):
    from . import ai
    if ai.setting() == "classic" or ai.mode() == "classic" or ai._playing():
        return None
    lines = []
    for f in reviews:
        r = f"{f['rating']}/5" if f.get("rating") is not None else ("liked" if f.get("liked") else "no rating")
        lines.append(f"- {f['title']} ({f.get('year') or '?'}), {r}: {f['review'][:380]}")
    text = ai.chat("Letterboxd reviews by one viewer:\n" + "\n".join(lines),
                   "From these film reviews, write up to 5 short notes about this viewer's taste, for a movie app's "
                   "memory. Each note is one plain sentence under 12 words about what they like or dislike in films "
                   "(tone, pacing, themes, genres, styles), e.g. 'Loves slow-burn, atmospheric sci-fi'. Don't name "
                   "the films, don't guess beyond the reviews. Reply with JSON only: [\"note\", ...]",
                   max_tokens=260, timeout=60, temperature=.3)
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return None
    notes = [re.sub(r"\s+", " ", str(n)).strip() for n in json.loads(m.group(0)) if str(n).strip()]
    return [n[:160] for n in notes[:5]]


PHRASES = [  # (regex, liked wording, disliked wording)
    (r"slow[- ]?burn|slow[- ]paced|patient", "Enjoys slow-burn films", "Finds slow-burn films a drag"),
    (r"atmospher|moody|haunting", "Loves atmospheric, moody films", "Not taken by mood-over-plot films"),
    (r"funny|hilarious|laugh", "Values films that make them laugh", "Comedy that misses doesn't work for them"),
    (r"twist|mind[- ]?bend|clever|smart", "Likes clever, twisty stories", "Twists for their own sake annoy them"),
    (r"beautiful|gorgeous|stunning|visuals?|cinematograph", "Notices and loves striking visuals",
     "Pretty visuals don't save a film for them"),
    (r"score|soundtrack|music", "A great score matters to them", "Overbearing music bothers them"),
    (r"emotional|cried|tears|moving|heartfelt", "Moved by emotional, heartfelt stories", "Finds sentimental films cloying"),
    (r"boring|dull|slow|dragged", None, "Gets bored when a film drags"),
    (r"jump ?scares?", "Enjoys a good jump scare", "Doesn't like cheap jump scares"),
    (r"gore|gory|violent|violence", "Fine with gore and violence", "Put off by gore and heavy violence"),
    (r"too long|overlong|bloated", None, "Prefers films that don't overstay their welcome"),
    (r"acting|performance|cast", "Great performances win them over", "Weak acting sinks a film for them"),
]


def rule_notes(reviews):
    """Classic mode: keyword phrases across reviews, kept only when they come up at least twice (or once with a
    strong rating), worded as plain notes."""
    tally = {}
    for f in reviews:
        r = f.get("rating")
        pol = 1 if (r is not None and r >= 3.5) or (r is None and f.get("liked")) else -1 if r is not None and r <= 2.5 else 0
        if not pol:
            continue
        low = f["review"].lower()
        for rx, good, bad in PHRASES:
            if re.search(rx, low):
                text = good if pol > 0 else bad
                if text:
                    t = tally.setdefault(text, [0, 0.0])
                    t[0] += 1
                    t[1] = max(t[1], abs((r or 3) - 3))
    notes = [f"{text} (from {n} Letterboxd review{'s' if n > 1 else ''})"
             for text, (n, strength) in sorted(tally.items(), key=lambda kv: -kv[1][0]) if n >= 2 or strength >= 1.5]
    return notes[:5]


# ------------------------------------------------------------------ entry points (run in the background)
def fetch_rss(username):
    if not USER_RX.match(username or ""):
        raise ValueError("That doesn't look like a Letterboxd username")
    r = requests.get(f"https://letterboxd.com/{username}/rss/", timeout=15,
                     headers={"User-Agent": "Marquee (taste import; RSS only)"}, stream=True)
    if r.status_code == 404:
        raise ValueError("No Letterboxd profile with that username (or it's private)")
    r.raise_for_status()
    body = r.raw.read(MAX_FEED + 1, decode_content=True)
    if len(body) > MAX_FEED:
        raise ValueError("That feed is unexpectedly large")
    return body.decode("utf-8", errors="replace")


def start(kind, payload):
    """Start an import in the background. kind "rss" (payload = username) or "zip" (payload = bytes)."""
    with _lock:
        if _status["state"] == "running" and time.time() - _status["at"] < 120:
            raise RuntimeError("An import is already running")
        _set(state="running", done=0, total=0, matched=0, message="Starting…", source=kind, report=None)

    def run():
        try:
            if kind == "rss":
                films = parse_rss(fetch_rss(payload))
                label = f"letterboxd.com/{payload} (RSS)"
                if not films:
                    _set(state="error", message="That feed has no diary entries yet")
                    return
                taste._edit(lambda p: p.setdefault("letterboxd", {}).update(username=payload))
            else:
                films = parse_zip(payload)
                label = "Letterboxd export"
            ingest(films, label)
        except Exception as e:
            _set(state="error", message=str(e) or type(e).__name__)
    threading.Thread(target=run, daemon=True, name="letterboxd-import").start()
    return status()
