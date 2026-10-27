"""Optional parts of the assistant, installed on first use, and the speech model fetched into %APPDATA%\\Marquee\\models.

voice and gpu are pinned wheels unpacked without pip (marquee/wheels.py), so they install the same way in the
packaged app and from source; when their modules are importable already (a dev venv with requirements-ai.txt)
nothing is fetched. wake (openWakeWord) still goes through pip, so only from source.
Nothing here runs unless the user asks for voice."""
import importlib
import importlib.util
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .. import wheels

wheels.add_lib_path()

PACKAGES = {
    "voice": wheels.GROUPS["voice"],   # faster-whisper, sounddevice, webrtcvad and what they need
    "gpu": wheels.GROUPS["gpu"],       # CUDA 12 / cuDNN 9 runtime for faster-whisper
    "wake": ["openwakeword>=0.6"],     # pip only
}
PIP = {"wake"}
MODULES = {"voice": ["sounddevice", "numpy", "webrtcvad", "faster_whisper"], "gpu": [], "wake": ["openwakeword"]}

state = {"running": False, "what": None, "step": "", "error": None, "done": [], "at": 0,
         "progress": {"done": 0, "total": 0}}
_lock = threading.Lock()


def have(what):
    if what in PIP:
        return all(importlib.util.find_spec(m) is not None for m in MODULES.get(what, []))
    return all(wheels.present(n) for n in PACKAGES.get(what, []))


def missing(what):
    """Wheels a part would still download: [{kind, name, size}] (empty for pip parts)."""
    return [] if what in PIP else wheels.missing(PACKAGES[what])


def nvidia_dll_dirs():
    """bin folders of the NVIDIA runtimes (cuBLAS, cuDNN, NVRTC) on sys.path, so CUDA can load from them."""
    wheels.add_lib_path()
    out = []
    for base in map(Path, sys.path):
        nv = base / "nvidia"
        if nv.is_dir():
            out += [str(p) for p in nv.glob("*/bin") if p.is_dir()]
    return out


_added = set()


def add_dll_dirs():
    for d in nvidia_dll_dirs():
        if d in _added:
            continue
        try:
            os.add_dll_directory(d)
            _added.add(d)
        except Exception:
            pass
        if d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def has_nvidia():
    try:
        return subprocess.run(["nvidia-smi", "-L"], capture_output=True, timeout=5,
                              creationflags=0x08000000).returncode == 0
    except Exception:
        return False


def _pip(part):
    if getattr(sys, "frozen", False):
        raise RuntimeError(f"This build can't add {part} (it needs pip); run Atlas from source for it")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                        *PACKAGES[part]], capture_output=True, text=True, timeout=1800, creationflags=0x08000000)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1][:300])
    importlib.invalidate_caches()


def install(parts, on_done=None):
    """Install the given parts in the background ('voice', 'gpu', 'wake', 'model')."""
    with _lock:
        if state["running"]:
            return False
        prog = {"done": 0, "total": sum(x["size"] for p in parts if p in PACKAGES for x in missing(p))}
        state.update(running=True, what=list(parts), step="starting", error=None, at=time.time(), progress=prog)

    def run():
        try:
            for part in parts:
                if part == "model":
                    state["step"] = "downloading the speech model"
                    from . import stt
                    stt.engine().load(wait=True)
                    if stt.engine().error:
                        raise RuntimeError(stt.engine().error)
                elif part in PIP:
                    if not have(part):
                        state["step"] = f"installing {part} ({', '.join(PACKAGES[part])})"
                        _pip(part)
                elif missing(part):
                    state["step"] = f"installing {part}"
                    wheels.install(PACKAGES[part], prog)
                    prog.pop("step", None)
                if part == "gpu":
                    add_dll_dirs()
                state["done"] = sorted(set(state["done"]) | {part})
            state["step"] = "done"
        except Exception as e:
            state["error"] = str(e)
            state["step"] = "failed"
        finally:
            state["running"] = False
            if on_done:
                try:
                    on_done()
                except Exception:
                    pass
    threading.Thread(target=run, daemon=True).start()
    return True


def status():
    st = {**state, "progress": dict(state["progress"])}
    if state["running"] and state["progress"].get("step"):
        st["step"] = f"{state['step']}: {state['progress']['step']}"
    sizes = {k: sum(x["size"] for x in missing(k)) for k in PACKAGES if k not in PIP}
    return {**st, "installed": {k: have(k) for k in PACKAGES},
            "download_mb": {k: round(v / 1e6) for k, v in sizes.items()},
            "nvidia": has_nvidia(), "frozen": bool(getattr(sys, "frozen", False))}
