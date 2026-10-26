"""Clip export: cut slices out of the video that's playing and save them as web-friendly .mp4 (H.264/AAC) and,
optionally, .gif. Video is stream-copied when a slice starts on a keyframe of an H.264 8-bit source; otherwise it's
a quick NVENC re-encode (x264 when NVENC isn't there), tone-mapped to SDR for HDR sources. Uses an ffmpeg that's
already on the PC (Jellyfin's), so nothing is downloaded."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

CANDIDATES = [r"C:\Program Files\Jellyfin\Server", r"C:\Program Files\Jellyfin\Server\ffmpeg"]
NO_WINDOW = 0x08000000
BELOW_NORMAL = 0x00004000
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
           "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


def tools():
    """(ffmpeg, ffprobe) paths, or (None, None)."""
    dirs = [os.environ.get("MARQUEE_FFMPEG_DIR", "")] + CANDIDATES + [str(Path(os.environ.get("APPDATA", "")) / "Marquee" / "tools")]
    for d in dirs:
        if d and os.path.exists(os.path.join(d, "ffmpeg.exe")):
            probe = os.path.join(d, "ffprobe.exe")
            return os.path.join(d, "ffmpeg.exe"), probe if os.path.exists(probe) else None
    ff = shutil.which("ffmpeg")
    return (ff, shutil.which("ffprobe")) if ff else (None, None)


def root():
    """The folder all clips are saved under (one subfolder per title)."""
    return os.environ.get("MARQUEE_DEV_CLIPS") or str(Path.home() / "Videos" / "Marquee Clips")


def out_dir(title):
    base = root()
    safe = re.sub(r'[<>:"/\\|?*]+', "", title or "Clip").strip() or "Clip"
    d = Path(base) / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run(args, progress=None, duration=None):
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         creationflags=NO_WINDOW | BELOW_NORMAL)
    err = []
    t = threading.Thread(target=lambda: err.extend(p.stderr), daemon=True)
    t.start()
    for line in p.stdout:
        if progress and duration and line.startswith("out_time_us="):
            try:
                progress(min(1.0, int(line.split("=")[1]) / 1e6 / duration))
            except ValueError:
                pass
    p.wait()
    t.join(1)
    if p.returncode:
        raise RuntimeError("ffmpeg: " + "".join(err[-6:]).strip()[-400:])


def probe(ffprobe, src):
    if not ffprobe:
        return {}
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name,pix_fmt,color_transfer,height", "-of", "json", src],
                       capture_output=True, text=True, creationflags=NO_WINDOW, timeout=20)
    try:
        return (json.loads(r.stdout).get("streams") or [{}])[0]
    except Exception:
        return {}


def keyframe_near(ffprobe, src, t, tol=0.05):
    """Is there a video keyframe within tol seconds of t?"""
    if not ffprobe:
        return False
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey", "-show_entries",
                        "frame=pts_time", "-read_intervals", f"{max(0, t - 6):.3f}%{t + 1:.3f}", "-of", "csv=p=0", src],
                       capture_output=True, text=True, creationflags=NO_WINDOW, timeout=30)
    for line in r.stdout.split():
        try:
            if abs(float(line.strip(",")) - t) <= tol:
                return True
        except ValueError:
            pass
    return False


def export(src, slices, title, join=False, gif=False, audio_index=None, progress=None):
    """slices: [(start, end)] seconds. audio_index: ffmpeg index of the audio stream to keep (None: first).
    progress(fraction, label). Returns (folder, [files])."""
    ffmpeg, ffprobe = tools()
    if not ffmpeg:
        raise RuntimeError("no ffmpeg on this PC (Jellyfin's wasn't found)")
    info = probe(ffprobe, src)
    hdr = info.get("color_transfer") in ("smpte2084", "arib-std-b67")
    copyable = info.get("codec_name") == "h264" and info.get("pix_fmt") in ("yuv420p", "yuvj420p") and not join
    folder = out_dir(title)
    stem = re.sub(r'[<>:"/\\|?*]+', "", title or "Clip").strip() or "Clip"
    n = len(slices)
    files, parts = [], []
    tmp = Path(tempfile.mkdtemp(prefix="marquee-clip-"))
    amap = ["-map", f"0:{audio_index}" if audio_index is not None else "0:a:0?"]
    try:
        for i, (a, b) in enumerate(slices):
            dur = max(0.1, b - a)
            name = f"{stem} {_stamp(a)}-{_stamp(b)}.mp4"
            out = (tmp / f"part{i}.mp4") if join else _unique(folder / name)
            label = f"Saving clip {i + 1}/{n}" if n > 1 else "Saving clip"
            prog = (lambda f, label=label: progress(f, label)) if progress else None
            common = ["-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]
            if copyable and keyframe_near(ffprobe, src, a):
                args = [ffmpeg, *common, "-ss", f"{a:.3f}", "-i", src, "-t", f"{dur:.3f}", "-map", "0:v:0", *amap,
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-movflags", "+faststart", str(out)]
                _run(args, prog, dur)
            else:
                vf = (TONEMAP + ",") if hdr else ""
                vf += "scale=-2:'min(1080,ih)':flags=lanczos,format=yuv420p"
                base = [ffmpeg, *common, "-ss", f"{a:.3f}", "-i", src, "-t", f"{dur:.3f}", "-map", "0:v:0", *amap,
                        "-vf", vf]
                tail = ["-c:a", "aac", "-b:a", "192k", "-ac", "2", "-movflags", "+faststart", str(out)]
                try:
                    _run(base + ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "21",
                                 "-profile:v", "high"] + tail, prog, dur)
                except RuntimeError:
                    _run(base + ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-profile:v", "high"] + tail,
                         prog, dur)
            (parts if join else files).append(out)
        if join:
            lst = tmp / "list.txt"
            lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
            out = _unique(folder / f"{stem} {len(parts)} clips.mp4")
            _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                  "-c", "copy", "-movflags", "+faststart", str(out)])
            files = [out]
        if gif:
            for f in list(files):
                g = f.with_suffix(".gif")
                if progress:
                    progress(0.0, "Making GIF")
                _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(f), "-vf",
                      "fps=15,scale=480:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];"
                      "[b][p]paletteuse=dither=bayer:bayer_scale=5", "-loop", "0", str(g)])
                files.append(g)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return folder, [str(f) for f in files]


def _stamp(t):
    t = int(t)
    return f"{t // 3600}h{t % 3600 // 60:02d}m{t % 60:02d}s" if t >= 3600 else f"{t // 60}m{t % 60:02d}s"


def _unique(p):
    if not p.exists():
        return p
    i = 2
    while True:
        q = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not q.exists():
            return q
        i += 1


def share(path, expires_in=None):
    """Share a saved clip to Google Drive (private: only people with the link can open it). For the clip result
    screen's Share button. path: one of the files export() returned. expires_in: seconds until the link turns off
    (None = never). Returns the share dict (marquee.share.public): status "queued"/"uploading" with progress coming
    as UI events "share-progress", then "share-ready" {id, link}; or status "ready" with link + qr right away when
    this clip was shared before. Raises marquee.share.ShareError (str() is a message for the user; .reason
    "needs_connect" means Google Drive isn't connected yet). Same as POST /api/share {clip_path}."""
    from . import share as gdrive
    expires = int(time.time() + expires_in) if expires_in else None
    return gdrive.share_file(gdrive.resolve({"clip_path": path})[0], kind="clip", expires=expires)
