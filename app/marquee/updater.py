"""Updates: Marquee checks its GitHub releases for a newer build, downloads it, checks it and swaps it in.

Where it looks: https://github.com/<update_repo>/releases/latest/download/marquee-update.json (the release manifest,
docs/updates.md), or MARQUEE_UPDATE_URL (development: a local test server). Checked 30 s after start and every 12 h,
never while a video plays, with a 10 s timeout and the ETag of the last answer. A private or missing repo (401/403/404)
is noted quietly ("can't reach updates"), never nagged about.

Settings: update_check auto (download in the background, then offer "Restart to update") | notify | off;
update_channel stable | beta (beta also takes pre-releases); update_skip (a skipped version); update_repo.

Download: %APPDATA%\\Marquee\\updates\\ (resumable with HTTP Range), SHA-256 and size from the manifest, then
unpacked next to the install folder (<install>.new, same drive, so the swap is two renames) and checked: Marquee.exe
is there and its VERSION file says the manifest's version.

Apply: a running exe can't replace itself, so apply_update.ps1 (shipped in the build, copied to the updates folder
first so it never holds the install folder open) does it: Marquee writes updates\\job.json, starts the script
detached and closes through its normal path (player shutdown etc.). The script waits for Marquee to exit, renames
<install> to <install>.old, renames <install>.new to <install>, starts Marquee.exe --updated <old version> and waits
up to 60 s for this module's heartbeat (updates\\heartbeat.json with the new version). No heartbeat (or the new exe
exits): it rolls back to <install>.old and starts that with --update-failed <version>. On success it deletes .old.

The install folder must be writable by the user (the installer puts Marquee in the stack's apps folder); that's
checked before anything is downloaded. If it isn't (e.g. Program Files), the user is told to run the installer.
Settings and WebView2 data live in %APPDATA%\\Marquee, so nothing of the user's is in the swapped folder.

Stack: the same manifest carries "installer" and "stack-assets" entries (Media Stack Setup reads them too). When
either is newer than what the stack runs (stack.json "deployed" next to mediastack.json), "Your stack" shows a quiet
line with an Update button that downloads the installer (checked) into Downloads and starts it with
--root <stack folder> --update ("Update my stack")."""
import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path

import requests
from flask import Blueprint, jsonify, request

from . import __version__, settings

DIR = settings.DIR / "updates"
MANIFEST_NAME = "marquee-update.json"
ASSET = "windows-x64"
FIRST_DELAY = float(os.environ.get("MARQUEE_UPDATE_FIRST_DELAY") or 30)
INTERVAL = 12 * 3600
TIMEOUT = 10
APPLY_SCRIPT = Path(__file__).resolve().parent / "apply_update.ps1"
UA = {"User-Agent": f"Marquee/{__version__} (Windows)"}

APP = None
_lock = threading.Lock()
_wake = threading.Event()
ST = {
    "checked": None,       # unix time of the last answer (or failure)
    "status": "idle",      # idle | checking | ok | unreachable | error
    "message": "",
    "available": None,     # {version, notes, size, published, critical, notes_url, prerelease}
    "download": {"state": "idle", "done": 0, "total": 0, "error": "", "version": None},
    "snoozed": None,       # "Later": the version the pill stays hidden for until the next start
    "just_updated": None,  # {from, to, notes} after a successful update (until the page acknowledges it)
    "update_failed": None, # {version, message} after a rollback
    "stack": None,         # {version, current, size, has_installer, message}
    "stack_download": {"state": "idle", "done": 0, "total": 0, "error": "", "path": ""},
}
_manifest = None


# ------------------------------------------------------------------ versions
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def parse_version(v):
    """'1.4.0' / 'v1.4.0-beta.2' -> a sort key (semver precedence: a pre-release sorts before its release)."""
    m = _SEMVER.match(str(v or "").strip())
    if not m:
        return None
    major, minor, patch, pre = int(m[1]), int(m[2]), int(m[3] or 0), m[4]
    if not pre:
        return (major, minor, patch, (1,))
    ids = tuple((0, int(x), "") if x.isdigit() else (1, 0, x) for x in pre.split("."))
    return (major, minor, patch, (0,) + ids)


def is_prerelease(v):
    k = parse_version(v)
    return bool(k and k[3][0] == 0)


def newer(a, b):
    """a is a newer version than b."""
    ka, kb = parse_version(a), parse_version(b)
    return bool(ka and kb and ka > kb)


