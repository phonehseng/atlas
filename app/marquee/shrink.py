"""Shrink watched 4K films to 1080p ("Shrink 4K films after watching" in Settings > Storage).

Once you've watched a 4K film (and nobody else is partway through it), it's re-encoded on this PC with the
NVIDIA encoder to 1080p HEVC, 10-bit with the HDR10 colours kept. Audio, subtitles, chapters and attachments are
copied untouched. The new file is written next to the old one and checked (it plays to the same length and is
smaller), then:
  * the 4K file goes to the Recycle Bin (never a hard delete: the space comes back when the bin is emptied),
  * Radarr stops monitoring the film, so it doesn't "upgrade" it back to 4K, and rescans it,
  * Jellyfin is told about the new file.
One film at a time, and only while nothing is playing: if playback starts, the encode stops and starts over later.
Dolby Vision profile 5 files (no HDR10 base) are skipped: without the Dolby Vision layer their colours would be
wrong. State: shrink.json next to the settings.
"""
import json
import os
import re
import subprocess
import threading
import time

from . import settings
from . import storage

STATE = settings.DIR / "shrink.json"
CHECK_EVERY = 30 * 60
_lock = threading.Lock()
_job = None


def _load():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"done": {}, "skipped": {}}


def _save(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)


def _tools():
    from .details import _ffmpeg
    ff = _ffmpeg()
    if not ff:
        return None, None
    probe = os.path.join(os.path.dirname(ff), "ffprobe.exe" if ff.lower().endswith(".exe") else "ffprobe")
    return ff, (probe if os.path.exists(probe) else None)


def probe(path):
    """The main video stream: width, height, pix_fmt, colours, Dolby Vision profile, and the duration."""
    _, fp = _tools()
    if not fp:
        raise RuntimeError("ffprobe isn't available")
    p = subprocess.run([fp, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,pix_fmt,color_primaries,color_transfer,color_space:"
                        "stream_side_data=dv_profile:format=duration", "-of", "json", path],
                       capture_output=True, timeout=120, creationflags=0x08000000)
    j = json.loads(p.stdout.decode("utf-8", "replace") or "{}")
    v = (j.get("streams") or [{}])[0]
    dv = next((sd.get("dv_profile") for sd in v.get("side_data_list") or [] if "dv_profile" in sd), None)
    return {"w": v.get("width") or 0, "h": v.get("height") or 0, "pix_fmt": v.get("pix_fmt") or "",
            "primaries": v.get("color_primaries"), "transfer": v.get("color_transfer"), "space": v.get("color_space"),
            "dv": dv, "duration": float((j.get("format") or {}).get("duration") or 0)}


def new_name(path):
    """The 1080p file's name: the quality tag swapped (Radarr reads it), else "[1080p]" added."""
    folder, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    out = re.sub(r"(?i)\b(?:remux|bluray|web-?dl|webrip|hdtv|uhd)?[ -]?2160p\b", "1080p", stem)
    out = re.sub(r"(?i)\b(?:4k|uhd)\b", "", out)
    out = re.sub(r"(?i)\bDV\b[ ]?", "", out).replace("[]", "").replace("  ", " ").strip()
    if out == stem or "1080p" not in out:
        out = f"{out} [1080p]"
    return os.path.join(folder, out + ".mkv")


def candidates(app):
    """Watched 4K films, oldest-watched first: [{movie, path, size, watched}]."""
    lib = storage.library(app)
    view = storage.viewing(app)
    st = _load()
    me = view.get("me") or "me"
    out = []
    for m in lib.get("movies") or []:
        f = m.get("movieFile") or {}
        p = f.get("path")
        if not p or p in st["done"] or p in st["skipped"] or not os.path.exists(p):
            continue
        res = ((f.get("quality") or {}).get("quality") or {}).get("resolution") or 0
        width = ((f.get("mediaInfo") or {}).get("resolution") or "").split("x")[0]
        if res < 2160 and not (width.isdigit() and int(width) >= 3000):
            continue
        users = view["by_path"].get(storage.pathkey(p)) or {}
        mine = users.get(me) or {}
        if not mine.get("played"):
            continue
        if any(u != me and not s.get("played") and s.get("pos", 0) > 60 for u, s in users.items()):
            continue   # someone else is partway through it
        out.append({"movie": m, "path": p, "size": f.get("size") or os.path.getsize(p), "watched": mine.get("last") or 0})
    return sorted(out, key=lambda c: c["watched"])


