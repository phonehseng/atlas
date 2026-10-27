"""The built-in AI: a small local model that runs on this PC, no account, key or Atlas needed.

Engine: llama.cpp's llama-server (MIT), an official pinned release from github.com/ggml-org/llama.cpp. The Vulkan
build runs on NVIDIA, AMD and Intel graphics (and falls back to the CPU). Model: Qwen3-VL Instruct (Apache-2.0) from
Qwen's own GGUF repository on Hugging Face, with its vision projector so it can look at a frame. Everything is
pinned by version/commit and SHA-256 and checked after download; nothing is fetched until the user installs it.

    standard  Qwen3-VL 4B Instruct, Q4_K_M + mmproj Q8_0   (about 3.0 GB, best on a graphics card)
    small     Qwen3-VL 2B Instruct, Q4_K_M + mmproj Q8_0   (about 1.6 GB, for weaker PCs / CPU only)

The server starts lazily on 127.0.0.1 at a random port (below-normal priority, no window, tied to Atlas's
process so it can't outlive it) and stops again after 5 minutes without a request. It is never started while a
video plays, except for callers that pass allow_during_playback (Ask Atlas, the ending comment).

Callers don't talk to llama-server directly: provider() hands out the address of a tiny in-process gateway, so
asking "is the AI set up?" never loads a model, and the model is only started by a real request."""
import atexit
import ctypes
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from . import settings

# ------------------------------------------------------------------ pinned downloads
TAG = "b11889"  # llama.cpp release
_GH = f"https://github.com/ggml-org/llama.cpp/releases/download/{TAG}/"
ENGINES = {
    # Runs on any GPU with a Vulkan driver (NVIDIA, AMD, Intel) and includes the CPU fallback. The CUDA build was
    # measured too (RTX 5080, same model): ~100-125 tokens/s against Vulkan's ~150, for a 590 MB bigger download,
    # so it isn't offered.
    "vulkan": [{"file": f"llama-{TAG}-bin-win-vulkan-x64.zip", "size": 37049027,
                "sha256": "3874d6cb33c3540a68191c1ca31b774bd72914e8ca65e51dfe23e3dae8201f00"}],
}
_HF = "https://huggingface.co/{repo}/resolve/{rev}/{file}"
MODELS = {
    "standard": {
        "label": "Standard (Qwen3-VL 4B)", "alias": "qwen3-vl-4b",
        "repo": "Qwen/Qwen3-VL-4B-Instruct-GGUF", "rev": "1cd86afb9a95c410a6038ab3b40d8b578c892266",
        "model": {"file": "Qwen3VL-4B-Instruct-Q4_K_M.gguf", "size": 2497281664,
                  "sha256": "66358cb18bb6b3b1b6675aa412c7a88ef01d228f481184d13668e5201c730a0a"},
        "mmproj": {"file": "mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf", "size": 453974304,
                   "sha256": "30ba2c7dd3127a4561b6cba9d13d0f711c91bdb38742e2f56d73c8cb596bd06d"},
    },
    "small": {
        "label": "Small (Qwen3-VL 2B)", "alias": "qwen3-vl-2b",
        "repo": "Qwen/Qwen3-VL-2B-Instruct-GGUF", "rev": "52d6c8ffea26cc873ac5ad116f8631268d7eb503",
        "model": {"file": "Qwen3VL-2B-Instruct-Q4_K_M.gguf", "size": 1107409952,
                  "sha256": "089d75c52f4b7ffc56ba998ffc50aae89fcafc755f9e7208aacca281dca6c2ae"},
        "mmproj": {"file": "mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf", "size": 445053216,
                   "sha256": "f9a68fabba69c3b81e153367b2c7521030b0fa8bb0de400c9599c8e6725f9c82"},
    },
}

AI_DIR = settings.DIR / "ai"
ENGINE_ROOT = AI_DIR / "llama"
MODEL_DIR = AI_DIR / "models"
LOG = AI_DIR / "llama-server.log"

CTX = 8192
IDLE_STOP = 5 * 60          # stop the server after this long without a request
IDLE_STOP_PLAYING = 5       # ...or almost at once while a video plays (gives the GPU and its memory back to the player)
LAYERS = {"standard": 36, "small": 28}   # transformer layers per model, for a graphics-memory cap
START_TIMEOUT = 180

CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL = 0x00004000


class Busy(RuntimeError):
    """A video is playing and the caller isn't allowed to start the model now."""


# ------------------------------------------------------------------ what's installed
def _engine_dir(backend):
    return ENGINE_ROOT / f"{TAG}-{backend}"


