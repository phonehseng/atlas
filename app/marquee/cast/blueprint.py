"""/api/cast/*: the window's side of casting (device picker, casting bar, Settings), plus the hooks that keep the
player's own Cast menu (osc/marquee.lua) and the phone remote in step. Registered on the local UI server
(127.0.0.1 only) by marquee.server; init(app) is called once the window and the player exist."""
import importlib
import threading

from flask import Blueprint, jsonify, request

from .. import wheels
from . import devices, endpoint, firewall, session
from .session import CastError

bp = Blueprint("cast", __name__)
APP = None
_install = {"state": None, "done": 0, "total": 0, "error": None}

AIRPLAY_WHEELS = ["pyatv", "aiohttp", "multidict", "yarl", "propcache", "frozenlist", "aiosignal", "aiohappyeyeballs",
                  "attrs", "idna", "cryptography", "cffi", "pycparser", "chacha20poly1305-reuseable", "miniaudio",
                  "pydantic", "pydantic-core", "annotated-types", "typing-inspection", "typing-extensions",
                  "srptools", "tabulate", "tinytag"]


def ok(**kw):
    return jsonify(ok=True, **kw)


def fail(msg, code=400, **kw):
    return jsonify(ok=False, message=str(msg), **kw), code


def _port():
    return endpoint.port_setting()


def _airplay_ready(deep=False):
    """deep: really import them (after an install, so a broken unpack shows up here and not at the TV)."""
    if deep:
        try:
            wheels.add_lib_path()
            importlib.import_module("pyatv")
        except Exception as e:
            print("AirPlay support doesn't load:", type(e).__name__, e)
            _install.update(state="error", error=f"AirPlay support doesn't load ({type(e).__name__}: {e})"[:300])
            return False
    return not wheels.missing(AIRPLAY_WHEELS)


_warmed = []


def _warm():
    """Someone is about to cast: find out which video encoders work now, so the first cast doesn't wait for it."""
    if not _warmed:
        _warmed.append(True)
        from ..mobile import stream
        threading.Thread(target=stream.encoders, daemon=True, name="cast-encoders").start()


@bp.get("/api/cast/devices")
def api_devices():
    """The picker: what's on the network (discovery starts on the first look), and whether the firewall lets the
    TVs in. ?fresh=1 looks again from scratch."""
    if not session.enabled():
        return ok(enabled=False, devices=[], searching=False)
    _warm()
    if request.args.get("fresh"):
        devices.discovery.rescan()
    else:
        devices.discovery.ensure()
    return ok(enabled=True, devices=[devices.public(d) for d in devices.discovery.list()],
              searching=devices.discovery.searching(), error=devices.discovery.error,
              firewall=firewall.status(_port()), port=_port(), endpoint_error=endpoint.last_error,
              casting=session.state())


@bp.get("/api/cast/state")
def api_state():
    return ok(cast=session.state())


@bp.post("/api/cast/start")
def api_start():
    b = request.get_json(force=True, silent=True) or {}
    dev = devices.discovery.get(str(b.get("device") or ""))
    if dev is not None and dev["kind"] == "airplay" and not _airplay_ready():
        return fail("AirPlay needs a one-time download", 409, reason="airplay-download",
                    size=sum(x["size"] for x in wheels.missing(AIRPLAY_WHEELS)))
    try:
        start = float(b["start"]) if b.get("start") is not None else None
        st = session.start(str(b.get("device") or ""), b.get("id") or None, start, b.get("audio"), b.get("sub"))
    except CastError as e:
        return fail(e, 409)
    except (TypeError, ValueError):
        return fail("bad request")
    return ok(cast=st)


@bp.post("/api/cast/cmd")
def api_cmd():
    b = request.get_json(force=True, silent=True) or {}
    try:
        session.command(str(b.get("action") or ""), b.get("value"))
    except CastError as e:
        return fail(e, 409)
    except NotImplementedError:
        return fail("This TV doesn't take that from Marquee", 409)
    except Exception as e:
        print("cast command failed:", b.get("action"), type(e).__name__, e)
        return fail(f"The TV didn't take that ({type(e).__name__})", 502)
    return ok(cast=session.state())


