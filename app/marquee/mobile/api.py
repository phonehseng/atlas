"""The phone app's API (/r/api/*) and media routes (/r/s/*), registered on the phone server by marquee.remote.

Everything is read from the local catalog, so browsing and playing keep working while Jellyfin is down. The guard in
marquee.remote has already checked the device cookie (flask.g.device) for /r/api and /r/img; media routes carry
their own per-session key in the path (iOS's native HLS player doesn't always send cookies).
"""
import datetime
import os
import random
import re
import secrets
import threading
import time

from flask import Blueprint, Response, g, jsonify, request

from .. import __version__, settings
from . import auth, media, stream, users

bp = Blueprint("mobile", __name__)
ID_RE = re.compile(r"[A-Za-z0-9:._-]{1,120}")
CARD_KEYS = ("id", "type", "name", "series", "series_id", "season", "episode", "year", "rating", "age", "runtime",
             "position", "progress", "played", "unplayed", "poster", "backdrop", "thumb", "logo", "genres", "anime",
             "added", "overview", "favorite")
APP = None          # set by marquee.remote.start()
_noise = os.urandom(1 << 20)


def ok(**kw):
    return jsonify(ok=True, **kw)


def fail(msg, code=400, **kw):
    return jsonify(ok=False, message=str(msg), **kw), code


def cat():
    from .. import server
    return server.catalog()


def _owner():
    return auth.is_owner(getattr(g, "device", None))


def _uid():
    """The Jellyfin user id of a signed-in friend, or None for the owner's devices."""
    dev = getattr(g, "device", None)
    return None if auth.is_owner(dev) else (dev.get("user") or {}).get("id")


def _check_id(item_id):
    if not ID_RE.fullmatch(item_id or ""):
        raise ValueError("bad id")
    return item_id


# ------------------------------------------------------------------ cards
def img(url):
    """A catalog artwork URL (/img/...) as one this server answers (/r/img/...)."""
    return ("/r" + url) if isinstance(url, str) and url.startswith("/img/") else None


def card(c, uid=None):
    out = {k: c.get(k) for k in CARD_KEYS}
    if uid:
        users.overlay(uid, out)
    for k in ("poster", "backdrop", "thumb", "logo"):
        out[k] = img(out.get(k))
    if out.get("overview") and len(out["overview"]) > 320:
        out["overview"] = out["overview"][:317].rsplit(" ", 1)[0] + "…"
    return out


def _visible(rows, uid):
    if not uid:
        return rows
    allow = users.allowed(uid)
    if allow is None:
        return []
    return [r for r in rows if r and r["id"] in allow and not r["id"].startswith("local:")]


def _cards(rows, uid=None):
    c = cat()
    return [card(c.card(r), uid) for r in _visible([r for r in rows if r], uid)]


def _may_see(item_id, uid):
    if not uid:
        return True
    allow = users.allowed(uid)
    return bool(allow) and item_id in allow


def _friend_refresh():
    uid = _uid()
    if uid:
        users.refresh(APP, uid, auth.jf_token(g.device), wait=users.allowed(uid) is None)
    return uid


# ------------------------------------------------------------------ who am I
@bp.get("/r/api/me")
def me():
    dev = g.device
    st = settings.load()
    return ok(device=auth.public(dev), owner=_owner(), user=(dev.get("user") or {}).get("name") or st.get("user_name"),
              quality=st.get("mobile_quality") or "auto", jellyfin=bool(APP.jf.configured),
              offline=bool(APP.jf.configured and not APP.jf.online), pc=os.environ.get("COMPUTERNAME", "PC").title(),
              qualities=[{"key": "auto", "label": "Auto"}, {"key": "original", "label": "Original"}] +
              [{"key": k, "label": f"{q['label']} · {q['bitrate'] / 1e6:g} Mbps"} for k, q in stream.QUALITIES.items()])


@bp.post("/r/api/signout")
def signout():
    stream.stop_device(g.device["id"])
    auth.revoke(g.device["id"])
    r = ok()
    r.delete_cookie(auth.COOKIE, path="/r")
    return r


