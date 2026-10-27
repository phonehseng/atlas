"""The local API: a few routes other programs on this PC (the LocalAI assistant, scripts) may call.

Atlas's server listens on 127.0.0.1 at a random port, so it publishes where it is in a discovery file,
settings.DIR / "local-api.json" (%APPDATA%\\Marquee\\local-api.json, or MARQUEE_HOME), rewritten at every start and
removed on exit. The same content also goes to atlas-local-api.json next to it (the app's name since the rename;
local-api.json stays the main one, as other programs already read it):

    {"port": 51234, "token": "...", "pid": 4242, "version": "1.4.5", "base": "http://127.0.0.1:51234",
     "routes": ["GET /api/watched", "POST /api/watched", "DELETE /api/watched"], "started": "...Z"}

The token is random per install, kept (DPAPI-encrypted) in settings, and stable across restarts. Only routes wrapped
in @local_api accept it: a request with header X-Marquee-Token: <token> (or X-Atlas-Token) and no browser Origin (and no cross-site
Sec-Fetch-Site) is let through. Without the token only Atlas's own page may call them (same-origin fetches, which
WebView2/Chromium label Sec-Fetch-Site: same-origin); everything else gets 403. See docs/local-api.md."""
import atexit
import functools
import hmac
import json
import os
import secrets
import threading
import time
import urllib.parse

from flask import jsonify, request

from . import settings

ROUTES = []          # "METHOD /path" of every route marked as local API (filled by @local_api)
_state = {"port": None, "written": False}
_lock = threading.Lock()


def discovery_file():
    return settings.DIR / "local-api.json"


def discovery_files():
    """The discovery file and its copy under the app's new name."""
    return [discovery_file(), settings.DIR / "atlas-local-api.json"]


def token():
    """This install's token (made once, then kept in settings)."""
    with _lock:
        tok = settings.load().get("local_api_token") or ""
        if not tok:
            tok = secrets.token_urlsafe(32)
            settings.save({"local_api_token": tok})
        return tok


def publish(port):
    """Write the discovery file for this run (called when the server starts)."""
    try:
        from . import __version__
    except Exception:
        __version__ = ""
    data = {"port": int(port), "token": token(), "pid": os.getpid(), "version": __version__,
            "base": f"http://127.0.0.1:{int(port)}", "routes": sorted(set(ROUTES)),
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    for i, f in enumerate(discovery_files()):
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            tmp.replace(f)
            _state.update(port=int(port), written=True)
        except OSError as e:
            print(f"local API: couldn't write {f.name}:", e)
            if i == 0:
                return
    if not _state.get("atexit"):
        _state["atexit"] = True
        atexit.register(unpublish)


def unpublish():
    """Remove the discovery file, if it is still this process's (a newer Atlas may have replaced it)."""
    if not _state.get("written"):
        return
    for f in discovery_files():
        try:
            cur = json.loads(f.read_text(encoding="utf-8"))
            if cur.get("pid") == os.getpid():
                f.unlink()
        except (OSError, ValueError):
            pass
    _state["written"] = False


def _host_ok():
    """Only the loopback names this server answers on (a page on another site can't rebind its name to us)."""
    host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def check():
    """None when the request may use a local API route, else a (response, status) to send instead."""
    no = lambda msg: (jsonify(ok=False, message=msg), 403)
    if not _host_ok():
        return no("forbidden")
    origin = request.headers.get("Origin")
    site = (request.headers.get("Sec-Fetch-Site") or "").lower()
    sent = request.headers.get("X-Marquee-Token")
    if sent is None:
        sent = request.headers.get("X-Atlas-Token")
    if sent is not None:
        # The token is for programs, never for web pages: a browser always sends Origin / Sec-Fetch-Site with it.
        if origin or site not in ("", "none"):
            return no("the local API token can't be used from a web page")
        if not hmac.compare_digest(sent.encode(), token().encode()):
            return no("wrong token")
        request.environ["marquee.local_api"] = "token"
        return None
    if origin and urllib.parse.urlsplit(origin).netloc != request.host:
        return no("forbidden")
    if site == "same-origin":  # Atlas's own page
        request.environ["marquee.local_api"] = "page"
        return None
    return no("send the X-Marquee-Token (or X-Atlas-Token) header (see local-api.json)")


def caller():
    """'token' (another program), 'page' (Atlas's own window) or None."""
    return request.environ.get("marquee.local_api")


def local_api(*methods_paths):
    """Mark a route as part of the local API: @local_api("GET /api/watched", "POST /api/watched")."""
    ROUTES.extend(methods_paths)

    def wrap(fn):
        @functools.wraps(fn)
        def inner(*a, **kw):
            bad = check()
            if bad is not None:
                return bad
            if request.method in ("POST", "DELETE", "PUT", "PATCH") and request.content_length and not request.is_json:
                return jsonify(ok=False, message="send JSON"), 415
            return fn(*a, **kw)
        return inner
    return wrap