def _marker(path):
    return path.with_name(path.name + ".verified")


def _file_ok(path, spec):
    try:
        return path.is_file() and path.stat().st_size == spec["size"] and \
            _marker(path).read_text().strip() == spec["sha256"]
    except OSError:
        return False


def engine_installed(backend):
    d = _engine_dir(backend)
    try:
        ok = (d / "VERIFIED").read_text().split() == [a["sha256"] for a in ENGINES[backend]]
    except OSError:
        return False
    return ok and (d / "llama-server.exe").is_file()


def model_installed(size):
    m = MODELS.get(size)
    return bool(m) and _file_ok(MODEL_DIR / m["model"]["file"], m["model"]) and \
        _file_ok(MODEL_DIR / m["mmproj"]["file"], m["mmproj"])


def installed():
    """True when an engine and at least one model are ready (cheap: file sizes and markers only)."""
    return any(engine_installed(b) for b in ENGINES) and any(model_installed(s) for s in MODELS)


def _backend():
    return next((b for b in ENGINES if engine_installed(b)), None)


def _size():
    want = settings.load().get("ai_builtin_size") or "standard"
    if model_installed(want):
        return want
    return next((s for s in MODELS if model_installed(s)), None)


def _dir_size(p):
    try:
        return sum(f.stat().st_size for f in Path(p).rglob("*") if f.is_file())
    except OSError:
        return 0


# ------------------------------------------------------------------ downloads
_install = {"state": "idle"}     # progress of the current/last install, shown by /api/ai/builtin/status
_install_lock = threading.Lock()
_cancel = threading.Event()


