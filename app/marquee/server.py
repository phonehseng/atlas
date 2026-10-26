"""The local web server the window shows: the UI files plus a small JSON API over the local catalog, Seerr and
the AI helper. Listens on 127.0.0.1 only; tokens stay in Python and never reach the page.

Local first: every library page (home, libraries, details, episodes, search) and every play is answered from the
local catalog (marquee.catalog), so nothing waits on Jellyfin and everything keeps working while it's down. Jellyfin
syncs in the background; the few extras only it can give (similar titles) are fetched with a short timeout after the
page has its answer. Artwork always comes from /img/..., a local cache filled from Jellyfin or TMDB."""
import random
import re
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from . import ai, settings
from .catalog import Catalog, images_for

WEB = Path(__file__).resolve().parent / "web"
app = Flask(__name__, static_folder=None)
APP = None  # set by marquee.app
TICKS = 10_000_000
_cache = {}
_catalog = None
_catalog_lock = threading.Lock()


def catalog():
    """The local catalog (created and started on first use; also reachable as APP.catalog)."""
    global _catalog
    if _catalog is None:
        with _catalog_lock:
            if _catalog is None:
                c = Catalog(APP)
                c.on_updated = _catalog_updated
                APP.catalog = c
                c.start()
                _catalog = c
    return _catalog


def _catalog_updated(event, data):
    _cache.clear()
    try:
        from . import homemix
        homemix.invalidate()
    except Exception:
        pass
    if event == "userdata-changed":  # smart season requests follow watch progress
        try:
            from . import smart
            smart.poke(APP)
        except Exception as e:
            print("smart requests:", type(e).__name__, e)
    try:
        APP.notify_ui(event, data)
    except Exception as e:
        print("ui notice failed:", e)


def jf_ready():
    """Jellyfin is set up and answering (as far as we know right now)."""
    return APP.jf.online


def cached(key, seconds, fn, retry=None):
    """Remember fn() for `seconds`; an empty result is only remembered for `retry` seconds (AI that failed)."""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < (seconds if hit[1] or retry is None else retry):
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def ok(**kw):
    return jsonify(ok=True, **kw)


def fail(msg, code=400):
    return jsonify(ok=False, message=str(msg)), code


@app.errorhandler(Exception)
def on_error(e):
    code = getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "code", 500)
    return fail(e, code if isinstance(code, int) and 400 <= code < 600 else 500)


# ------------------------------------------------------------------ cards
def card(i):
    """What the UI needs to draw a poster/card for a Jellyfin item (artwork through the local /img cache).
    Library pages use catalog().card() instead; this is for items that come straight from Jellyfin."""
    kind = i.get("Type")
    ud = i.get("UserData") or {}
    series = i.get("SeriesId")
    poster, backdrop, thumb, logo = images_for(i)
    runtime = (i.get("RunTimeTicks") or 0) / TICKS
    pos = (ud.get("PlaybackPositionTicks") or 0) / TICKS
    path = (i.get("Path") or "").lower()
    return {
        "id": i["Id"], "type": kind, "name": i.get("Name"), "series": i.get("SeriesName"), "series_id": series,
        "season": i.get("ParentIndexNumber"), "episode": i.get("IndexNumber"), "year": i.get("ProductionYear"),
        "rating": i.get("CommunityRating"), "age": i.get("OfficialRating"), "genres": i.get("Genres") or [],
        "overview": i.get("Overview") or "", "runtime": runtime, "position": pos,
        "progress": (pos / runtime) if runtime else 0, "played": ud.get("Played", False),
        "favorite": ud.get("IsFavorite", False), "unplayed": ud.get("UnplayedItemCount"),
        "poster": poster, "backdrop": backdrop, "thumb": thumb, "logo": logo,
        "anime": "\\anime" in path or "/anime" in path, "added": i.get("DateCreated"),
    }


def cards(items):
    return [card(i) for i in items or []]


# ------------------------------------------------------------------ pages
@app.get("/")
def index():
    r = send_from_directory(WEB, "index.html")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/<path:name>")