@bp.get("/r/api/speed")
def speed():
    """A burst of incompressible bytes for the bandwidth probe (Auto quality)."""
    kb = max(16, min(int(request.args.get("kb", 1024)), 16384))
    n = kb * 1024
    data = (_noise * (n // len(_noise) + 1))[:n]
    return Response(data, mimetype="application/octet-stream",
                    headers={"Cache-Control": "no-store", "Content-Length": str(n)})


# ------------------------------------------------------------------ browsing
def _daily_sample(rows, n, salt):
    rnd = random.Random(f"{datetime.date.today().isoformat()}:{salt}")
    rows = list(rows)
    rnd.shuffle(rows)
    return rows[:n]


@bp.get("/r/api/home")
def home():
    uid = _friend_refresh()
    c = cat()
    rows = []

    def add(key, title, items, style="poster", more=None):
        items = [x for x in items if x]
        if items:
            rows.append({"key": key, "title": title, "style": style, "items": items, "more": more})

    with c.lock:
        if uid:
            add("continue", "Continue watching", _cards([c.items.get(i) for i in users.resume_ids(uid)], uid), "wide")
        else:
            add("continue", "Continue watching", _cards(c.resume(20)), "wide")
            add("nextup", "Next up", _cards(c.next_up(20)), "wide")
            try:
                add("mylist", "My List", [card(x[2]) for x in c.mylist() if x[2]][:30])
            except Exception as e:
                print("mobile: my list unavailable:", e)
        libs = {r.get("library") for r in c.items.values()}
        recent = []
        for lib in ("movies", "shows", "anime"):
            if lib in libs:
                recent += [(r.get("added") or 0, r) for r in c.latest(lib, 16)]
        recent.sort(key=lambda x: -x[0])
        add("recent", "Recently added", _cards([r for _, r in recent][:24], uid))
        for lib, title, types in (("movies", "Movies", "Movie"), ("shows", "Shows", "Series"),
                                  ("anime", "Anime", "Series,Movie")):
            if lib in libs:
                pool = _visible(c._of(types, lib), uid)
                add(lib, title, _cards(_daily_sample(pool, 30, lib), uid), more=lib)
        pool = [r for r in _visible(c._of("Movie,Series"), uid) if r.get("backdrop")]
        hero = _cards(_daily_sample(pool, 6, "hero"), uid)
    return ok(rows=rows, hero=hero, offline=bool(APP.jf.configured and not APP.jf.online))


@bp.get("/r/api/library/<kind>")
def library(kind):
    if kind not in ("movies", "shows", "anime"):
        return fail("unknown library", 404)
    uid = _friend_refresh()
    c = cat()
    types = {"movies": "Movie", "shows": "Series", "anime": "Series,Movie"}[kind]
    with c.lock:
        from ..catalog import norm
        rows = sorted(c._of(types, kind), key=lambda r: norm(r.get("sort_name") or r.get("name")))
        return ok(items=_cards(rows, uid), title={"movies": "Movies", "shows": "Shows", "anime": "Anime"}[kind])


@bp.get("/r/api/search")
def search():
    q = request.args.get("q", "").strip()[:80]
    if not q:
        return ok(items=[])
    uid = _friend_refresh()
    c = cat()
    hits = c.search(q, limit=60)
    if uid:
        allow = users.allowed(uid) or set()
        hits = [users.overlay(uid, h) for h in hits if h["id"] in allow]
    return ok(items=[card(h) for h in hits[:40]])


@bp.get("/r/api/item/<item_id>")
def item(item_id):
    _check_id(item_id)
    uid = _friend_refresh()
    if not _may_see(item_id, uid):
        return fail("That title isn't available", 404)
    c = cat()
    d = c.detail(item_id)
    if d is None:
        return fail("That title isn't in the library on this PC", 404)
    out = card(d, uid)
    out.update({k: d.get(k) for k in ("tagline", "resolution", "hdr", "studios", "critic", "available", "type")})
    m = d.get("media") or {}
    h = max(m.get("height") or 0, int((m.get("width") or 0) * 9 / 16))   # a 1920x800 film is still 1080p
    out["quality"] = " ".join(x for x in ("4K" if h >= 1600 else "1080p" if h >= 1000 else "720p" if h >= 700 else "",
                                          "Dolby Vision" if m.get("dv") else "HDR" if m.get("hdr") else "",
                                          "Atmos" if m.get("atmos") else "") if x)
    out["people"] = [{"name": p.get("name"), "role": p.get("role"), "type": p.get("type"), "photo": img(p.get("photo"))}
                     for p in d.get("people") or []][:24]
    out["similar"] = [card(x, uid) for x in d.get("similar") or [] if _may_see(x["id"], uid)][:16]
    if d["type"] == "Series":
        out["seasons"] = [{**s, "poster": img(s.get("poster"))} for s in d.get("seasons") or []]
        nu = d.get("next_up")
        if uid:
            nu = None
            for e in c.eps.get(item_id, []):
                st = users.state(uid, e["id"])
                if not st.get("played"):
                    nu = c.card(e)
                    break
        out["next_up"] = card(nu, uid) if nu else None
    if _owner():
        try:
            out["mylist"] = c.mylist_key(item_id=d["id"]) in set(c.mylist_keys())
        except Exception:
            out["mylist"] = None
    return ok(item=out)


@bp.get("/r/api/episodes/<series>/<season>")
def episodes(series, season):
    _check_id(series)
    _check_id(season)
    uid = _friend_refresh()
    if not _may_see(series, uid):
        return fail("That title isn't available", 404)
    items = cat().episodes(series, season)
    out = []
    for e in items:
        if uid and not _may_see(e["id"], uid):
            continue
        x = card(e, uid)
        x["quality"], x["available"] = e.get("quality"), e.get("available")
        out.append(x)
    return ok(items=out)


@bp.post("/r/api/mylist")
def mylist():
    if not _owner():
        return fail("My List belongs to the owner's account", 403)
    b = request.get_json(force=True, silent=True) or {}
    item_id = _check_id(str(b.get("id", "")))
    c = cat()
    key = c.mylist_key(item_id=item_id)
    if not key:
        return fail("That title isn't in the library", 404)
    (c.mylist_add if b.get("on", True) else c.mylist_remove)(key)
    return ok(on=bool(b.get("on", True)))


@bp.get("/r/img/<path:p>")
def image(p):
    if ".." in p:
        return Response(status=404)
    url = "/img/" + p + (("?" + request.query_string.decode("latin-1")) if request.query_string else "")
    if not (url.startswith("/img/Items/") or url.startswith("/img/tmdb/")):
        return Response(status=404)
    path = cat().images.get_url(url)
    if not path:
        return Response(status=404)
    with open(path, "rb") as f:
        data = f.read()
    return Response(data, mimetype=cat().images.mimetype(path),
                    headers={"Cache-Control": "private, max-age=604800"})


# ------------------------------------------------------------------ playback
def _playable(item_id, uid):
    _check_id(item_id)
    if not _may_see(item_id, uid):
        raise PermissionError("That title isn't available")
    info = cat().lookup(item_id)
    if not info:
        raise LookupError("That title isn't in the library on this PC")
    if uid and not _may_see(info["id"], uid):
        raise PermissionError("That title isn't available")
    if not info.get("exists"):
        raise FileNotFoundError("That file isn't on this PC")
    return info


SEG_KIND = {"intro": "intro", "recap": "intro", "outro": "credits", "credits": "credits"}
CH_INTRO = ("intro", "opening", "op", "opening credits", "prologue")
CH_CREDITS = ("credits", "ending", "ed", "end credits", "outro", "closing credits")


def _segments(info, probe_info):
    out = []
    for s in info.get("segments") or []:
        kind = SEG_KIND.get((s.get("type") or "").lower())
        if kind and s.get("end", 0) > s.get("start", 0):
            out.append({"kind": kind, "start": s["start"], "end": s["end"]})
    if not out:
        dur = probe_info.get("duration") or 0
        for c in probe_info.get("chapters") or []:
            name = (c.get("title") or "").strip().lower()
            if name in CH_INTRO or name.startswith("opening"):
                out.append({"kind": "intro", "start": c["start"], "end": c["end"]})
            elif (name in CH_CREDITS or name.startswith("ending") or "credits" in name) and c["start"] > dur * 0.5:
                out.append({"kind": "credits", "start": c["start"], "end": c["end"]})
    return out


def _default_tracks(pinfo, anime):
    st = settings.load()
    sub_lang = (st.get("sub_lang") or "eng").lower()
    a = stream.pick_audio(pinfo, prefer_lang=(st.get("audio_lang_anime") or "jpn") if anime else None)
    sub = None
    texts = [s for s in pinfo["subs"] if s["kind"] == "text"]
    same = lambda s: (s.get("lang") or "").lower()[:2] == sub_lang[:2]  # noqa: E731
    if a and (a.get("lang") or "").lower()[:2] not in ("", sub_lang[:2]):
        full = [s for s in texts if same(s) and not s["forced"] and "sign" not in s["label"].lower()]
        sub = (full or [s for s in pinfo["subs"] if same(s) and not s["forced"]] or [None])[0]
    else:
        sub = next((s for s in texts if same(s) and s["forced"]), None)
    return (a["index"] if a else None), (sub["id"] if sub else None)


@bp.get("/r/api/play/<item_id>/info")
def play_info(item_id):
    uid = _friend_refresh()
    try:
        info = _playable(item_id, uid)
        p = media.probe(info["path"])["info"]
    except (PermissionError, LookupError, FileNotFoundError) as e:
        return fail(e, 404)
    c = cat()
    pos = info.get("position") or 0
    if uid:
        stt = users.state(uid, info["id"])
        pos = 0 if stt.get("played") else (stt.get("p") or 0)
    if pos and p["duration"] and pos > p["duration"] - 30:
        pos = 0
    nxt = None
    if info.get("next_episode_id") and _may_see(info["next_episode_id"], uid):
        r = c.get(info["next_episode_id"])
        if r:
            nxt = card(c.card(r), uid)
    audio, sub = _default_tracks(p, info.get("anime"))
    v = p.get("video") or {}
    return ok(id=info["id"], type=info["type"], title=info.get("series") or info.get("name"),
              name=info.get("name"), season=info.get("season"), episode=info.get("episode"),
              subtitle=(f"S{info.get('season') or 0}:E{info.get('episode') or 0} · {info.get('name') or ''}"
                        if info["type"] == "Episode" else str(info.get("year") or "")),
              poster=img(info.get("poster")), backdrop=img(info.get("backdrop")), thumb=img(info.get("thumb")),
              logo=img(info.get("logo")), duration=p["duration"], position=pos,
              audio=[{k: a[k] for k in ("index", "label", "lang", "codec", "channels")} for a in p["audio"]],
              subs=[{k: s.get(k) for k in ("id", "label", "lang", "kind", "forced")} for s in p["subs"]],
              default_audio=audio, default_sub=sub, segments=_segments(info, p), next=nxt,
              media={"container": p["container"], "codec": v.get("codec"), "height": v.get("height"),
                     "width": v.get("width"), "hdr": v.get("hdr"), "bits": v.get("bits"), "bitrate": p["bitrate"],
                     "dv": v.get("dv_profile")})


@bp.post("/r/api/play/<item_id>/start")
def play_start(item_id):
    uid = _friend_refresh()
    b = request.get_json(force=True, silent=True) or {}
    try:
        info = _playable(item_id, uid)
        p = media.probe(info["path"])["info"]
    except (PermissionError, LookupError, FileNotFoundError) as e:
        return fail(e, 404)
    caps = b.get("caps") or {}
    if not isinstance(caps, dict) or not isinstance(caps.get("audio", {}), dict):
        return fail("bad caps")
    quality = str(b.get("quality") or settings.load().get("mobile_quality") or "auto")
    if quality not in ("auto", "original", *stream.QUALITIES):
        quality = "auto"
    sub = next((s for s in p["subs"] if s["id"] == str(b.get("sub"))), None) if b.get("sub") not in (None, "") else None
    try:
        bandwidth = float(b.get("bandwidth") or 0) or None
        start = max(0.0, float(b.get("start") or 0))
    except (TypeError, ValueError):
        return fail("bad request")
    force = b.get("force") if b.get("force") in ("remux", "transcode") else None
    kf = media.keyframes(info["path"], p)
    plan = stream.decide(p, caps, quality=quality, audio=b.get("audio"), sub=sub, bandwidth=bandwidth, force=force,
                         prefer_lang=(settings.load().get("audio_lang_anime") or "jpn") if info.get("anime") else None,
                         keyframes=kf)
    title = info.get("series") or info.get("name")
    if info["type"] == "Episode":
        title = f"{title} S{info.get('season') or 0}:E{info.get('episode') or 0}"
    try:
        s = stream.create({"id": info["id"], "title": title, "poster": img(info.get("poster"))}, info["path"], p, plan,
                          g.device, kf, start)
    except OverflowError as e:
        return fail(e, 429)
    base = f"/r/s/{s.id}/{s.key}/"
    url = base + ("file." + (p["container"] if p["container"] in stream.DIRECT_CONTAINERS | {"mkv", "webm"} else "mp4")
                  if s.mode == "direct" else "index.m3u8")
    subs = None
    if sub and sub["kind"] == "text":
        subs = base + f"sub/{sub['id']}.vtt"
        media.subtitle_vtt(info["path"], sub)
    return ok(session=s.id, mode=s.mode, url=url, kind="file" if s.mode == "direct" else "hls", base=base,
              label=plan.get("label"), quality=plan.get("quality"), reason=plan.get("reason"),
              height=plan.get("height"), bitrate=plan.get("bitrate"), sub_url=subs, burned=plan.get("burn") is not None,
              audio=plan.get("audio"), encoder=plan.get("encoder"))


@bp.post("/r/api/progress")
def progress():
    b = request.get_json(force=True, silent=True) or {}
    item_id = _check_id(str(b.get("id", "")))
    try:
        pos = float(b.get("position") or 0)
    except (TypeError, ValueError):
        return fail("bad position")
    uid = _uid()
    if not _may_see(item_id, uid):
        return fail("That title isn't available", 404)
    c = cat()
    info = c.lookup(item_id)
    if not info:
        return fail("That title isn't in the library on this PC", 404)
    runtime = info.get("runtime") or float(b.get("duration") or 0)
    played = True if b.get("ended") else None
    if uid:
        users.save(APP, uid, auth.jf_token(g.device), info["id"], pos, runtime, played)
    else:
        if played is None and not info.get("runtime") and runtime:
            played = pos >= runtime * 0.9  # a local file the catalog has no runtime for
        c.save_position(info["id"], pos, played=played)
    return ok()


@bp.post("/r/api/stream/<sid>/stop")
def stream_stop(sid):
    s = stream.get(sid)
    if s and s.device == g.device["id"]:
        stream.stop(sid)
    return ok()


@bp.get("/r/api/stream/<sid>/stats")
def stream_stats(sid):
    s = stream.get(sid)
    if not s or s.device != g.device["id"]:
        return fail("no such stream", 404)
    return ok(stats=s.stats())


# ------------------------------------------------------------------ media (per-session key in the path)
def _session(sid, key):
    # an unknown or ended session is just gone (the PC restarted, the player kept an old URL): no rate-limit strike,
    # since session ids and keys are random and can't usefully be guessed
    return stream.get(sid, key)


def send_file_range(path, mimetype, download_name=None):
    """A file with HTTP Range support (206), streamed by the server's own I/O loop in 256 KB reads (never read whole
    into memory, no worker thread held while the phone downloads)."""
    size = os.path.getsize(path)
    start, end = 0, size - 1
    status = 200
    rng = request.headers.get("Range", "")
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip()) if rng else None
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
        else:  # last N bytes
            start = max(0, size - int(m.group(2)))
        if start >= size or start > end:
            return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206
    length = end - start + 1
    f = open(path, "rb")
    f.seek(start)
    wrapper = request.environ.get("wsgi.file_wrapper")
    body = wrapper(f, 256 * 1024) if wrapper else _chunks(f, length)
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length), "Cache-Control": "private, max-age=3600"}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if download_name:
        safe = re.sub(r'[\\/:*?"<>|]+', " ", download_name).strip() or "video.mp4"
        headers["Content-Disposition"] = f'attachment; filename="{safe.encode("ascii", "replace").decode()}"'
    r = Response(body, status=status, mimetype=mimetype, headers=headers, direct_passthrough=True)
    return r