def _download(url, dest, spec, on_bytes):
    """Download to dest (resuming a .part file), checking size and SHA-256 before it takes the final name."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    have = part.stat().st_size if part.is_file() else 0
    if have > spec["size"]:
        part.unlink()
        have = 0
    if have:
        with open(part, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        on_bytes(have)
    headers = {"Range": f"bytes={have}-"} if have else {}
    with requests.get(url, stream=True, timeout=(20, 120), headers=headers) as r:
        if have and r.status_code != 206:  # no resume: start over
            have, h = 0, hashlib.sha256()
            on_bytes(-part.stat().st_size)
        r.raise_for_status()
        with open(part, "ab" if have else "wb") as f:
            for chunk in r.iter_content(1 << 20):
                if _cancel.is_set():
                    raise RuntimeError("cancelled")
                f.write(chunk)
                h.update(chunk)
                on_bytes(len(chunk))
    got = part.stat().st_size
    if got != spec["size"] or h.hexdigest() != spec["sha256"]:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{dest.name}: download didn't match the pinned checksum")
    part.replace(dest)


def _extract(zpath, target):
    """Unzip into a fresh folder next to target, refusing paths that would land outside it, then swap it in."""
    tmp = target.with_name(target.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    root = tmp.resolve()
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            out = (tmp / info.filename).resolve()
            if root != out and root not in out.parents:
                raise RuntimeError(f"unsafe path in {zpath.name}: {info.filename}")
        z.extractall(tmp)
    exe = next(tmp.rglob("llama-server.exe"), None)
    if exe and exe.parent != tmp:  # flatten a nested folder
        for f in list(exe.parent.iterdir()):
            f.replace(tmp / f.name)
    return tmp


def install(size="standard"):
    """Start installing in the background (engine if missing + the model of this size); returns the progress."""
    if size not in MODELS:
        raise ValueError("size must be one of: " + ", ".join(MODELS))
    with _install_lock:
        if _install.get("state") in ("downloading", "verifying", "extracting"):
            return dict(_install)
        _cancel.clear()
        backends = ["vulkan"]
        m = MODELS[size]
        jobs = []  # (url, dest, spec, kind)
        for b in backends:
            if not engine_installed(b):
                for a in ENGINES[b]:
                    jobs.append((_GH + a["file"], AI_DIR / "downloads" / a["file"], a, "engine:" + b))
        for part in ("model", "mmproj"):
            spec = m[part]
            if not _file_ok(MODEL_DIR / spec["file"], spec):
                jobs.append((_HF.format(repo=m["repo"], rev=m["rev"], file=spec["file"]), MODEL_DIR / spec["file"],
                             spec, part))
        _install.clear()
        _install.update(state="downloading", size=size, engine=backends[-1], file=None, done=0,
                        total=sum(j[2]["size"] for j in jobs), error=None, started=time.time())
    threading.Thread(target=_install_worker, args=(jobs, backends, size), daemon=True).start()
    return dict(_install)


def _install_worker(jobs, backends, size):
    def tick(n):
        _install["done"] = max(0, _install["done"] + n)
    try:
        for url, dest, spec, kind in jobs:
            _install["file"] = spec["file"]
            if dest.is_file() and dest.stat().st_size == spec["size"]:
                _install["state"] = "verifying"  # already here (an earlier, interrupted install): check it
                h = hashlib.sha256()
                with open(dest, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 22), b""):
                        h.update(chunk)
                        tick(len(chunk))
                if h.hexdigest() != spec["sha256"]:
                    dest.unlink()
                    tick(-spec["size"])
            if not dest.is_file():
                _install["state"] = "downloading"
                _download(url, dest, spec, tick)
            if kind in ("model", "mmproj"):
                _marker(dest).write_text(spec["sha256"])
        for b in backends:
            if engine_installed(b):
                continue
            _install["state"], _install["file"] = "extracting", f"llama.cpp {TAG} ({b})"
            target = _engine_dir(b)
            tmp = None
            for a in ENGINES[b]:  # extra archives (runtimes) land in the same folder
                z = AI_DIR / "downloads" / a["file"]
                t = _extract(z, target if tmp is None else target.with_name(target.name + ".rt"))
                if tmp is None:
                    tmp = t
                else:
                    for f in t.iterdir():
                        f.replace(tmp / f.name)
                    shutil.rmtree(t, ignore_errors=True)
            if not (tmp / "llama-server.exe").is_file():
                raise RuntimeError("llama-server.exe missing from the release")
            (tmp / "VERIFIED").write_text(" ".join(a["sha256"] for a in ENGINES[b]))
            shutil.rmtree(target, ignore_errors=True)
            tmp.replace(target)
            for a in ENGINES[b]:
                (AI_DIR / "downloads" / a["file"]).unlink(missing_ok=True)
        shutil.rmtree(AI_DIR / "downloads", ignore_errors=True)
        if not settings.load().get("ai_builtin_size") or not model_installed(settings.load().get("ai_builtin_size")):
            settings.save({"ai_builtin_size": size})
        _install.update(state="done", file=None, finished=time.time())
    except Exception as e:
        _install.update(state="cancelled" if _cancel.is_set() else "error", error=str(e), finished=time.time())
        print("built-in AI install failed:", e)


def remove(size=None):
    """Delete a model (size) or everything (engine + all models + downloads). Stops the server first."""
    _cancel.set()
    ENGINE.stop()
    if size in MODELS:
        for part in ("model", "mmproj"):
            p = MODEL_DIR / MODELS[size][part]["file"]
            for f in (p, _marker(p), p.with_name(p.name + ".part")):
                f.unlink(missing_ok=True)
        return
    for _ in range(20):  # a just-stopped process can hold its DLLs for a moment
        shutil.rmtree(AI_DIR, ignore_errors=True)
        if not AI_DIR.exists():
            break
        time.sleep(0.25)
    _install.clear()
    _install["state"] = "idle"


# ------------------------------------------------------------------ the server process
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _JobObject:
    """A Windows job that kills its processes when Atlas exits, even on a hard exit (os._exit / a crash)."""

    def __init__(self):
        self.handle = None
        try:
            k = ctypes.windll.kernel32
            k.CreateJobObjectW.restype = ctypes.c_void_p
            h = k.CreateJobObjectW(None, None)

            class Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                            ("SchedulingClass", ctypes.c_uint32)]

            class Extended(ctypes.Structure):
                _fields_ = [("Basic", Basic), ("Io", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]
            info = Extended()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if h and k.SetInformationJobObject(ctypes.c_void_p(h), 9, ctypes.byref(info), ctypes.sizeof(info)):
                self.handle = h
        except Exception as e:
            print("job object unavailable:", e)

    def add(self, proc):
        if self.handle:
            try:
                ctypes.windll.kernel32.AssignProcessToJobObject(ctypes.c_void_p(self.handle),
                                                                ctypes.c_void_p(int(proc._handle)))
            except Exception:
                pass


_devices = {}
# Integrated graphics share system memory and are much slower; a discrete card is preferred when there is one.
_INTEGRATED = re.compile(r"\b(UHD|Iris|HD Graphics|Intel\(R\) Graphics|Radeon\(TM\) (?:\d+M )?Graphics|Radeon Graphics|"
                         r"Vega \d+|Adreno|llvmpipe|Microsoft Basic)", re.I)


def list_devices(exe):
    """GPUs llama.cpp can use: [{"id": "Vulkan0", "name", "total_mb", "free_mb"}] (cached per engine)."""
    if str(exe) not in _devices:
        out = []
        try:
            r = subprocess.run([str(exe), "--list-devices"], capture_output=True, text=True, timeout=30,
                               cwd=str(Path(exe).parent), creationflags=CREATE_NO_WINDOW)
            for m in re.finditer(r"^\s*(\w+\d+): (.+?) \((\d+) MiB, (\d+) MiB free\)", r.stdout + r.stderr, re.M):
                out.append({"id": m.group(1), "name": m.group(2).strip(), "total_mb": int(m.group(3)),
                            "free_mb": int(m.group(4))})
        except Exception as e:
            print("llama.cpp device list failed:", e)
        _devices[str(exe)] = out
    return _devices[str(exe)]


def best_device(exe):
    """One GPU to run on (never split across a discrete card and integrated graphics), or None for the CPU."""
    devs = list_devices(exe)
    if not devs:
        return None

    def rank(d):
        integrated = bool(_INTEGRATED.search(d["name"]))
        brand = 0 if "nvidia" in d["name"].lower() else 1
        return (integrated, brand, -d["free_mb"])
    return sorted(devs, key=rank)[0]


def _playing():
    try:
        from . import server
        return bool(getattr(getattr(server.APP, "player", None), "playing", False))
    except Exception:
        return False


class Engine:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.port = None
        self.size = None
        self.backend = None
        self.device = None
        self.active = 0
        self.last_used = 0.0
        self.started_at = None
        self.error = None
        self._job = None
        self._watch = None

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def acquire(self, allow_during_playback=False):
        """Make sure the server runs (starting it if needed) and count a request in. Returns its port."""
        with self.lock:
            if not self.running:
                if _playing() and not allow_during_playback:
                    raise Busy("a video is playing")
                self._start()
            elif _playing() and not allow_during_playback:
                raise Busy("a video is playing")
            self.active += 1
            self.last_used = time.time()
            return self.port

    def release(self):
        with self.lock:
            self.active = max(0, self.active - 1)
            self.last_used = time.time()

    def _args(self, exe, size, device):
        m = MODELS[size]
        return [str(exe), "-m", str(MODEL_DIR / m["model"]["file"]), "--mmproj", str(MODEL_DIR / m["mmproj"]["file"]),
                "--host", "127.0.0.1", "--port", str(self.port), "-c", str(CTX), "-np", "1",
                "-ngl", str(self._gpu_layers(size)) if device else "0", "-dev", device["id"] if device else "none",
                "--alias", m["alias"], "--no-webui"] + ([] if device and not self._mmproj_on_cpu(size) else ["--no-mmproj-offload"])

    @staticmethod
    def _gpu_layers(size):
        """All layers on the graphics card, unless Settings caps its memory: then as many as fit (the rest run on
        the CPU, slower but leaving the memory to everything else)."""
        cap = float(settings.load().get("ai_vram_gb") or 0)
        if cap <= 0:
            return 999
        m = MODELS[size]
        per_layer = m["model"]["size"] / 1e9 / LAYERS.get(size, 32)
        room = cap - 0.6 - (m["mmproj"]["size"] / 1e9 if not Engine._mmproj_on_cpu(size) else 0)  # 0.6: context
        return max(0, min(LAYERS.get(size, 32), int(room / per_layer)))

    @staticmethod
    def _mmproj_on_cpu(size):
        cap = float(settings.load().get("ai_vram_gb") or 0)
        return 0 < cap < 2.5

    def _start(self):
        backend, size = _backend(), _size()
        if not backend or not size:
            raise RuntimeError("The built-in AI isn't installed (Settings > AI)")
        exe = _engine_dir(backend) / "llama-server.exe"
        gpu = bool(settings.load().get("ai_builtin_gpu", True))
        device = best_device(exe) if gpu else None
        if self._job is None:
            self._job = _JobObject()
        for attempt in ([device, None] if device else [None]):
            self.port = _free_port()
            AI_DIR.mkdir(parents=True, exist_ok=True)
            log = open(LOG, "wb")
            try:
                self.proc = subprocess.Popen(self._args(exe, size, attempt), cwd=str(exe.parent),
                                             stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                             creationflags=CREATE_NO_WINDOW | BELOW_NORMAL)
            finally:
                log.close()
            self._job.add(self.proc)
            if self._wait_ready():
                self.size, self.backend, self.device = size, backend, attempt
                self.started_at = time.time()
                self.error = None
                if self._watch is None or not self._watch.is_alive():
                    self._watch = threading.Thread(target=self._watchdog, daemon=True)
                    self._watch.start()
                return
            self._kill()
            self.error = _log_tail()
            print("built-in AI failed to start" + (" on the GPU, trying the CPU" if attempt else ""))
        raise RuntimeError("The built-in AI couldn't start: " + (self.error or "unknown error")[-300:])

    def _wait_ready(self):
        end = time.time() + START_TIMEOUT
        while time.time() < end:
            if self.proc.poll() is not None:
                return False
            try:
                if requests.get(f"http://127.0.0.1:{self.port}/health", timeout=2).status_code == 200:
                    return True
            except requests.RequestException:
                pass
            time.sleep(0.3)
        return False

    def _kill(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    def stop(self):
        with self.lock:
            self._kill()
            self.port = None

    def _watchdog(self):
        while True:
            time.sleep(10)
            with self.lock:
                if not self.running:
                    self.proc = None
                    return
                idle = time.time() - self.last_used
                if self.active == 0 and (idle > IDLE_STOP or (_playing() and idle > IDLE_STOP_PLAYING)):
                    self._kill()
                    return

    def info(self):
        on = self.running
        return {"running": on, "size": self.size if on else None, "backend": self.backend if on else None,
                "gpu": (self.device or {}).get("name") if on else None, "cpu_only": on and not self.device,
                "idle_seconds": int(time.time() - self.last_used) if on else None, "error": self.error}


def _log_text():
    try:
        return LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _log_tail():
    lines = [ln for ln in _log_text().splitlines() if ln.strip()]
    return " | ".join(lines[-4:])


ENGINE = Engine()
atexit.register(ENGINE.stop)


# ------------------------------------------------------------------ the in-process gateway
class _Gateway(BaseHTTPRequestHandler):
    """/v1/... -> llama-server, started on demand. /play/v1/... is the same but may start during playback."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _reply(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self, method):
        path = self.path
        allow = path.startswith("/play/")
        if allow:
            path = path[5:]
        if not path.startswith("/v1/"):
            return self._reply(404, b'{"error":"not found"}')
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if method == "POST" else None
        if path == "/v1/models":  # answer without starting anything
            size = _size()
            return self._reply(200, json.dumps({"object": "list", "data": [
                {"id": MODELS[size]["alias"], "object": "model", "owned_by": "marquee"}] if size else []}).encode())
        try:
            port = ENGINE.acquire(allow)
        except Busy as e:
            return self._reply(503, json.dumps({"error": {"message": str(e), "type": "busy"}}).encode())
        except Exception as e:
            return self._reply(503, json.dumps({"error": {"message": str(e), "type": "unavailable"}}).encode())
        try:
            r = requests.request(method, f"http://127.0.0.1:{port}{path}", data=body, timeout=600,
                                 headers={"Content-Type": self.headers.get("Content-Type") or "application/json"})
            self._reply(r.status_code, r.content, r.headers.get("Content-Type") or "application/json")
        except Exception as e:
            self._reply(502, json.dumps({"error": {"message": str(e)}}).encode())
        finally:
            ENGINE.release()

    def do_GET(self):
        self._forward("GET")

    def do_POST(self):
        self._forward("POST")