def static_files(name):
    return send_from_directory(WEB, name)


# ------------------------------------------------------------------ artwork (local cache)
def _send_img(path):
    if not path:
        return Response(status=404)
    r = send_file(path, mimetype=catalog().images.mimetype(path), conditional=True, max_age=86400)
    r.headers["Cache-Control"] = "private, max-age=86400"
    return r


@app.get("/img/Items/<item_id>/Images/<kind>")
def img_jellyfin(item_id, kind):
    """Jellyfin artwork from the local cache; fetched once (and kept) while Jellyfin answers."""
    if kind not in ("Primary", "Backdrop", "Thumb", "Logo", "Banner", "Art") or "/" in item_id or ".." in item_id:
        return Response(status=404)
    params = {k: v for k, v in request.args.items() if k in ("fillHeight", "fillWidth", "maxWidth", "maxHeight",
                                                              "quality") and v.isdigit()}
    return _send_img(catalog().images.get_jf(item_id, kind, params))


@app.get("/img/tmdb/<size>/<path:file>")
def img_tmdb(size, file):
    if ".." in file or not re.fullmatch(r"[wh]\d{2,4}|original", size):
        return Response(status=404)
    return _send_img(catalog().images.get_tmdb(size, file))


@app.get("/api/state")
def state():
    s = settings.load()
    jf, cat = APP.jf, catalog()
    return ok(settings=settings.public(s), ai=ai.available(), atlas=ai.atlas_detected(),
              server_ok=jf.configured or cat.has_items(),
              offline=jf.configured and not jf.online, since=jf.down_since,
              mode="jellyfin" if jf.configured else "local", catalog=cat.stats())


@app.post("/api/settings")
def save_settings():
    # Secrets are never written through here (a page must not be able to replace a sign-in), except the two the
    # user types in themselves, which are set-only: an empty value is ignored and they are never sent back.
    set_only = {"ai_key", "gdrive_client_secret"}
    blocked = (set(settings.SECRET) - set_only) | {"user_id", "user_name", "gdrive_token", "update_repo"}
    allowed = set(settings.DEFAULTS) - blocked
    changes = {}
    for k, v in (request.get_json(force=True) or {}).items():
        if k in blocked or k not in allowed and not k.startswith("ai_"):
            continue
        if k in set_only and not (isinstance(v, str) and v.strip()):
            continue
        changes[k] = v
    s = settings.save(changes)
    if "remote" in changes or "remote_port" in changes or any(k.startswith("mobile") for k in changes):
        try:
            from . import remote
            remote.apply(APP)
        except Exception as e:
            print("phone remote:", e)
    APP.jf.reload()
    APP.seerr.reload()
    APP.player.settings_changed()
    catalog().kick(scan="library_folders" in changes)
    return ok(settings=settings.public(s))


@app.post("/api/login")
def login():
    b = request.get_json(force=True) or {}
    name = APP.jf.login(b["server"], b["user"], b["password"])
    seerr_msg = None
    if b.get("seerr"):
        try:
            APP.seerr.login(b["seerr"], b["user"], b["password"])
        except Exception as e:
            seerr_msg = f"Signed in to Jellyfin, but Seerr said: {e}"
    _cache.clear()
    APP.jf.ping()
    catalog().kick(snapshot=True)
    return ok(user=name, seerr=seerr_msg)


@app.post("/api/seerr/login")
def seerr_login():
    """Connect Seerr on its own (Settings): its address and the Jellyfin username/password it signs in with."""
    b = request.get_json(force=True) or {}
    APP.seerr.login(b["seerr"], b.get("user") or settings.load()["user_name"], b["password"])
    _cache.pop("discover", None)
    return ok()


@app.post("/api/logout")
def logout():
    settings.save({"token": "", "user_id": "", "seerr_cookie": ""})
    APP.jf.reload()
    APP.seerr.reload()
    return ok()


