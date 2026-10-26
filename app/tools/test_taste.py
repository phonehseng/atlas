"""Taste profile checks that touch nothing real: they run against a throwaway MARQUEE_HOME holding a copy of the dev
catalog (tools/dev.ps1's .devhome), so watch history and profile changes never reach Jellyfin or the real files.

    python tools/test_taste.py drift      stated romance + a month of finished horror: the weights before/after
    python tools/test_taste.py letterboxd  the export-ZIP parser (a synthetic export) and the RSS parser (a saved sample)
    python tools/test_taste.py rules       memory extraction, text parsing, fit scores and classic comments
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(tempfile.mkdtemp(prefix="marquee-taste-test-"))
DEV = ROOT / ".devhome"
if (DEV / "catalog.db").is_file():
    for f in ("catalog.db", "catalog.db-wal", "catalog.db-shm"):
        if (DEV / f).is_file():
            shutil.copy(DEV / f, HOME / f)
os.environ["MARQUEE_HOME"] = str(HOME)
sys.path.insert(0, str(ROOT))

from marquee import taste  # noqa: E402
from marquee.catalog import Catalog  # noqa: E402

DAY = 86400
SAMPLE_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:letterboxd="https://letterboxd.com" xmlns:tmdb="https://themoviedb.org" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Letterboxd - sample</title>
<item><title>Arrival, 2016 - ★★★★★</title><link>https://letterboxd.com/sample/film/arrival-2016/</link>
 <guid isPermaLink="false">letterboxd-review-1</guid><pubDate>Sat, 3 Oct 2026 21:00:00 +1300</pubDate>
 <letterboxd:watchedDate>2026-10-03</letterboxd:watchedDate><letterboxd:rewatch>Yes</letterboxd:rewatch>
 <letterboxd:filmTitle>Arrival</letterboxd:filmTitle><letterboxd:filmYear>2016</letterboxd:filmYear>
 <letterboxd:memberRating>5.0</letterboxd:memberRating><letterboxd:memberLike>Yes</letterboxd:memberLike><tmdb:movieId>329865</tmdb:movieId>
 <description><![CDATA[ <p><img src="https://a.ltrbxd.com/x.jpg"/></p> <p>A patient, atmospheric slow-burn with a gorgeous score. Made me cry.</p> ]]></description>
 <dc:creator>Sample</dc:creator></item>
<item><title>Cats, 2019 - ★</title><link>https://letterboxd.com/sample/film/cats-2019/</link>
 <guid isPermaLink="false">letterboxd-watch-2</guid><pubDate>Fri, 2 Oct 2026 21:00:00 +1300</pubDate>
 <letterboxd:watchedDate>2026-10-02</letterboxd:watchedDate><letterboxd:rewatch>No</letterboxd:rewatch>
 <letterboxd:filmTitle>Cats</letterboxd:filmTitle><letterboxd:filmYear>2019</letterboxd:filmYear>
 <letterboxd:memberRating>1.0</letterboxd:memberRating><tmdb:movieId>536869</tmdb:movieId>
 <description><![CDATA[ <p><img src="https://a.ltrbxd.com/y.jpg"/></p> <p>Boring, and it dragged. The music did nothing for me.</p> ]]></description></item>
<item><title>A list: comfort films</title><link>https://letterboxd.com/sample/list/comfort/</link><description>a list</description></item>
</channel></rss>"""


def show(p, keys=("Romance", "Horror", "Thriller", "Comedy")):
    return "  ".join(f"{k} {p['genres'].get(k, {}).get('w', 0):+.2f}" for k in keys)


def drift():
    cat = Catalog(None)
    taste.onboard(likes=["Romance", "Comedy"], vibes=["Feel-good"])
    p = taste.profile()
    print("stated (Launchpad: romance, comedy):", show(p))
    # a month of horror: four finished horror films a week, added to the catalog copy in memory only
    start = time.time() - 30 * DAY
    films = []
    for k in range(16):
        rid = f"test-horror-{k}"
        cat.items[rid] = {"id": rid, "type": "Movie", "source": "jellyfin", "library": "movies", "name": f"Horror Test {k}",
                          "year": 2015 + k % 8, "genres": ["Horror", "Thriller"] if k % 3 else ["Horror"],
                          "runtime": 5400, "provider": {}, "extra": {"people": [{"name": "Mike Flanagan", "type": "Director"}]}}
        films.append(rid)
    for week in range(4):
        for k in range(4):
            rid = films[week * 4 + k]
            at = start + week * 7 * DAY + k * 1.5 * DAY
            cat.ud[rid] = {"played": 1, "position": 0, "favorite": 0, "last_played": at, "play_count": 1, "changed": at,
                           "dirty": 0, "remote": None}
        res = taste.update_from_history(cat, now=start + (week + 1) * 7 * DAY)
        print(f"after week {week + 1} ({res['plays']} horror films finished):", show(taste.profile()))
        for w in res.get("why") or []:
            print("   why:", w["text"])
    p = taste.profile()
    print("summary:\n" + taste.summary())
    people = taste.favourite_people(3)
    print("favourite people:", [(x["name"], round(x["w"], 2), x["n"]) for x in people])
    # an abandoned romance (stopped at 10%, three days ago)
    rid = "test-romance-0"
    cat.items[rid] = {"id": rid, "type": "Movie", "name": "Romance Test", "year": 2020, "genres": ["Romance", "Comedy"],
                      "runtime": 6000, "provider": {}, "extra": {}}
    cat.ud[rid] = {"played": 0, "position": 600, "favorite": 0, "last_played": time.time() - 3 * DAY, "play_count": 0,
                   "changed": 0, "dirty": 0, "remote": None}
    res = taste.update_from_history(cat)
    print("after abandoning a romance in the first 10%:", show(taste.profile()), f"(abandoned={res['abandoned']})")
    # reset + undo
    taste.reset()
    print("after reset: empty =", taste.is_empty())
    taste.undo_reset()
    print("after undo:", show(taste.profile()))


