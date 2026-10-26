"""/api/index/... : the media index's JSON API (a Flask blueprint)."""
import threading

from flask import Blueprint, Response, jsonify, request

from . import analysis, deps, store
from .worker import PRIO_NOW, PRIO_SIBLING, Worker, load_config, save_config

bp = Blueprint("marquee_index", __name__)
THUMBS = deps.HOME / "index" / "thumbs"
TICKS = 10_000_000


def _app():
    from .. import server
    return server.APP


worker = Worker(_app)


def ok(**kw):
    return jsonify(ok=True, **kw)


def not_available():
    return jsonify(ok=True, available=False, **{k: v for k, v in deps.availability().items() if k != "available"})


def _norm(item_id):
    item_id = (item_id or "").lower().replace("-", "")
    store.path(item_id)
    return item_id


def _ready(item_id, queue=True):
    """The finished index for an item, or a response saying why there isn't one (queues it if asked)."""
    if not deps.availability()["available"]:
        return None, not_available()
    d = store.load(item_id)
    if d is not None and d["meta"].get("complete"):
        return d, None
    st = worker.state(item_id)
    if queue and st["state"] in ("none", "partial"):
        worker.queue(item_id, PRIO_NOW)
        st = worker.state(item_id)
    return None, jsonify(ok=True, available=True, indexed=False, **{"index": st})


def _with_sens(d):
    """Recompute sensitive scores if the prompt set changed since the title was indexed."""
    if d["meta"].get("sens_version") != analysis.SENS_VERSION and len(d["emb"]):
        d["sens"] = analysis.sensitive_scores(d["emb"], worker.get_clip())
        d["sens_cats"] = list(analysis.SENSITIVE)
        d["meta"]["sens_version"] = analysis.SENS_VERSION
        store.save(d["meta"]["item"], d)
    return d


def _thumb(item_id, t):
    return f"/api/index/{item_id}/thumb?t={t:.2f}"


# ---------------------------------------------------------------- status / setup
@bp.get("/api/index/status")
def status():
    a = deps.availability()
    return ok(**a, worker=worker.status() if a["available"] else None, config=load_config())


@bp.post("/api/index/install")
def install():
    if not deps.INSTALL["running"]:
        threading.Thread(target=deps.install, daemon=True).start()
    a = deps.availability()
    return ok(**{**a, "installing": {**a["installing"], "running": True}})


@bp.route("/api/index/config", methods=["GET", "POST"])
def config():
    if request.method == "POST":
        c = save_config(request.get_json(force=True) or {})
        if c["library"]:
            worker.start()
        return ok(config=c)
    return ok(config=load_config())


# ---------------------------------------------------------------- per item
@bp.post("/api/index/<item_id>")
def queue(item_id):
    item_id = _norm(item_id)
    if not deps.availability()["available"]:
        return not_available()
    b = request.get_json(silent=True) or {}
    worker.queue(item_id, PRIO_NOW, urgent=bool(b.get("urgent")))
    return ok(available=True, index=worker.state(item_id))


@bp.get("/api/index/<item_id>")
def item_state(item_id):
    return ok(available=deps.availability()["available"], index=worker.state(_norm(item_id)))


@bp.delete("/api/index/<item_id>")
def forget(item_id):
    item_id = _norm(item_id)
    worker.cancel(item_id)
    store.delete(item_id)
    return ok()


@bp.get("/api/index/<item_id>/search")
def search(item_id):
    item_id = _norm(item_id)
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify(ok=False, message="q is required"), 400
    d, resp = _ready(item_id)
    if resp:
        return resp
    k = max(1, min(int(request.args.get("k", 8)), 50))
    hits = analysis.search(d, worker.get_clip().query(q), k=k)
    for h in hits:
        h["thumb"] = _thumb(item_id, h["t"])
    return ok(available=True, indexed=True, q=q, results=hits)