@app.get("/api/home")
def home():
    """Home in one pass (marquee.homemix.assemble): the personal rows, then a capped, rotating mix of library and
    requestable titles, one card per title, series in order. ?page=2,3...: more rows for infinite scroll."""
    from . import homemix
    page = request.args.get("page", 1, type=int) or 1
    cat = catalog()
    rows = cat.home_rows()
    try:  # rule-made rows (Classic mode's picks; cheap and offline, shown in every mode)
        from . import scripted
        keys = {r["key"] for r in rows}
        rows += [r for r in scripted.rows(APP) if r["key"] not in keys]
    except Exception as e:
        print("classic rows unavailable:", type(e).__name__, e)
    pending = []
    av = ai.available()
    picks = None
    if av["mode"] == "classic":
        try:
            from . import scripted
            picks = scripted.picks(APP).get("items")
        except Exception:
            picks = None
    else:
        hit = _cache.get("picks")
        picks = (hit[1] or {}).get("items") if hit else None
        if not hit:
            pending.append("ai-picks")
    if picks:
        rows.append({"key": "ai-picks", "title": "Picked for you", "ai": av["name"], "items": picks, "style": "poster"})
    hit = _cache.get("home-discover-ai")
    if hit and hit[1]:
        rows.append({**hit[1], "ai": av["name"]})
    elif not hit and APP.seerr.ready:
        pending.append("ai-discover")
    taste = None
    try:
        from .taste_api import taste_row
        taste = taste_row()
    except Exception as e:
        print("taste row unavailable:", type(e).__name__, e)
    mylist = []
    try:
        mylist = [k if card is None else homemix.key_of(cat, card) for k, _, card, _ in cat.mylist()]
    except Exception as e:
        print("my list unavailable:", type(e).__name__, e)
    out = homemix.assemble(APP, rows, mylist, page=page, taste=taste)
    library = homemix.library_view(APP, rows) if page <= 1 else []
    # the hero comes from what Home recommends (so never a later part of a series you haven't reached)
    featured, seen = [], set()
    for r in out["rows"] + library:
        for c in r["items"]:
            if c.get("backdrop") and c.get("type") in ("Movie", "Series") and c["id"] not in seen:
                seen.add(c["id"])
                featured.append(c)
    hero = random.sample(featured, min(5, len(featured))) if featured and page <= 1 else []
    return ok(rows=out["rows"], library=library, hero=hero, more=out["more"], page=page, warming=out["warming"],
              seerr=out["seerr"], pending=pending, user=settings.load()["user_name"],
              offline=APP.jf.configured and not APP.jf.online)


@app.get("/api/home/warm")
def home_warm():
    """Is Home still filling its cache from Seerr? The page asks again for its rows when this says no."""
    from . import homemix
    return ok(busy=homemix.warming())


@app.get("/api/discover/<kind>")
def discover_kind(kind):
    """Requestable titles for a library page's grid, a page at a time (infinite scroll)."""
    if kind not in ("movies", "shows", "anime"):
        return fail("unknown library", 404)
    from . import homemix
    return ok(**homemix.discover_page(APP, kind, request.args.get("page", 1, type=int) or 1,
                                      request.args.get("sort") or "popular", request.args.get("genre") or None))


@app.get("/api/home/discover")
def home_discover():
    """Rows of titles to request, mixed into Home after the library rows: sequels and the rest of collections you
    own or watched, "because you watched" picks, and (with AI on) a curated row. Built without AI first; cached."""
    if not APP.seerr.ready:
        return ok(rows=[])
    return ok(rows=cached("home-discover", 6 * 3600, _discover_rows))


