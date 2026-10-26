"""HTTP API of the assistant (registered on Marquee's local server, 127.0.0.1 only).

POST /api/assistant            {text, item_id?}  -> {ok, reply, actions, source, intent, ms}
                                                     (item_id: the title on the details page; a scene request
                                                     there answers with play_from actions)
POST /api/assistant/scene/choose {n}             -> pick candidate n of the scene picker (1..3, 0 = close)
POST /api/assistant/scene/undo                   -> back to where you were before the last scene jump
POST /api/assistant/listen     {on, mode?, once?} -> voice status (on: start the configured or given mode;
                                                     once: listen for one sentence; on=false: close every mic)
GET  /api/assistant/status                       -> listening, devices, model ready, last transcript, settings
GET  /api/assistant/devices                      -> microphones that would be opened
GET|POST /api/assistant/settings                 -> the assistant's settings
POST /api/assistant/install    {parts}           -> install optional parts: voice, gpu, wake, model
POST /api/assistant/selftest   {text?}           -> speech recognition check without a microphone: a Windows voice
                                                     sentence rendered in memory, transcribed {said, heard, ms, device}
POST /api/assistant/look       {question?, pause?} -> Ask Atlas: {id, state: thinking|listening}; the answer arrives
                                                     as the "marquee-atlas" banner / notify_ui("atlas-answer")
GET  /api/assistant/answer                       -> the latest Ask Atlas job {id, state, question, text, ms}
POST /api/assistant/answer/clear                 -> cancel / hide it
POST /api/assistant/ending/<item>/prepare {credits_at} -> start the end-of-title comment + picks (low priority)
GET  /api/assistant/ending/<item>                -> {state: ready|pending, comment, picks, thinking}
POST /api/assistant/ending/<item>/cancel         -> stopped watching early: drop it"""
import threading

from flask import Blueprint, jsonify, request

from . import config, installer, llm

bp = Blueprint("assistant", __name__)
_assistant = None


def assistant():
    global _assistant
    if _assistant is None:
        from .. import server
        from .core import Assistant
        _assistant = Assistant(server.APP)
    return _assistant


def listener():
    return assistant().listener()


def _ok(**kw):
    return jsonify(ok=True, **kw)


def _fail(msg, code=400):
    return jsonify(ok=False, message=str(msg)), code


@bp.post("/api/assistant")
def ask():
    b = request.get_json(force=True, silent=True) or {}
    text = (b.get("text") or "").strip()
    if not text:
        return _fail("say something")
    return _ok(**assistant().handle(text, source=b.get("source") or "text", item_id=b.get("item_id")))


@bp.post("/api/assistant/listen")
def listen():
    b = request.get_json(force=True, silent=True) or {}
    try:
        if b.get("once"):
            return _ok(voice=listener().listen_once(float(b.get("seconds") or 8)))
        if b.get("on"):
            mode = b.get("mode") or config.load()["voice"]
            if mode == "off":
                mode = "push_to_talk"
            if b.get("mode") or config.load()["voice"] == "off":
                config.save({"voice": mode})
            return _ok(voice=listener().start(mode))
        config.save({"voice": "off"})
        return _ok(voice=listener().stop() if assistant().voice else {"mode": "off", "open": False})
    except Exception as e:
        return _fail(e, 409)


@bp.get("/api/assistant/status")
def status():
    from . import stt
    a = assistant()
    voice = a.voice.status() if a.voice else {"mode": "off", "open": False, "listening": False, "devices": []}
    return _ok(voice=voice, listening=bool(voice.get("listening")), devices=voice.get("devices", []),
               model=stt.engine().status() if installer.have("voice") else {"ready": False, "installed": False},
               last_transcript=voice.get("last_transcript"), last=a.last, settings=config.load(),
               llm={"on": llm.available(), "name": (llm.provider() or {}).get("name"),
                    "model": (llm.provider() or {}).get("model")},
               install=installer.status(), vision=_vision_info(), atlas=a.atlas.current)


@bp.get("/api/assistant/devices")
def devices():
    if not installer.have("voice"):
        return _fail("Voice isn't installed yet", 409)
    from .voice import list_inputs
    import sounddevice as sd
    return _ok(devices=list_inputs(), all=[d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0])


@bp.route("/api/assistant/settings", methods=["GET", "POST"])
def assistant_settings():
    if request.method == "POST":
        cfg = config.save(request.get_json(force=True, silent=True) or {})
        a = assistant()
        if a.voice and a.voice.running:  # mode changed: apply it
            if cfg["voice"] == "off":
                a.voice.stop()
            else:
                a.voice.mode = cfg["voice"]
        return _ok(settings=cfg)
    return _ok(settings=config.load())


@bp.post("/api/assistant/install")
def install():
    b = request.get_json(force=True, silent=True) or {}
    parts = b.get("parts") or ["voice", "model"]
    if "gpu" in parts and not installer.has_nvidia():
        parts = [p for p in parts if p != "gpu"]
    bad = [p for p in parts if p not in (*installer.PACKAGES, "model")]
    if bad:
        return _fail(f"unknown part: {', '.join(bad)}")
    started = installer.install(parts)
    return _ok(started=started, install=installer.status())


@bp.post("/api/assistant/selftest")
def selftest():
    if not installer.have("voice"):
        return _fail("Voice isn't installed yet", 409)
    from . import stt
    b = request.get_json(force=True, silent=True) or {}
    try:
        return _ok(**stt.selftest(*([b["text"]] if b.get("text") else [])))
    except Exception as e:
        return _fail(e, 500)


