"""Routes for the built-in AI (download, status, remove) and the Classic-mode extras (title blurbs, extra Home rows,
the Up-next payload without the assistant).

    from .ai_setup import bp as ai_setup_bp
    app.register_blueprint(ai_setup_bp)

GET    /api/ai/builtin/status            -> installed, sizes, download progress, running, GPU used
POST   /api/ai/builtin/install {size}    -> start downloading ("standard" | "small"); poll status for progress
DELETE /api/ai/builtin[?size=small]      -> remove the model and engine (or just one model size)
POST   /api/ai/builtin/stop              -> unload the model now (it also unloads itself after 5 idle minutes)
GET    /api/classic/intro/<item>[?because=Title] -> {text}: a short "why watch" line (no AI)
GET    /api/classic/rows                 -> {rows}: "Because you watched X", "Short tonight", ... (no AI)
GET    /api/classic/ending/<item>        -> the Up-next payload (same shape as /api/assistant/ending/<item>)"""
from flask import Blueprint, jsonify, request

from . import localllm, scripted

bp = Blueprint("ai_setup", __name__)


def _app():
    from . import server
    return server.APP


def _ok(**kw):
    return jsonify(ok=True, **kw)


@bp.get("/api/ai/builtin/status")
def builtin_status():
    return _ok(**localllm.status())


@bp.post("/api/ai/builtin/install")
def builtin_install():
    size = (request.get_json(silent=True) or {}).get("size") or "standard"
    if size not in localllm.MODELS:
        return jsonify(ok=False, message="size must be standard or small"), 400
    return _ok(download=localllm.install(size))


@bp.delete("/api/ai/builtin")
def builtin_remove():
    size = request.args.get("size")
    localllm.remove(size if size in localllm.MODELS else None)
    return _ok(**localllm.status())


@bp.post("/api/ai/builtin/stop")
def builtin_stop():
    localllm.ENGINE.stop()
    return _ok()


@bp.get("/api/classic/intro/<path:item_id>")
def classic_intro(item_id):
    return _ok(**scripted.intro(_app(), item_id, request.args.get("because") or None))


@bp.get("/api/classic/rows")
def classic_rows():
    return _ok(rows=scripted.rows(_app()))


@bp.get("/api/classic/ending/<path:item_id>")
def classic_ending(item_id):
    return _ok(**scripted.ending(_app(), item_id))
