"""The cast-only web server: where a TV pulls the video from.

It listens on this PC's home-network address only (never 0.0.0.0, never the Tailscale or VPN addresses), on the
cast port (setting "cast_port", 8738), and only while something is being cast. Every URL carries the cast's random
token (/c/<token>/...), which is valid for that one cast and dropped when it ends; anything else is a 404. Requests
from outside private address ranges are refused. Responses allow any origin (CORS): the Chromecast receiver is a
web page on the TV and fetches the playlist, segments and subtitles with XHR.

The media itself comes from Marquee Mobile's streaming engine (marquee.mobile.stream sessions), served by the same
helpers the phone app uses (marquee.mobile.api.serve_*).
"""
import ipaddress
import secrets
import threading
import time

from flask import Flask, Response, request

from .. import settings
from ..mobile import api as mapi
from ..mobile import net

DEFAULT_PORT = 8738
FAIL_LIMIT, BLOCK_FOR = 20, 600

capp = Flask(__name__, static_folder=None)
_lock = threading.RLock()
_srv = None
_thread = None
_host = None
_port = None
_tokens = {}       # token -> cast session (marquee.cast.session.CastSession)
_fails = {}
_blocked = {}
_ended = {}        # token -> when its cast ended (a TV may still ask for it for a moment: no strike for that)
last_error = None
hits = {}          # token -> {"path": last path, "at": time, "client": ip} (for the casting bar's "TV connected")


