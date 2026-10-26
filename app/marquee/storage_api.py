"""Routes for storage, request quality, cleanup, deleting by hand, and smart season requests (marquee.storage,
marquee.smart). Register in server.py: `from .storage_api import bp as storage_bp; app.register_blueprint(storage_bp)`.
Every answer is {ok, ...}; errors are {ok: false, message} (show `message` as is)."""
import urllib.parse

from flask import Blueprint, jsonify, request

from . import smart, storage

bp = Blueprint("marquee_storage", __name__)


def APP():
    from . import server
    try:
        server.catalog()  # the catalog exists (and is APP.catalog) before anything here reads it
    except Exception:
        pass
    return server.APP


def ok(**kw):
    return jsonify(ok=True, **kw)


def fail(msg, code=400):
    return jsonify(ok=False, message=str(msg)), code


def _bool(v):
    return str(v).lower() in ("1", "true", "yes", "on")


@bp.before_request
def _guard():
    origin = request.headers.get("Origin")
    if origin and urllib.parse.urlsplit(origin).netloc != request.host:
        return fail("forbidden", 403)
    if request.method in ("POST", "DELETE") and request.content_length and not request.is_json:
        return fail("send JSON", 415)


@bp.errorhandler(PermissionError)
def _denied(e):
    return fail(e, 409)


@bp.errorhandler(LookupError)
def _missing(e):
    return fail(e, 404)


@bp.errorhandler(ValueError)
def _bad(e):
    return fail(e, 400)


# ------------------------------------------------------------------ dashboard, plan, log
@bp.get("/api/storage")
def route_status():
    if _bool(request.args.get("refresh")):
        storage.forget()
    return ok(**storage.status(APP()))


@bp.get("/api/storage/plan")
def route_plan():
    """What would be removed and why (nothing changes). ?run=1 also shows the job's dry-run actions."""
    app = APP()
    p = storage.plan(app)
    out = {k: p[k] for k in ("items", "could", "skipped", "can_delete", "why_not", "totals", "options", "users")}
    if _bool(request.args.get("run")):
        out["run"] = storage.run(app, dry_run=True)
    return ok(**out)


@bp.post("/api/storage/run")
def route_run():
    """Run the cleanup now. Dry run unless {"dry_run": false}."""
    b = request.get_json(silent=True) or {}
    return ok(**storage.run(APP(), dry_run=b.get("dry_run", True) is not False))


@bp.get("/api/storage/log")
def route_log():
    return ok(items=storage.read_log(int(request.args.get("limit") or 100)))


@bp.get("/api/storage/leaving")
def route_leaving():
    return ok(items=storage.leaving_soon(APP(), int(request.args.get("days") or 7)))


@bp.post("/api/storage/keep/<path:item_id>")
def route_keep(item_id):
    b = request.get_json(silent=True) or {}
    ids = storage.keep(item_id, b.get("keep", True) is not False)
    storage.forget("plan")
    return ok(keep_ids=ids)


@bp.post("/api/storage/settings")
def route_settings():
    return ok(settings=storage.save_settings(request.get_json(force=True) or {}))


# ------------------------------------------------------------------ request quality
@bp.get("/api/storage/estimate")
def route_estimate():
    a = request.args
    is4k = a.get("is4k")
    return ok(**storage.estimate(APP(), a.get("kind") or "movie", int(a["tmdb"]), a.get("profile"),
                                 a.get("seasons") or "all", a.get("server"),
                                 None if is4k in (None, "") else _bool(is4k)))


@bp.get("/api/requests/options/<kind>/<int:tmdb>")
def route_options(kind, tmdb):
    if kind not in ("movie", "tv", "anime"):
        return fail("unknown kind", 404)
    return ok(**storage.request_options(APP(), kind, tmdb, request.args.get("seasons") or "all"))


@bp.post("/api/storage/request")
def route_request():
    """The request sheet's send: same body as POST /api/requests plus root, season_mode "smart", dry_run."""
    b = request.get_json(force=True) or {}
    if not APP().seerr.ready:
        return fail("Seerr isn't connected - add it in Settings", 409)
    return ok(**storage.send_request(APP(), b, dry_run=bool(b.get("dry_run"))))


# ------------------------------------------------------------------ deleting by hand
@bp.get("/api/storage/delete/preview")
def route_delete_preview():
    a = request.args
    tor = a.get("also_torrent")
    return ok(**storage.delete_preview(APP(), a["item_id"], a.get("scope") or None,
                                       None if tor in (None, "") else _bool(tor), _bool(a.get("keep_monitored"))))


@bp.post("/api/storage/delete")
def route_delete():
    b = request.get_json(force=True) or {}
    if not b.get("item_id"):
        return fail("item_id is missing")
    entry = storage.delete_now(APP(), b["item_id"], b.get("scope"), b.get("also_torrent"),
                               bool(b.get("keep_monitored")))
    msg = f"Deleted {entry['title']}{(' ' + entry['subtitle']) if entry.get('subtitle') else ''}"
    if entry.get("freed"):
        msg += f", freed {storage.fmt_size(entry['freed'])}"
    if entry.get("still_seeding"):
        msg += f" ({storage.fmt_size(entry['still_seeding'])} stays until its torrent is removed)"
    return ok(entry=entry, message=msg)


# ------------------------------------------------------------------ removed titles (watched history survives)
@bp.get("/api/storage/removed")
def route_removed():
    cat = APP().catalog
    return ok(items=cat.removed_cards(request.args.get("kind") or None))


@bp.get("/api/storage/removed/<kind>/<tmdb>")
def route_removed_state(kind, tmdb):
    cat = APP().catalog
    st = cat.removed_state(tmdb, request.args.get("season"), request.args.get("episode"))
    if st and (kind == "movie") != (st.get("kind") == "movie"):
        st = None
    return ok(removed=st)


@bp.post("/api/storage/removed/forget")
def route_removed_forget():
    b = request.get_json(force=True) or {}
    APP().catalog.forget_removed(key=b.get("key"), tmdb=b.get("tmdb"))
    return ok()


# ------------------------------------------------------------------ smart season requests
@bp.get("/api/storage/smart/<int:tmdb>")
def route_smart(tmdb):
    return ok(**smart.status_line(APP(), tmdb))


@bp.get("/api/storage/smart")
def route_smart_all():
    """Every smart show with what the watcher would do now (dry run)."""
    return ok(shows=smart.shows(), check=smart.check(APP(), dry_run=True))


@bp.post("/api/storage/smart/<int:tmdb>/all")
def route_smart_all_now(tmdb):
    b = request.get_json(silent=True) or {}
    return ok(**smart.request_all(APP(), tmdb, dry_run=bool(b.get("dry_run"))))


@bp.post("/api/storage/smart/<int:tmdb>/stop")
def route_smart_stop(tmdb):
    smart.stop(tmdb)
    return ok(**smart.status_line(APP(), tmdb))


@bp.post("/api/storage/smart/check")
def route_smart_check():
    b = request.get_json(silent=True) or {}
    return ok(results=smart.check(APP(), b.get("tmdb"), dry_run=b.get("dry_run", True) is not False))