@bp.get("/api/index/<item_id>/sensitive")
def sensitive(item_id):
    item_id = _norm(item_id)
    d, resp = _ready(item_id)
    if resp:
        return resp
    d = _with_sens(d)
    th = {c: float(request.args[c]) for c in analysis.SENSITIVE if c in request.args}
    cfg = load_config()["sensitive"]
    ranges = analysis.sensitive_ranges(d, th)
    for r in ranges:
        r["action"] = cfg.get(r["category"], "off")
    return ok(available=True, indexed=True, ranges=ranges, settings=cfg, thresholds={**analysis.THRESHOLDS, **th})


@bp.get("/api/index/<item_id>/chapters")
def chapters(item_id):
    item_id = _norm(item_id)
    d, resp = _ready(item_id)
    if resp:
        return resp
    k = request.args.get("k", type=int)
    ch = analysis.chapters(d, k=max(2, min(k, 40)) if k else None)
    for c in ch:
        c["thumb"] = _thumb(item_id, c["thumb_t"])
    return ok(available=True, indexed=True, chapters=ch, duration=d["meta"].get("duration"))


@bp.get("/api/index/<item_id>/intro")
def intro(item_id):
    """Intro and outro (credits) of an episode: Jellyfin's media segments when it has them, otherwise the
    stretch near the start/end whose frames also appear in other episodes of the same season."""
    item_id = _norm(item_id)
    app = _app()
    out = {}
    try:
        for s in app.jf.segments(item_id):
            kind = {"Intro": "intro", "Outro": "outro"}.get(s.get("Type"))
            if kind:
                out[kind] = {"start": s["StartTicks"] / TICKS, "end": s["EndTicks"] / TICKS, "source": "jellyfin"}
    except Exception:
        pass
    if "intro" in out and "outro" in out:
        return ok(available=True, **out)
    d, resp = _ready(item_id)
    if resp:
        return resp
    meta = d["meta"]
    if meta.get("Type") != "Episode" or not meta.get("SeasonId"):
        return ok(available=True, indexed=True, **out, note="not an episode")
    eps = app.jf.episodes(meta["SeriesId"], meta["SeasonId"])
    ids = [e["Id"] for e in eps]
    me = next((i for i, x in enumerate(ids) if x.replace("-", "").lower() == item_id), None)
    if me is None:
        return ok(available=True, indexed=True, **out, note="episode not found in its season")
    # The nearest episodes first (an opening usually stays the same for a cour).
    near = sorted((i for i in range(len(ids)) if i != me), key=lambda i: (abs(i - me), i))[:3]
    others, pending = [], []
    for i in near:
        o = store.load(_norm(ids[i]))
        if o is not None and o["meta"].get("complete"):
            others.append(o)
        else:
            pending.append(ids[i])
    if len(others) < 2:
        for i in pending[:2 - len(others)]:
            worker.queue(_norm(i), PRIO_SIBLING)
    for kind in ("intro", "outro"):
        if kind not in out and others:
            r = analysis.recurring(d, others, kind)
            if r:
                out[kind] = {**r, "source": "index"}
    return ok(available=True, indexed=True, **out, compared=len(others),
              pending=[_norm(p) for p in pending] if len(others) < 2 else [])


@bp.get("/api/index/<item_id>/thumb")
def thumb(item_id):
    item_id = _norm(item_id)
    t = max(0.0, float(request.args.get("t", 0)))
    w = max(64, min(int(request.args.get("w", 320)), 1280))
    f = THUMBS / item_id / f"{int(t * 100)}_{w}.jpg"
    if not f.is_file():
        if not deps.find_ffmpeg():
            return not_available()
        src, info = worker.source(item_id)
        if info.get("duration"):
            t = min(t, max(0.0, info["duration"] - 1.0))
        data = worker.get_decoder().frame_jpeg(src, info, t, w)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    r = Response(f.read_bytes(), mimetype="image/jpeg")
    r.headers["Cache-Control"] = "max-age=86400"
    return r
