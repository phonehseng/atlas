"""Atlas Mobile's web server: the phone app (watch on the phone, or remote-control the PC) on its own port.

Off unless "Atlas Mobile" (or the older "Phone remote") is on in Settings. It listens on this PC's Tailscale
address (and 127.0.0.1, which `tailscale serve` forwards HTTPS to), plus the home network address when the bind mode
is "lan+tailscale" - never on 0.0.0.0. Pairing: a one-time token in the QR code's URL fragment (never in a request
line, so it stays out of logs) is traded for a long-lived HttpOnly cookie for that device alone (marquee.mobile.auth).
Friends can sign in with their own Jellyfin account instead, when that's allowed in Settings. Failed attempts are
rate-limited per address. Nothing here hands out the Jellyfin or Seerr token: artwork comes from the local image
cache and video straight from the files on this PC.

The desktop calls apply(app) at start (it then follows the settings by itself), stop() when closing; the local UI
server registers `desk` for the Settings page (QR codes, addresses, devices, streams, HTTPS, firewall).
"""
import base64
import io
import json
import os
import re
import threading
import time
from pathlib import Path

from flask import Blueprint, Flask, Response, g, jsonify, redirect, request, send_from_directory

from . import server, settings
from .mobile import api as mapi
from .mobile import auth, net, stream

WEB = Path(__file__).resolve().parent / "web" / "remote"
ICON_SRC = Path(__file__).resolve().parent.parent / "assets" / "marquee.png"
DEFAULT_PORT = 8737
COOKIE = auth.COOKIE
FAIL_LIMIT, FAIL_WINDOW, BLOCK_FOR = 8, 600, 600   # 8 bad attempts in 10 minutes -> that address waits 10 minutes
OWNER_ONLY = ("/r/api/state", "/r/api/cmd", "/r/api/pcplay", "/r/api/mylist")
STATIC = {"app.css", "app.js", "player.js", "remote.js", "sw.js", "vendor/hls.min.js", "vendor/hls.js-LICENSE.txt",
          "icons/icon-192.png", "icons/icon-512.png", "icons/maskable-512.png", "icons/apple-touch-icon.png",
          "icons/icon.svg"}
SETTING_KEYS = ("mobile", "remote", "remote_port", "mobile_bind")

APP = None          # the marquee App while the server runs
_srv = None         # waitress server
_thread = None
_port = None
_hosts = []
_lock = threading.RLock()
_fails = {}         # ip -> [timestamps]
_blocked = {}       # ip -> until
_card_cache = {}    # item id -> trimmed card of the item playing on the PC
_last_sid = {}      # item id -> subtitle track to bring back when captions go on again
_watcher = None
_last_error = None

rapp = Flask(__name__, static_folder=None)
rapp.config["MAX_CONTENT_LENGTH"] = 64 * 1024
rapp.register_blueprint(mapi.bp)


# ------------------------------------------------------------------ old names (kept for callers of the remote)
def token():
    return auth.pairing_token()


def reset_token():
    """A fresh pairing code; every paired device is signed out and has to pair again."""
    stream.stop_all()
    auth.revoke_all()
    return auth.pairing_token(fresh=True)