# ------------------------------------------------------------------ small helpers
def _app():
    if APP is not None:
        return APP
    from . import server
    return server.APP


def _playing():
    try:
        return bool(_app().player.playing)
    except Exception:
        return False


def _notify(event, data=None):
    try:
        _app().notify_ui(event, data or {})
    except Exception:
        pass


def _read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _write_json(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(p)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def install_dir():
    """The folder Marquee.exe runs from (None when running from source)."""
    if os.environ.get("MARQUEE_UPDATE_INSTALL_DIR"):  # development: pretend to be installed here
        return Path(os.environ["MARQUEE_UPDATE_INSTALL_DIR"])
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None


def releases_page():
    return f"https://github.com/{settings.load().get('update_repo') or 'phonehseng/marquee'}/releases"


_can = {"at": 0, "val": (False, "")}


def can_apply():
    """(True, "") when this copy can replace itself, else (False, a sentence for the user). Cached for a minute."""
    if time.time() - _can["at"] > 60:
        _can["val"], _can["at"] = _can_apply(), time.time()
    return _can["val"]


def _can_apply():
    inst = install_dir()
    if inst is None:
        return False, "Marquee is running from source here, so it can't update itself. Pull and rebuild instead."
    if not (inst / "Marquee.exe").exists():
        return False, f"Marquee.exe wasn't found in {inst}, so this copy can't update itself."
    probe = inst / f".write-test-{uuid.uuid4().hex[:8]}"
    sib = inst.parent / f"{inst.name}.write-test-{uuid.uuid4().hex[:8]}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        sib.mkdir()
        sib2 = sib.with_name(sib.name + "-r")
        sib.rename(sib2)  # the swap renames folders next to the install
        sib2.rmdir()
    except OSError:
        for p in (probe,):
            try:
                p.unlink()
            except OSError:
                pass
        for p in (sib, sib.with_name(sib.name + "-r")):
            try:
                p.rmdir()
            except OSError:
                pass
        short = f"…\\{inst.parent.name}\\{inst.name}" if len(str(inst)) > 40 else str(inst)
        return False, (f"Your account can't change Marquee's folder ({short}), so Marquee can't update itself. "
                       "Run Media Stack Setup, or download the new version, to update.")
    return True, ""


def _os_ok(min_os):
    """min_os like "10.0.17763" (Windows build); empty = any."""
    if not min_os:
        return True
    try:
        have = tuple(int(x) for x in platform.version().split(".")[:3])
        need = tuple(int(x) for x in str(min_os).split(".")[:3])
        return have >= need
    except Exception:
        return True


# ------------------------------------------------------------------ the manifest
def manifest_url(s=None):
    s = s or settings.load()
    if os.environ.get("MARQUEE_UPDATE_URL"):
        return os.environ["MARQUEE_UPDATE_URL"]
    repo = (s.get("update_repo") or "phonehseng/marquee").strip().strip("/")
    return f"https://github.com/{repo}/releases/latest/download/{MANIFEST_NAME}"


def _beta_manifest_url(repo):
    """The newest release (pre-releases included) that has a manifest. GitHub's /latest skips pre-releases."""
    r = requests.get(f"https://api.github.com/repos/{repo}/releases", params={"per_page": 15},
                     headers={**UA, "Accept": "application/vnd.github+json"}, timeout=TIMEOUT)
    if r.status_code in (401, 403, 404):
        raise PermissionError(r.status_code)
    r.raise_for_status()
    best = None
    for rel in r.json():
        if rel.get("draft"):
            continue
        asset = next((a for a in rel.get("assets") or [] if a.get("name") == MANIFEST_NAME), None)
        if asset and parse_version(rel.get("tag_name")) and (best is None or newer(rel["tag_name"], best[0])):
            best = (rel["tag_name"], asset["browser_download_url"])
    return best[1] if best else None


def entry_file(entry, key=ASSET):
    """A product entry's download for a platform: files[key], else a flat url/sha256/size (docs/updates.md)."""
    if not isinstance(entry, dict):
        return None
    f = (entry.get("files") or {}).get(key)
    if not (isinstance(f, dict) and f.get("url")):
        f = entry if entry.get("url") else None
    if not f or not re.fullmatch(r"[0-9a-fA-F]{64}", str(f.get("sha256") or "")) or not isinstance(f.get("size"), int):
        return None
    return {"url": f["url"], "sha256": f["sha256"].lower(), "size": f["size"], "name": f.get("name")}


def valid_manifest(m):
    """The manifest has a Marquee entry with a version and a checked Windows download."""
    e = isinstance(m, dict) and m.get("marquee")
    return bool(isinstance(e, dict) and parse_version(e.get("version")) and entry_file(e))


def _set(**kw):
    with _lock:
        ST.update(kw)


def check(manual=False):
    """Fetch the manifest and work out what's available. Returns the public status."""
    global _manifest
    s = settings.load()
    if not manual and (s.get("update_check") == "off" or _playing()):
        return status()
    _set(status="checking")
    cache = _read_json(DIR / "check.json", {}) or {}
    url = manifest_url(s)
    try:
        if s.get("update_channel") == "beta" and not os.environ.get("MARQUEE_UPDATE_URL"):
            url = _beta_manifest_url((s.get("update_repo") or "phonehseng/marquee").strip("/")) or url
        headers = {**UA, "Accept": "application/json"}
        if cache.get("etag") and cache.get("url") == url and cache.get("manifest"):
            headers["If-None-Match"] = cache["etag"]
        r = requests.get(url, headers=headers, timeout=TIMEOUT)
        if r.status_code == 304:
            m = cache["manifest"]
        elif r.status_code in (401, 403, 404):
            raise PermissionError(r.status_code)
        else:
            r.raise_for_status()
            m = r.json()
            if not valid_manifest(m):
                raise ValueError("the release manifest is incomplete")
            _write_json(DIR / "check.json", {"url": url, "etag": r.headers.get("ETag") or "", "manifest": m})
    except PermissionError:
        _set(status="unreachable", checked=time.time(), message="Can't reach updates right now.")
        return status()
    except (requests.RequestException, ValueError) as e:
        msg = "Couldn't check for updates (no connection?)." if isinstance(e, requests.RequestException) \
            else f"Couldn't read the update information: {e}."
        _set(status="error", checked=time.time(), message=msg)
        return status()
    _manifest = m
    _set(status="ok", checked=time.time(), message="")
    _evaluate(m, s, manual)
    return status()


def _evaluate(m, s, manual=False):
    e = m["marquee"]
    ver = str(e["version"]).lstrip("v")
    beta = s.get("update_channel") == "beta"
    avail = None
    if newer(ver, __version__) and (beta or not is_prerelease(ver)):
        a = entry_file(e)
        avail = {"version": ver, "notes": str(e.get("notes") or "")[:6000], "size": a["size"],
                 "published": e.get("published") or m.get("published"), "critical": bool(e.get("critical")),
                 "notes_url": e.get("notes_url") or f"{releases_page()}/tag/v{ver}",
                 "prerelease": is_prerelease(ver), "os_ok": _os_ok(e.get("min_os"))}
    prev = ST["available"]
    _set(available=avail)
    _evaluate_stack(m)
    if not avail:
        return
    skipped = s.get("update_skip") == ver and not avail["critical"]
    ok, why = can_apply()
    fresh = not prev or prev.get("version") != ver
    if (fresh or manual) and not skipped:
        _notify("update-available", {"version": ver, "notes": avail["notes"], "size": avail["size"],
                                     "critical": avail["critical"], "can_apply": ok and avail["os_ok"],
                                     "reason": why})
    failed_before = (_read_json(DIR / "failed.json", {}) or {}).get("version") == ver
    if s.get("update_check") == "auto" and ok and avail["os_ok"] and not skipped and not failed_before:
        start_download(background=True)


# ------------------------------------------------------------------ download, verify, unpack
def _paths(ver):
    return DIR / f"Marquee-{ver}-{ASSET}.zip", DIR / f"Marquee-{ver}-{ASSET}.zip.part"


def staging_dir():
    inst = install_dir()
    return inst.with_name(inst.name + ".new") if inst else DIR / "staging"


def start_download(background=False):
    """Start (or resume) downloading the available version. Returns the download state."""
    with _lock:
        avail, dl = ST["available"], ST["download"]
        if not avail or not _manifest:
            raise ValueError("There's no update to download")
        if dl["state"] in ("downloading", "paused", "verifying", "unpacking") and dl["version"] == avail["version"]:
            return dict(dl)
        if dl["state"] == "ready" and dl["version"] == avail["version"] and staging_dir().exists():
            return dict(dl)
        ok, why = can_apply()
        if not ok:
            raise PermissionError(why)
        ST["download"] = {"state": "downloading", "done": 0, "total": avail["size"], "error": "",
                          "version": avail["version"]}
    threading.Thread(target=_download, args=(dict(_manifest), background), daemon=True,
                     name="marquee-update").start()
    return dict(ST["download"])


def _dl(**kw):
    with _lock:
        ST["download"].update(kw)


def _fail(msg):
    _dl(state="error", error=msg)
    _notify("update-error", {"message": msg, "version": ST["download"].get("version")})


def _download(m, background):
    e = m["marquee"]
    ver = str(e["version"]).lstrip("v")
    a = entry_file(e)
    final, part = _paths(ver)
    DIR.mkdir(parents=True, exist_ok=True)
    try:
        if not (final.exists() and final.stat().st_size == a["size"] and _sha256(final) == a["sha256"].lower()):
            final.unlink(missing_ok=True)
            _fetch(a["url"], part, a["size"], ver, background)
            _dl(state="verifying")
            _notify("update-progress", {"version": ver, "state": "verifying", "done": a["size"], "total": a["size"]})
            got = _sha256(part)
            if part.stat().st_size != a["size"] or got != a["sha256"].lower():
                part.unlink(missing_ok=True)
                return _fail("The download didn't match its checksum, so it was thrown away. Try again later.")
            part.replace(final)
        _dl(state="unpacking")
        _notify("update-progress", {"version": ver, "state": "unpacking", "done": a["size"], "total": a["size"]})
        _unpack(final, ver)
    except _Stop:
        return
    except ValueError as e:
        return _fail(f"The update didn't pass its checks ({e}), so it wasn't kept.")
    except Exception as e:
        return _fail(f"The update couldn't be downloaded: {e}")
    _dl(state="ready", done=a["size"])
    _write_json(DIR / "pending.json", {"version": ver, "from": __version__, "notes": str(e.get("notes") or "")[:6000],
                                       "notes_url": (ST["available"] or {}).get("notes_url"),
                                       "staged": str(staging_dir())})
    _notify("update-ready", {"version": ver})


class _Stop(Exception):
    pass


def _fetch(url, part, size, ver, background):
    for attempt in range(4):
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        if have == size:
            return
        headers = dict(UA)
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(TIMEOUT, 60)) as r:
                if r.status_code == 416:
                    part.unlink(missing_ok=True)
                    continue
                r.raise_for_status()
                mode = "ab" if have and r.status_code == 206 else "wb"
                done = have if mode == "ab" else 0
                last = 0
                with open(part, mode) as f:
                    for chunk in r.iter_content(256 * 1024):
                        f.write(chunk)
                        done += len(chunk)
                        while background and _playing():  # a video started: wait (never compete with it)
                            if ST["download"]["state"] != "paused":
                                _dl(state="paused")
                                _notify("update-progress", {"version": ver, "state": "paused", "done": done,
                                                            "total": size})
                            time.sleep(2)
                        if ST["download"]["state"] == "cancelled":
                            raise _Stop()
                        if time.time() - last > 0.4:
                            last = time.time()
                            _dl(state="downloading", done=done)
                            _notify("update-progress", {"version": ver, "state": "downloading", "done": done,
                                                        "total": size})
            if part.stat().st_size >= size:
                return
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))
    raise OSError("the download kept stopping")