def _chunks(f, length):
    try:
        left = length
        while left > 0:
            b = f.read(min(1 << 20, left))
            if not b:
                break
            left -= len(b)
            yield b
    finally:
        f.close()


MIME = {"mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime", "mkv": "video/x-matroska",
        "webm": "video/webm"}


@bp.get("/r/s/<sid>/<key>/file.<ext>")
def media_file(sid, key, ext):
    s = _session(sid, key)
    if not s or s.mode != "direct":
        return Response(status=404)
    s.last_access = time.time()
    return send_file_range(s.path, MIME.get(s.info.get("container"), "video/mp4"))


@bp.get("/r/s/<sid>/<key>/index.m3u8")
def media_playlist(sid, key):
    s = _session(sid, key)
    if not s or s.mode == "direct":
        return Response(status=404)
    s.last_access = time.time()
    return Response(s.playlist(), mimetype="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})


@bp.get("/r/s/<sid>/<key>/init.mp4")
def media_init(sid, key):
    s = _session(sid, key)
    if not s or s.mode == "direct":
        return Response(status=404)
    try:
        path = s.get_init()
    except Exception as e:
        return Response(str(e), status=503, mimetype="text/plain")
    return Response(path.read_bytes(), mimetype="video/mp4", headers={"Cache-Control": "no-store"})


@bp.get("/r/s/<sid>/<key>/seg/<int:n>.m4s")
def media_segment(sid, key, n):
    s = _session(sid, key)
    if not s or s.mode == "direct":
        return Response(status=404)
    try:
        units = s.get_segment(n)
    except KeyError:
        return Response(status=404)
    except Exception as e:
        return Response(str(e), status=503, mimetype="text/plain")
    parts = []
    total = 0
    for p in units:
        try:
            off = media.offset_cached(p)
            size = os.path.getsize(p) - off
        except OSError:
            return Response("segment went away", status=503, mimetype="text/plain")
        parts.append((p, off, size))
        total += size
    if len(parts) == 1:
        p, off, size = parts[0]
        f = open(p, "rb")
        f.seek(off)
        wrapper = request.environ.get("wsgi.file_wrapper")
        body = wrapper(f, 256 * 1024) if wrapper else _chunks(f, size)
    else:
        def gen():
            for p, off, size in parts:
                with open(p, "rb") as f:
                    f.seek(off)
                    left = size
                    while left > 0:
                        b = f.read(min(1 << 20, left))
                        if not b:
                            break
                        left -= len(b)
                        yield b
        body = gen()
    return Response(body, mimetype="video/mp4", direct_passthrough=True,
                    headers={"Content-Length": str(total), "Cache-Control": "no-store"})


@bp.get("/r/s/<sid>/<key>/sub/<sub_id>.vtt")
def media_sub(sid, key, sub_id):
    s = _session(sid, key)
    if not s:
        return Response(status=404)
    sub = next((x for x in s.info["subs"] if x["id"] == sub_id and x["kind"] == "text"), None)
    if not sub:
        return Response(status=404)
    state, path = media.subtitle_vtt(s.path, sub)
    if state == "pending":
        return jsonify(ok=False, pending=True, message="Getting the subtitles ready"), 202
    if state != "ready":
        return Response(status=404)
    return Response(path.read_bytes(), mimetype="text/vtt", headers={"Cache-Control": "private, max-age=3600"})


# ------------------------------------------------------------------ downloads for offline
def _user_key():
    return _uid() or "owner"


@bp.get("/r/api/downloads")
def downloads_list():
    return ok(items=[_dl_public(d) for d in stream.downloads.list(_user_key())])


def _dl_public(d):
    out = {k: d.get(k) for k in ("id", "item_id", "title", "label", "state", "progress", "size", "created", "message",
                                 "poster")}
    out["url"] = f"/r/api/downloads/{d['id']}/file" if d.get("state") == "ready" else None
    return out


@bp.post("/r/api/downloads")
def downloads_add():
    b = request.get_json(force=True, silent=True) or {}
    uid = _uid()
    try:
        info = _playable(str(b.get("id", "")), uid)
        p = media.probe(info["path"])["info"]
    except (PermissionError, LookupError, FileNotFoundError, ValueError) as e:
        return fail(e, 404)
    title = info.get("series") or info.get("name")
    if info["type"] == "Episode":
        title = f"{title} S{info.get('season') or 0:02d}E{info.get('episode') or 0:02d}"
    q = str(b.get("quality") or "720")
    d = stream.downloads.add({"id": info["id"], "title": title, "poster": img(info.get("poster"))}, info["path"], p,
                             _user_key(), audio=b.get("audio"), quality=q if q in stream.QUALITIES else "720")
    return ok(item=_dl_public(d))


@bp.delete("/r/api/downloads/<did>")
def downloads_remove(did):
    return ok(removed=stream.downloads.remove(did, _user_key()))


@bp.get("/r/api/downloads/<did>/file")
def downloads_file(did):
    d = stream.downloads.get(did)
    if not d or d["user"] != _user_key() or d["state"] != "ready":
        return Response(status=404)
    path = stream.downloads.file(did)
    if not path.exists():
        return Response(status=404)
    return send_file_range(str(path), "video/mp4", download_name=f"{d['title']} ({d['label']}).mp4")


# ------------------------------------------------------------------ Jellyfin sign-in (friends)
def jellyfin_login(username, password, ua, ip):
    """Check a Jellyfin username/password once and pair this device to that account. The password is never kept.
    Returns (cookie, device) or raises ValueError with a message for the page."""
    import requests
    jf = APP.jf
    if not jf.base:
        raise ValueError("Jellyfin isn't set up on this PC")
    try:
        r = requests.post(jf.base + "/Users/AuthenticateByName", json={"Username": username, "Pw": password},
                          timeout=15, headers={"Authorization": f'MediaBrowser Client="Marquee Mobile", '
                                                                f'Device="{auth.device_name(ua)}", '
                                                                f'DeviceId="marquee-m-{secrets.token_hex(6)}", '
                                                                f'Version="{__version__}"'})
    except requests.RequestException:
        raise ValueError("Jellyfin isn't answering right now")
    if r.status_code in (401, 403):
        raise PermissionError("Wrong username or password")
    if r.status_code != 200:
        raise ValueError(f"Jellyfin said {r.status_code}")
    d = r.json()
    user = d.get("User") or {}
    owner_id = (os.environ.get("MARQUEE_DEV_USER") or settings.load().get("user_id") or "").replace("-", "")
    if (user.get("Id") or "").replace("-", "") == owner_id:
        return auth.create_device({"kind": "owner", "name": user.get("Name")}, ua, ip)
    cookie, dev = auth.create_device({"kind": "jellyfin", "id": user.get("Id"), "name": user.get("Name")}, ua, ip,
                                     jf_token=d.get("AccessToken"))
    users.refresh(APP, user.get("Id"), d.get("AccessToken"))
    return cookie, dev


def warm(app):
    """Probe the encoders in the background when the server starts (the first stream shouldn't wait for it)."""
    threading.Thread(target=lambda: (stream.encoders(), stream._audio_encoders()), daemon=True,
                     name="mobile-encoders").start()
