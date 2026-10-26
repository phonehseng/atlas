"""Share to Google Drive: upload a file from this PC to the user's own Google Drive and hand back a link that only
people who have it can open (not searchable, no sign-in needed to view).

How it works
- Sign-in is OAuth 2.0 for installed apps: the system browser opens Google's consent page, Google redirects to a
  one-shot listener on 127.0.0.1 (random port), and the code is exchanged with PKCE. The only scope asked for is
  drive.file, so Marquee can see and change nothing in Drive except the files it uploaded itself.
- The refresh token is kept DPAPI-encrypted in settings ("gdrive_token"). Signing out revokes it at Google.
- The OAuth client (ID + secret) is the user's own (see docs/google-drive.md). A packaged build can embed a default
  through MARQUEE_GDRIVE_CLIENT_ID / MARQUEE_GDRIVE_CLIENT_SECRET or the DEFAULT_CLIENT_* constants below.
- Uploads use Drive's resumable protocol in 16 MB chunks on a background thread at background priority, with an
  optional speed cap. The session URI and offset are kept in %APPDATA%\\Marquee\\shares.json, so an upload picks up
  where it stopped after a network drop or an app restart.
- Files go into a "Marquee Shares" folder (made on first use). A video with subtitles next to it (same name,
  .srt/.ass/.ssa/.vtt) goes into its own subfolder and the folder is what gets shared.
- The share is a permission {type: anyone, role: reader, allowFileDiscovery: false}.

No Google client library: plain requests."""
import base64
import ctypes
import hashlib
import html
import io
import json
import mimetypes
import os
import queue
import re
import secrets
import threading
import time
import urllib.parse
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
from flask import Blueprint, jsonify, request

from . import settings

# ------------------------------------------------------------------ configuration
# A packaged build can embed its own OAuth client here (or through the environment). Desktop-app client secrets
# aren't confidential (Google says so for installed apps); PKCE is what protects the sign-in.
DEFAULT_CLIENT_ID = ""
DEFAULT_CLIENT_SECRET = ""

SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"

FOLDER_NAME = "Marquee Shares"
FOLDER_MIME = "application/vnd.google-apps.folder"
CHUNK = 16 * 1024 * 1024          # multiple of 256 KiB, as Drive requires (16-32 MB is a good size)
CONNECT_TIMEOUT = 300             # seconds to finish signing in in the browser
EXPIRY_CHECK = 30                 # seconds between checks for links Marquee has to turn off itself
SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt")
SHAREABLE_EXTS = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg",
                  ".gif") + SUB_EXTS
MIME = {".mkv": "video/x-matroska", ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
        ".avi": "video/x-msvideo", ".mov": "video/quicktime", ".ts": "video/mp2t", ".m2ts": "video/mp2t",
        ".srt": "application/x-subrip", ".ass": "text/x-ssa", ".ssa": "text/x-ssa", ".vtt": "text/vtt",
        ".gif": "image/gif"}

bp = Blueprint("share", __name__)


class ShareError(Exception):
    """Something the user should be told, as is. code = HTTP status for the API."""

    def __init__(self, message, code=400, reason=None):
        super().__init__(message)
        self.code = code
        self.reason = reason


class AuthError(ShareError):
    """Not signed in, or Google no longer accepts the sign-in: the user has to connect again."""

    def __init__(self, message="Connect Google Drive first (Settings > Sharing).", reason="needs_connect"):
        super().__init__(message, 401, reason)


class _Cancelled(Exception):
    pass


class _Retry(Exception):
    """A temporary problem (network, Google busy): wait and try again."""


# ------------------------------------------------------------------ small helpers
def _app():
    from . import server
    return server.APP


def notify(event, data):
    try:
        app = _app()
        if app is not None:
            app.notify_ui(event, data)
    except Exception as e:
        print("share: ui event failed:", e)


def client():
    """(client_id, client_secret) in effect: settings first, then the embedded/env default."""
    s = settings.load()
    cid = (s.get("gdrive_client_id") or os.environ.get("MARQUEE_GDRIVE_CLIENT_ID") or DEFAULT_CLIENT_ID).strip()
    sec = (s.get("gdrive_client_secret") or os.environ.get("MARQUEE_GDRIVE_CLIENT_SECRET")
           or DEFAULT_CLIENT_SECRET).strip()
    return cid, sec