def _discover_rows():
    cat, sr = catalog(), APP.seerr
    rows, seen = [], set()

    def fresh(items):
        out = []
        for c in items:
            k = (c["type"], c["id"])
            if k not in seen:
                seen.add(k)
                out.append(c)
        return out

    with cat.lock:
        played = cat.recent_played_rows("Movie", 8)
        owned = cat._of("Movie")
        owned = random.sample(owned, min(12, len(owned)))
    sequels, names = [], []
    for i in (played + owned)[:16]:
        tmdb = (i.get("provider") or {}).get("Tmdb")
        if not tmdb:
            continue
        try:
            name, rest = sr.collection_rest(tmdb)
        except Exception:
            continue
        if rest:
            sequels += [{**c, "because": f"the {name}"} for c in rest]
            names.append(name)
    sequels = fresh(sorted(sequels, key=lambda c: c.get("year") or "9999"))
    if sequels:
        rows.append({"key": "sequels", "title": "Sequels & the rest of the collection", "items": sequels[:20],
                     "style": "request"})
    with cat.lock:
        recent = cat.recent_played_rows("Movie,Episode", 10)
    done = set()
    for i in recent:
        src = cat.items.get(i.get("series_id")) if i["type"] == "Episode" else i
        tmdb = ((src or {}).get("provider") or {}).get("Tmdb")
        if not tmdb or src["id"] in done:
            continue
        done.add(src["id"])
        try:
            recs = [{**c, "because": f"people who liked {src['name']}"} for c in
                    fresh(sr.recommended("tv" if src["type"] == "Series" else "movie", tmdb))]
        except Exception:
            continue
        if len(recs) >= 4:
            rows.append({"key": "more-" + src["id"], "title": f"More like {src['name']}, ready to request",
                         "items": recs, "style": "request"})
        if len(done) >= 2:
            break
    return rows


@app.get("/api/home/discover/ai")
def home_discover_ai():
    """The AI-curated request row, separate so the plain rows never wait for it (slow on a local model)."""
    if not APP.seerr.ready or APP.player.playing or not ai.available()["on"]:
        return ok(row=None)

    def build():
        pool = [c for r in cached("home-discover", 6 * 3600, _discover_rows) for c in r["items"]]
        try:
            pool += [{**c, "because": "trending this week"} for c in APP.seerr.cards("/discover/trending")[:10]]
        except Exception:
            pass
        items = ai.curate(APP, pool)
        return {"key": "ai-discover", "title": f"{ai.available()['name']} thinks you'd like these",
                "items": items, "style": "request"} if items else None
    hit = _cache.get("home-discover-ai")
    if hit and time.time() - hit[0] < (12 * 3600 if hit[1] else 600):  # failures retry after 10 minutes
        row = hit[1]
    else:
        row = build()
        _cache["home-discover-ai"] = (time.time(), row)
    return ok(row=_late_row(row))


def _late_row(row):
    """A row that lands after Home was drawn: series order and de-duplication against what Home shows."""
    if not row:
        return None
    from . import homemix
    items = homemix.filter_late(APP, row.get("items"))
    return {**row, "items": items, "style": "poster"} if items else None


@app.get("/api/library/<kind>")
def library(kind):
    if kind not in ("movies", "shows", "anime"):
        return fail("unknown library", 404)
    from . import homemix
    rows = catalog().library_rows(kind)
    try:  # genre rows also carry Seerr's popular titles in that genre (one card per title)
        rows = homemix.library_extra(APP, kind, rows)
    except Exception as e:
        print("library mix unavailable:", type(e).__name__, e)
    return ok(rows=rows, seerr={"ready": homemix.seerr_ready(APP), "online": homemix.seerr_online(APP)})


def _fetch_similar(item_id):
    """Jellyfin's own "more like this", kept for next time (the page already has the local answer)."""
    try:
        with APP.jf.quick():
            ids = [i["Id"] for i in APP.jf.similar(item_id)]
        catalog().remember_similar(item_id, ids)
    except Exception:
        pass


def touch_index(item_id):
    """The media index (optional): a title was opened or played, so index it soon. No-op when not installed."""
    def run():
        try:
            from . import index
            index.touch(item_id)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