def port_setting():
    try:
        return int(settings.load().get("cast_port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PORT


def base_url():
    return f"http://{_host}:{_port}" if _srv is not None else None


def running():
    return _srv is not None


def ensure():
    """Start (or move to a new home-network address) the server. Returns the base URL, or raises RuntimeError."""
    global _srv, _thread, _host, _port, last_error
    from waitress import create_server
    with _lock:
        net.forget()
        host = net.lan_ip()
        if not host:
            raise RuntimeError("This PC isn't on a home network (Wi-Fi or Ethernet), so a TV can't reach it")
        port = port_setting()
        if _srv is not None and _host == host and _port == port:
            return base_url()
        _stop_locked()
        try:
            srv = create_server(capp, host=host, port=port, threads=8, ident="Marquee", channel_timeout=120,
                                connection_limit=50, outbuf_high_watermark=8 << 20)
        except OSError as e:
            last_error = f"Port {port} on {host} is in use by another program ({e.strerror or e})"
            raise RuntimeError(last_error)
        last_error = None
        _srv, _host, _port = srv, host, port
        _thread = threading.Thread(target=_run, args=(srv,), name="marquee-cast-http", daemon=True)
        _thread.start()
        print(f"cast endpoint on {host}:{port}", flush=True)
        return base_url()


def _run(srv):
    try:
        srv.run()
    except Exception as e:
        if _srv is srv:
            print("cast endpoint stopped:", e)


def _stop_locked():
    global _srv, _thread
    srv, _srv, _thread = _srv, None, None
    if srv is not None:
        try:
            srv.close()
            srv.task_dispatcher.shutdown(timeout=2)
        except Exception as e:
            print("cast endpoint stop failed:", e)


def stop():
    with _lock:
        _stop_locked()


def add(session):
    """A token for a cast session (valid until revoke())."""
    tok = secrets.token_urlsafe(24)
    with _lock:
        _tokens[tok] = session
    return tok


def revoke(token, stop_when_idle=True):
    with _lock:
        if _tokens.pop(token, None) is not None:
            now = time.time()
            for k in [k for k, t in _ended.items() if now - t > 3600]:
                _ended.pop(k, None)
            _ended[token] = now
        hits.pop(token, None)
        if stop_when_idle and not _tokens:
            _stop_locked()


def _client():
    return request.remote_addr or "?"


def _private(ip):
    try:
        a = ipaddress.ip_address(ip)
        return (a.is_private or a.is_link_local) and a not in net.CGNAT
    except ValueError:
        return False


@capp.before_request
def guard():
    ip = _client()
    if _blocked.get(ip, 0) > time.time():
        return Response(status=429)
    if not _private(ip):
        return Response(status=403)
    if request.method == "OPTIONS":
        return _cors(Response(status=204))
    return None


@capp.after_request
def headers(r):
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Referrer-Policy"] = "no-referrer"
    return _cors(r)


def _cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
    r.headers["Access-Control-Allow-Headers"] = "Range, Content-Type, Accept, Origin"
    r.headers["Access-Control-Expose-Headers"] = "Content-Length, Content-Range, Accept-Ranges, Content-Type"
    r.headers["Access-Control-Max-Age"] = "600"
    return r


@capp.errorhandler(Exception)
def on_error(e):
    code = getattr(e, "code", 500)
    if not isinstance(code, int) or not 400 <= code < 600:
        code = 500
    if code == 500:
        print("cast endpoint error:", type(e).__name__, e)
    return Response(status=code)


def _fail(ip):
    now = time.time()
    hits_ = [t for t in _fails.get(ip, []) if now - t < 600] + [now]
    _fails[ip] = hits_
    if len(hits_) >= FAIL_LIMIT:
        _blocked[ip] = now + BLOCK_FOR
        _fails.pop(ip, None)
    return Response(status=404)


def _miss(token):
    """An unknown token: a strike for the asking address, unless it belonged to a cast that just ended."""
    if token in _ended:
        return Response(status=404)
    return _fail(_client())


def _cast(token):
    """The cast session and its current stream for a token, or (None, None)."""
    with _lock:
        cs = _tokens.get(token)
    if cs is None:
        return None, None
    s = cs.stream
    if s is None:
        return cs, None
    hits[token] = {"path": request.path.rsplit("/", 1)[-1], "at": time.time(), "client": _client()}
    return cs, s


@capp.route("/c/<token>/file.<ext>", methods=["GET", "HEAD"])
def media_file(token, ext):
    cs, s = _cast(token)
    if cs is None:
        return _miss(token)
    if s is None or s.mode != "direct":
        return Response(status=404)
    return mapi.serve_file(s)


@capp.get("/c/<token>/index.m3u8")
def media_playlist(token):
    cs, s = _cast(token)
    if cs is None:
        return _miss(token)
    if s is None or s.mode == "direct":
        return Response(status=404)
    return mapi.serve_playlist(s)


@capp.get("/c/<token>/init.mp4")
def media_init(token):
    cs, s = _cast(token)
    if cs is None:
        return _miss(token)
    if s is None or s.mode == "direct":
        return Response(status=404)
    return mapi.serve_init(s)


@capp.get("/c/<token>/seg/<int:n>.m4s")
def media_segment(token, n):
    cs, s = _cast(token)
    if cs is None:
        return _miss(token)
    if s is None or s.mode == "direct":
        return Response(status=404)
    return mapi.serve_segment(s, n)


@capp.get("/c/<token>/sub/<sub_id>.vtt")
def media_sub(token, sub_id):
    cs, s = _cast(token)
    if cs is None:
        return _miss(token)
    if s is None:
        return Response(status=404)
    r = mapi.serve_sub(s, sub_id, wait=40)
    r = r[0] if isinstance(r, tuple) else r
    if cs.dev["kind"] == "airplay" and r.status_code == 200:
        # HLS WebVTT renditions say where they sit on the video's timeline (90 kHz)
        text = r.get_data(as_text=True)
        start = int(round(float(s.info.get("start") or 0) * 90000))
        head, _, rest = text.partition("\n")
        r = Response(f"{head}\nX-TIMESTAMP-MAP=MPEGTS:{start},LOCAL:00:00:00.000\n{rest}", mimetype="text/vtt")
    return r


@capp.get("/c/<token>/master.m3u8")
def master_playlist(token):
    cs, s = _cast(token)
    if cs is None:
        return _miss(token)
    if s is None or s.mode == "direct":
        return Response(status=404)
    return Response(cs.master_playlist(), mimetype="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})


@capp.get("/c/<token>/subs/<sub_id>.m3u8")
def sub_playlist(token, sub_id):
    cs, s = _cast(token)
    if cs is None:
        return _miss(token)
    if s is None or not any(x["id"] == sub_id and x["kind"] == "text" for x in s.info["subs"]):
        return Response(status=404)
    return Response(cs.sub_playlist(sub_id), mimetype="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store"})


@capp.get("/c/<token>/art.jpg")
def artwork(token):
    """The poster/backdrop the TV shows while loading and in its own controls."""
    cs, _s = _cast(token)
    if cs is None:
        return _miss(token)
    path = cs.artwork_path()
    if not path:
        return Response(status=404)
    from .. import server
    with open(path, "rb") as f:
        data = f.read()
    return Response(data, mimetype=server.catalog().images.mimetype(path), headers={"Cache-Control": "max-age=3600"})
