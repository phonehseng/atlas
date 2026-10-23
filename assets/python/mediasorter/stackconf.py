"""Settings shared by MediaSorter, the Media Hub and the health check.

Media Stack Setup writes <root>/config/mediastack.json describing every installed app (address, API key,
log file, background task/container) and the library folders. On Windows the apps run as scheduled tasks;
on Linux they are Docker containers ("platform": "docker").
"""
import http.client
import json
import os
import socket
import subprocess
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
WINDOWS = os.name == "nt"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if WINDOWS else 0


def config_path():
    env = os.environ.get("MEDIASTACK_CONFIG")
    if env:
        return Path(env)
    # Windows layout: <root>\apps\mediastack-py\mediasorter\stackconf.py -> <root>\config\shared\mediastack.json
    return HERE.parents[2] / "config" / "shared" / "mediastack.json"


def load():
    return json.loads(config_path().read_text(encoding="utf-8-sig"))


def docker(cfg):
    return cfg.get("platform") == "docker"


def has(cfg, app_id):
    return app_id in cfg.get("apps", {})


def app(cfg, app_id):
    return cfg.get("apps", {}).get(app_id) or {}


def url(cfg, app_id, path=""):
    return app(cfg, app_id).get("url", "").rstrip("/") + path


def key(cfg, app_id):
    return app(cfg, app_id).get("key") or ""


def own_state(cfg, default_key):
    """This process's own state folder (database, logs)."""
    d = Path(os.environ.get("MEDIASTACK_STATE") or cfg[default_key])
    d.mkdir(parents=True, exist_ok=True)
    return d


def hub_hook(cfg, title, message, source="mediasorter", **extra):
    """Post an event to the Media Hub's activity feed (silently does nothing if the hub isn't there)."""
    if not has(cfg, "hub"):
        return
    try:
        requests.post(url(cfg, "hub", f"/api/hook/{source}"), timeout=5, json={"title": title, "message": message, **extra})
    except Exception:
        pass


def jellyfin_headers(cfg):
    return {"Authorization": f'MediaBrowser Token="{key(cfg, "jellyfin")}"'}


# --------------------------------------------------------------------------- qBittorrent

_qbt = {"session": None, "at": 0}


def qbt(cfg):
    """A requests session for qBittorrent's API. On Windows this PC needs no login; containers sign in."""
    s = _qbt["session"]
    if s is not None and time.time() - _qbt["at"] < 1800:
        return s
    s = requests.Session()
    s.headers["Referer"] = url(cfg, "qbittorrent")
    login = cfg.get("qbittorrent_login")
    if login:
        r = s.post(url(cfg, "qbittorrent", "/api/v2/auth/login"), timeout=10,
                   data={"username": login["username"], "password": login["password"]})
        if not r.text.strip().startswith("Ok"):
            raise RuntimeError("qBittorrent rejected the stored login")
    _qbt.update(session=s, at=time.time())
    return s


def qbt_get(cfg, path, **kw):
    kw.setdefault("timeout", 10)
    r = qbt(cfg).get(url(cfg, "qbittorrent", "/api/v2" + path), **kw)
    if r.status_code == 403:
        _qbt["session"] = None
        r = qbt(cfg).get(url(cfg, "qbittorrent", "/api/v2" + path), **kw)
    r.raise_for_status()
    return r.json() if "json" in r.headers.get("content-type", "") else r.text


def qbt_post(cfg, path, **kw):
    kw.setdefault("timeout", 10)
    r = qbt(cfg).post(url(cfg, "qbittorrent", "/api/v2" + path), **kw)
    r.raise_for_status()
    return r


# --------------------------------------------------------------------------- Windows scheduled tasks

def task_name(cfg, name):
    return cfg.get("task_folder", "\\Media Stack\\") + name


def schtasks(*args):
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, creationflags=NO_WINDOW).stdout


def task_states(cfg):
    import csv
    import io
    folder = cfg.get("task_folder", "\\Media Stack\\")
    states = {}
    for row in csv.reader(io.StringIO(schtasks("/Query", "/FO", "CSV", "/NH"))):
        if len(row) >= 3 and row[0].startswith(folder):
            states[row[0][len(folder):]] = row[2]
    return states


def kill_own(cfg, *names):
    """Force-stop processes with these program names that belong to this install (their program or
    command line is inside the install folder). Other copies on the computer are left alone."""
    import psutil
    root = str(cfg.get("root") or "").lower().rstrip("\\/")
    if not root:
        return 0
    killed = 0
    for p in psutil.process_iter(["name", "exe", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() not in names:
                continue
            where = ((p.info["exe"] or "") + " " + " ".join(p.info["cmdline"] or [])).lower()
            if root + "\\" in where or root + "/" in where:
                p.kill()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return killed


def restart_task(cfg, name):
    schtasks("/End", "/TN", task_name(cfg, name))
    time.sleep(2)
    schtasks("/Run", "/TN", task_name(cfg, name))


# --------------------------------------------------------------------------- Docker (Linux)

class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=30)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        self.sock.connect(self.path)


def docker_api(method, path, body=None, raw=False):
    """Call the Docker Engine API through /var/run/docker.sock (mounted into the hub container)."""
    con = _UnixHTTP("/var/run/docker.sock")
    data = json.dumps(body).encode() if body is not None else None
    con.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
    resp = con.getresponse()
    out = resp.read()
    con.close()
    if resp.status >= 400:
        raise RuntimeError(f"docker {method} {path}: {resp.status} {out[:200]!r}")
    if raw:
        return out
    return json.loads(out) if out else None


def containers(cfg):
    """service name -> {"id", "state", "health"} for this install's Compose project."""
    proj = cfg.get("compose_project", "mediastack")
    flt = json.dumps({"label": [f"com.docker.compose.project={proj}"]})
    out = {}
    for c in docker_api("GET", "/containers/json?all=1&filters=" + requests.utils.quote(flt)) or []:
        svc = (c.get("Labels") or {}).get("com.docker.compose.service")
        if svc:
            status = c.get("Status") or ""
            health = "healthy" if "(healthy)" in status else "unhealthy" if "(unhealthy)" in status else ""
            out[svc] = {"id": c["Id"], "state": c.get("State"), "health": health}
    return out


def restart_container(cfg, service):
    c = containers(cfg).get(service)
    if not c:
        raise RuntimeError(f"no container for {service}")
    docker_api("POST", f"/containers/{c['id']}/restart?t=20")


def container_exec(cfg, service, cmd):
    """Run a command inside a container (detached)."""
    c = containers(cfg).get(service)
    if not c:
        raise RuntimeError(f"no container for {service}")
    ex = docker_api("POST", f"/containers/{c['id']}/exec", {"Cmd": cmd, "AttachStdout": False, "AttachStderr": False})
    docker_api("POST", f"/exec/{ex['Id']}/start", {"Detach": True})


def container_logs(cfg, service, lines=400):
    c = containers(cfg).get(service)
    if not c:
        return []
    raw = docker_api("GET", f"/containers/{c['id']}/logs?stdout=1&stderr=1&tail={lines}", raw=True)
    # Without a TTY the stream is framed: 8-byte header (stream, 0, 0, 0, size big-endian) + payload.
    out, i = [], 0
    while i + 8 <= len(raw) and raw[i] in (0, 1, 2) and raw[i + 1:i + 4] == b"\0\0\0":
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    text = b"".join(out) if out else raw
    return text.decode("utf-8", "replace").splitlines()