def _human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def _rfc3339(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _when(ts):
    return datetime.fromtimestamp(ts).strftime("%d %b %Y %H:%M")


def _pathkey(p):
    return os.path.normcase(os.path.abspath(p))


def qr_data_uri(text):
    """A PNG QR code of text as a data: URI (for opening the link on a phone)."""
    import qrcode
    from qrcode.image.pil import PilImage
    qr = qrcode.QRCode(border=2, box_size=10, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(image_factory=PilImage, fill_color="#0d1738", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def copy_to_clipboard(text):
    """Put text on the Windows clipboard. True if it worked."""
    try:
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        k32.GlobalAlloc.restype = ctypes.c_void_p
        k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [ctypes.c_void_p]
        k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        k32.GlobalFree.argtypes = [ctypes.c_void_p]
        u32.SetClipboardData.restype = ctypes.c_void_p
        u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        u32.OpenClipboard.argtypes = [ctypes.c_void_p]
        data = (text + "\0").encode("utf-16-le")
        for _ in range(10):  # another app may hold the clipboard for a moment
            if u32.OpenClipboard(None):
                break
            time.sleep(0.05)
        else:
            return False
        try:
            u32.EmptyClipboard()
            h = k32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
            p = k32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            k32.GlobalUnlock(h)
            if not u32.SetClipboardData(13, h):  # CF_UNICODETEXT; the clipboard owns h on success
                k32.GlobalFree(h)
                return False
            return True
        finally:
            u32.CloseClipboard()
    except Exception as e:
        print("share: clipboard failed:", e)
        return False


def _background_priority():
    """Lower this thread's CPU and disk priority so playback never stutters while an upload runs."""
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentThread.restype = ctypes.c_void_p
        k32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        h = k32.GetCurrentThread()
        if not k32.SetThreadPriority(h, 0x00010000):  # THREAD_MODE_BACKGROUND_BEGIN (CPU + I/O)
            k32.SetThreadPriority(h, -1)  # THREAD_PRIORITY_BELOW_NORMAL
    except Exception:
        pass


# ------------------------------------------------------------------ OAuth (installed app, loopback + PKCE)
def pkce_pair():
    """(code_verifier, code_challenge) per RFC 7636 with S256."""
    verifier = secrets.token_urlsafe(64)[:86]  # 43..128 unreserved characters
    return verifier, challenge_for(verifier)


def challenge_for(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


def auth_url(client_id, redirect_uri, challenge, state):
    q = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
         "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
         "access_type": "offline", "prompt": "consent"}
    return AUTH_URL + "?" + urllib.parse.urlencode(q)


_PAGE = """<!doctype html><meta charset="utf-8"><title>Marquee</title>
<body style="font:16px system-ui,sans-serif;background:#0d1738;color:#fff;display:grid;place-items:center;height:90vh">
<div style="text-align:center"><h2>{title}</h2><p>{text}</p></div></body>"""


class _Receiver(HTTPServer):
    """One-shot loopback listener for Google's redirect."""
    result = None


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if not q.get("code") and not q.get("error"):
            self.send_response(404)
            self.end_headers()
            return
        self.server.result = {k: v[0] for k, v in q.items()}
        ok = "code" in q and q.get("state", [""])[0] == self.server.state
        title = "Google Drive is connected" if ok else "Couldn't connect Google Drive"
        text = ("You can close this tab and go back to Marquee." if ok else
                html.escape(q.get("error", ["the reply didn't match this sign-in"])[0]) + ". Go back to Marquee and try again.")
        body = _PAGE.format(title=title, text=text).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


_connect_lock = threading.Lock()
_token = {"access": "", "expires": 0.0}
_token_lock = threading.Lock()


def connect(timeout=CONNECT_TIMEOUT, open_browser=None):
    """Run the whole sign-in: open the browser, wait for the redirect, exchange the code. Returns the account email
    (or ""). Raises ShareError with a readable message."""
    cid, sec = client()
    if not cid:
        raise ShareError("Add your Google OAuth client ID first (Settings > Sharing; see the guide).", 400,
                         "needs_client")
    if not _connect_lock.acquire(blocking=False):
        raise ShareError("A Google sign-in is already open in your browser.", 409)
    try:
        srv = _Receiver(("127.0.0.1", 0), _Handler)
        srv.timeout = 1
        srv.state = secrets.token_urlsafe(24)
        redirect = f"http://127.0.0.1:{srv.server_address[1]}/"
        verifier, challenge = pkce_pair()
        url = auth_url(cid, redirect, challenge, srv.state)
        try:
            (open_browser or webbrowser.open)(url)
            deadline = time.time() + timeout
            while srv.result is None and time.time() < deadline:
                srv.handle_request()
        finally:
            srv.server_close()
        r = srv.result
        if r is None:
            raise ShareError("Sign-in timed out. Try Connect again.", 408)
        if r.get("error"):
            raise ShareError("Google sign-in was cancelled." if r["error"] == "access_denied"
                             else f"Google sign-in failed: {r['error']}", 400)
        if r.get("state") != srv.state:
            raise ShareError("The sign-in reply didn't match (possible stale tab). Try again.", 400)
        granted = r.get("scope")
        if granted and SCOPE not in granted.split():
            raise ShareError("Google Drive access wasn't granted; tick the Drive box on Google's page.", 400)
        tok = _token_request({"client_id": cid, "client_secret": sec, "code": r["code"], "code_verifier": verifier,
                              "grant_type": "authorization_code", "redirect_uri": redirect})
        if not tok.get("refresh_token"):
            raise ShareError("Google didn't return a long-lived sign-in. Remove Marquee's access at "
                             "myaccount.google.com/permissions and connect again.", 400)
        settings.save({"gdrive_token": tok["refresh_token"]})
        with _token_lock:
            _token.update(access=tok.get("access_token", ""), expires=time.time() + int(tok.get("expires_in", 0)) - 60)
        _about_cache.clear()
        email = ""
        try:
            email = (about().get("user") or {}).get("emailAddress", "")
        except Exception:
            pass
        _resume_paused()
        return email
    finally:
        _connect_lock.release()


def _token_request(data):
    try:
        r = requests.post(TOKEN_URL, data=data, timeout=30)
    except requests.RequestException as e:
        raise _Retry(f"network: {e.__class__.__name__}")
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code == 200:
        return body
    err = body.get("error", "")
    if err == "invalid_grant":
        raise AuthError("Google sign-in has expired or was revoked. Connect Google Drive again (Settings > Sharing).",
                        "reauth")
    if err in ("invalid_client", "unauthorized_client"):
        raise ShareError("Google rejected the OAuth client ID/secret. Check them in Settings > Sharing.", 400,
                         "bad_client")
    if r.status_code >= 500:
        raise _Retry(f"Google token service error {r.status_code}")
    raise ShareError(f"Google sign-in failed: {body.get('error_description') or err or r.status_code}", 400)


def signed_in():
    return bool(settings.load().get("gdrive_token"))


def access_token(force=False):
    with _token_lock:
        if not force and _token["access"] and time.time() < _token["expires"]:
            return _token["access"]
        refresh = settings.load().get("gdrive_token")
        if not refresh:
            raise AuthError()
        cid, sec = client()
        try:
            tok = _token_request({"client_id": cid, "client_secret": sec, "refresh_token": refresh,
                                  "grant_type": "refresh_token"})
        except AuthError:
            settings.save({"gdrive_token": ""})
            _token.update(access="", expires=0)
            notify("share-auth", {"message": "Google sign-in has expired. Connect Google Drive again."})
            raise
        _token.update(access=tok["access_token"], expires=time.time() + int(tok.get("expires_in", 3600)) - 60)
        return _token["access"]


def disconnect():
    """Revoke the sign-in at Google and forget it here. Links already shared keep working (they belong to the
    Drive files); stop them first if they should go."""
    refresh = settings.load().get("gdrive_token")
    _pause_all("Signed out of Google Drive")
    revoked = False
    if refresh:
        try:
            r = requests.post(REVOKE_URL, data={"token": refresh}, timeout=15)
            revoked = r.status_code in (200, 400)  # 400 = already invalid
        except requests.RequestException:
            pass
    settings.save({"gdrive_token": ""})
    with _token_lock:
        _token.update(access="", expires=0)
    _about_cache.clear()
    return revoked


# ------------------------------------------------------------------ Drive API calls
_http = requests.Session()


def _reason(r):
    try:
        err = r.json().get("error") or {}
        errs = err.get("errors") or [{}]
        return errs[0].get("reason") or err.get("status") or "", err.get("message") or ""
    except Exception:
        return "", ""


def call(method, url, *, ok=(200,), **kw):
    """One Drive request with the bearer token; refreshes once on 401. Returns the response.
    Network failures, 429 and 5xx raise _Retry; quota/permission problems raise ShareError."""
    for attempt in (0, 1):
        headers = dict(kw.pop("headers", None) or {})
        headers["Authorization"] = "Bearer " + access_token(force=attempt == 1)
        try:
            r = _http.request(method, url, headers=headers, timeout=kw.pop("timeout", 60), **kw)
        except requests.RequestException as e:
            raise _Retry(f"network: {e.__class__.__name__}")
        kw["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
        if r.status_code == 401 and attempt == 0:
            continue
        if r.status_code in ok:
            return r
        reason, msg = _reason(r)
        if r.status_code == 401:
            raise AuthError("Google sign-in has expired. Connect Google Drive again (Settings > Sharing).", "reauth")
        if r.status_code == 429 or r.status_code >= 500 or reason in ("rateLimitExceeded", "userRateLimitExceeded",
                                                                       "backendError"):
            raise _Retry(f"Drive busy ({r.status_code} {reason})")
        if reason in ("storageQuotaExceeded", "quotaExceeded"):
            raise ShareError("Your Google Drive is full.", 507, "quota")
        if reason == "accessNotConfigured":
            raise ShareError("The Google Drive API isn't enabled in your Google Cloud project (see the guide).", 400,
                             "api_disabled")
        if r.status_code == 404:
            raise ShareError("That file is no longer on Google Drive.", 404, "not_found")
        raise ShareError(f"Google Drive said: {msg or r.status_code}", r.status_code if r.status_code < 600 else 502,
                         reason)
    raise AuthError()


_about_cache = {}


def about(max_age=60):
    """{user: {displayName, emailAddress}, storageQuota: {limit?, usage}} (limit is absent for unlimited)."""
    hit = _about_cache.get("v")
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    r = call("GET", f"{API}/about", params={"fields": "user(displayName,emailAddress),storageQuota(limit,usage)"})
    v = r.json()
    _about_cache["v"] = (time.time(), v)
    return v


def check_quota(needed):
    """Raise ShareError if `needed` bytes don't fit in the account's free Drive space."""
    q = (about(max_age=0).get("storageQuota") or {})
    if not q.get("limit"):
        return  # unlimited
    limit, usage = int(q["limit"]), int(q.get("usage") or 0)
    free = max(0, limit - usage)
    if needed > free:
        raise ShareError(f"Not enough space on Google Drive: this needs {_human(needed)}, but only {_human(free)} of "
                         f"{_human(limit)} is free. Free up space in Drive (or the trash) and try again.", 507, "quota")


def create_folder(name, parent=None):
    meta = {"name": name, "mimeType": FOLDER_MIME}
    if parent:
        meta["parents"] = [parent]
    return call("POST", f"{API}/files", params={"fields": "id"}, json=meta).json()["id"]


def root_folder():
    """The "Marquee Shares" folder's id, made on first use (and again if it was deleted or trashed)."""
    with _lock:
        fid = _store().get("folder_id")
    if fid and fid in _verified:
        return fid
    if fid:
        try:
            meta = call("GET", f"{API}/files/{fid}", params={"fields": "id,trashed"}).json()
            if not meta.get("trashed"):
                _verified.add(fid)
                return fid
        except ShareError as e:
            if e.reason != "not_found":
                raise
    q = (f"name = '{FOLDER_NAME}' and mimeType = '{FOLDER_MIME}' and trashed = false and 'root' in parents")
    found = call("GET", f"{API}/files", params={"q": q, "fields": "files(id)", "spaces": "drive"}).json()
    fid = (found.get("files") or [{}])[0].get("id") or create_folder(FOLDER_NAME)
    with _lock:
        _store()["folder_id"] = fid
        _save()
    _verified.add(fid)
    return fid


_verified = set()


def add_permission(file_id, expires=None):
    """Make file_id readable by anyone with the link, not discoverable. Tries Drive's own expiry first.
    Returns (permission_id, expiry_mode) where expiry_mode is "drive", "marquee" or None."""
    body = {"type": "anyone", "role": "reader", "allowFileDiscovery": False}
    url = f"{API}/files/{file_id}/permissions"
    if expires:
        try:
            r = call("POST", url, params={"fields": "id,expirationTime"},
                     json={**body, "expirationTime": _rfc3339(expires)})
            if r.json().get("expirationTime"):
                return r.json()["id"], "drive"
            return r.json()["id"], "marquee"  # accepted but ignored: Marquee turns it off itself
        except ShareError as e:
            if e.code not in (400, 403):
                raise
            # Drive only allows expiry on some permission/account types: fall back to Marquee doing it
    r = call("POST", url, params={"fields": "id"}, json=body)
    return r.json()["id"], ("marquee" if expires else None)


def remove_permission(file_id, perm_id):
    try:
        call("DELETE", f"{API}/files/{file_id}/permissions/{perm_id}", ok=(200, 204))
    except ShareError as e:
        if e.reason != "not_found":
            raise


def web_link(file_id):
    return call("GET", f"{API}/files/{file_id}", params={"fields": "webViewLink"}).json().get("webViewLink", "")


def delete_drive_file(file_id):
    try:
        call("DELETE", f"{API}/files/{file_id}", ok=(200, 204))
    except ShareError as e:
        if e.reason != "not_found":
            raise


# ------------------------------------------------------------------ shares.json
_lock = threading.RLock()
_data = None


def _file():
    return Path(settings.DIR) / "shares.json"


def _store():
    global _data
    if _data is None:
        try:
            _data = json.loads(_file().read_text(encoding="utf-8"))
        except Exception:
            _data = {}
        _data.setdefault("folder_id", None)
        _data.setdefault("shares", {})
    return _data


def _save():
    with _lock:
        f = _file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(_store(), indent=1), encoding="utf-8")
        tmp.replace(f)


def _get(sid):
    with _lock:
        rec = _store()["shares"].get(sid)
        if not rec:
            raise ShareError("That share isn't known.", 404)
        return rec


def _update(sid, **kw):
    with _lock:
        rec = _store()["shares"].get(sid)
        if rec is not None:
            rec.update(kw)
            _save()
        return rec


def public(rec, qr=False):
    """A share as the UI sees it (session URIs stay in Python)."""
    out = {k: rec.get(k) for k in ("id", "path", "name", "title", "kind", "item_id", "size", "created", "expires",
                                   "expiry_mode", "status", "message", "link", "target_id", "reason")}
    out["subtitles"] = [os.path.basename(f["path"]) for f in rec.get("files", [])[1:]]
    out["folder"] = bool(rec.get("folder_id"))
    live = _live.get(rec["id"])
    if live:
        out["progress"] = live
    if qr and rec.get("link") and rec.get("status") == "ready":
        out["qr"] = qr_data_uri(rec["link"])
    return out


def shares():
    with _lock:
        return sorted((dict(r) for r in _store()["shares"].values()), key=lambda r: r.get("created") or 0, reverse=True)


# ------------------------------------------------------------------ sharing a file
def subtitles_for(path):
    """Subtitle files beside a video with the same name (Movie.srt, Movie.en.srt, Movie.forced.ass...)."""
    p = Path(path)
    if p.suffix.lower() in SUB_EXTS:
        return []
    out = []
    try:
        for f in sorted(p.parent.iterdir()):
            if f.is_file() and f.suffix.lower() in SUB_EXTS and (f.stem == p.stem or f.name.startswith(p.stem + ".")):
                out.append(str(f))
    except OSError:
        pass
    return out


def _title_for(info, path):
    if info:
        if info.get("type") == "Episode" and info.get("series"):
            se = ""
            if info.get("season") is not None and info.get("episode") is not None:
                se = f" S{int(info['season']):02d}E{int(info['episode']):02d}"
            return f"{info['series']}{se}"
        if info.get("name"):
            return f"{info['name']} ({info['year']})" if info.get("year") else info["name"]
    return Path(path).stem


def _safe_name(s):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", s or "").strip() or "Shared"


def _fileinfo(path):
    st = os.stat(path)
    return {"path": path, "size": st.st_size, "mtime": int(st.st_mtime), "drive_id": None, "session": None,
            "offset": 0, "done": False}


def share_file(path, kind="file", item_id=None, info=None, expires=None):
    """Share a local file: returns the share (public form). Reuses an existing link for the same file; otherwise
    checks Drive space and queues the upload (progress via "share-progress", done via "share-ready")."""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise ShareError("That file isn't on this PC, so it can't be shared (only files stored on this PC can).", 404)
    if not signed_in():
        raise AuthError()
    st = os.stat(path)
    key = _pathkey(path)
    with _lock:
        existing = next((r for r in _store()["shares"].values() if _pathkey(r["path"]) == key
                         and r.get("size") == st.st_size and r.get("status") not in ("error", "cancelled")), None)
    if existing:
        return _reuse(existing, expires)
    subs = subtitles_for(path)
    files = [_fileinfo(path)] + [_fileinfo(s) for s in subs]
    total = sum(f["size"] for f in files)
    check_quota(total)
    sid = uuid.uuid4().hex[:12]
    rec = {"id": sid, "path": path, "name": os.path.basename(path), "title": _title_for(info, path), "kind": kind,
           "item_id": item_id, "size": st.st_size, "total": total, "created": int(time.time()),
           "expires": int(expires) if expires else None, "expiry_mode": None, "status": "queued", "message": "",
           "link": "", "target_id": None, "folder_id": None, "permission_id": None, "files": files}
    with _lock:
        _store()["shares"][sid] = rec
        _save()
    _enqueue(sid)
    return public(rec)


def _reuse(rec, expires):
    """The same file was shared before: hand back its link, turning sharing back on if it had been stopped."""
    sid = rec["id"]
    if rec["status"] in ("queued", "uploading", "paused"):
        if expires is not None:
            _update(sid, expires=int(expires) if expires else None)
        if rec["status"] == "paused":
            _enqueue(sid)
        return public(rec)
    if rec["status"] == "ready":
        try:  # still on Drive?
            meta = call("GET", f"{API}/files/{rec['target_id']}", params={"fields": "id,trashed"}).json()
            gone = bool(meta.get("trashed"))
        except ShareError as e:
            if e.reason != "not_found":
                raise
            gone = True
        if gone:
            with _lock:
                _store()["shares"].pop(sid, None)
                _save()
            return share_file(rec["path"], rec.get("kind", "file"), rec.get("item_id"), None, expires)
        if expires is not None and (int(expires) if expires else None) != rec.get("expires"):
            set_expiry(sid, expires)
        copy_to_clipboard(rec["link"])
        out = public(_get(sid), qr=True)
        out["reused"] = True
        return out
    if rec["status"] in ("stopped", "expired"):
        perm, mode = add_permission(rec["target_id"], expires)
        _update(sid, permission_id=perm, expiry_mode=mode, status="ready", expires=int(expires) if expires else None,
                message=_expiry_message(mode, expires))
        copy_to_clipboard(rec["link"])
        out = public(_get(sid), qr=True)
        out["reused"] = True
        notify("share-ready", {"id": sid, "link": rec["link"], "name": rec["name"]})
        return out
    return public(rec)


def _expiry_message(mode, expires):
    if not expires:
        return ""
    if mode == "drive":
        return f"Google turns this link off on {_when(expires)}."
    return (f"Google Drive doesn't support automatic expiry for public links on this account, so Marquee will turn "
            f"the link off on {_when(expires)}. That only happens while Marquee is running; if it's closed then, the "
            f"link stays live until Marquee next starts.")


def set_expiry(sid, expires):
    """Change or clear (expires=None/0) when a ready link turns off."""
    rec = _get(sid)
    expires = int(expires) if expires else None
    if rec["status"] != "ready":
        _update(sid, expires=expires)
        return public(_get(sid))
    old = rec.get("permission_id")
    perm, mode = add_permission(rec["target_id"], expires)
    if old and old != perm:
        remove_permission(rec["target_id"], old)
    _update(sid, permission_id=perm, expiry_mode=mode, expires=expires, message=_expiry_message(mode, expires))
    return public(_get(sid), qr=True)


def stop_sharing(sid):
    """Turn the link off (files stay on Drive; sharing the same file again turns it back on). An upload that's
    still running is cancelled instead."""
    rec = _get(sid)
    if rec["status"] in ("queued", "uploading", "paused"):
        return delete(sid)
    if rec.get("permission_id") and rec.get("target_id"):
        remove_permission(rec["target_id"], rec["permission_id"])
    _update(sid, status="stopped", permission_id=None, expiry_mode=None, message="Sharing stopped.")
    return public(_get(sid))


def delete(sid):
    """Delete the share's files from Drive (permanently) and forget it."""
    rec = _get(sid)
    ev = _cancel.get(sid)
    if ev:
        ev.set()
        _wait_idle(sid)
    rec = _get(sid)
    for f in rec.get("files", []):
        if f.get("session") and not f.get("done"):
            try:
                _http.delete(f["session"], timeout=10)  # cancels the resumable session
            except requests.RequestException:
                pass
    if rec.get("folder_id"):
        delete_drive_file(rec["folder_id"])
    else:
        for f in rec.get("files", []):
            if f.get("drive_id"):
                delete_drive_file(f["drive_id"])
    with _lock:
        _store()["shares"].pop(sid, None)
        _save()
    _live.pop(sid, None)
    return {"id": sid, "status": "deleted"}


# ------------------------------------------------------------------ the upload worker
_queue = queue.Queue()
_cancel = {}           # share id -> Event (set = stop this upload now)
_live = {}             # share id -> progress dict for the UI
_busy = set()
_worker = None
_expiry = None
_stop = threading.Event()
_started_lock = threading.Lock()


def init(app=None):
    """Start the worker and the expiry watcher, and pick up unfinished uploads (call once when the app starts;
    the routes also call it lazily)."""
    global _worker, _expiry
    with _started_lock:
        if _worker and _worker.is_alive():
            return
        _stop.clear()
        _worker = threading.Thread(target=_work, name="share-upload", daemon=True)
        _worker.start()
        _expiry = threading.Thread(target=_expiry_loop, name="share-expiry", daemon=True)
        _expiry.start()
    with _lock:
        pending = [sid for sid, r in _store()["shares"].items() if r.get("status") in ("queued", "uploading", "paused")
                   and r.get("reason") != "reauth"]
    if pending and signed_in():
        for sid in pending:
            _enqueue(sid)


def shutdown(timeout=3):
    """Stop uploading (progress is kept in shares.json; it resumes on the next start)."""
    _stop.set()
    for ev in list(_cancel.values()):
        ev.set()
    _queue.put(None)
    if _worker:
        _worker.join(timeout)


def _enqueue(sid):
    if sid in _busy or sid in _cancel:
        return
    _cancel[sid] = threading.Event()
    _update(sid, status="queued" if _get(sid)["status"] != "uploading" else "uploading", reason=None)
    _queue.put(sid)


def _wait_idle(sid, timeout=15):
    end = time.time() + timeout
    while sid in _busy and time.time() < end:
        time.sleep(0.05)


def _pause_all(message):
    for sid, ev in list(_cancel.items()):
        ev.set()
        _wait_idle(sid, 5)
        _update(sid, status="paused", message=message)


def _resume_paused():
    with _lock:
        paused = [sid for sid, r in _store()["shares"].items() if r.get("status") in ("paused", "queued", "uploading")]
    init()
    for sid in paused:
        _enqueue(sid)


def _work():
    _background_priority()
    while not _stop.is_set():
        sid = _queue.get()
        if sid is None:
            break
        ev = _cancel.get(sid)
        if ev is None or ev.is_set():
            _cancel.pop(sid, None)
            continue
        _busy.add(sid)
        try:
            _run(sid, ev)
        except Exception as e:  # never let the worker die
            print("share: upload failed:", e.__class__.__name__, e)
            _update(sid, status="error", message=str(e) or e.__class__.__name__)
            notify("share-progress", {"id": sid, "status": "error", "message": str(e)})
        finally:
            _busy.discard(sid)
            _cancel.pop(sid, None)


def _retrying(sid, ev, fn):
    """Run fn(), waiting out network drops and busy replies (forever, while not cancelled)."""
    delay = 2
    while True:
        if ev.is_set():
            raise _Cancelled()
        try:
            return fn()
        except _Retry as e:
            print("share: waiting to retry:", e)
            _update(sid, status="paused", message="Waiting for the network; the upload resumes by itself.")
            _emit(sid, force=True)
            if ev.wait(delay):
                raise _Cancelled()
            delay = min(60, delay * 2)


def _run(sid, ev):
    rec = _get(sid)
    _live[sid] = {"sent": sum(f["offset"] if not f["done"] else f["size"] for f in rec["files"]),
                  "total": rec.get("total") or sum(f["size"] for f in rec["files"]), "speed": 0, "eta": None,
                  "file": rec["name"], "_samples": [], "_at": 0}
    try:
        root = _retrying(sid, ev, root_folder)
        rec = _get(sid)
        if len(rec["files"]) > 1 and not rec.get("folder_id"):
            fid = _retrying(sid, ev, lambda: create_folder(_safe_name(rec["title"]), root))
            _update(sid, folder_id=fid)
        parent = _get(sid).get("folder_id") or root
        _update(sid, status="uploading", message="")
        for i in range(len(rec["files"])):
            f = _get(sid)["files"][i]
            if not f["done"]:
                _upload_one(sid, ev, i, parent)
        rec = _get(sid)
        target = rec.get("folder_id") or rec["files"][0]["drive_id"]
        perm, mode = _retrying(sid, ev, lambda: add_permission(target, rec.get("expires")))
        link = _retrying(sid, ev, lambda: web_link(target))
        _update(sid, target_id=target, permission_id=perm, expiry_mode=mode, link=link, status="ready",
                message=_expiry_message(mode, rec.get("expires")), finished=int(time.time()))
        _live.pop(sid, None)
        copied = copy_to_clipboard(link)
        notify("share-ready", {"id": sid, "link": link, "name": rec["name"], "copied": copied,
                               "message": _expiry_message(mode, rec.get("expires"))})
    except _Cancelled:
        rec = _store()["shares"].get(sid)
        if rec and rec.get("status") in ("uploading", "queued"):
            _update(sid, status="paused", message="Paused; resumes when Marquee starts again.")
    except AuthError as e:
        _update(sid, status="paused", message=str(e), reason="reauth")
        notify("share-auth", {"message": str(e)})
        _emit(sid, force=True)
    except ShareError as e:
        _update(sid, status="error", message=str(e), reason=e.reason)
        _emit(sid, force=True)
        _live.pop(sid, None)


def _upload_one(sid, ev, i, parent):
    def file_rec():
        return _get(sid)["files"][i]

    def save_file(**kw):
        with _lock:
            _get(sid)["files"][i].update(kw)
            _save()

    f = file_rec()
    try:
        st = os.stat(f["path"])
    except OSError:
        raise ShareError(f"{os.path.basename(f['path'])} is no longer on this PC; the upload can't continue.", 404)
    if st.st_size != f["size"] or int(st.st_mtime) != f["mtime"]:  # changed since the upload began: start over
        save_file(size=st.st_size, mtime=int(st.st_mtime), session=None, offset=0)
        f = file_rec()
    _live[sid]["file"] = os.path.basename(f["path"])
    total = f["size"]
    mime = MIME.get(Path(f["path"]).suffix.lower()) or mimetypes.guess_type(f["path"])[0] or "application/octet-stream"

    def start_session():
        r = call("POST", f"{UPLOAD}/files", ok=(200,),
                 params={"uploadType": "resumable", "fields": "id,name,size,webViewLink"},
                 headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(total),
                          "Content-Type": "application/json; charset=UTF-8"},
                 json={"name": os.path.basename(f["path"]), "parents": [parent]})
        loc = r.headers.get("Location")
        if not loc:
            raise _Retry("no upload session returned")
        save_file(session=loc, offset=0)
        return loc

    def query(session):
        """Ask Drive how much of the file it has. Returns offset, or the finished file's JSON."""
        r = _session_put(session, b"", {"Content-Range": f"bytes */{total}"})
        if r.status_code in (200, 201):
            return r.json()
        if r.status_code == 308:
            return _range_end(r)
        if r.status_code in (404, 410):
            return None  # session expired (they last about a week): start a new one
        _session_error(r)

    session = f.get("session")
    offset = f.get("offset") or 0
    if session:
        got = _retrying(sid, ev, lambda: query(session))
        if isinstance(got, dict):
            save_file(drive_id=got["id"], done=True, offset=total, session=None)
            return
        if got is None:
            session, offset = None, 0
        else:
            offset = got
    if not session:
        session = _retrying(sid, ev, start_session)
        offset = 0
    save_file(offset=offset)
    _set_sent(sid)

    while True:
        if ev.is_set():
            raise _Cancelled()
        end = min(total, offset + CHUNK)
        n = end - offset
        body = _Body(f["path"], offset, n, ev, lambda k: _progress(sid, k))
        headers = {"Content-Range": f"bytes {offset}-{end - 1}/{total}" if total else f"bytes */{total}"}
        try:
            r = _session_put(session, body, headers)
        except _Retry:
            if ev.is_set():
                raise _Cancelled()
            _update(sid, status="paused", message="Connection lost; resuming the upload...")
            _emit(sid, force=True)
            got = _retrying(sid, ev, lambda: query(session))  # how far did it get before the drop?
            if isinstance(got, dict):
                save_file(drive_id=got["id"], done=True, offset=total, session=None)
                return
            if got is None:
                session = _retrying(sid, ev, start_session)
                offset = 0
            else:
                offset = got
            save_file(offset=offset)
            _set_sent(sid)
            continue
        if r.status_code in (200, 201):
            save_file(drive_id=r.json()["id"], done=True, offset=total, session=None)
            _set_sent(sid)
            _update(sid, status="uploading", message="")
            _emit(sid, force=True)
            return
        if r.status_code == 308:
            offset = _range_end(r)
            save_file(offset=offset)
            _set_sent(sid)
            _update(sid, status="uploading", message="")
            _emit(sid)
            continue
        if r.status_code in (404, 410):
            session = _retrying(sid, ev, start_session)
            offset = 0
            continue
        if r.status_code == 401:
            access_token(force=True)
            continue
        try:
            _session_error(r)
        except _Retry:
            _update(sid, status="paused", message="Google Drive is busy; retrying...")
            _emit(sid, force=True)
            if ev.wait(3):
                raise _Cancelled()
            got = _retrying(sid, ev, lambda: query(session))
            if isinstance(got, dict):
                save_file(drive_id=got["id"], done=True, offset=total, session=None)
                return
            offset = got or 0
            save_file(offset=offset)


def _session_put(session, data, headers):
    h = dict(headers)
    h["Content-Length"] = str(len(data))
    h["Authorization"] = "Bearer " + access_token()
    try:
        return _http.put(session, data=data, headers=h, timeout=(20, 120))
    except requests.RequestException as e:
        raise _Retry(f"network: {e.__class__.__name__}")


def _session_error(r):
    reason, msg = _reason(r)
    if r.status_code == 429 or r.status_code >= 500 or reason in ("rateLimitExceeded", "userRateLimitExceeded"):
        raise _Retry(f"Drive busy ({r.status_code})")
    if reason in ("storageQuotaExceeded", "quotaExceeded"):
        raise ShareError("Your Google Drive is full; the upload stopped.", 507, "quota")
    raise ShareError(f"Google Drive refused the upload: {msg or r.status_code}", 502, reason)


def _range_end(r):
    """Next byte Drive wants, from a 308's Range header ("bytes=0-1234" -> 1235; no header -> 0)."""
    m = re.match(r"bytes=(\d+)-(\d+)", r.headers.get("Range", ""))
    return int(m.group(2)) + 1 if m else 0


class _Body:
    """A slice of a file as a request body: read in small pieces so the speed cap, cancel and progress work
    while a chunk is on its way."""

    def __init__(self, path, offset, length, ev, on_bytes):
        self.f = open(path, "rb")
        self.f.seek(offset)
        self.left = length
        self.length = length
        self.ev = ev
        self.on_bytes = on_bytes
        self.t0 = time.monotonic()
        self.sent = 0

    def __len__(self):
        return self.length

    def read(self, n=65536):
        if self.ev.is_set():
            self.f.close()
            raise _Cancelled()
        if self.left <= 0:
            self.f.close()
            return b""
        n = min(n if n and n > 0 else 65536, self.left, 256 * 1024)
        cap = _cap_bytes()
        if cap:
            ahead = self.sent / cap - (time.monotonic() - self.t0)
            if ahead > 0:
                self.ev.wait(min(ahead, 2))
        data = self.f.read(n)
        if not data:
            self.f.close()
            return b""
        self.left -= len(data)
        self.sent += len(data)
        self.on_bytes(len(data))
        return data


_cap_cache = {"t": 0.0, "v": 0.0}


def _cap_bytes():
    """The upload speed cap in bytes/s (setting share_speed_cap in Mbit/s; 0 = none). Re-read every 2 s so a
    change in Settings applies to the upload that's running."""
    now = time.monotonic()
    if now - _cap_cache["t"] > 2:
        try:
            mbps = float(settings.load().get("share_speed_cap") or 0)
        except (TypeError, ValueError):
            mbps = 0
        _cap_cache.update(t=now, v=mbps * 125_000 if mbps > 0 else 0)
    return _cap_cache["v"]


def _set_sent(sid):
    rec = _store()["shares"].get(sid)
    live = _live.get(sid)
    if rec and live:
        live["sent"] = sum(f["size"] if f["done"] else f["offset"] for f in rec["files"])


def _progress(sid, n):
    live = _live.get(sid)
    if not live:
        return
    live["sent"] = min(live["total"], live["sent"] + n)
    _emit(sid)


def _emit(sid, force=False):
    live = _live.get(sid)
    rec = _store()["shares"].get(sid)
    if not live or not rec:
        return
    now = time.monotonic()
    s = live["_samples"]
    s.append((now, live["sent"]))
    while len(s) > 2 and now - s[0][0] > 15:
        s.pop(0)
    if len(s) >= 2 and s[-1][0] > s[0][0]:
        live["speed"] = max(0, (s[-1][1] - s[0][1]) / (s[-1][0] - s[0][0]))
    live["eta"] = int((live["total"] - live["sent"]) / live["speed"]) if live["speed"] > 0 else None
    if not force and now - live["_at"] < 0.5:
        return
    live["_at"] = now
    total = live["total"] or 1
    notify("share-progress", {"id": sid, "name": rec["name"], "file": live["file"], "sent": live["sent"],
                              "total": live["total"], "fraction": round(live["sent"] / total, 4),
                              "speed": int(live["speed"]), "eta": live["eta"], "status": rec["status"],
                              "message": rec.get("message", "")})


def progress_of(sid):
    live = _live.get(sid)
    if not live:
        return None
    return {k: v for k, v in live.items() if not k.startswith("_")}


# ------------------------------------------------------------------ links that Marquee turns off itself
def _expiry_loop():
    while not _stop.is_set():
        try:
            expire_due()
        except Exception as e:
            print("share: expiry check failed:", e)
        _stop.wait(EXPIRY_CHECK)


def expire_due(now=None):
    """Turn off links whose time has come. Returns the ids turned off."""
    now = now or time.time()
    with _lock:
        due = [r for r in _store()["shares"].values()
               if r.get("status") == "ready" and r.get("expires") and r["expires"] <= now]
    done = []
    for r in due:
        try:
            if r.get("permission_id") and r.get("expiry_mode") != "drive":
                remove_permission(r["target_id"], r["permission_id"])
            elif r.get("permission_id"):  # Google should already have done it; make sure
                try:
                    remove_permission(r["target_id"], r["permission_id"])
                except ShareError:
                    pass
            _update(r["id"], status="expired", permission_id=None, message="The link expired and was turned off.")
            notify("share-progress", {"id": r["id"], "status": "expired", "name": r["name"]})
            done.append(r["id"])
        except (_Retry, AuthError) as e:
            print("share: couldn't expire yet:", e)
    return done


# ------------------------------------------------------------------ resolving what to share
def _clips_root():
    from . import clips
    return Path(clips.root())


def _inside(path, folder):
    try:
        return os.path.commonpath([_pathkey(path), _pathkey(folder)]) == _pathkey(folder)
    except ValueError:
        return False


def resolve(body):
    """(path, kind, item_id, info) from a POST /api/share body. Only files on this PC that Marquee knows about
    (library items, library folders, exported clips) can be shared."""
    from . import server
    if body.get("item_id"):
        item_id = str(body["item_id"])
        cat = server.catalog()
        try:
            raw = cat.items.get(cat._resolve(item_id)) or {}
        except Exception:
            raw = {}
        if raw.get("type") in ("Series", "Season", "BoxSet"):
            raise ShareError("Pick a movie or an episode to share (not a whole show or season).", 400)
        info = cat.lookup(item_id)
        if not info:
            raise ShareError("That title isn't in the library on this PC.", 404)
        if not info.get("path") or not info.get("exists"):
            raise ShareError("Only files stored on this PC can be shared, and this one isn't here (it's streamed "
                             "from the server).", 409, "not_local")
        return info["path"], "item", info.get("id"), info
    if body.get("clip_path"):
        path = os.path.abspath(str(body["clip_path"]))
        if not _inside(path, _clips_root()):
            raise ShareError("That isn't one of Marquee's saved clips.", 400)
        return path, "clip", None, None
    if body.get("path"):
        path = os.path.abspath(str(body["path"]))
        if Path(path).suffix.lower() not in SHAREABLE_EXTS:
            raise ShareError("Only video and subtitle files can be shared.", 400)
        folders = settings.load().get("library_folders") or []
        known = any(_inside(path, f) for f in folders) or _inside(path, _clips_root())
        info = None
        if not known:
            try:
                info = server.catalog().lookup("file:" + path)
            except Exception:
                info = None
            known = bool(info)
        if not known:
            raise ShareError("Only files in your library or clips folder can be shared.", 403)
        return path, "file", info.get("id") if info else None, info
    raise ShareError("Say what to share (item_id, path or clip_path).", 400)


def _expiry_from(body):
    """Absolute epoch seconds from {expires: epoch} or {expires_in: seconds}; 0/None = never."""
    if body.get("expires_in") not in (None, "", 0):
        return int(time.time() + float(body["expires_in"]))
    if body.get("expires") not in (None, "", 0):
        return int(float(body["expires"]))
    if "expires" in body or "expires_in" in body:
        return 0
    return None


# ------------------------------------------------------------------ routes
def _ok(**kw):
    return jsonify(ok=True, **kw)


def _fail(e):
    return jsonify(ok=False, message=str(e), reason=getattr(e, "reason", None)), getattr(e, "code", 500)


@bp.before_request
def _guard():
    # Only Marquee's own page may drive this (a web page elsewhere can't post JSON here without a preflight,
    # and a foreign Origin is refused outright).
    origin = request.headers.get("Origin")
    if origin and urllib.parse.urlsplit(origin).netloc != request.host:
        return jsonify(ok=False, message="forbidden"), 403
    if request.method in ("POST", "DELETE") and not request.is_json:
        return jsonify(ok=False, message="send JSON"), 415
    init()


@bp.errorhandler(ShareError)
def _share_error(e):
    return _fail(e)


@bp.errorhandler(_Retry)
def _retry_error(e):
    return _fail(ShareError("Couldn't reach Google Drive. Check the internet connection and try again.", 503,
                            "offline"))


@bp.get("/api/share/status")
def route_status():
    cid, sec = client()
    out = {"configured": bool(cid), "client_id": cid, "client_secret_set": bool(sec), "signed_in": signed_in(),
           "email": "", "name": "", "quota": None, "speed_cap": settings.load().get("share_speed_cap") or 0,
           "uploads": [], "guide": "docs/google-drive.md"}
    with _lock:
        active = [r for r in _store()["shares"].values() if r.get("status") in ("queued", "uploading", "paused")]
    out["uploads"] = [public(r) for r in active]
    if out["signed_in"]:
        try:
            a = about()
            out["email"] = (a.get("user") or {}).get("emailAddress", "")
            out["name"] = (a.get("user") or {}).get("displayName", "")
            q = a.get("storageQuota") or {}
            out["quota"] = {"used": int(q.get("usage") or 0), "limit": int(q["limit"]) if q.get("limit") else None}
        except AuthError as e:
            out["signed_in"] = False
            out["message"] = str(e)
            out["reason"] = e.reason
        except (ShareError, _Retry) as e:
            out["message"] = str(e) if isinstance(e, ShareError) else "Google Drive isn't reachable right now."
    return _ok(**out)


@bp.post("/api/share/config")
def route_config():
    b = request.get_json(silent=True) or {}
    changes = {}
    if "client_id" in b:
        changes["gdrive_client_id"] = str(b["client_id"] or "").strip()
    if "client_secret" in b:
        changes["gdrive_client_secret"] = str(b["client_secret"] or "").strip()
    if "speed_cap" in b:
        try:
            changes["share_speed_cap"] = max(0.0, float(b["speed_cap"] or 0))
        except (TypeError, ValueError):
            raise ShareError("The speed cap must be a number (Mbit/s, 0 for none).")
    if changes:
        settings.save(changes)
    cid, sec = client()
    return _ok(configured=bool(cid), client_id=cid, client_secret_set=bool(sec),
               speed_cap=settings.load().get("share_speed_cap") or 0)


@bp.post("/api/share/connect")
def route_connect():
    email = connect()
    return _ok(signed_in=True, email=email)


@bp.post("/api/share/disconnect")
def route_disconnect():
    revoked = disconnect()
    return _ok(signed_in=False, revoked=revoked,
               message="Signed out. Links you already shared keep working until you stop them or delete the files.")


@bp.post("/api/share")
def route_share():
    b = request.get_json(silent=True) or {}
    if b.get("share_id"):
        rec = _get(str(b["share_id"]))
        return _ok(share=_reuse(rec, _expiry_from(b)))
    path, kind, item_id, info = resolve(b)
    return _ok(share=share_file(path, kind, item_id, info, _expiry_from(b) or None))


@bp.get("/api/shares")
def route_list():
    return _ok(shares=[public(r) for r in shares()])


@bp.get("/api/share/<sid>")
def route_one(sid):
    return _ok(share=public(_get(sid), qr=True))


@bp.post("/api/share/<sid>/stop")
def route_stop(sid):
    return _ok(share=stop_sharing(sid))


@bp.post("/api/share/<sid>/expiry")
def route_expiry(sid):
    b = request.get_json(silent=True) or {}
    return _ok(share=set_expiry(sid, _expiry_from(b) or None))


@bp.post("/api/share/<sid>/copy")
def route_copy(sid):
    rec = _get(sid)
    if not rec.get("link"):
        raise ShareError("There's no link yet.", 409)
    return _ok(copied=copy_to_clipboard(rec["link"]), link=rec["link"])


@bp.delete("/api/share/<sid>")
def route_delete(sid):
    return _ok(**delete(sid))