@bp.post("/api/assistant/look")
def look():
    b = request.get_json(force=True, silent=True) or {}
    a = assistant()
    q = (b.get("question") or b.get("text") or "").strip()
    if b.get("pause", True):
        job = a.atlas.button(q)
    else:
        job = a.atlas.ask(q)
    return _ok(**job, vision=_vision_info())


@bp.post("/api/assistant/scenes/<path:item_id>/prepare")
def scenes_prepare(item_id):
    """Opened a title's page: make its stretch summaries ahead of time (never while something plays)."""
    from .. import server
    from . import scene_search
    app = server.APP
    if getattr(getattr(app, "player", None), "playing", False):
        return _ok(started=False)
    scene_search.prepare(app, app.jf.item(item_id))
    return _ok(started=True)


@bp.get("/api/assistant/sdh/<path:item_id>")
def sdh_state(item_id):
    from .. import server
    from . import sdh_fetch
    it = server.APP.jf.item(item_id)
    f = sdh_fetch.sidecar(it)
    return _ok(has_sdh=sdh_fetch.has_sdh(it), sidecar=str(f) if f else None, bazarr=bool(sdh_fetch.bazarr()))


@bp.post("/api/assistant/sdh/<path:item_id>/fetch")
def sdh_fetch_now(item_id):
    from .. import server
    from . import sdh_fetch
    return _ok(**sdh_fetch.fetch(server.APP, server.APP.jf.item(item_id), why="asked"))


@bp.post("/api/assistant/scene/choose")
def scene_choose():
    n = int((request.get_json(force=True, silent=True) or {}).get("n") or 0)
    return _ok(reply=assistant().scenes.choose(n))


@bp.post("/api/assistant/scene/undo")
def scene_undo():
    return _ok(reply=assistant().scenes.undo_jump())


@bp.get("/api/assistant/answer")
def answer():
    return _ok(**assistant().atlas.current)


@bp.post("/api/assistant/answer/clear")
def answer_clear():
    assistant().atlas.clear()
    return _ok()


@bp.post("/api/assistant/ending/<path:item_id>/prepare")
def ending_prepare(item_id):
    from . import ending
    from .. import server
    b = request.get_json(force=True, silent=True) or {}
    at = b.get("credits_at")
    return _ok(**ending.prepare(server.APP, item_id, float(at) if at else None))


@bp.get("/api/assistant/ending/<path:item_id>")
def ending_get(item_id):
    from . import ending
    from .. import server
    return _ok(**ending.get(server.APP, item_id))


@bp.post("/api/assistant/ending/<path:item_id>/cancel")
def ending_cancel(item_id):
    from . import ending
    return _ok(cancelled=ending.cancel(item_id))


def _vision_info():
    from . import vision
    p = vision.provider()
    return {"on": bool(p), "model": (p or {}).get("model"), "via": (p or {}).get("kind")}


def hook_player(app=None):
    """Listen for the OSC's Ask Atlas button / A key: script-message marquee-ask-atlas [question]. Call after
    player.attach() (mpv must exist); safe to call again if mpv is recreated."""
    from .. import server
    app = app or server.APP
    m = getattr(getattr(app, "player", None), "mpv", None)
    if m is None or getattr(m, "_marquee_atlas_hooked", False):
        return False

    def on_msg(*args):
        q = " ".join(x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x) for x in args).strip()
        import threading
        threading.Thread(target=assistant().atlas.button, args=(q,), daemon=True).start()
    m.register_message_handler("marquee-ask-atlas", on_msg)

    def on_choose(pick_id=b"", n=b"0", *_):
        dec = lambda v: v.decode() if isinstance(v, bytes) else str(v)  # noqa: E731
        import threading
        threading.Thread(target=assistant().scenes.choose, args=(int(dec(n) or 0), dec(pick_id)), daemon=True).start()

    def on_undo(*_):
        import threading
        threading.Thread(target=assistant().scenes.undo_jump, daemon=True).start()
    m.register_message_handler("marquee-scene-choose", on_choose)
    m.register_message_handler("marquee-scene-undo-now", on_undo)
    m._marquee_atlas_hooked = True
    return True


def _sdh_watch(app):
    """SDH subtitles from Bazarr: for the title being played (a few minutes in), and a slow idle sweep."""
    import time
    from . import sdh_fetch
    last_id, last_play, last_sweep = None, time.time(), 0.0
    while True:
        time.sleep(30)
        try:
            p = getattr(app, "player", None)
            item = getattr(p, "item", None) if getattr(p, "playing", False) else None
            if item:
                last_play = time.time()
                if item.get("Id") != last_id:
                    last_id = item.get("Id")
                    if not str(last_id).startswith("file:"):
                        sdh_fetch.fetch_soon(app, item)
            elif time.time() - last_play > 1200 and time.time() - last_sweep > 1200:
                last_sweep = time.time()
                sdh_fetch.idle_sweep(app)
        except Exception as e:
            print("sdh watch:", e)


def init(app):
    """Optional: build the assistant now and start voice if it was left on (when its parts are installed)."""
    a = assistant()
    try:
        app.assistant = a  # app.assistant.prepare_ending / cancel_ending / ask_atlas for the player
    except Exception:
        pass
    threading.Thread(target=_sdh_watch, args=(app,), daemon=True).start()
    try:
        hook_player(app)
    except Exception as e:
        print("ask atlas hook failed:", e)
    try:
        if config.load()["voice"] != "off" and installer.have("voice"):
            listener().start()
    except Exception as e:
        print("assistant voice not started:", e)
    return a
