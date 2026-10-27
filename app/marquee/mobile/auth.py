"""Paired devices.

A phone pairs once: the QR code in Settings carries a one-time pairing token in the URL fragment (never in a request
line, so it stays out of logs); the page sends it in a header and gets back a long-lived HttpOnly cookie that belongs
to that device alone. The token is used up by that pairing (a fresh one is made for the next phone) and expires after
a day if nobody scans it. Each device can be renamed or revoked in Settings; "New pairing code" revokes them all.

Friends who share this PC over Tailscale can instead sign in with their own Jellyfin username and password. The
password is checked against Jellyfin once (AuthenticateByName) and never stored; only the session token Jellyfin hands
back is kept (DPAPI-sealed), so their watch progress can be written to their own Jellyfin account.

TVs have no camera, so they pair with a short code instead: the TV shows one and the owner types it into Settings
on the PC (or the PC shows one and it's typed on the TV). Either way the TV ends up with its own device cookie.

Cookie value: "<device id>.<secret>"; only a SHA-256 of the secret is stored.
"""
import hashlib
import hmac
import json
import re
import secrets
import threading
import time

from .. import settings

FILE = settings.DIR / "mobile_devices.json"
LEGACY_FILE = settings.DIR / "remote.json"       # the old single shared remote token
COOKIE = "marquee_remote"
PAIR_TTL = 24 * 3600          # an unscanned pairing code stops working after a day
CLONE_TTL = 7 * 86400         # "add to home screen" hand-over tokens
SEEN_SAVE_EVERY = 120         # write last-seen at most every 2 minutes per device

_lock = threading.RLock()
_data = None
listeners = []                # fn(event, device) -> "paired" / "revoked"