def _encode_cmd(ff, src, dst, info, cuda=True):
    hdr = info["transfer"] in ("smpte2084", "arib-std-b67") or "10" in info["pix_fmt"]
    colours = []
    for opt, key in (("-color_primaries", "primaries"), ("-color_trc", "transfer"), ("-colorspace", "space")):
        if info.get(key) and info[key] != "unknown":
            colours += [opt, info[key]]
    head = [ff, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    if cuda:
        head += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        vf = f"scale_cuda=w=1920:h=-2:format={'p010le' if hdr else 'nv12'}"
    else:
        vf = f"scale=1920:-2:flags=lanczos,format={'p010le' if hdr else 'yuv420p'}"
    return head + ["-i", src, "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?",
                   "-map_chapters", "0", "-map_metadata", "0", "-vf", vf,
                   "-c:v", "hevc_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "20", "-b:v", "0",
                   "-maxrate", "25M", "-bufsize", "50M", "-spatial-aq", "1", "-profile:v", "main10" if hdr else "main",
                   *colours, "-c:a", "copy", "-c:s", "copy", "-c:t", "copy", "-max_muxing_queue_size", "4096",
                   "-progress", "pipe:1", "-nostats", dst]


class Busy(Exception):
    """Playback started: stop and try again later."""


def _run(cmd, duration, playing, progress):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=0x08000000)
    err = []
    threading.Thread(target=lambda: err.extend(p.stderr.read().decode("utf-8", "replace").splitlines()[-8:]),
                     daemon=True).start()
    try:
        for raw in p.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("out_time_us=") and duration:
                try:
                    progress(min(1.0, int(line.split("=", 1)[1]) / 1e6 / duration))
                except ValueError:
                    pass
            if playing():
                p.kill()
                p.wait()
                raise Busy()
        p.wait()
    finally:
        if p.poll() is None:
            p.kill()
    if p.returncode != 0:
        raise RuntimeError("ffmpeg: " + " | ".join(err[-3:]) if err else f"ffmpeg exited with {p.returncode}")


def shrink(app, c, progress=lambda f: None):
    """One film: encode, check, swap. Returns a summary; raises Busy if playback started."""
    src = c["path"]
    ff, _ = _tools()
    if not ff:
        raise RuntimeError("ffmpeg isn't available")
    info = probe(src)
    if info["dv"] == 5:
        raise ValueError("Dolby Vision profile 5 (no HDR10 picture to keep)")
    if info["w"] and info["w"] < 3000:
        raise ValueError("not 4K")
    dst = new_name(src)
    tmp = os.path.splitext(dst)[0] + ".shrinking.mkv"
    free = storage._free_space(os.path.dirname(src))
    if free is not None and free < c["size"] * 0.4 + 5e9:
        raise RuntimeError("not enough free space for the 1080p copy")
    playing = lambda: bool(getattr(app.player, "playing", False))  # noqa: E731
    try:
        try:
            _run(_encode_cmd(ff, src, tmp, info, cuda=True), info["duration"], playing, progress)
        except RuntimeError:
            _run(_encode_cmd(ff, src, tmp, info, cuda=False), info["duration"], playing, progress)
        out = probe(tmp)
        size = os.path.getsize(tmp)
        if abs(out["duration"] - info["duration"]) > 3 or out["w"] > 1920 or size >= c["size"]:
            raise RuntimeError(f"the new file didn't check out ({out['duration']:.0f}s, {out['w']}px, {size} bytes)")
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)  # our own half-written copy, never the film
        raise
    m, arr = c["movie"], c["movie"].get("_arr")
    if arr is not None:  # no "upgrade" back to 4K
        try:
            body = arr.get(f"/movie/{m['id']}")
            body["monitored"] = False
            arr.send("PUT", f"/movie/{m['id']}", body)
        except Exception as e:
            print("shrink: couldn't unmonitor in Radarr:", type(e).__name__, e)
    os.replace(tmp, dst)
    gone = storage.recycle([src]) if os.path.normcase(dst) != os.path.normcase(src) else [src]
    if not gone:
        print("shrink: the 4K file couldn't go to the Recycle Bin; both copies are kept:", src)
    if arr is not None:
        try:
            arr.send("POST", "/command", {"name": "RescanMovie", "movieId": m["id"]})
        except Exception as e:
            print("shrink: Radarr rescan failed:", type(e).__name__, e)
    try:
        app.jf.call("POST", "/Library/Media/Updated", json={"Updates": [
            {"Path": dst, "UpdateType": "Created"}, {"Path": src, "UpdateType": "Deleted"}]})
    except Exception as e:
        print("shrink: Jellyfin update failed:", type(e).__name__, e)
    storage.forget()
    return {"title": m.get("title"), "year": m.get("year"), "from": src, "to": dst, "before": c["size"],
            "after": os.path.getsize(dst), "recycled": bool(gone), "at": time.time()}


