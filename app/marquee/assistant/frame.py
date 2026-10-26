"""Grab the picture that's on screen right now, as a JPEG about 768 px wide (kept in memory only)."""
import io
import os
import subprocess
import tempfile

WIDTH = 768


def _jpeg(img, width=WIDTH, quality=85):
    from PIL import Image
    img = img.convert("RGB")
    if img.width > width:
        img = img.resize((width, max(2, round(img.height * width / img.width))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def from_mpv(m, width=WIDTH):
    """The current video frame from mpv (no controls or cards on it)."""
    from PIL import Image
    try:
        res = m.command("screenshot-raw", "video", "bgra")
        if res and res.get("w"):
            w, h, stride = res["w"], res["h"], res["stride"]
            img = Image.frombuffer("RGBA", (w, h), bytes(res["data"]), "raw", "BGRA", stride, 1)
            return _jpeg(img, width)
    except Exception as e:
        print("screenshot-raw failed:", e)
    fd, path = tempfile.mkstemp(suffix=".png", prefix="marquee-frame-")
    os.close(fd)
    try:  # older mpv / odd formats: through a temporary PNG, deleted right away
        m.command("screenshot-to-file", path, "video")
        with Image.open(path) as img:
            return _jpeg(img, width)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
           "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


def is_hdr(item):
    """Does the item's video carry HDR (PQ/HLG, Dolby Vision)? Its frames need tone mapping to look right."""
    v = next((s for s in (item.get("MediaSources") or [{}])[0].get("MediaStreams") or [] if s.get("Type") == "Video"), {})
    return (v.get("VideoRangeType") or v.get("VideoRange") or "SDR").upper() not in ("SDR", "UNKNOWN", "")


def from_file(path, t, width=WIDTH, hdr=False):
    """A frame of a file at t seconds through ffmpeg (when the player can't give one); HDR is tone mapped."""
    from ..index import deps
    ff = deps.find_ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg not found")
    chains = ([f"scale={width}:-2:flags=bicubic,{TONEMAP}"] if hdr else []) + [f"scale={width}:-2"]
    err = b""
    for vf in chains:  # an ffmpeg without zscale: the plain frame
        r = subprocess.run([ff, "-v", "error", "-ss", f"{max(0, t):.2f}", "-i", path, "-frames:v", "1",
                            "-vf", vf, "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "pipe:1"],
                           capture_output=True, timeout=30, creationflags=0x08000000)
        if not r.returncode and r.stdout:
            return r.stdout
        err = r.stderr
    raise RuntimeError("couldn't read a frame: " + err.decode(errors="replace")[-200:])


def grab(player, width=WIDTH):
    """The frame on screen in app.player, else from the file at the player's position."""
    m = getattr(player, "mpv", None)
    if m is not None and getattr(player, "playing", False):
        try:
            return from_mpv(m, width)
        except Exception as e:
            print("frame from mpv failed:", e)
    item = getattr(player, "item", None) or {}
    path = (item.get("MediaSources") or [{}])[0].get("Path") or item.get("Path")
    if path and os.path.exists(path):
        pos = getattr(player, "_pos", 0) or 0
        try:
            pos = m.time_pos or pos
        except Exception:
            pass
        return from_file(path, pos, width)
    raise RuntimeError("no picture to look at")