def _unpack(zpath, ver):
    stage = staging_dir()
    tmp = stage.with_name(stage.name + ".tmp")
    for p in (tmp, stage):
        if p.exists():
            shutil.rmtree(p)
    tmp.mkdir(parents=True)
    with zipfile.ZipFile(zpath) as z:
        root = tmp.resolve()
        for info in z.infolist():  # never write outside the staging folder
            target = (tmp / info.filename).resolve()
            if root != target and root not in target.parents:
                raise ValueError("the update archive has a bad path in it")
        z.extractall(tmp)
    # The zip holds Marquee\... (build.ps1); accept the files at the top level too.
    top = tmp / "Marquee" if (tmp / "Marquee" / "Marquee.exe").exists() else tmp
    if not (top / "Marquee.exe").exists():
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError("Marquee.exe isn't in the update")
    got = (top / "VERSION").read_text(encoding="utf-8").strip() if (top / "VERSION").exists() else ""
    if got != ver:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError(f"the update says it is version {got or '?'}, not {ver}")
    top.rename(stage)
    shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ apply (restart to update)
def apply():
    """Hand over to apply_update.ps1 and close Marquee. Raises with a sentence when it can't."""
    if _playing():
        raise RuntimeError("Stop the video first: Marquee restarts to update.")
    dl = ST["download"]
    pend = _read_json(DIR / "pending.json", {}) or {}
    stage = staging_dir()
    if dl["state"] != "ready" or not stage.exists() or pend.get("version") != dl["version"]:
        raise RuntimeError("The update isn't downloaded yet.")
    ok, why = can_apply()
    if not ok:
        raise PermissionError(why)
    inst = install_dir()
    script = DIR / "apply_update.ps1"
    shutil.copyfile(APPLY_SCRIPT, script)
    job = {"pid": os.getpid(), "install": str(inst), "staged": str(stage), "old": str(inst.with_name(inst.name + ".old")),
           "exe": "Marquee.exe", "from": __version__, "to": dl["version"], "heartbeat": str(DIR / "heartbeat.json"),
           "result": str(DIR / "result.json"), "log": str(DIR / "update.log"), "wait_exit": 60, "wait_start": 60,
           "ack": str(DIR / "job.ack")}
    _write_json(DIR / "job.json", job)
    (DIR / "job.ack").unlink(missing_ok=True)
    (DIR / "heartbeat.json").unlink(missing_ok=True)
    (DIR / "result.json").unlink(missing_ok=True)
    ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    args = [str(ps), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
            "-File", str(script), "-Job", str(DIR / "job.json")]
    # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW (a hidden console). Not DETACHED_PROCESS: powershell.exe without
    # any console exits at once.
    flags = 0x00000200 | 0x08000000
    try:
        proc = subprocess.Popen(args, cwd=str(DIR), creationflags=flags | 0x01000000, close_fds=True)  # + BREAKAWAY
    except OSError:
        proc = subprocess.Popen(args, cwd=str(DIR), creationflags=flags, close_fds=True)
    # Only close once the script is really running (a PC whose policy blocks scripts must not lose Marquee).
    for _ in range(100):
        if (DIR / "job.ack").exists():
            break
        if proc.poll() is not None:
            raise RuntimeError("The updater couldn't run on this PC (scripts may be blocked). "
                               "Run Media Stack Setup or download the new version to update.")
        time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("The updater didn't start. Try again, or run Media Stack Setup to update.")
    _dl(state="applying")

    def close():
        time.sleep(0.6)  # let the page get its answer first
        try:
            _app().window.destroy()  # the normal closing path: player shutdown, remote, sharing...
        except Exception as e:
            print("update: closing failed:", e)
        time.sleep(20)  # the updater waits up to 60 s for this process; never leave it hanging
        os._exit(0)
    threading.Thread(target=close, daemon=True).start()


