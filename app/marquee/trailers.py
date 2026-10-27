"""Trailers for titles that aren't in the library, played by Marquee itself instead of a YouTube embed.

The title page knows a trailer by its YouTube key (TMDB videos, in the details extras). yt-dlp resolves the key to
its streams; the best video up to 1080p (H.264 first, else VP9/AV1) is fetched in ranged chunks and remuxed with the
best audio (AAC first) by ffmpeg into a fragmented MP4, which is served while it grows, so the first frame shows a
moment after the download starts. Once complete it's remuxed again (faststart, seekable) and kept in
%APPDATA%\\Marquee\\cache\\trailers (MARQUEE_HOME-aware), LRU-pruned at about 1.5 GB, so it plays instantly and
offline next time. English captions (manual first, else auto-generated) go alongside as WebVTT, like the previews'.

yt-dlp needs a JavaScript runtime to solve YouTube's stream challenges, without which YouTube throttles the download
to a crawl: the bundled QuickJS (vendor\\quickjs\\qjs.exe, MIT) is used, or deno / node when installed. yt-dlp is
bundled with each Marquee release; YouTube changes break older versions now and then, so releases bump it. When
resolving fails the page falls back to the YouTube embed and the reason is logged.

Routes: GET /api/trailer/<key> -> {available, url, subs}; GET /api/trailer/<key>/file (Range once complete);
GET /api/trailer/<key>/subs.vtt.
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
from flask import Blueprint, Response, jsonify, send_file

from . import settings

bp = Blueprint("marquee_trailers", __name__)

DIR = settings.DIR / "cache" / "trailers"
YTDLP_CACHE = settings.DIR / "cache" / "yt-dlp"
LIMIT = int(1.5 * 2**30)
KEY = re.compile(r"^[A-Za-z0-9_-]{11}$")
FIRST_CHUNK, CHUNK = 1 << 20, 4 << 20
BELOW_NORMAL = 0x00004000

_jobs = {}
_jobs_lock = threading.Lock()


def _final(key):
    return DIR / f"{key}.mp4"


def _vtt(key):
    return DIR / f"{key}.vtt"


def _meta(key):
    return DIR / f"{key}.json"


def _live(key):
    return DIR / f"{key}.live.mp4"


def _ffmpeg():
    try:
        from .index.deps import find_ffmpeg
        return find_ffmpeg()
    except Exception:
        return None


def _qjs():
    roots = [Path(getattr(sys, "_MEIPASS", "")) / "quickjs", Path(__file__).resolve().parents[1] / "vendor" / "quickjs"]
    return next((str(r / "qjs.exe") for r in roots if (r / "qjs.exe").is_file()), None)


def ytdlp_version():
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:
        return None


def _log(*a):
    print("trailer:", *a)


class _Quiet:   # yt-dlp's errors come back as exceptions, logged once by Job.run
    def debug(self, msg):
        pass

    info = warning = error = debug


def _low_priority():
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetThreadPriority(k.GetCurrentThread(), -1)  # THREAD_PRIORITY_BELOW_NORMAL
        except Exception:
            pass


def _ydl_opts():
    runtimes = {"deno": {}, "node": {}}
    q = _qjs()
    if q:
        runtimes["quickjs"] = {"path": q}
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "logger": _Quiet(), "skip_download": True,
            "noplaylist": True, "socket_timeout": 10, "cachedir": str(YTDLP_CACHE), "js_runtimes": runtimes}
    ff = _ffmpeg()
    if ff:
        opts["ffmpeg_location"] = ff
    return opts


def _enable_solver_cache():
    """yt-dlp ships with its preprocessed-player cache off (no rotation); with it on, solving YouTube's challenges
    with QuickJS takes well under a second instead of about six. _prune keeps only the newest few players."""
    try:
        from yt_dlp.extractor.youtube.jsc._builtin import ejs
        ejs.EJSBaseJCP._ENABLE_PREPROCESSED_PLAYER_CACHE = True
    except Exception:
        pass


def _pick(formats):
    """Best video up to 1080p over plain HTTPS (H.264, then VP9, then AV1 at the same height) and the best audio
    (AAC, then Opus)."""
    def vcodec(f):
        c = (f.get("vcodec") or "").lower()
        return 0 if c.startswith("avc1") else 1 if c.startswith(("vp09", "vp9")) else 2 if c.startswith("av01") else 9

    def plain(f):
        return f.get("protocol") in ("https", "http") and f.get("url") and "-drc" not in str(f.get("format_id"))

    vids = [f for f in formats if plain(f) and (f.get("acodec") in (None, "none")) and f.get("height")
            and f["height"] <= 1080 and vcodec(f) < 9]
    auds = [f for f in formats if plain(f) and f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]
    if not vids or not auds:
        return None, None
    v = max(vids, key=lambda f: (f["height"], -vcodec(f), -(f.get("fps") or 30) if (f.get("fps") or 30) > 30 else 0,
                                 f.get("tbr") or 0))
    a = max(auds, key=lambda f: ((f.get("acodec") or "").startswith("mp4a"), f.get("abr") or f.get("tbr") or 0))
    return v, a


def _vtt_time(ms):
    ms = max(0, int(ms))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def _json3_to_vtt(data, auto):
    cues = []
    for e in data.get("events") or []:
        text = "".join(s.get("utf8", "") for s in e.get("segs") or []).strip()
        if not text or "tStartMs" not in e:
            continue
        a = e["tStartMs"]
        cues.append([a, a + (e.get("dDurationMs") or 2000), re.sub(r"[ \t]+", " ", text)])
    cues.sort(key=lambda c: c[0])
    if auto:  # auto captions roll: one line on screen at a time
        for c, n in zip(cues, cues[1:]):
            c[1] = min(c[1], max(c[0] + 300, n[0]))
    esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "WEBVTT\n\n" + "".join(f"{_vtt_time(a)} --> {_vtt_time(b)}\n{esc(t)}\n\n" for a, b, t in cues)


def _fetch_subs(key, info):
    """English captions as WebVTT: the uploader's own first, else YouTube's auto-generated ones."""
    for kind, auto in (("subtitles", False), ("automatic_captions", True)):
        tracks = info.get(kind) or {}
        langs = sorted((l for l in tracks if l.split("-")[0] == "en" and "-orig" not in l or l == "en-orig"),
                       key=lambda l: (l != "en", l == "en-orig", l))
        for lang in langs:
            f = next((f for f in tracks[lang] if f.get("ext") == "json3" and f.get("url")), None)
            if not f:
                continue
            try:
                r = requests.get(f["url"], timeout=8, headers=f.get("http_headers") or {})
                r.raise_for_status()
                body = _json3_to_vtt(r.json(), auto)
            except Exception as e:
                _log(key, "captions failed:", type(e).__name__, e)
                continue
            if body.count("-->"):
                _vtt(key).write_text(body, "utf-8")
                return True
    return False


class Job:
    def __init__(self, key):
        self.key = key
        self.ready = threading.Event()   # resolved (or failed)
        self.muxing = False              # ffmpeg is writing the live file
        self.finished = threading.Event()
        self.error = None
        self.meta = {}
        self.started = time.time()

    def run(self):
        _low_priority()
        try:
            self._run()
        except Exception as e:
            self.error = self.error or f"{type(e).__name__}: {e}"
        finally:
            if self.error:
                _log(self.key, f"failed, using the YouTube player instead (yt-dlp {ytdlp_version()}):", self.error)
            self.muxing = False
            self.ready.set()
            self.finished.set()
            with _jobs_lock:
                if _jobs.get(self.key) is self and not self.error:
                    _jobs.pop(self.key, None)
            _prune()

    def _run(self):
        key = self.key
        ff = _ffmpeg()
        if not ff:
            self.error = "no ffmpeg"
            return
        try:
            import yt_dlp
        except ImportError:
            self.error = "yt-dlp isn't installed"
            return
        _enable_solver_cache()
        DIR.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={key}", download=False)
        v, a = _pick(info.get("formats") or [])
        if not v:
            self.error = "no usable streams"
            return
        self.meta = {"width": v.get("width"), "height": v.get("height"), "vcodec": v.get("vcodec"),
                     "acodec": a.get("acodec"), "title": info.get("title"), "duration": info.get("duration"),
                     "resolved_s": round(time.time() - t0, 2)}
        subs = threading.Thread(target=lambda: self.meta.__setitem__("subs", _fetch_subs(key, info)), daemon=True)
        subs.start()
        # Audio (small, to a file ffmpeg can read whole) and video (piped) are fetched side by side: the first
        # request to YouTube's servers can take a couple of seconds.
        audio, failed = DIR / f"{key}.audio", []
        video = queue.Queue(maxsize=16)

        def get_audio():
            try:
                with open(audio, "wb") as fh:
                    for chunk in _chunks(requests.Session(), a):
                        fh.write(chunk)
            except Exception as e:
                failed.append(e)

        def get_video():
            try:
                for chunk in _chunks(requests.Session(), v):
                    video.put(chunk, timeout=120)
                video.put(None, timeout=120)
            except Exception as e:  # incl. queue.Full: ffmpeg went away
                try:
                    video.put(e, timeout=5)
                except queue.Full:
                    pass

        ta = threading.Thread(target=get_audio, daemon=True)
        ta.start()
        threading.Thread(target=get_video, daemon=True).start()
        ta.join()
        if failed:
            raise failed[0]
        subs.join(10)
        live = _live(key)
        live.unlink(missing_ok=True)
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", "pipe:0", "-i", str(audio),
               "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-flush_packets", "1",
               "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", str(live)]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             creationflags=BELOW_NORMAL)
        self.muxing = True
        self.ready.set()
        try:
            while True:
                chunk = video.get(timeout=120)
                if chunk is None:
                    break
                if isinstance(chunk, Exception):
                    self.error = f"download: {chunk}"
                    break
                p.stdin.write(chunk)
            p.stdin.close()
        except (BrokenPipeError, OSError, queue.Empty):
            pass
        err = p.stderr.read().decode("utf-8", "replace").strip()
        rc = p.wait()
        self.muxing = False
        audio.unlink(missing_ok=True)
        if self.error or rc != 0 or not live.exists() or live.stat().st_size < 100_000:
            self.error = self.error or f"ffmpeg exit {rc}: {err[-300:]}"
            return
        # A seekable copy (moov up front) to keep; the live file goes once nothing reads it any more.
        tmp = DIR / f"{key}.tmp.mp4"
        q = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(live), "-c", "copy",
                            "-movflags", "+faststart", str(tmp)], capture_output=True, timeout=120,
                           creationflags=BELOW_NORMAL)
        if q.returncode != 0 or not tmp.exists():
            shutil.copyfile(live, tmp)
        self.meta["fetched_s"] = round(time.time() - t0, 2)
        self.meta["size"] = tmp.stat().st_size
        _meta(key).write_text(json.dumps(self.meta), "utf-8")
        tmp.replace(_final(key))
        _log(key, f"{self.meta['height']}p {self.meta['vcodec']} cached in {self.meta['fetched_s']} s "
                  f"({self.meta['size'] / 2**20:.1f} MB)")


def _chunks(session, f):
    """A stream in ranged requests (one long request would be throttled), retrying each a few times."""
    size = f.get("filesize") or f.get("filesize_approx")
    headers = f.get("http_headers") or {}
    pos, n = 0, FIRST_CHUNK
    while True:
        end = pos + n - 1
        if size:
            end = min(end, size - 1)
        err = None
        for attempt in range(4):
            try:
                r = session.get(f"{f['url']}&range={pos}-{end}", headers=headers, timeout=(5, 20))
                if r.status_code in (200, 206):
                    break
                err = f"HTTP {r.status_code}"
            except requests.RequestException as e:
                err = str(e)
            time.sleep(0.5 * (attempt + 1))
        else:
            raise IOError(f"download failed at {pos}: {err}")
        data = r.content
        if data:
            yield data
        if not data or (size and pos + len(data) >= size) or (not size and len(data) < end - pos + 1):
            return
        pos += len(data)
        n = CHUNK


def _job(key):
    with _jobs_lock:
        j = _jobs.get(key)
        if j and j.error and time.time() - j.started > 300:   # failed a while ago: try again
            j = None
        if not j:
            j = _jobs[key] = Job(key)
            threading.Thread(target=j.run, name=f"trailer-{key}", daemon=True).start()
        return j


def _touch(*paths):
    now = time.time()
    for p in paths:
        try:
            os.utime(p, (now, now))
        except OSError:
            pass


def _prune():
    """Keep the cache under LIMIT, least recently played first; drop leftovers of downloads that aren't running."""
    try:
        with _jobs_lock:
            busy = {k for k, j in _jobs.items() if not j.finished.is_set()}
        groups = {}
        for f in DIR.iterdir():
            key = f.name[:11]
            if key in busy:
                continue
            if f.name.endswith((".live.mp4", ".tmp.mp4", ".audio")):
                try:
                    f.unlink()
                except OSError:
                    pass   # still being read
                continue
            groups.setdefault(key, []).append(f)
        sized = []
        for key, files in groups.items():
            mp4 = next((f for f in files if f.suffix == ".mp4"), None)
            sized.append((mp4.stat().st_mtime if mp4 else 0, sum(f.stat().st_size for f in files), files))
        total = sum(s for _, s, _ in sized)
        for _, s, files in sorted(sized, key=lambda x: x[0]):
            if total <= LIMIT:
                break
            for f in files:
                f.unlink(missing_ok=True)
            total -= s
    except OSError:
        pass
    try:  # yt-dlp's solver cache: one preprocessed player per YouTube player version, newest few kept
        players = sorted((YTDLP_CACHE / "challenge-solver").glob("player*"), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in players[3:]:
            f.unlink(missing_ok=True)
    except OSError:
        pass


def _read_meta(key):
    try:
        return json.loads(_meta(key).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


@bp.get("/api/trailer/<key>")
def trailer(key):
    """Where the trailer plays from. The first time, this waits for yt-dlp to resolve it (a couple of seconds) and
    starts the download; {available: false} means the page should fall back to the YouTube embed."""
    if not KEY.fullmatch(key):
        return jsonify(ok=True, available=False, reason="bad key")
    subs = f"/api/trailer/{key}/subs.vtt"
    if _final(key).exists():
        threading.Thread(target=_prune, daemon=True).start()   # also clears live files nothing reads any more
        m = _read_meta(key)
        return jsonify(ok=True, available=True, cached=True, url=f"/api/trailer/{key}/file",
                       subs=subs if _vtt(key).exists() else None, width=m.get("width"), height=m.get("height"))
    j = _job(key)
    j.ready.wait(30)
    if j.error or not j.ready.is_set():
        return jsonify(ok=True, available=False, reason=j.error or "timed out", ytdlp=ytdlp_version())
    return jsonify(ok=True, available=True, cached=False, url=f"/api/trailer/{key}/file",
                   subs=subs if _vtt(key).exists() else None, width=j.meta.get("width"), height=j.meta.get("height"))


@bp.get("/api/trailer/<key>/file")
def trailer_file(key):
    if not KEY.fullmatch(key):
        return Response(status=404)
    final = _final(key)
    if not final.exists():
        j = _jobs.get(key)
        if not j or j.error:
            return Response(status=404)
        live = _live(key)
        deadline = time.time() + 30
        while not final.exists() and not (live.exists() and live.stat().st_size) and time.time() < deadline:
            if j.finished.is_set() and (j.error or not j.muxing):
                break
            time.sleep(0.05)
        if not final.exists():
            if j.error or not live.exists():
                return Response(status=404)
            return Response(_follow(live, j), mimetype="video/mp4", direct_passthrough=True,
                            headers={"Cache-Control": "no-store", "Accept-Ranges": "none"})
    _touch(final, _vtt(key), _meta(key))
    r = send_file(final, mimetype="video/mp4", conditional=True, max_age=0)
    r.headers["Cache-Control"] = "private, max-age=3600"
    return r


def _follow(path, job):
    """The live file as ffmpeg writes it, until it's complete."""
    idle = 0.0
    with open(path, "rb") as fh:
        while True:
            data = fh.read(256 * 1024)
            if data:
                idle = 0.0
                yield data
                continue
            if not job.muxing or job.error:   # ffmpeg is done with it: whatever is left, then the end
                rest = fh.read()
                if rest:
                    yield rest
                return
            if idle > 60:
                return
            time.sleep(0.05)
            idle += 0.05


@bp.get("/api/trailer/<key>/subs.vtt")
def trailer_subs(key):
    if not KEY.fullmatch(key) or not _vtt(key).exists():
        return Response(status=404)
    r = Response(_vtt(key).read_text("utf-8"), mimetype="text/vtt")
    r.headers["Cache-Control"] = "private, max-age=3600"
    return r