class Job:
    def __init__(self, app):
        self.app = app
        self.wake = threading.Event()
        self.current = None   # {"title", "progress"} while encoding
        self.thread = threading.Thread(target=self.loop, name="shrink", daemon=True)

    def loop(self):
        time.sleep(180)
        while True:
            try:
                if settings.load().get("shrink_4k_watched") and not getattr(self.app.player, "playing", False):
                    self.step()
            except Exception as e:
                print("shrink job failed:", type(e).__name__, e, flush=True)
            self.wake.wait(CHECK_EVERY)
            self.wake.clear()

    def step(self):
        if not storage.deletes_allowed():  # development runs point at the real library: hands off
            return
        for c in candidates(self.app):
            if getattr(self.app.player, "playing", False) or not settings.load().get("shrink_4k_watched"):
                return
            title = c["movie"].get("title") or os.path.basename(c["path"])
            self.current = {"title": title, "progress": 0.0}
            print(f"shrink: {title} to 1080p", flush=True)
            try:
                r = shrink(self.app, c, lambda f: self.current.__setitem__("progress", round(f, 3)))
            except Busy:
                print("shrink: playback started, trying again later", flush=True)
                return
            except (ValueError, RuntimeError, OSError) as e:
                with _lock:
                    st = _load()
                    st["skipped"][c["path"]] = {"title": title, "why": str(e), "at": time.time()}
                    _save(st)
                print(f"shrink: skipped {title}: {e}", flush=True)
                continue
            finally:
                self.current = None
            with _lock:
                st = _load()
                st["done"][r["to"]] = r
                _save(st)
            saved = r["before"] - r["after"]
            print(f"shrink: {title} {storage.fmt_size(r['before'])} -> {storage.fmt_size(r['after'])}", flush=True)
            try:
                self.app.notify_ui("storage-shrunk", {"title": title, "saved": saved,
                                                      "message": f"{title} is now 1080p: {storage.fmt_size(saved)} freed"
                                                      + ("" if r["recycled"] else " (the 4K file is still there)")
                                                      + (" once the Recycle Bin is emptied" if r["recycled"] else "")})
            except Exception:
                pass


def start(app):
    global _job
    if _job is None:
        _job = Job(app)
        _job.thread.start()
    return _job


def kick():
    if _job is not None:
        _job.wake.set()


def status():
    st = _load()
    done = sorted(st["done"].values(), key=lambda r: -r.get("at", 0))
    return {"current": _job.current if _job else None, "saved": sum(r["before"] - r["after"] for r in done),
            "done": done[:10], "skipped": list(st["skipped"].values())[:10]}