# ------------------------------------------------------------------ start-up: heartbeat, results, leftovers
def _argv_value(flag):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
    return None


def started(app):
    """Called once the window shows and the player is attached: tells a waiting updater this build works, picks up
    what the last update did, tidies leftovers and starts the background checks."""
    global APP
    APP = app
    try:
        _write_json(DIR / "heartbeat.json", {"version": __version__, "pid": os.getpid(), "at": time.time()})
    except Exception as e:
        print("update heartbeat:", e)
    try:
        _after_update()
    except Exception as e:
        print("update leftovers:", type(e).__name__, e)
    threading.Thread(target=_loop, daemon=True, name="marquee-update-check").start()


def _after_update():
    res = _read_json(DIR / "result.json", {}) or {}
    pend = _read_json(DIR / "pending.json", {}) or {}
    upd_from, failed_ver = _argv_value("--updated"), _argv_value("--update-failed")
    if upd_from is not None and pend.get("version") == __version__:
        ST["just_updated"] = {"from": upd_from or pend.get("from"), "to": __version__, "notes": pend.get("notes") or "",
                              "notes_url": pend.get("notes_url")}
    elif failed_ver is not None or res.get("status") == "failed":
        ST["update_failed"] = {"version": failed_ver or res.get("to"),
                               "message": res.get("message") or "The new version didn't start, so Marquee went back "
                                                                  "to this one."}
    if res:  # shown once, kept for a look later
        try:
            (DIR / "result.json").replace(DIR / "result.last.json")
        except OSError:
            pass
    if ST["update_failed"] and ST["update_failed"].get("version"):
        _write_json(DIR / "failed.json", {"version": ST["update_failed"]["version"], "at": time.time()})
    inst = install_dir()
    if inst and upd_from is None and failed_ver is None:  # just started by the updater: it tidies up itself
        for leftover in (inst.with_name(inst.name + ".old"), inst.with_name(inst.name + ".failed")):
            if leftover.exists():
                shutil.rmtree(leftover, ignore_errors=True)
        stage = inst.with_name(inst.name + ".new")
        if stage.exists() and pend.get("version") and not newer(pend["version"], __version__):
            shutil.rmtree(stage, ignore_errors=True)  # already applied (or rolled back from)
    # downloads of versions that are no longer ahead
    for f in DIR.glob("Marquee-*.zip*"):
        m = re.match(r"Marquee-(.+?)-" + ASSET, f.name)
        if m and (not newer(m.group(1), __version__) or
                  (ST["update_failed"] or {}).get("version") == m.group(1)):
            f.unlink(missing_ok=True)
    if pend.get("version") and not newer(pend["version"], __version__) and not ST["update_failed"]:
        (DIR / "pending.json").unlink(missing_ok=True)
    elif pend.get("version") and newer(pend["version"], __version__):
        if staging_dir().exists() and not ST["update_failed"]:
            ST["download"] = {"state": "ready", "done": 0, "total": 0, "error": "", "version": pend["version"]}
        else:
            (DIR / "pending.json").unlink(missing_ok=True)