# ------------------------------------------------------------------ addresses / QR
def _qr(url):
    import qrcode
    from qrcode.image.pil import PilImage
    qr = qrcode.QRCode(border=2, box_size=10, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(image_factory=PilImage, fill_color="#0d1738", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _bind_mode():
    m = settings.load().get("mobile_bind") or "tailscale"
    return m if m in ("tailscale", "lan+tailscale") else "tailscale"


def addresses(port=None):
    """Where phones reach this PC: HTTPS through `tailscale serve` (when on), the Tailscale name and the home
    network address (lan+tailscale only). -> [{label, ip, address}]"""
    port = port or _port or settings.load().get("remote_port") or DEFAULT_PORT
    out = []
    ts = net.tailscale_status()
    if ts.get("running"):
        https = net.serve_status(port)
        if https.get("on") and https.get("url"):
            out.append({"label": "Tailscale (HTTPS)", "ip": ts.get("ip"), "address": https["url"], "secure": True})
        if ts.get("ip"):
            host = ts.get("name") or ts["ip"]
            out.append({"label": "Tailscale", "ip": ts["ip"], "address": f"http://{host}:{port}", "secure": False})
    elif ts.get("ip"):
        out.append({"label": "Tailscale", "ip": ts["ip"], "address": f"http://{ts['ip']}:{port}", "secure": False})
    if _bind_mode() == "lan+tailscale":
        lan = net.lan_ip()
        if lan:
            out.append({"label": "Wi-Fi / LAN", "ip": lan, "address": f"http://{lan}:{port}", "secure": False})
    return out


def pairing_info(port=None):
    """What Settings shows: is it on, the addresses and a pairing QR code for each (the code works once)."""
    port = port or _port or settings.load().get("remote_port") or DEFAULT_PORT
    tok = auth.pairing_token()
    out = []
    for a in addresses(port):
        url = f"{a['address']}/r#{tok}"
        out.append({**a, "url": url, "qr": _qr(url)})
    ts = net.tailscale_status()
    hint = None
    if not ts.get("installed") and _bind_mode() == "tailscale":
        hint = "Install Tailscale on this PC and your phone, or switch to \"Home network + Tailscale\"."
    elif not ts.get("running") and _bind_mode() == "tailscale":
        hint = "Tailscale isn't running on this PC, so phones can't reach it yet."
    return {"running": running(), "port": port, "bind": _bind_mode(), "hosts": list(_hosts), "addresses": out,
            "qr": out[0]["qr"] if out else None, "url": out[0]["url"] if out else None,
            "pair_version": auth.pairing_version(), "hint": hint, "error": _last_error}


# ------------------------------------------------------------------ server lifecycle
def running():
    return _srv is not None


def wanted(st=None):
    st = st or settings.load()
    return bool(st.get("mobile") or st.get("remote"))


def start(app, port=None, hosts=None):
    """Start (or restart on new addresses/port) the phone server. Returns pairing_info()."""
    global APP, _srv, _thread, _port, _hosts, _last_error
    from waitress import create_server
    with _lock:
        APP = app
        mapi.APP = app
        port = int(port or settings.load().get("remote_port") or DEFAULT_PORT)
        hosts = list(hosts or net.bind_hosts(_bind_mode()))
        if _srv is not None and _port == port and sorted(_hosts) == sorted(hosts):
            return pairing_info(port)
        _stop_locked(keep_streams=True)
        auth.pairing_token()
        good = []
        srv = None
        try:
            srv = create_server(rapp, listen=" ".join(f"{h}:{port}" for h in hosts), threads=16, ident="Atlas",
                                channel_timeout=120, connection_limit=200, outbuf_high_watermark=8 << 20)
            good = hosts
        except OSError:
            for h in hosts:  # one address may be gone (Tailscale just stopped): keep the others
                try:
                    create_server(rapp, host=h, port=port).close()
                    good.append(h)
                except OSError as e:
                    print(f"Atlas Mobile can't listen on {h}:{port}: {e}", flush=True)
            if good:
                srv = create_server(rapp, listen=" ".join(f"{h}:{port}" for h in good), threads=16, ident="Atlas",
                                    channel_timeout=120, connection_limit=200, outbuf_high_watermark=8 << 20)
        if srv is None:
            _last_error = f"Port {port} is in use by another program"
            return pairing_info(port)
        _last_error = None
        _srv, _port, _hosts = srv, port, good
        _thread = threading.Thread(target=_run, args=(srv,), name="marquee-mobile", daemon=True)
        _thread.start()
    mapi.warm(app)
    print(f"Atlas Mobile on {', '.join(good)} port {port}", flush=True)
    return pairing_info(port)


def _run(srv):
    try:
        srv.run()
    except Exception as e:  # closed under it by stop(), or the address vanished
        if _srv is srv:
            print("Atlas Mobile stopped:", e)


def _stop_locked(keep_streams=False):
    global _srv, _thread
    srv, _srv, _thread = _srv, None, None
    if srv is not None:
        try:
            srv.close()
            srv.task_dispatcher.shutdown(timeout=2)
        except Exception as e:
            print("Atlas Mobile stop failed:", e)
    if not keep_streams:
        stream.stop_all()


def stop():
    with _lock:
        _stop_locked()


def apply(app):
    """Settings changed (or the app started): start, restart or stop to match them, and keep following them."""
    global APP
    APP = app
    mapi.APP = app
    _ensure_watcher()
    st = settings.load()
    if wanted(st):
        return start(app, st.get("remote_port") or DEFAULT_PORT)
    stop()
    return None


def _ensure_watcher():
    global _watcher
    if _watcher and _watcher.is_alive():
        return
    _watcher = threading.Thread(target=_watch, daemon=True, name="marquee-mobile-watch")
    _watcher.start()


def _watch():
    """Follow the settings file (on/off, port, bind mode) and Tailscale coming and going."""
    def snapshot():
        st = settings.load()
        return tuple(st.get(k) for k in SETTING_KEYS)
    last = snapshot()
    last_mtime = _mtime()
    last_net = 0
    while True:
        time.sleep(3)
        try:
            m = _mtime()
            if m != last_mtime:
                last_mtime = m
                cur = snapshot()
                if cur != last and APP is not None:
                    last = cur
                    apply(APP)
                    continue
            if running() and time.time() - last_net > 30 and APP is not None:
                last_net = time.time()
                net.forget()
                want = net.bind_hosts(_bind_mode())
                if sorted(want) != sorted(_hosts):
                    print("Atlas Mobile: network changed, listening on", ", ".join(want), flush=True)
                    start(APP, _port, want)
        except Exception as e:
            print("Atlas Mobile watcher:", type(e).__name__, e)


def _mtime():
    try:
        return settings.FILE.stat().st_mtime
    except OSError:
        return 0


# ------------------------------------------------------------------ auth
def _client_ip():
    ip = request.remote_addr or "?"
    if ip == "127.0.0.1" and request.headers.get("X-Forwarded-For"):  # through `tailscale serve`
        ip = request.headers["X-Forwarded-For"].split(",")[0].strip() or ip
    return ip


def _blocked_now(ip):
    return _blocked.get(ip, 0) > time.time()


def count_failure(ip=None, slow=True):
    ip = ip or _client_ip()
    now = time.time()
    if len(_fails) > 1000 or len(_blocked) > 1000:  # keep the bookkeeping small on a noisy network
        for k in [k for k, v in _blocked.items() if v < now]:
            _blocked.pop(k, None)
        for k in [k for k, v in _fails.items() if not v or now - v[-1] > FAIL_WINDOW]:
            _fails.pop(k, None)
    hits = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
    hits.append(now)
    _fails[ip] = hits
    if len(hits) >= FAIL_LIMIT:
        _blocked[ip] = now + BLOCK_FOR
        _fails.pop(ip, None)
    if slow:
        time.sleep(0.4)  # slow guessing down further


def _secure():
    return request.headers.get("X-Forwarded-Proto", "").lower() == "https" or request.scheme == "https"


def _set_cookie(r, value):
    r.set_cookie(COOKIE, value, max_age=400 * 86400, httponly=True, samesite="Strict", path="/r", secure=_secure())
    return r


@rapp.before_request
def guard():
    p = request.path
    g.device = None
    private = p.startswith("/r/api/") or p.startswith("/r/img/") or p.startswith("/r/s/")
    if not private:
        return None  # the page and its files hold nothing private
    ip = _client_ip()
    if _blocked_now(ip):
        return jsonify(ok=False, message="Too many wrong codes - try again in a few minutes"), 429
    if p.startswith("/r/s/"):
        return None  # media: the session key in the path is checked by the route
    if request.method not in ("GET", "HEAD") and request.headers.get("X-Marquee") != "1":
        return jsonify(ok=False, message="bad request"), 400
    if p in ("/r/api/pair", "/r/api/login", "/r/api/login-info"):
        return None
    dev = auth.device_for(request.cookies.get(COOKIE), ip)
    if dev:
        g.device = dev
        _fails.pop(ip, None)
        if p.startswith(OWNER_ONLY) and not auth.is_owner(dev):
            return jsonify(ok=False, message="Only the PC's owner can do that"), 403
        return None
    count_failure(ip)
    return jsonify(ok=False, message="Not paired - scan the QR code in Atlas's Settings", reason="unpaired"), 401


@rapp.after_request
def headers(r):
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Referrer-Policy"] = "no-referrer"
    r.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    r.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; worker-src 'self' blob:; "
        "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self'; "
        "manifest-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    if request.path.startswith("/r/api/"):
        r.headers.setdefault("Cache-Control", "no-store")
    return r


@rapp.errorhandler(Exception)
def on_error(e):
    code = getattr(e, "code", 500)
    if isinstance(e, ValueError):
        code = 400
    if not isinstance(code, int) or not 400 <= code < 600:
        code = 500
    if code == 500:
        print("Atlas Mobile error:", type(e).__name__, e)
    return jsonify(ok=False, message="Something went wrong" if code == 500 else str(e)), code


def ok(**kw):
    return jsonify(ok=True, **kw)


# ------------------------------------------------------------------ pages and files
@rapp.get("/")
def root():
    return redirect("/r", 302)


@rapp.get("/r")
@rapp.get("/r/")
def page():
    r = send_from_directory(WEB, "index.html")
    r.headers["Cache-Control"] = "no-cache"
    return r


@rapp.get("/r/<path:name>")
def files(name):
    if name not in STATIC:
        return Response(status=404)
    if name == "icons/icon.svg":
        r = send_from_directory(WEB.parent, "icon.svg")
    else:
        if name.startswith("icons/"):
            _ensure_icons()
        r = send_from_directory(WEB, name)
    r.headers["Cache-Control"] = "no-cache"
    if name == "sw.js":
        r.headers["Service-Worker-Allowed"] = "/r/"
    return r


def _ensure_icons():
    """App icons for the home screen, made once from assets/marquee.png."""
    d = WEB / "icons"
    want = {"icon-192.png": (192, False), "icon-512.png": (512, False), "maskable-512.png": (512, True),
            "apple-touch-icon.png": (180, True)}
    if all((d / n).exists() for n in want) or not ICON_SRC.exists():
        return   # shipped with the app; this only fills in missing ones in a source checkout
    try:
        _make_icons(d, want)
    except Exception as e:  # e.g. a read-only install folder
        print("Atlas Mobile icons:", e)


def _make_icons(d, want):
    from PIL import Image
    d.mkdir(parents=True, exist_ok=True)
    src = Image.open(ICON_SRC).convert("RGBA")
    for name, (size, padded) in want.items():
        if (d / name).exists():
            continue
        if padded:  # maskable / iOS: the logo inside the safe zone on the app's own background
            bg = Image.new("RGBA", (size, size), (7, 12, 29, 255))
            inner = int(size * 0.72)
            bg.alpha_composite(src.resize((inner, inner), Image.LANCZOS), ((size - inner) // 2, (size - inner) // 2))
            bg.convert("RGB").save(d / name, optimize=True)
        else:
            src.resize((size, size), Image.LANCZOS).save(d / name, optimize=True)


@rapp.get("/r/manifest.webmanifest")
def manifest():
    """The install manifest. For a paired device the start URL carries a one-time hand-over code, because an
    installed iPhone app has its own cookie jar and would otherwise have to be paired again."""
    start_url = "/r"
    dev = auth.device_for(request.cookies.get(COOKIE), _client_ip())
    if dev:
        start_url = "/r#h=" + auth.clone_token(dev["id"])
    m = {"name": "Atlas", "short_name": "Atlas", "id": "/r", "start_url": start_url, "scope": "/r",
         "display": "standalone", "orientation": "any", "background_color": "#070c1d", "theme_color": "#070c1d",
         "description": "Your movies and shows from your PC",
         "icons": [{"src": "/r/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                   {"src": "/r/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                   {"src": "/r/icons/maskable-512.png", "sizes": "512x512", "type": "image/png",
                    "purpose": "maskable"}]}
    _ensure_icons()
    return Response(json.dumps(m), mimetype="application/manifest+json", headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ pairing and sign-in
@rapp.post("/r/api/pair")
def api_pair():
    """First visit with the QR code's token (or an installed app's hand-over code): this device gets its own
    cookie, so the page never has to hold anything."""
    tok = request.headers.get("X-Marquee-Token", "")
    ua, ip = request.headers.get("User-Agent", ""), _client_ip()
    if auth.use_pairing_token(tok):
        cookie, dev = auth.create_device({"kind": "owner"}, ua, ip)
    else:
        parent = auth.use_clone_token(tok)
        if not parent:
            count_failure(ip)
            return jsonify(ok=False, reason="used", message="This pairing code was already used or has expired. "
                           "Show a new one in Atlas's Settings on your PC."), 401
        cookie, dev = auth.create_device(parent.get("user") or {"kind": "owner"}, ua, ip,
                                         jf_token=auth.jf_token(parent) or None,
                                         name=(parent.get("name") or "") + " (app)")
    r = _set_cookie(ok(device=dev), cookie)
    try:
        if getattr(APP.player, "playing", False):
            APP.player.toast(f"{dev['name']} paired")
    except Exception:
        pass
    return r


@rapp.get("/r/api/login-info")
def api_login_info():
    st = settings.load()
    return ok(jellyfin=bool(st.get("mobile_jellyfin_signin") and APP and APP.jf.base))


@rapp.post("/r/api/login")
def api_login():
    """Sign in with a Jellyfin account (friends sharing this PC over Tailscale). The password is checked once."""
    if not settings.load().get("mobile_jellyfin_signin"):
        return jsonify(ok=False, message="Signing in with Jellyfin is turned off on this PC"), 403
    b = request.get_json(force=True, silent=True) or {}
    user, pw = str(b.get("user", ""))[:120].strip(), str(b.get("password", ""))[:500]
    if not user:
        return jsonify(ok=False, message="Enter your Jellyfin username"), 400
    ip = _client_ip()
    try:
        cookie, dev = mapi.jellyfin_login(user, pw, request.headers.get("User-Agent", ""), ip)
    except PermissionError as e:
        count_failure(ip)
        return jsonify(ok=False, message=str(e)), 401
    except ValueError as e:
        return jsonify(ok=False, message=str(e)), 503
    return _set_cookie(ok(device=dev), cookie)


# ------------------------------------------------------------------ remote control of the PC player (owner)
_IMG_RE = re.compile(r"/Items/([0-9a-fA-F-]{32,36})/Images/(\w+)(?:\?(.*))?$")


def _trim(c):
    """A card for the phone: catalog/Jellyfin artwork as this server's /r/img URLs."""
    keep = ("id", "type", "name", "series", "series_id", "season", "episode", "year", "age", "runtime",
            "position", "progress", "played", "overview", "genres", "rating")
    out = {k: c.get(k) for k in keep}
    for k in ("poster", "backdrop", "thumb", "logo"):
        out[k] = mapi.img(c.get(k))
    return out


def _player():
    return APP.player


def _mpv():
    return getattr(_player(), "mpv", None)


def _prop(name, default=None):
    m = _mpv()
    if m is None:
        return default
    try:
        v = getattr(m, name)
        return default if v is None else v
    except Exception:
        return default


def _call(name, *args, **kw):
    """Use the player's public method when it has one; returns False when it doesn't."""
    fn = getattr(_player(), name, None)
    if callable(fn):
        fn(*args, **kw)
        return True
    return False


def _osc(action, value=""):
    """Things only the player's own control path knows how to do (window changes, next episode)."""
    p = _player()
    if hasattr(p, "_message"):
        p._message(action, value)
    else:
        raise RuntimeError(f"player can't {action}")


def _now_card(item):
    if not item:
        return None
    hit = _card_cache.get(item["Id"])
    if hit is None:
        c = server.catalog().get(item.get("_catalog_id") or item["Id"])
        hit = _trim(server.catalog().card(c) if c else server.card(item))
        _card_cache.clear()
        _card_cache[item["Id"]] = hit
    return hit


def _subs_on():
    sid = _prop("sid", False)
    return bool(sid) and sid not in ("no", False)


def _cast():
    """The cast in progress (marquee.cast), or None: while casting, the Remote tab controls the TV."""
    try:
        from .cast import session
        cs = session.get()
        return cs if cs is not None and cs.state not in ("ended", "stopped", "error") else None
    except Exception:
        return None


def cast_state(cs):
    st = cs.public()
    item = None
    if st.get("item") and st["item"].get("id"):
        c = server.catalog().get(st["item"]["id"])
        item = _trim(server.catalog().card(c)) if c else None
    nxt = st.get("next")
    return {"playing": bool(item), "paused": st["state"] == "paused", "position": st["position"],
            "duration": st["duration"], "volume": round((st["volume"] or 0) * 100), "muted": bool(st["muted"]),
            "subs": bool(st["sub"]) and any(s["id"] == st["sub"] and s["kind"] == "text" for s in st["subs"]),
            "has_subs": any(s["kind"] == "text" for s in st["subs"]), "fullscreen": False, "pip": False,
            "buffering": st["state"] in ("buffering", "loading", "connecting"),
            "next": {"id": nxt["id"], "title": nxt["title"]} if nxt else None, "item": item,
            "casting": st["device"]["name"]}


def _cast_cmd(cs, action, value, b):
    from .cast import session
    if action == "pause":
        mode = value if value in ("toggle", "pause", "play") else "toggle"
        session.command(mode)
    elif action == "seek":
        session.command("seek_rel" if b.get("relative") else "seek", float(value or 0))
    elif action == "volume":
        session.command("volume", max(0.0, min(100.0, float(value or 0))) / 100)
    elif action == "mute":
        session.command("mute", value)
    elif action == "subs":
        st = cs.public()
        texts = [s for s in st["subs"] if s["kind"] == "text"]
        if st["sub"]:
            _last_sid["cast:" + str((st["item"] or {}).get("id"))] = st["sub"]
            session.command("sub", "off")
        elif texts:
            want = _last_sid.get("cast:" + str((st["item"] or {}).get("id")))
            session.command("sub", want if any(s["id"] == want for s in texts) else texts[0]["id"])
        else:
            raise ValueError("This video has no subtitles the TV can show")
    elif action == "next":
        session.command("next")
    elif action == "stop":
        session.command("stop")
    else:
        return jsonify(ok=False, message="Not while casting to a TV"), 409
    return ok()


def player_state():
    cs = _cast()
    if cs is not None:
        return cast_state(cs)
    p = _player()
    item = getattr(p, "item", None)
    playing = bool(getattr(p, "playing", False) and item)
    tracks = _prop("track_list", []) or []
    nxt = getattr(p, "next_item", None)
    st = {
        "playing": playing,
        "paused": bool(_prop("pause", False)),
        "position": float(_prop("time_pos", 0) or getattr(p, "_pos", 0) or 0),
        "duration": float(_prop("duration", 0) or ((item or {}).get("RunTimeTicks") or 0) / server.TICKS),
        "volume": float(_prop("volume", 0) or 0),
        "muted": bool(_prop("mute", False)),
        "subs": _subs_on(),
        "has_subs": any(t.get("type") == "sub" for t in tracks),
        "fullscreen": bool(getattr(APP, "fullscreen", False)),
        "pip": bool(getattr(p, "pip", False)),
        "buffering": bool(_prop("paused_for_cache", False)),
        "next": ({"id": nxt["Id"], "title": f"S{nxt.get('ParentIndexNumber', 0)}:E{nxt.get('IndexNumber', 0)}"
                                            f" · {nxt.get('Name', '')}"} if nxt else None),
        "item": _now_card(item) if playing else None,
    }
    pub = getattr(p, "state", None)
    if callable(pub):  # the player's own view wins where it has an answer
        try:
            extra = pub() or {}
            for ours, theirs in (("position", "position"), ("position", "time_pos"), ("duration", "duration"),
                                 ("paused", "paused"), ("volume", "volume"), ("muted", "muted"),
                                 ("muted", "mute"), ("subs", "subs"), ("fullscreen", "fullscreen"),
                                 ("pip", "pip"), ("playing", "playing")):
                if extra.get(theirs) is not None:
                    st[ours] = extra[theirs]
        except Exception as e:
            print("player state failed:", e)
    return st


@rapp.get("/r/api/state")
def api_state():
    return ok(state=player_state(), user=settings.load().get("user_name") or "")


@rapp.post("/r/api/cmd")
def api_cmd():
    b = request.get_json(force=True, silent=True) or {}
    action, value = str(b.get("action", "")), b.get("value")
    cs = _cast()
    if cs is not None:
        try:
            return _cast_cmd(cs, action, value, b)
        except Exception as e:
            return jsonify(ok=False, message=str(e) or "The TV didn't take that"), 409
    p, m = _player(), _mpv()
    if not getattr(p, "playing", False) and action not in ("fullscreen",):
        return jsonify(ok=False, message="Nothing is playing"), 409
    if action == "pause":  # value: "toggle" | "pause" | "play"
        mode = value if value in ("toggle", "pause", "play") else "toggle"
        if not _call("pause", mode):
            m.pause = (not m.pause) if mode == "toggle" else (mode == "pause")
    elif action == "seek":
        rel = bool(b.get("relative"))
        v = float(value or 0)
        if not _call("seek", v, rel):
            m.seek(v, "relative" if rel else "absolute")
    elif action == "volume":
        v = max(0.0, min(100.0, float(value or 0)))
        if not _call("set_volume", v):
            m.volume = v
        if m is not None and m.mute and v > 0:
            m.mute = False
    elif action == "mute":
        m.mute = (not m.mute) if value is None else bool(value)
    elif action == "subs":
        _toggle_subs(m, p)
    elif action == "next":
        if not getattr(p, "next_item", None):
            return jsonify(ok=False, message="No next episode"), 409
        _osc("next")
    elif action in ("fullscreen", "pip"):
        _osc(action)
    elif action == "stop":
        _osc("close")
    else:
        return jsonify(ok=False, message="unknown action"), 400
    return ok()


def _toggle_subs(m, p):
    item_id = (getattr(p, "item", None) or {}).get("Id")
    if _subs_on():
        _last_sid[item_id] = m.sid
        m.sid = "no"
        msg = "Subtitles off"
    else:
        subs = [t for t in (m.track_list or []) if t.get("type") == "sub"]
        if not subs:
            raise ValueError("This video has no subtitles")
        want = _last_sid.get(item_id)
        m.sid = want if any(t.get("id") == want for t in subs) else subs[0]["id"]
        msg = "Subtitles on"
    try:
        p.toast(msg)
    except Exception:
        pass


@rapp.post("/r/api/pcplay")
def api_pcplay():
    """Play a title on the PC (from the local catalog when Jellyfin isn't needed, like the desktop does)."""
    b = request.get_json(force=True, silent=True) or {}
    item_id = str(b.get("id", ""))
    if not mapi.ID_RE.fullmatch(item_id):
        return jsonify(ok=False, message="bad id"), 400
    start = float(b["start"]) if b.get("start") is not None else None
    info = server.catalog().lookup(item_id)
    if info and (info.get("source") == "local" or not APP.jf.fresh_online(45)):
        if not info.get("exists"):
            return jsonify(ok=False, message="That file isn't on this PC"), 409
        if start is None and info.get("position"):
            start = info["position"]
        threading.Thread(target=APP.player.play_file, args=(server.play_card(info),), kwargs={"start": start},
                         daemon=True).start()
    else:
        threading.Thread(target=_player().play, args=(item_id,), kwargs={"start": start}, daemon=True).start()
    return ok()


# ------------------------------------------------------------------ desktop (local UI server only)
desk = Blueprint("remote_desk", __name__)


@desk.get("/api/remote")
def desk_info():
    """Settings: the QR codes (PNG data URLs), the addresses and whether the server is listening."""
    return jsonify(ok=True, **pairing_info())


@desk.post("/api/remote/reset")
def desk_reset():
    reset_token()
    return jsonify(ok=True, **pairing_info())


_fw = {"busy": False}


def _firewall():
    """Cached firewall check; the first one runs in the background (PowerShell takes a second or two)."""
    port = _port or settings.load().get("remote_port") or DEFAULT_PORT
    key = f"fw{port}"
    hit = net._cache.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    if not _fw["busy"]:
        _fw["busy"] = True

        def run():
            try:
                net.firewall_status(port)
            finally:
                _fw["busy"] = False
        threading.Thread(target=run, daemon=True).start()
    return {"checked": False, "pending": True}


@desk.get("/api/mobile")
def desk_mobile():
    """Everything Settings > Atlas Mobile shows."""
    st = settings.load()
    port = _port or st.get("remote_port") or DEFAULT_PORT
    info = pairing_info(port)
    ts = net.tailscale_status()
    return jsonify(ok=True, enabled=wanted(st), **info, tailscale=ts,
                   https=net.serve_status(port) if ts.get("running") else {"on": False, "available": False},
                   https_command=" ".join(net.serve_command(port, True)),
                   firewall=_firewall(), devices=auth.devices(), streams=stream.active(),
                   max_streams=stream.setting_max(), quality=st.get("mobile_quality") or "auto",
                   jellyfin_signin=bool(st.get("mobile_jellyfin_signin")),
                   encoders=stream._enc.get("h264") if stream._enc else None)


@desk.post("/api/mobile/pair")
def desk_new_code():
    auth.pairing_token(fresh=True)
    return jsonify(ok=True, **pairing_info())


@desk.post("/api/mobile/devices/<did>/revoke")
def desk_revoke(did):
    stream.stop_device(did)
    return jsonify(ok=True, revoked=auth.revoke(did), devices=auth.devices())


@desk.post("/api/mobile/devices/<did>/rename")
def desk_rename(did):
    b = request.get_json(force=True, silent=True) or {}
    return jsonify(ok=auth.rename(did, b.get("name")), devices=auth.devices())


@desk.post("/api/mobile/streams/<sid>/stop")
def desk_stop_stream(sid):
    return jsonify(ok=stream.stop(sid), streams=stream.active())


@desk.post("/api/mobile/https")
def desk_https():
    on = bool((request.get_json(force=True, silent=True) or {}).get("on"))
    port = _port or settings.load().get("remote_port") or DEFAULT_PORT
    good, msg = net.serve_set(port, on)
    if good:
        settings.save({"mobile_https": on})
    return jsonify(ok=good, message=msg, https=net.serve_status(port), **pairing_info())


@desk.post("/api/mobile/firewall")
def desk_firewall():
    port = _port or settings.load().get("remote_port") or DEFAULT_PORT
    shown = net.firewall_fix(port)
    return jsonify(ok=shown, message="Windows will ask for permission" if shown else "Couldn't ask Windows",
                   command=" ".join(net.firewall_fix_command(port)))


def _tell_desktop(event, dev):
    try:
        if APP is not None:
            APP.notify_ui("mobile-devices", {"event": event, "device": dev, "pair_version": auth.pairing_version()})
    except Exception:
        pass


auth.listeners.append(_tell_desktop)
