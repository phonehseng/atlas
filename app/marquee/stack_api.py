"""Routes for "Your stack" (marquee.stack): the Media Hub's dashboard inside Atlas.
Register in server.py: `from .stack_api import bp; app.register_blueprint(bp)`.
Every answer is {ok, ...}; errors are {ok: false, message}."""
import urllib.parse

from flask import Blueprint, jsonify, request

from . import settings, stack

bp = Blueprint("marquee_stack", __name__)


def APP():
    from . import server
    return server.APP


def ok(**kw):
    return jsonify(ok=True, **kw)


def fail(msg, code=400):
    return jsonify(ok=False, message=str(msg)), code


@bp.before_request
def _guard():
    origin = request.headers.get("Origin")
    if origin and urllib.parse.urlsplit(origin).netloc != request.host:
        return fail("forbidden", 403)
    if request.method == "POST" and request.content_length and not request.is_json:
        return fail("send JSON", 415)


@bp.errorhandler(PermissionError)
def _denied(e):
    return fail(e, 409)


@bp.errorhandler(ValueError)
def _bad(e):
    return fail(e, 400)


@bp.errorhandler(RuntimeError)
def _hub_said(e):
    return fail(e, 502)


@bp.get("/api/stack")
def route_stack():
    snap = stack.snapshot(APP(), force=request.args.get("refresh") == "1")
    return ok(**snap, settings=_settings_view())


@bp.post("/api/stack/action")
def route_action():
    b = request.get_json(force=True) or {}
    return ok(message=stack.action(APP(), b.get("action") or "", b.get("arg") or ""))


@bp.post("/api/stack/restore")
def route_restore():
    b = request.get_json(force=True) or {}
    if not b.get("name") or not b.get("confirm"):
        return fail("Say which backup, and confirm")
    return ok(message=stack.restore(APP(), b["name"]))


@bp.post("/api/stack/phone")
def route_phone():
    b = request.get_json(force=True) or {}
    return ok(**stack.phone(APP(), b.get("changes"), test=bool(b.get("test"))))


def _settings_view():
    s = settings.load()
    h = stack.hub()
    return {"hub_url": s.get("hub_url") or "", "stack_config": s.get("stack_config") or "",
            "hub_token_set": bool(s.get("hub_token")), "found_hub": h.base if h else None,
            "hub_configured": bool(h and h.configured)}


@bp.post("/api/stack/settings")
def route_settings():
    """{hub_url, stack_config, hub_token (set-only; "" is ignored, "-" clears)}"""
    b = request.get_json(force=True) or {}
    changes = {}
    if "hub_url" in b:
        url = (b.get("hub_url") or "").strip().rstrip("/")
        if url and not urllib.parse.urlsplit(url).scheme in ("http", "https"):
            return fail("The hub address starts with http:// or https://")
        changes["hub_url"] = url
    if "stack_config" in b:
        changes["stack_config"] = (b.get("stack_config") or "").strip()
    tok = (b.get("hub_token") or "").strip()
    if tok:
        changes["hub_token"] = "" if tok == "-" else tok
    settings.save(changes)
    stack.forget()
    return ok(settings=_settings_view())