def _loop():
    time.sleep(FIRST_DELAY)
    while True:
        if settings.load().get("update_check") != "off":
            while _playing():
                time.sleep(60)
            try:
                check()
            except Exception as e:
                print("update check:", type(e).__name__, e)
        _wake.wait(INTERVAL)
        _wake.clear()


# ------------------------------------------------------------------ the stack (installer + stack assets)
def _stack_versions():
    """What the stack runs: {"stack": version} from <root>/stack.json "deployed" (hub / mediasorter)."""
    try:
        from . import stack
        cfg = stack.stack_config() or {}
    except Exception:
        cfg = {}
    root = cfg.get("root")
    if not root:
        return None
    st = _read_json(Path(root) / "stack.json", {}) or {}
    dep = st.get("deployed") or {}
    vers = [str(dep.get(k) or "").split(":")[-1] for k in ("hub", "mediasorter")]
    vers = [v for v in vers if parse_version(v)]
    best = max(vers, key=parse_version) if vers else None
    return {"root": root, "version": best}


def _evaluate_stack(m):
    inst, assets = m.get("installer") or {}, m.get("stack-assets") or {}
    offered = [v for v in (inst.get("version"), assets.get("version")) if parse_version(v)]
    have = _stack_versions()
    if not offered or not have or not have.get("version"):
        _set(stack=None)
        return
    best = max(offered, key=parse_version)
    if not newer(best, have["version"]):
        _set(stack=None)
        return
    exe = entry_file(inst) or {}
    _set(stack={"version": best.lstrip("v"), "current": have["version"], "has_installer": bool(exe.get("url")),
                "size": exe.get("size"), "notes_url": inst.get("notes_url") or releases_page()})