def _h(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _load():
    global _data
    if _data is None:
        try:
            _data = json.loads(FILE.read_text(encoding="utf-8"))
        except Exception:
            _data = {}
        _data.setdefault("devices", {})
        _data.setdefault("clones", {})
        if not _data.get("pair"):
            try:  # keep the code the old remote showed working for one more pairing
                old = settings._open(json.loads(LEGACY_FILE.read_text(encoding="utf-8")).get("token", ""))
                if old:
                    _data["pair"] = {"token": settings._seal(old), "created": time.time()}
            except Exception:
                pass
    return _data


def _save():
    settings.DIR.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_data, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def _tell(event, dev):
    for fn in list(listeners):
        try:
            fn(event, public(dev))
        except Exception as e:
            print("mobile device listener failed:", e)


# ------------------------------------------------------------------ pairing tokens
def pairing_token(fresh=False):
    """The current one-time pairing token (made when missing, used up or expired)."""
    with _lock:
        d = _load()
        p = d.get("pair") or {}
        tok = settings._open(p.get("token", "")) if p else ""
        if fresh or not tok or time.time() - (p.get("created") or 0) > PAIR_TTL:
            tok = secrets.token_urlsafe(24)
            d["pair"] = {"token": settings._seal(tok), "created": time.time()}
            _save()
        return tok


def pairing_version():
    """Changes whenever the pairing code does (the desktop re-draws its QR code when it sees a new one)."""
    with _lock:
        return int((_load().get("pair") or {}).get("created") or 0)


def use_pairing_token(tok):
    """True (and the token is used up) when `tok` is the current pairing token."""
    if not tok:
        return False
    with _lock:
        cur = pairing_token()
        if hmac.compare_digest(tok.encode(), cur.encode()):
            pairing_token(fresh=True)
            return True
        return False


def clone_token(device_id):
    """A one-time token that pairs another copy of this device (the home-screen app on iOS has its own cookies)."""
    tok = secrets.token_urlsafe(24)
    with _lock:
        d = _load()
        now = time.time()
        d["clones"] = {k: v for k, v in d["clones"].items() if v.get("expires", 0) > now and v.get("device") != device_id}
        d["clones"][_h(tok)] = {"device": device_id, "expires": now + CLONE_TTL}
        _save()
    return tok


def use_clone_token(tok):
    """The device a hand-over token was made for (used up), or None."""
    if not tok:
        return None
    with _lock:
        d = _load()
        hit = d["clones"].pop(_h(tok), None)
        if hit is None:
            return None
        _save()
        if hit.get("expires", 0) < time.time():
            return None
        dev = d["devices"].get(hit.get("device"))
        return dict(dev) if dev else None


# ------------------------------------------------------------------ devices
def device_name(ua):
    ua = ua or ""
    if "AtlasTV" in ua:  # the Android TV app (its WebView says which TV it is)
        m = re.search(r"Android [\d.]+; ([^;)]+?)(?: Build|\))", ua)
        model = (m.group(1).strip() if m else "")
        return (model if model and model not in ("K", "wv") and len(model) < 30 else "Android TV") + " · TV app"
    if "iPhone" in ua:
        os_ = "iPhone"
    elif "iPad" in ua or ("Macintosh" in ua and "Mobile" in ua):
        os_ = "iPad"
    elif "Android" in ua:
        m = re.search(r"Android [\d.]+; ([^;)]+?)(?: Build|\))", ua)
        model = (m.group(1).strip() if m else "")
        os_ = model if model and model not in ("K", "wv") and len(model) < 30 else "Android"
    elif "Windows" in ua:
        os_ = "Windows"
    elif "Mac OS" in ua:
        os_ = "Mac"
    elif "Linux" in ua:
        os_ = "Linux"
    else:
        os_ = "Device"
    br = ("Edge" if "Edg/" in ua else "Samsung Internet" if "SamsungBrowser" in ua else "Firefox" if "Firefox" in ua
          or "FxiOS" in ua else "Chrome" if "Chrome" in ua or "CriOS" in ua else "Safari" if "Safari" in ua else "")
    return f"{os_} · {br}" if br else os_


def create_device(user, ua="", ip="", jf_token=None, name=None, client=None):
    """A new paired device. user = {"kind": "owner"} or {"kind": "jellyfin", "id", "name"}.
    Returns (cookie value, public dict)."""
    did = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    now = time.time()
    dev = {"id": did, "name": (name or device_name(ua))[:60], "ua": (ua or "")[:240], "created": now,
           "last_seen": now, "ip": ip, "user": user, "hash": _h(secret)}
    if client:
        dev["client"] = client
    if jf_token:
        dev["jf_token"] = settings._seal(jf_token)
    with _lock:
        _load()["devices"][did] = dev
        _save()
    _tell("paired", dev)
    return f"{did}.{secret}", public(dev)


def device_for(cookie, ip=None):
    """The device a cookie belongs to (a copy), or None. Keeps last-seen up to date."""
    if not cookie or "." not in cookie or len(cookie) > 200:
        return None
    did, _, secret = cookie.partition(".")
    with _lock:
        dev = _load()["devices"].get(did)
        if not dev or not hmac.compare_digest(dev.get("hash", ""), _h(secret)):
            return None
        now = time.time()
        if now - (dev.get("last_seen") or 0) > SEEN_SAVE_EVERY or (ip and dev.get("ip") != ip):
            dev["last_seen"] = now
            if ip:
                dev["ip"] = ip
            _save()
        else:
            dev["_seen"] = now
        return dict(dev)


def get(did):
    with _lock:
        dev = _load()["devices"].get(did)
        return dict(dev) if dev else None


def jf_token(dev):
    return settings._open(dev.get("jf_token", "")) if dev and dev.get("jf_token") else ""


def public(dev):
    """What Settings may show about a device (no secrets)."""
    u = dev.get("user") or {}
    return {"id": dev["id"], "name": dev.get("name"), "created": dev.get("created"),
            "last_seen": max(dev.get("last_seen") or 0, dev.get("_seen") or 0), "ip": dev.get("ip"),
            "user": u.get("name") or ("You" if u.get("kind") == "owner" else ""),
            "kind": u.get("kind", "owner"), "browser": device_name(dev.get("ua")), "tv": dev.get("client") == "tv"}


def devices():
    with _lock:
        return sorted((public(d) for d in _load()["devices"].values()), key=lambda d: -(d["last_seen"] or 0))


def rename(did, name):
    with _lock:
        dev = _load()["devices"].get(did)
        if not dev:
            return False
        dev["name"] = str(name or "").strip()[:60] or device_name(dev.get("ua"))
        _save()
        return True


def revoke(did):
    with _lock:
        d = _load()
        dev = d["devices"].pop(did, None)
        d["clones"] = {k: v for k, v in d["clones"].items() if v.get("device") != did}
        _save()
    if dev:
        _tell("revoked", dev)
    return bool(dev)


def revoke_all():
    with _lock:
        d = _load()
        gone = list(d["devices"].values())
        d["devices"], d["clones"] = {}, {}
        _save()
    for dev in gone:
        _tell("revoked", dev)
    return len(gone)


def is_owner(dev):
    return bool(dev) and (dev.get("user") or {}).get("kind", "owner") == "owner"


# ------------------------------------------------------------------ TVs: short pairing codes
# Kept in memory only: a code lives for 10 minutes, and a restart just means asking for a new one.
TV_TTL = 600
TV_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O or 1/I to mix up on a TV screen
_tv_waiting = {}      # code -> {"rid": sha256 of the TV's request id, "created", "ip", "ua", "cookie", "device"}
_tv_pc = {}           # sha256 of a code the PC showed -> expires


def _tv_code():
    return "".join(secrets.choice(TV_ALPHABET) for _ in range(6))


def tv_norm(code):
    """What someone typed -> the canonical code (upper case, no spaces or dashes)."""
    return re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()[:12]


def _tv_gc(now):
    for k in [k for k, v in _tv_waiting.items() if now - v["created"] > TV_TTL]:
        _tv_waiting.pop(k, None)
    for k in [k for k, v in _tv_pc.items() if v < now]:
        _tv_pc.pop(k, None)


def tv_request(ip="", ua=""):
    """A TV asks to pair: -> (code to show, secret request id the TV polls with). None when that address already
    has a few codes waiting (or too many TVs are waiting at once)."""
    now = time.time()
    with _lock:
        _tv_gc(now)
        if len(_tv_waiting) >= 30 or sum(1 for v in _tv_waiting.values() if v["ip"] == ip) >= 3:
            return None
        code = _tv_code()
        while code in _tv_waiting:
            code = _tv_code()
        rid = secrets.token_urlsafe(32)
        _tv_waiting[code] = {"rid": _h(rid), "created": now, "ip": ip, "ua": (ua or "")[:240], "cookie": None}
    _tell("tv-waiting", {"id": "", "name": device_name(ua)})
    return code, rid


def tv_waiting():
    """How many TVs are showing a code right now (Settings mentions it)."""
    with _lock:
        _tv_gc(time.time())
        return sum(1 for v in _tv_waiting.values() if not v["cookie"])


def tv_approve(code):
    """The owner typed the TV's code on the PC: that TV becomes a paired device. -> public device or None."""
    code = tv_norm(code)
    with _lock:
        _tv_gc(time.time())
        req = _tv_waiting.get(code)
        if not req or req["cookie"]:
            return None
        cookie, dev = create_device({"kind": "owner"}, req["ua"], req["ip"], client="tv",
                                    name=_tv_name(req["ua"]))
        req["cookie"], req["device"] = cookie, dev
        return dev


def tv_poll(rid):
    """The TV checks whether its code was typed in. -> ("waiting"|"expired", None, None) or ("paired", cookie, dev);
    the cookie is handed out once."""
    if not rid or len(rid) > 100:
        return "expired", None, None
    h = _h(rid)
    with _lock:
        _tv_gc(time.time())
        for code, req in list(_tv_waiting.items()):
            if hmac.compare_digest(req["rid"], h):
                if not req["cookie"]:
                    return "waiting", None, None
                _tv_waiting.pop(code, None)
                return "paired", req["cookie"], req["device"]
    return "expired", None, None


def tv_pc_code():
    """A code the PC shows, to type on the TV instead (one use, 10 minutes). -> (code, expires)."""
    now = time.time()
    code = _tv_code()
    with _lock:
        _tv_gc(now)
        _tv_pc.clear()  # only the newest code works
        _tv_pc[_h(code)] = now + TV_TTL
    return code, now + TV_TTL


def use_tv_pc_code(code):
    """True (and the code is used up) when `code` is the one the PC is showing."""
    code = tv_norm(code)
    if len(code) != 6:
        return False
    with _lock:
        _tv_gc(time.time())
        return _tv_pc.pop(_h(code), None) is not None


def _tv_name(ua):
    name = device_name(ua)
    return name if "TV" in name else name + " · TV"