@app.get("/api/item/<item_id>")
def item(item_id):
    cat = catalog()
    out = cat.detail(item_id)
    if out is not None:
        out["mylist"] = cat.mylist_key(item_id=out["id"]) in set(cat.mylist_keys())
        if out["type"] in ("Movie", "Episode"):
            touch_index(out["id"])
    if out is None:
        if not jf_ready():
            return fail("That title isn't in the library on this PC", 404)
        with APP.jf.quick((2.5, 8)):
            i = APP.jf.item(item_id)  # not in the catalog yet (just added): ask Jellyfin, then catch up
        cat.kick(snapshot=True)
        out = card(i)
        out.update(people=[], studios=[], tagline=(i.get("Taglines") or [None])[0], similar=[], resolution=None,
                   hdr=False)
        return ok(item=out)
    r = cat.get(item_id)
    stale = time.time() - ((r.get("extra") or {}).get("similar_at") or 0) > 86400
    if r.get("source") == "jellyfin" and r["type"] in ("Movie", "Series") and stale and jf_ready():
        threading.Thread(target=_fetch_similar, args=(r["id"],), daemon=True).start()
    return ok(item=out)


@app.get("/api/episodes/<series>/<season>")
def episodes(series, season):
    return ok(items=catalog().episodes(series, season))