def _downloads_folder():
    try:
        from ctypes import wintypes
        guid = uuid.UUID("374DE290-123F-4565-9164-39C4925E467B")  # FOLDERID_Downloads
        buf = ctypes.c_wchar_p()
        g = (ctypes.c_byte * 16).from_buffer_copy(guid.bytes_le)
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(buf)) == 0:
            p = Path(buf.value)
            ctypes.windll.ole32.CoTaskMemFree(buf)
            return p
    except Exception:
        pass
    return Path.home() / "Downloads"


def start_stack_download():
    m = _manifest or {}
    inst = m.get("installer") or {}
    exe = entry_file(inst)
    if not ST["stack"] or not exe:
        raise ValueError("There's no installer to download")
    if ST["stack_download"]["state"] == "downloading":
        return dict(ST["stack_download"])
    ST["stack_download"] = {"state": "downloading", "done": 0, "total": exe.get("size") or 0, "error": "", "path": ""}
    threading.Thread(target=_stack_download, args=(exe, inst.get("version")), daemon=True).start()
    return dict(ST["stack_download"])


def _stack_download(exe, ver):
    sd = ST["stack_download"]
    name = re.sub(r"[^\w.-]", "", exe.get("name") or f"MediaStackSetup-{ver}.exe") or "MediaStackSetup.exe"
    dest = _downloads_folder() / name
    part = dest.with_name(dest.name + ".part")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        with requests.get(exe["url"], headers=UA, stream=True, timeout=(TIMEOUT, 60)) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    f.write(chunk)
                    h.update(chunk)
                    sd["done"] += len(chunk)
        if h.hexdigest() != exe["sha256"].lower() or (exe.get("size") and part.stat().st_size != exe["size"]):
            part.unlink(missing_ok=True)
            sd.update(state="error", error="The installer didn't match its checksum, so it was thrown away.")
            return _notify("stack-update", dict(sd))
        part.replace(dest)
    except Exception as e:
        part.unlink(missing_ok=True)
        sd.update(state="error", error=f"The installer couldn't be downloaded: {e}")
        return _notify("stack-update", dict(sd))
    # Media Stack Setup --root <stack folder> --update opens "Update my stack" for the existing install.
    args = [str(dest)]
    root = (_stack_versions() or {}).get("root")
    if root:
        args += ["--root", root, "--update"]
    try:
        subprocess.Popen(args, cwd=str(dest.parent), close_fds=True, creationflags=0x00000010)  # its own console
        sd.update(state="launched", path=str(dest))
    except OSError as e:
        sd.update(state="downloaded", path=str(dest), error=f"Saved to {dest}, but it couldn't be started: {e}")
    _notify("stack-update", dict(sd))