def letterboxd():
    from marquee import letterboxd as lb
    films = lb.parse_rss(SAMPLE_RSS)
    print("RSS:", [(f["title"], f["year"], f["rating"], f["liked"], f["rewatch"], f["tmdb"], f["review"][:40]) for f in films])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("letterboxd-sample-2026-10-26/ratings.csv", "Date,Name,Year,Letterboxd URI,Rating\n"
                   "2026-09-01,Arrival,2016,https://boxd.it/a,5\n2026-09-02,Cats,2019,https://boxd.it/b,1\n"
                   "2026-09-03,Dune,2021,https://boxd.it/c,4.5\n2026-09-04,\"Crazy, Stupid, Love.\",2011,https://boxd.it/d,2\n")
        z.writestr("letterboxd-sample-2026-10-26/reviews.csv", "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Review,Tags,Watched Date\n"
                   "2026-09-01,Arrival,2016,https://boxd.it/a,5,Yes,\"Slow-burn and atmospheric, the score is stunning.\",,2026-09-01\n"
                   "2026-09-03,Dune,2021,https://boxd.it/c,4.5,No,\"Atmospheric and beautiful, a slow-burn epic.\",,2026-09-03\n"
                   "2026-09-04,\"Crazy, Stupid, Love.\",2011,https://boxd.it/d,2,No,\"Boring, it dragged.\",,2026-09-04\n")
        z.writestr("letterboxd-sample-2026-10-26/watched.csv", "Date,Name,Year,Letterboxd URI\n"
                   "2026-09-01,Arrival,2016,https://boxd.it/a\n2026-09-05,Paddington 2,2017,https://boxd.it/e\n")
        z.writestr("letterboxd-sample-2026-10-26/likes/films.csv", "Date,Name,Year,Letterboxd URI\n"
                   "2026-09-05,Paddington 2,2017,https://boxd.it/e\n")
    films = lb.parse_zip(buf.getvalue())
    print("ZIP:", [(f["title"], f["year"], f["rating"], f["liked"]) for f in films])
    try:
        lb.parse_zip(b"not a zip")
    except ValueError as e:
        print("bad file ->", e)
    print("rule notes from reviews:", lb.rule_notes([f for f in films if f["review"]]))
    # ingest with stand-ins for TMDB (no network): matches by a fixed table, genres from it
    table = {"arrival": (329865, ["Science Fiction", "Drama"], "Denis Villeneuve"),
             "cats": (536869, ["Comedy", "Fantasy", "Family"], "Tom Hooper"),
             "dune": (438631, ["Science Fiction", "Adventure"], "Denis Villeneuve"),
             "crazy stupid love": (50646, ["Comedy", "Drama", "Romance"], "Glenn Ficarra"),
             "paddington 2": (346648, ["Adventure", "Comedy", "Family"], "Paul King")}

    def search(title):
        hit = table.get(taste.norm(title))
        return [{"type": "movie", "id": hit[0], "title": title, "year": None}] if hit else []

    def detail(tmdb):
        for name, (tid, genres, director) in table.items():
            if tid == tmdb:
                return {"title": name.title(), "genres": genres, "crew": [{"name": director, "job": "Director"}]}
        return {}
    report = lb.ingest(films, "test export", search=search, detail=detail, summarise=lambda r: None)
    print("ingest report:", report)
    p = taste.profile()
    print("after import:", show(p, ("Science Fiction", "Comedy", "Romance", "Family")))
    print("liked:", [(t["title"], t.get("rating")) for t in p["titles"]["liked"]])
    print("disliked:", [(t["title"], t.get("rating")) for t in p["titles"]["disliked"]])
    print("people:", [(k, round(v["w"], 2)) for k, v in p["people"].items()])


def rules():
    for t in ["remember I hate jump scares", "I love slow-burn sci-fi", "I like horror, recommend something",
              "I can't stand musicals", "note that I prefer films under two hours", "what's good tonight?"]:
        print(f"{t!r} -> {taste.extract_memory(t)!r}")
    taste.onboard(likes=["Action", "Sci-fi"], dislikes=["Musicals"])
    taste.add_note("Hates jump scares", "chat")
    for card in [{"type": "Movie", "name": "Dune", "year": 2021, "genres": ["Science Fiction", "Adventure"], "rating": 7.8},
                 {"type": "movie", "title": "La La Land", "year": 2016, "genres": ["Comedy", "Drama", "Romance", "Music"]},
                 {"type": "movie", "title": "The Conjuring", "year": 2013, "genres": ["Horror", "Thriller"]},
                 {"type": "movie", "title": "Lady Bird", "year": 2017, "genres": ["Drama", "Comedy"], "rating": 7.3,
                  "crew": [{"name": "Greta Gerwig", "job": "Director"}]}]:
        f = taste.score(card)
        print(f"{card.get('title') or card.get('name')}: {f['score']} {f['verdict']} | {f['reasons']}")
        print("   classic comment:", taste.classic_comment(f))


if __name__ == "__main__":
    try:
        {"drift": drift, "letterboxd": letterboxd, "rules": rules}[sys.argv[1] if len(sys.argv) > 1 else "drift"]()
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