@app.get("/api/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return ok(items=[], requestable=[])
    cat = catalog()
    items = cat.search(q)
    intent = ai.read_query(q)
    if intent["genres"] and len(items) < 6:
        # "horror movies", "funny shows": answer at once from the library's genres, no AI needed
        types = {"movie": "Movie", "tv": "Series"}.get(intent["kind"], "Movie,Series")
        seen = {i["id"] for i in items}
        items += [i for i in cat.by_genres(intent["genres"], types) if i["id"] not in seen]
    req = []
    if APP.seerr.ready:
        try:
            if intent["prompt"] and intent["genres"]:
                req = APP.seerr.by_genre(intent["genres"], intent["kind"])
            else:
                req = [c for c in APP.seerr.search(q) if c.get("status") != 5][:20]
        except Exception:
            req = []
    # one card per title: a requestable result that's in the library already is the library card above
    shown = {i["id"] for i in items}
    for c in list(req):
        r = cat.by_tmdb(c.get("type"), c.get("id")) if c.get("type") in ("movie", "tv") else None
        if r:
            req.remove(c)
            if r["id"] not in shown:
                items.append(cat.card(r))
                shown.add(r["id"])
    # "prompt": the page should also call /api/search/ai (a question or a mood, not a title)
    return ok(items=items, requestable=req, prompt=intent["prompt"] and ai.available()["on"] and jf_ready(),
              genres=intent["genres"])


@app.get("/api/search/ai")
def search_ai():
    """The slow half of a prompt-style search: the AI re-sorts the library for the request and answers in a line.
    One small call per distinct query (cached a day); never while a video plays."""
    q = request.args.get("q", "").strip()
    if not q or APP.player.playing or not jf_ready():  # the AI reads the library through Jellyfin
        return ok(answer=None, items=[], requestable=[])
    key = "ai-search:" + " ".join(q.lower().split())
    return ok(**cached(key, 86400, lambda: ai.search(APP, q)))


def play_card(info):
    """The card Player.play_file() takes, from catalog().lookup()."""
    return {k: info.get(k) for k in ("id", "path", "name", "series", "series_id", "season", "episode", "year",
                                     "poster", "backdrop", "runtime", "segments", "next_episode_id", "anime")}


@app.post("/api/play")
def play():
    b = request.get_json(force=True) or {}
    item_id, start = b["id"], b.get("start")
    info = catalog().lookup(item_id)
    touch_index(info["id"] if info else item_id)
    online = info.get("source") != "local" and APP.jf.fresh_online(45) if info else jf_ready()
    if info and not online:
        # Jellyfin isn't needed for a file on this PC: play it straight from the catalog
        if not info["exists"]:
            return fail("That file isn't on this PC" + ("" if info.get("source") == "local" else
                                                        ", and Jellyfin isn't reachable"), 409)
        if start is None and info.get("position"):
            start = info["position"]
        threading.Thread(target=APP.player.play_file, args=(play_card(info),), kwargs={"start": start},
                         daemon=True).start()
        return ok(local=True)
    if not info and not online:
        return fail("That title isn't in the library on this PC", 404)
    kw = {"start": start}
    if b.get("card"):
        kw["card"] = b["card"]
    threading.Thread(target=APP.player.play, args=(item_id,), kwargs=kw, daemon=True).start()
    return ok()


# ------------------------------------------------------------------ player window (mini tile, full) and bookmarks
@app.post("/api/player/mini")
def player_mini():
    fn = getattr(APP.player, "to_mini", None)
    if not fn:
        return fail("The player can't do that", 409)
    fn()
    return ok()


@app.post("/api/player/full")
def player_full():
    fn = getattr(APP.player, "restore_full", None)
    if not fn:
        return fail("The player can't do that", 409)
    fn()
    return ok()


@app.get("/api/audio-devices")
def audio_devices():
    try:
        devs = APP.player.mpv.audio_device_list or []
    except Exception:
        devs = []
    return ok(devices=[{"name": d.get("name"), "description": d.get("description") or d.get("name")} for d in devs
                       if isinstance(d, dict) and d.get("name")])


@app.get("/api/bookmarks")
def bookmarks_all():
    from . import bookmarks
    return ok(items=bookmarks.all_items())


@app.get("/api/bookmarks/<item_id>")
def bookmarks_item(item_id):
    from . import bookmarks
    return ok(items=bookmarks.for_item(item_id))


@app.delete("/api/bookmarks/<item_id>/<bm_id>")
def bookmarks_remove(item_id, bm_id):
    from . import bookmarks
    return ok(removed=bookmarks.remove(item_id, bm_id))


@app.get("/api/arrivals")
def arrivals():
    """Downloads that finished but Jellyfin hasn't imported yet (playable now), and ones still downloading."""
    arr = getattr(APP, "arrivals", None)
    return ok(**(arr.list() if arr else {"ready": [], "coming": []}))


@app.post("/api/arrivals/play")
def arrival_play():
    arr = getattr(APP, "arrivals", None)
    card = arr.get((request.get_json(force=True) or {}).get("key", "")) if arr else None
    if not card or not card.get("path"):
        return fail("That download isn't ready yet", 404)
    if card.get("jellyfin_id"):  # Jellyfin caught up in the meantime: play the real entry
        threading.Thread(target=APP.player.play, args=(card["jellyfin_id"],), daemon=True).start()
    else:
        threading.Thread(target=APP.player.play_file, args=(card,), daemon=True).start()
    return ok()


@app.post("/api/played/<item_id>")
def played(item_id):
    on = bool((request.get_json(force=True) or {}).get("played", True))
    if not catalog().set_played(item_id, on):  # kept here, synced to Jellyfin in the background
        if not jf_ready():
            return fail("That title isn't in the library on this PC", 404)
        APP.jf.mark(item_id, on)
    return ok()


@app.post("/api/favorite/<item_id>")
def favorite(item_id):
    on = bool((request.get_json(force=True) or {}).get("on", True))
    if not catalog().set_favorite(item_id, on):
        if not jf_ready():
            return fail("That title isn't in the library on this PC", 404)
        APP.jf.call("POST" if on else "DELETE", f"/UserFavoriteItems/{item_id}", params={"userId": APP.jf.user})
    return ok()


# ------------------------------------------------------------------ requests (Seerr)
@app.get("/api/requests/discover")
def req_discover():
    if not APP.seerr.ready:
        return fail("Seerr isn't connected - add it in Settings", 409)
    return ok(**cached("discover", 1800, APP.seerr.discover))


@app.get("/api/requests/search")
def req_search():
    return ok(items=APP.seerr.search(request.args.get("q", "")))


@app.get("/api/requests/details/<kind>/<int:tmdb>")
def req_details(kind, tmdb):
    return ok(item=APP.seerr.details(kind, tmdb))


@app.get("/api/requests/options/<kind>")
def req_options(kind):
    prefs = settings.load()["request_prefs"]
    return ok(servers=cached("opts-" + kind, 600, lambda: APP.seerr.options(kind)), prefs=prefs)


@app.post("/api/requests")
def req_send():
    b = request.get_json(force=True) or {}
    # The page sends through POST /api/storage/request; this older route applies the same storage rule.
    try:
        from . import storage
        fit = storage.estimate(APP, b["type"], b["id"], b.get("profile"), b.get("seasons", "all"), b.get("server"),
                               b.get("is4k"))["fit"]
    except Exception as e:
        print("storage estimate unavailable:", type(e).__name__, e)
        fit = {}
    if fit.get("blocked"):
        return fail(fit.get("message") or "Not enough space for this request", 409)
    APP.seerr.request(b["type"], b["id"], profile=b.get("profile"), server=b.get("server"),
                      seasons=b.get("seasons", "all"), is4k=b.get("is4k", False), root_folder=b.get("root"))
    pref_key = "anime" if b.get("anime") else b["type"]
    prefs = settings.load()["request_prefs"]
    prefs[pref_key] = {k: b.get(k) for k in ("profile", "server", "seasons", "is4k", "season_mode") if k in b}
    settings.save({"request_prefs": prefs})
    _cache.pop("discover", None)
    return ok(message="Requested - it'll show up in your library once it downloads")


@app.get("/api/requests/mine")
def req_mine():
    return ok(items=APP.seerr.my_requests())


# ------------------------------------------------------------------ AI (optional)
@app.get("/api/ai/welcome")
def ai_welcome():
    llm = ai.available().get("llm", True)
    if llm and (APP.player.playing or not jf_ready()):
        return ok(text=None)  # never while something plays: the local model shares the GPU (Classic needs neither)
    return ok(text=cached("welcome", 3 * 3600, lambda: ai.welcome(APP), retry=600))


@app.get("/api/ai/picks")
def ai_picks():
    llm = ai.available().get("llm", True)
    if llm and (APP.player.playing or not jf_ready()):
        return ok(items=[])
    res = cached("picks", 12 * 3600, lambda: (lambda r: r if r.get("items") else None)(ai.picks(APP)), retry=600)
    row = _late_row({"items": (res or {}).get("items") or []})
    return ok(items=row["items"] if row else [])


# ------------------------------------------------------------------ parts that live in their own modules
def _register(label, get_bp):
    try:
        app.register_blueprint(get_bp())
    except Exception as e:  # an optional part that fails to import must never take the UI down
        print(f"{label} unavailable:", type(e).__name__, e)


def _details_bp():
    from .details import bp
    return bp


def _remote_bp():
    from .remote import desk
    return desk


def _index_bp():
    from .index import blueprint
    return blueprint


def _assistant_bp():
    from .assistant import bp
    return bp


_register("details", _details_bp)
_register("phone remote", _remote_bp)
_register("media index", _index_bp)
_register("assistant", _assistant_bp)


def _share_bp():
    from .share import bp
    return bp


_register("share", _share_bp)


def _ai_setup_bp():
    from .ai_setup import bp
    return bp


_register("built-in AI", _ai_setup_bp)


def _taste_bp():
    from .taste_api import bp
    return bp


_register("taste", _taste_bp)  # also starts the job that learns from finished plays


def _storage_bp():
    from .storage_api import bp
    return bp


_register("storage", _storage_bp)


def _stack_bp():
    from .stack_api import bp
    return bp


_register("your stack", _stack_bp)  # the Media Hub's dashboard inside Marquee (reads the hub; no background work)


def _update_bp():
    from .updater import bp
    return bp


_register("updates", _update_bp)  # update checks, download, restart to update (marquee.updater)


def serve(port):
    from waitress import serve as wserve
    try:
        catalog()  # load the local library and start syncing before the first page asks
    except Exception as e:
        print("catalog unavailable:", e)
    wserve(app, host="127.0.0.1", port=port, threads=12, _quiet=True)