# ------------------------------------------------------------------ status + routes
def status():
    s = settings.load()
    ok, why = can_apply()
    with _lock:
        avail = dict(ST["available"]) if ST["available"] else None
        out = {"version": __version__, "checked": ST["checked"], "status": ST["status"], "message": ST["message"],
               "available": avail, "download": dict(ST["download"]), "can_apply": ok, "reason": why,
               "skipped": bool(avail and s.get("update_skip") == avail["version"] and not avail["critical"]),
               "snoozed": bool(avail and ST["snoozed"] == avail["version"]),
               "just_updated": ST["just_updated"], "update_failed": ST["update_failed"],
               "stack": ST["stack"], "stack_download": dict(ST["stack_download"]),
               "mode": s.get("update_check"), "channel": s.get("update_channel"),
               "releases_url": releases_page(), "playing": _playing(), "installed_in": str(install_dir() or "")}
    return out


bp = Blueprint("marquee_update", __name__)


def _ok(**kw):
    return jsonify(ok=True, **kw)


def _no(msg, code=400):
    return jsonify(ok=False, message=str(msg)), code


@bp.before_request
def _guard():
    origin = request.headers.get("Origin")
    if origin and origin.split("://", 1)[-1] != request.host:
        return _no("forbidden", 403)


@bp.get("/api/update/status")
def route_status():
    return _ok(**status())


@bp.post("/api/update/check")
def route_check():
    return _ok(**check(manual=True))


@bp.post("/api/update/download")
def route_download():
    try:
        start_download()
    except PermissionError as e:
        return _no(e, 409)
    except ValueError as e:
        return _no(e, 404)
    return _ok(**status())


@bp.post("/api/update/apply")
def route_apply():
    try:
        apply()
    except (RuntimeError, PermissionError) as e:
        return _no(e, 409)
    return _ok(message="Restarting to update…")


@bp.post("/api/update/skip")
def route_skip():
    v = str((request.get_json(silent=True) or {}).get("version") or "")
    if not parse_version(v):
        return _no("Which version?")
    settings.save({"update_skip": v})
    with _lock:
        if ST["download"]["version"] == v and ST["download"]["state"] in ("downloading", "paused"):
            ST["download"]["state"] = "cancelled"
    return _ok(**status())


@bp.post("/api/update/later")
def route_later():
    with _lock:
        ST["snoozed"] = (ST["available"] or {}).get("version")
    return _ok(**status())


@bp.post("/api/update/ack")
def route_ack():
    with _lock:
        ST["just_updated"] = ST["update_failed"] = None
    return _ok()


@bp.post("/api/update/stack/download")
def route_stack_download():
    try:
        return _ok(stack_download=start_stack_download())
    except ValueError as e:
        return _no(e, 404)