_gw = {"server": None, "lock": threading.Lock()}


def gateway_url():
    with _gw["lock"]:
        if _gw["server"] is None:
            srv = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            _gw["server"] = srv
        return f"http://127.0.0.1:{_gw['server'].server_address[1]}"


def provider(allow_during_playback=True):
    """The OpenAI-compatible provider dict ai._provider() hands out, or None when not installed. Cheap: nothing
    starts until a request arrives at the gateway."""
    size = _size()
    if not size or not _backend():
        return None
    return {"url": gateway_url() + ("/play" if allow_during_playback else "") + "/v1", "key": "local",
            "model": MODELS[size]["alias"], "name": "Atlas AI", "kind": "builtin", "vision": True}


# ------------------------------------------------------------------ status for the settings page
def status():
    sizes = {}
    for k, m in MODELS.items():
        sizes[k] = {"label": m["label"], "installed": model_installed(k),
                    "download": m["model"]["size"] + m["mmproj"]["size"] + ENGINES["vulkan"][0]["size"]}
    return {
        "installed": installed(),
        "sizes": sizes,
        "selected": _size(),
        "engine": {"name": "llama.cpp", "version": TAG, "installed": bool(_backend()),
                   "download": sum(a["size"] for a in ENGINES["vulkan"])},
        "gpu_enabled": bool(settings.load().get("ai_builtin_gpu", True)),
        "disk": _dir_size(AI_DIR) if AI_DIR.exists() else 0,
        "download": dict(_install),
        **ENGINE.info(),
    }