@bp.get("/api/cast/firewall")
def api_firewall():
    fresh = bool(request.args.get("fresh"))
    if fresh:
        firewall.forget()
    st = firewall.status(_port(), wait=fresh)
    return ok(firewall=st, command=firewall.command_line(_port(), st.get("category") or "Private"))


@bp.post("/api/cast/firewall")
def api_firewall_fix():
    st = firewall.status(_port(), wait=True)
    shown = firewall.fix(_port(), st.get("category") or "Private")
    return jsonify(ok=shown, message="Windows will ask for permission" if shown else "Couldn't ask Windows",
                   command=firewall.command_line(_port(), st.get("category") or "Private"))


@bp.get("/api/cast/airplay")
def api_airplay():
    return ok(ready=_airplay_ready(deep=_install["state"] == "done"),
              size=sum(x["size"] for x in wheels.missing(AIRPLAY_WHEELS)), install=_install)


@bp.post("/api/cast/airplay/install")
def api_airplay_install():
    if _install["state"] == "working":
        return ok(install=_install)
    _install.update(state="working", done=0, total=0, error=None)

    def run():
        try:
            wheels.install(AIRPLAY_WHEELS, progress=_install)
            _install["state"] = "done"
        except Exception as e:
            _install.update(state="error", error=str(e)[:200])
    threading.Thread(target=run, daemon=True, name="cast-airplay-install").start()
    return ok(install=_install)


# ---------------------------------------------------------------------------------------------- hooks
def _to_window(st):
    if APP is not None:
        try:
            APP.notify_ui("cast-state", st)
        except Exception:
            pass


def _to_player_devices(devs):
    p = getattr(APP, "player", None)
    if p is None or not getattr(p, "mpv", None) or not getattr(p, "playing", False):
        return
    try:
        p._set("cast_devices", [{"id": d["id"], "name": d["name"], "type": d["type"]} for d in devs])
        p._set("cast_searching", devices.discovery.searching())
    except Exception:
        pass


def osc_scan(player):
    """The player's Cast menu opened: look for TVs, and tell the menu what's there (it redraws on its own)."""
    if not session.enabled():
        return
    _warm()
    devices.discovery.ensure()
    _to_player_devices(devices.discovery.list())
    fw = firewall.status(_port())
    try:
        player._set("cast_searching", True)
        player._set("cast_note", "Windows Firewall may block casting: see Settings > Casting"
                    if fw.get("checked") and not fw.get("allowed") else "")
    except Exception:
        pass

    def later():
        import time
        time.sleep(devices.SEARCH_SECS + 0.5)
        _to_player_devices(devices.discovery.list())
    threading.Thread(target=later, daemon=True).start()


def osc_cast(player, dev_id):
    """A TV was picked in the player's Cast menu: hand the video over at the current position."""
    dev = devices.discovery.get(dev_id)
    if dev is not None and dev["kind"] == "airplay" and not _airplay_ready():
        player.toast("AirPlay needs a one-time download: pick the TV from the title page")
        return
    try:
        session.start(dev_id)
    except CastError as e:
        player.toast(str(e))


def init(app):
    global APP
    APP = app
    session.APP = app
    if _to_window not in session.listeners:
        session.listeners.append(_to_window)
    if _to_player_devices not in devices.discovery.listeners:
        devices.discovery.listeners.append(_to_player_devices)
    try:
        app.player._set("cast_on", session.enabled())
    except Exception:
        pass


def shutdown():
    session.stop_all()
    devices.discovery.stop()
    endpoint.stop()


def settings_changed(changes):
    """Settings saved: casting turned off stops a cast and the TV search; the player's Cast button follows."""
    if "cast" in changes and not changes["cast"]:
        session.stop_all()
        devices.discovery.stop()
    if "cast_port" in changes:
        firewall.forget()
    try:
        APP.player._set("cast_on", session.enabled())
    except Exception:
        pass
