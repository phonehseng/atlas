"""What the media index needs, and installing it on first use.

Three things, all optional for the rest of Marquee:
  * Python packages (numpy, onnxruntime-directml, tokenizers). Taken from the environment when present
    (requirements-ai.txt); otherwise their pinned wheels are fetched from PyPI and unpacked into
    %APPDATA%\\Marquee\\ai\\py312, which also works in the packaged app (no pip needed).
  * The CLIP ViT-B/32 image and text encoders (ONNX, fp16) and tokenizer, into %APPDATA%\\Marquee\\models.
  * ffmpeg: Jellyfin's own (jellyfin-ffmpeg) when it's installed, else a pinned build into
    %APPDATA%\\Marquee\\tools.
Every download is pinned to an exact URL and checked against its SHA-256.
"""
import importlib
import os
import shutil
import threading
import zipfile
from pathlib import Path

from .. import settings, wheels

HOME = settings.DIR
LIB_DIR = wheels.LIB_DIR
MODEL_DIR = HOME / "models" / "clip-vit-b32"
TOOLS_DIR = HOME / "tools"

# ---------------------------------------------------------------- pinned sources
# Wheels (CPython 3.12, Windows x64) are pinned in marquee/wheels.py, shared with the assistant.
WHEELS = wheels.GROUPS["index"]

# OpenAI CLIP ViT-B/32 (weights MIT-licensed, https://github.com/openai/CLIP), ONNX export by Xenova
# (https://huggingface.co/Xenova/clip-vit-base-patch32), pinned to a commit.
MODEL_ID = "clip-vit-b32-fp16"
_HF = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/d15189d7028b43f1d3e65039190477f6af591c2a/"
MODEL_FILES = {
    "vision_model_fp16.onnx": {"url": _HF + "onnx/vision_model_fp16.onnx", "size": 176080659,
                               "sha256": "35c4e0fb0aeee527dcde1693520b214a34424a786babd530f35366bad5844efd"},
    "text_model_fp16.onnx": {"url": _HF + "onnx/text_model_fp16.onnx", "size": 127339794,
                             "sha256": "df587ffbf248bf20d44fa6e16adc5ebc27ead691860e5333dbdaab5fd6bf3f6e"},
    "tokenizer.json": {"url": _HF + "tokenizer.json", "size": 2224119,
                       "sha256": "f7f3b7af117d467b58374797691a6438d3e6b9e9cef800dfd5dced7f697a90cd"},
}
MODEL_LICENCE = "MIT (OpenAI CLIP)"

# Static Windows build of ffmpeg (GPL), only used when Jellyfin's ffmpeg isn't installed.
FFMPEG_BUILD = {"name": "ffmpeg 9.0.2 essentials (gyan.dev, GPL-3.0)", "size": 114768076,
                "sha256": "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba",
                "url": "https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip"}
FFMPEG_CANDIDATES = [
    os.environ.get("MARQUEE_FFMPEG", ""),
    r"C:\Program Files\Jellyfin\Server\ffmpeg.exe",
    r"C:\Program Files\jellyfin-ffmpeg\ffmpeg.exe",
    r"C:\Program Files\Jellyfin\jellyfin-ffmpeg\ffmpeg.exe",
    r"C:\ProgramData\Jellyfin\Server\ffmpeg.exe",
]



class _Lazy:
    """A module imported on first attribute use, so this package imports fine before numpy is installed."""

    def __init__(self, name):
        self._name = name

    def __getattr__(self, k):
        return getattr(importlib.import_module(self._name), k)


np = _Lazy("numpy")

INSTALL_HINT = "POST /api/index/install downloads what's missing (about {mb} MB)."


# ---------------------------------------------------------------- checks
add_lib_path = wheels.add_lib_path


def find_ffmpeg():
    for c in FFMPEG_CANDIDATES:
        if c and os.path.isfile(c):
            return c
    local = next(TOOLS_DIR.glob("ffmpeg-*/bin/ffmpeg.exe"), None) if TOOLS_DIR.is_dir() else None
    if local:
        return str(local)
    return shutil.which("ffmpeg")


def missing():
    """What install() would fetch: a list of dicts {kind, name, size}."""
    out = wheels.missing(WHEELS, deep=True)
    for fn, f in MODEL_FILES.items():
        if not (MODEL_DIR / fn).is_file():
            out.append({"kind": "model", "name": f"CLIP ViT-B/32 {fn}", "size": f["size"]})
    if not find_ffmpeg():
        out.append({"kind": "ffmpeg", "name": FFMPEG_BUILD["name"], "size": FFMPEG_BUILD["size"]})
    return out


def availability():
    m = missing()
    mb = round(sum(x["size"] for x in m) / 1e6)
    return {"available": not m, "missing": m, "download_mb": mb,
            "install": INSTALL_HINT.format(mb=mb) if m else None, "installing": dict(INSTALL)}


# ---------------------------------------------------------------- install
INSTALL = {"running": False, "step": "", "done": 0, "total": 0, "error": None}
_install_lock = threading.Lock()


def _download(url, dest: Path, sha256, size):
    wheels.download(url, dest, sha256, INSTALL)


def install():
    """Fetch everything that's missing. Blocking; run it in a thread. Progress in INSTALL."""
    if not _install_lock.acquire(blocking=False):
        return
    try:
        todo = missing()
        INSTALL.update(running=True, error=None, done=0, total=sum(x["size"] for x in todo))
        wheels.install(WHEELS, INSTALL, deep=True)
        for fn, f in MODEL_FILES.items():
            if not (MODEL_DIR / fn).is_file():
                INSTALL["step"] = f"model {fn}"
                _download(f["url"], MODEL_DIR / fn, f["sha256"], f["size"])
        if not find_ffmpeg():
            INSTALL["step"] = FFMPEG_BUILD["name"]
            tmp = TOOLS_DIR / "ffmpeg.zip"
            _download(FFMPEG_BUILD["url"], tmp, FFMPEG_BUILD["sha256"], FFMPEG_BUILD["size"])
            with zipfile.ZipFile(tmp) as z:
                z.extractall(TOOLS_DIR)
            tmp.unlink(missing_ok=True)
        add_lib_path()
        INSTALL["step"] = "done"
    except Exception as e:
        INSTALL["error"] = str(e)
    finally:
        INSTALL["running"] = False
        _install_lock.release()
