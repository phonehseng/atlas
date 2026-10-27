"""Friends who signed in with their own Jellyfin account (Settings > Atlas Mobile > "Sign in with Jellyfin").

The library itself comes from the local catalog, but what they may see and their watch state are theirs:
  * allowed items: the ids Jellyfin lists for their account (refreshed every 15 minutes while they use the app, and
    remembered, so library limits set in Jellyfin keep working while Jellyfin is down)
  * resume points / watched: kept here per user and written to their own Jellyfin account with their session token
    (never with the owner's), newest change winning.
"""
import json
import threading
import time

import requests

from .. import settings

FILE = settings.DIR / "mobile_users.json"
REFRESH = 15 * 60
TICKS = 10_000_000

_lock = threading.RLock()
_data = None
_refreshing = set()
_http = requests.Session()


def _load():
    global _data
    if _data is None:
        try:
            _data = json.loads(FILE.read_text(encoding="utf-8"))
        except Exception:
            _data = {}
    return _data


def _save():
    settings.DIR.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_data), encoding="utf-8")
    tmp.replace(FILE)


def _user(uid):
    return _load().setdefault(uid, {"allowed": None, "allowed_at": 0, "pos": {}})


def _headers(app, token):
    return {"Authorization": app.jf.auth_header(token)}


def refresh(app, uid, token, wait=False):
    """Fetch the user's visible items and their user data from Jellyfin (in the background unless wait)."""
    def run():
        try:
            jf = app.jf
            ids, pos = [], {}
            start = 0
            while True:
                r = _http.get(f"{jf.base}/Items", headers=_headers(app, token), timeout=30, params={
                    "userId": uid, "Recursive": "true", "IncludeItemTypes": "Movie,Series,Season,Episode",
                    "Fields": "", "EnableImages": "false", "EnableUserData": "true", "StartIndex": start,
                    "Limit": 2000})
                r.raise_for_status()
                page = r.json()
                batch = page.get("Items") or []
                for i in batch:
                    ids.append(i["Id"])
                    u = i.get("UserData") or {}
                    if u.get("PlaybackPositionTicks") or u.get("Played"):
                        pos[i["Id"]] = {"p": round((u.get("PlaybackPositionTicks") or 0) / TICKS, 1),
                                        "played": bool(u.get("Played")), "at": _ts(u.get("LastPlayedDate"))}
                start += len(batch)
                if not batch or start >= (page.get("TotalRecordCount") or 0):
                    break
            with _lock:
                u = _user(uid)
                u["allowed"], u["allowed_at"] = ids, time.time()
                for iid, theirs in pos.items():
                    mine = u["pos"].get(iid)
                    if not mine or (theirs["at"] or 0) > (mine.get("at") or 0) + 1:
                        u["pos"][iid] = theirs
                _save()
        except Exception as e:
            print("mobile: couldn't refresh a Jellyfin user's library:", type(e).__name__, e)
        finally:
            _refreshing.discard(uid)

    with _lock:
        u = _user(uid)
        fresh = time.time() - (u.get("allowed_at") or 0) < REFRESH
    if fresh or uid in _refreshing or not app.jf.base:
        return
    _refreshing.add(uid)
    if wait:
        run()
    else:
        threading.Thread(target=run, daemon=True, name="mobile-user-refresh").start()


def _ts(s):
    if not s:
        return 0
    try:
        from ..catalog import ts
        return ts(s)
    except Exception:
        return 0


def allowed(uid):
    """Set of item ids this user may see, or None when not known yet."""
    with _lock:
        a = _user(uid).get("allowed")
        return set(a) if a is not None else None


def state(uid, item_id):
    with _lock:
        return dict(_user(uid)["pos"].get(item_id) or {})


def overlay(uid, card):
    """A catalog card with this user's progress instead of the owner's."""
    st = state(uid, card["id"])
    runtime = card.get("runtime") or 0
    pos = 0 if st.get("played") else (st.get("p") or 0)
    card.update(position=pos, progress=(pos / runtime) if runtime else 0, played=bool(st.get("played")),
                favorite=False)
    if card.get("type") in ("Series", "Season"):
        card["unplayed"] = None
    return card


def resume_ids(uid, limit=20):
    with _lock:
        rows = [(v.get("at") or 0, k) for k, v in _user(uid)["pos"].items() if v.get("p") and not v.get("played")]
    rows.sort(reverse=True)
    return [k for _, k in rows[:limit]]


def save(app, uid, token, item_id, seconds, runtime, played=None):
    seconds = max(0.0, float(seconds or 0))
    if played is None:
        played = bool(runtime) and seconds >= runtime * 0.9
    now = time.time()
    with _lock:
        _user(uid)["pos"][item_id] = {"p": 0 if played else round(seconds, 1), "played": bool(played), "at": now}
        _save()
    if item_id.startswith("local:") or not token:
        return

    def push():
        try:
            from ..catalog import iso
            body = {"PlaybackPositionTicks": 0 if played else int(seconds * TICKS), "LastPlayedDate": iso(now)}
            if played:
                body["Played"] = True
            _http.post(f"{app.jf.base}/UserItems/{item_id}/UserData", params={"userId": uid}, json=body,
                       headers=_headers(app, token), timeout=10).raise_for_status()
        except Exception as e:  # kept here; Jellyfin gets it the next time it's saved
            print("mobile: couldn't save a friend's position to Jellyfin:", type(e).__name__, e)
    threading.Thread(target=push, daemon=True, name="mobile-user-save").start()
