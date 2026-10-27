"""Streaming to phones: direct play, remux and transcode.

  direct     the file itself, with HTTP Range (206), when the phone can play its container, codecs and bitrate
  remux      the video copied untouched into fMP4 HLS (audio copied or turned into stereo AAC): cheap, no GPU
  transcode  ffmpeg -> fMP4 HLS on the GPU (NVENC, else Quick Sync / AMF, else x264), scaled to the chosen quality,
             HDR tone-mapped to SDR, image subtitles burned in when picked, audio downmixed to stereo AAC

HLS sessions are seekable like Jellyfin's: the phone gets a complete VOD playlist for the whole title up front. Segment
k has a fixed place on the timeline (every 4 s when transcoding; groups of the file's own keyframes, >= 4 s, when
copying). ffmpeg writes small "units" (one GOP each) with the file's own timestamps (-copyts); a segment is served by
stitching its units together. A request for a segment the encoder isn't about to reach restarts ffmpeg right there
(-ss), so seeking anywhere costs one segment's worth of work. The encoder is paused (process suspended) once it is
LEAD segments ahead of what the phone has asked for, and killed after IDLE_KILL seconds without requests. ffmpeg runs
at below-normal priority, so the PC's own playback always wins.
"""
import bisect
import ctypes
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .. import settings
from . import media

STREAMS = media.CACHE / "streams"
DOWNLOADS = media.CACHE / "downloads"
SEG = 4.0                 # transcode segment length (s)
REMUX_SEG = 4.0           # remux: segments are at least this long (cut on the file's keyframes)
LEAD = 5                  # pause the encoder this many segments ahead of the phone's last request
LOOKAHEAD = 6             # a request this far past the encoder's position waits instead of restarting it
IDLE_KILL = 60            # no requests for this long: stop ffmpeg (the session stays, a new request resumes it)
SESSION_TTL = 30 * 60     # forget an HLS session after this long without requests
DIRECT_TTL = 8 * 3600     # direct-play sessions hold nothing, keep them through long pauses
WAIT = 25                 # longest a segment request waits for the encoder
BELOW_NORMAL = 0x00004000
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

QUALITIES = {
    "1080": {"height": 1080, "bitrate": 8_000_000, "audio": "160k", "label": "1080p"},
    "720": {"height": 720, "bitrate": 4_000_000, "audio": "160k", "label": "720p"},
    "480": {"height": 480, "bitrate": 1_500_000, "audio": "128k", "label": "480p"},
}
QUALITY_ORDER = ["1080", "720", "480"]
ORIGINAL_MAX_BITRATE = 30_000_000
DIRECT_CONTAINERS = {"mp4", "m4v", "mov"}
COPY_AUDIO = {"aac", "ac3", "eac3", "mp3", "opus", "flac"}

_lock = threading.RLock()
sessions = {}             # sid -> Session
_janitor = None


def setting_max():
    try:
        return max(1, int(settings.load().get("mobile_max_streams") or 2))
    except (TypeError, ValueError):
        return 2


# ------------------------------------------------------------------ process helpers
def _suspend(proc, on):
    """Pause/continue a process (NtSuspendProcess); pausing ffmpeg is how the encoder is throttled."""
    try:
        k32, nt = ctypes.windll.kernel32, ctypes.windll.ntdll
        h = k32.OpenProcess(0x0800, False, proc.pid)   # PROCESS_SUSPEND_RESUME
        if not h:
            return False
        try:
            return (nt.NtSuspendProcess(h) if on else nt.NtResumeProcess(h)) == 0
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


_job_handle = None


def _tie_to_us(proc):
    """Put a child process in a Job Object that is closed (and its processes killed) when Atlas exits, so an
    encoder can never outlive the app, even after a crash."""
    global _job_handle
    try:
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE
        if _job_handle is None:
            h = k32.CreateJobObjectW(None, None)

            class _Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class _IO(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

            class _Ext(ctypes.Structure):
                _fields_ = [("Basic", _Basic), ("IoInfo", _IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]
            info = _Ext()
            info.Basic.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            k32.SetInformationJobObject(wintypes.HANDLE(h), 9, ctypes.byref(info), ctypes.sizeof(info))
            _job_handle = h
        ph = k32.OpenProcess(0x0001 | 0x0100, False, proc.pid)   # PROCESS_TERMINATE | PROCESS_SET_QUOTA
        if ph:
            k32.AssignProcessToJobObject(wintypes.HANDLE(_job_handle), wintypes.HANDLE(ph))
            k32.CloseHandle(wintypes.HANDLE(ph))
    except Exception as e:
        print("mobile: couldn't tie ffmpeg to the app:", e)


def _rmtree(p):
    shutil.rmtree(p, ignore_errors=True)


# ------------------------------------------------------------------ encoders (probed once)
_enc = {}
_enc_lock = threading.Lock()


def _try(args):
    try:
        r = subprocess.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", *args, "-f", "null", "-"],
                           capture_output=True, timeout=30, creationflags=NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False


def encoders():
    """{"h264": [names that work], "hevc": [...], "cuda": bool (GPU decode + scale/tonemap filters)}."""
    with _enc_lock:
        if _enc:
            return _enc
        src = ["-f", "lavfi", "-i", "testsrc2=s=640x360:d=0.3"]
        h264 = [e for e in ("h264_nvenc", "h264_qsv", "h264_amf") if _try(src + ["-c:v", e])]
        hevc = [e for e in ("hevc_nvenc", "hevc_qsv", "hevc_amf") if _try(src + ["-c:v", e])]
        h264.append("libx264")
        cuda = "h264_nvenc" in h264 and _try(src + ["-vf", "format=nv12,hwupload_cuda,scale_cuda=w=320:h=180",
                                                   "-c:v", "h264_nvenc"])
        _enc.update(h264=h264, hevc=hevc, cuda=cuda,
                    tonemap_cuda=cuda and "tonemap_cuda" in media.filters(),
                    overlay_cuda=cuda and "overlay_cuda" in media.filters())
        print("mobile encoders:", json.dumps(_enc), flush=True)
        return _enc


# ------------------------------------------------------------------ deciding how to play
def _even(x):
    return max(2, int(round(x / 2)) * 2)


def video_playable(v, caps, why):
    codec = v.get("codec")
    if codec == "h264":
        if not caps.get("h264"):
            return why("this phone doesn't play H.264")
        if v.get("bits", 8) > 8:
            return why("10-bit H.264 isn't supported by phones")
    elif codec == "hevc":
        if not caps.get("hevc"):
            return why("this phone doesn't play HEVC")
        if v.get("bits", 8) > 8 and not caps.get("hevc10"):
            return why("this phone doesn't play 10-bit HEVC")
    elif codec == "av1":
        if not caps.get("av1"):
            return why("this phone doesn't play AV1")
    elif codec == "vp9":
        if not caps.get("vp9"):
            return why("this phone doesn't play VP9")
    else:
        return why(f"{(codec or 'this video').upper()} needs converting")
    if v.get("hdr"):
        if v.get("dv_profile") == 5 or (v.get("dv_profile") and v.get("dv_compat") in (0, None)):
            return why("Dolby Vision only (no HDR10 base layer): tone-mapped")
        if not caps.get("hdr"):
            return why("HDR on an SDR screen: tone-mapped")
    max_h = int(caps.get("max_height") or 2160)
    if (v.get("height") or 0) > max_h + 8:
        return why(f"{v.get('height')}p is more than this phone shows")
    return True


def audio_playable(a, caps):
    return bool(a) and bool(caps.get("audio", {}).get(a.get("codec")))


def pick_audio(info, want=None, prefer_lang=None):
    tracks = info["audio"]
    if not tracks:
        return None
    if want is not None:
        hit = next((a for a in tracks if str(a["index"]) == str(want)), None)
        if hit:
            return hit
    if prefer_lang:
        hit = next((a for a in tracks if (a.get("lang") or "").lower().startswith(prefer_lang[:2].lower())), None)
        if hit:
            return hit
    return next((a for a in tracks if a.get("default")), tracks[0])


def decide(info, caps, quality="auto", audio=None, sub=None, bandwidth=None, force=None, prefer_lang=None,
           keyframes=None):
    """How to play a file on a phone. -> plan dict {mode, quality, height, bitrate, audio, audio_copy, burn, reason}.
    bandwidth: measured bits/s (Auto uses 75% of it). force: "remux" / "transcode" after a direct-play failure."""
    v = info.get("video")
    if not v:
        raise ValueError("This file has no video")
    a = pick_audio(info, audio, prefer_lang)
    reasons = []

    def why(msg):
        reasons.append(msg)
        return False

    burn = sub if (sub and sub.get("kind") == "image") else None
    budget = int(bandwidth * 0.75) if bandwidth else None
    src_rate = info.get("bitrate") or 0
    v_ok = video_playable(v, caps, why)
    a_ok = audio_playable(a, caps)
    container = info.get("container")
    c_ok = container in DIRECT_CONTAINERS or (container in ("mkv", "webm") and caps.get("mkv"))
    if quality in QUALITIES:
        q = QUALITIES[quality]
        small = (v.get("height") or 0) <= q["height"] + 8 and src_rate <= q["bitrate"] * 1.25
        fits = small
        if not small:
            why(f"{q['label']} picked")
    elif quality == "original":
        fits = True
    else:  # auto
        fits = budget is None or src_rate <= budget
        if not fits:
            why(f"{src_rate / 1e6:.0f} Mbps file, connection about {bandwidth / 1e6:.0f} Mbps")
    if burn:
        why("burning in picture subtitles")
    first_audio = not info["audio"] or a is info["audio"][0]
    mode = "transcode"
    if v_ok and fits and not burn and force != "transcode":
        if c_ok and a_ok and first_audio and force != "remux":
            mode = "direct"
        elif keyframes:
            mode = "remux"
            if not c_ok:
                why(f"{container.upper()} container")
            elif not a_ok:
                why(f"{(a or {}).get('codec', '').upper()} audio")
            elif not first_audio:
                why("another audio track picked")
        else:
            why("no keyframe index to copy the video on")
    plan = {"mode": mode, "audio": a["index"] if a else None, "audio_copy": bool(a_ok and a and a["codec"] in COPY_AUDIO
                                                                                   and (a.get("channels") or 2) <= 6),
            "burn": burn["index"] if burn else None, "burn_size": (burn.get("width"), burn.get("height")) if burn else None,
            "reason": "; ".join(reasons), "src_bitrate": src_rate}
    if mode == "transcode":
        if quality in QUALITIES:
            qk = quality
        elif quality == "original":
            qk = None
        else:
            qk = next((k for k in QUALITY_ORDER if budget is None or QUALITIES[k]["bitrate"] <= budget), "480")
        src_h = v.get("height") or 1080
        if qk:
            q = QUALITIES[qk]
            h = min(q["height"], src_h)
            br = q["bitrate"] if h == q["height"] else int(q["bitrate"] * (h / q["height"]) ** 1.5)
            if src_rate:
                br = min(br, max(int(src_rate * 1.1), 1_000_000))
            plan.update(quality=qk, height=h, bitrate=br, abitrate=q["audio"], label=f"{q['label']}")
        else:
            h = min(src_h, 2160)
            br = min(ORIGINAL_MAX_BITRATE, max(int((src_rate or 20_000_000) * 0.8), 8_000_000))
            plan.update(quality="original", height=h, bitrate=br, abitrate="192k", label="Original")
        plan["tonemap"] = bool(v.get("hdr"))
        plan["hevc"] = bool(caps.get("hevc_out")) and bool(encoders()["hevc"])
        plan["audio_copy"] = False
    else:
        plan.update(quality="original", height=v.get("height"), bitrate=src_rate, label="Original")
        if mode == "remux" and not a_ok:
            plan["audio_copy"] = False
    return plan


# ------------------------------------------------------------------ ffmpeg command
def build_command(path, info, plan, start, start_number, out_dir, pipeline, seg_len=SEG):
    v = info["video"]
    out_dir = Path(out_dir)
    args = [media.ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error", "-progress", "pipe:1",
            "-stats_period", "1"]
    transcode = plan["mode"] == "transcode"
    if transcode and pipeline == "cuda":
        args += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-extra_hw_frames", "6"]
    if start > 0.01:
        args += ["-ss", f"{start:.3f}"]
    args += ["-copyts", "-i", path, "-map", f"0:{v['index']}"]
    if plan.get("audio") is not None:
        args += ["-map", f"0:{plan['audio']}"]
    args += ["-map_metadata", "-1", "-map_chapters", "-1", "-sn", "-dn"]
    if transcode:
        h = plan["height"]
        src_w, src_h = v.get("width") or 1920, v.get("height") or 1080
        w = _even(src_w * h / src_h)
        scale = h != src_h
        chain = []
        if pipeline == "cuda":
            if plan.get("tonemap"):
                if scale:
                    chain.append(f"scale_cuda=w={w}:h={h}:interp_algo=bicubic")
                chain.append("tonemap_cuda=format=nv12:p=bt709:t=bt709:m=bt709:tonemap=bt2390:peak=100:desat=0")
            else:
                chain.append(f"scale_cuda=w={w}:h={h}:interp_algo=bicubic:format=nv12")
        else:
            if scale:
                chain.append(f"scale={w}:{h}:flags=bicubic")
            if plan.get("tonemap"):
                if "tonemapx" in media.filters():
                    chain.append("tonemapx=tonemap=bt2390:desat=0:peak=100:t=bt709:m=bt709:p=bt709:format=yuv420p")
                else:
                    chain.append("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                                 "zscale=t=bt709:m=bt709:r=tv")
            chain.append("format=yuv420p")
        vf = ",".join(chain) or "null"
        if plan.get("burn") is not None:
            cw, ch = plan.get("burn_size") or (None, None)
            cw, ch = cw or src_w, ch or src_h
            sh = _even(ch * w / cw)
            y = h - sh
            if pipeline == "cuda":
                graph = (f"[0:{v['index']}]{vf}[v];[0:{plan['burn']}]scale={w}:{sh},format=yuva420p,hwupload_cuda[s];"
                         f"[v][s]overlay_cuda=x=0:y={y}:eof_action=pass[out]")
            else:
                graph = (f"[0:{v['index']}]{vf}[v];[0:{plan['burn']}]scale={w}:{sh}[s];"
                         f"[v][s]overlay=x=0:y={y}:eof_action=pass,format=yuv420p[out]")
            i = args.index("-map")
            args[i:i + 2] = ["-filter_complex", graph, "-map", "[out]"]
        else:
            args += ["-vf", vf]
        br = int(plan["bitrate"])
        enc = plan["encoder"]
        rate = ["-b:v", str(br), "-maxrate", str(int(br * 1.5)), "-bufsize", str(int(br * 2))]
        kf = ["-force_key_frames", f"expr:gte(t,n_forced*{seg_len:g})"]
        if enc.endswith("_nvenc"):
            args += ["-c:v", enc, "-preset", "p4", "-tune", "hq", "-rc", "vbr", *rate, "-g", "100000",
                     "-forced-idr", "1", "-spatial-aq", "1", *kf]
            args += ["-profile:v", "main", "-tag:v", "hvc1"] if enc.startswith("hevc") else ["-profile:v", "high"]
        elif enc.endswith("_qsv"):
            args += ["-c:v", enc, "-preset", "veryfast", *rate, "-g", "100000", "-forced_idr", "1", *kf]
            args += ["-tag:v", "hvc1"] if enc.startswith("hevc") else ["-profile:v", "high"]
        elif enc.endswith("_amf"):
            args += ["-c:v", enc, "-quality", "speed", "-rc", "vbr_peak", *rate, "-g", "100000", "-forced_idr", "1", *kf]
            args += ["-tag:v", "hvc1"] if enc.startswith("hevc") else ["-profile:v", "high"]
        else:
            args += ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", *rate, "-g", "100000",
                     "-sc_threshold", "0", *kf]
        hls_time = f"{seg_len:g}"
    else:
        args += ["-c:v", "copy"]
        if v.get("codec") == "hevc":
            args += ["-tag:v", "hvc1"]
        hls_time = "0.05"   # one unit per GOP: segments are stitched from them on the keyframe table
    if plan.get("audio") is not None:
        if plan.get("audio_copy"):
            args += ["-c:a", "copy"]
        else:
            aac = "libfdk_aac" if "libfdk_aac" in _audio_encoders() else "aac"
            args += ["-c:a", aac, "-ac", "2", "-ar", "48000", "-b:a", plan.get("abitrate") or "160k"]
    args += ["-max_muxing_queue_size", "4096", "-f", "hls", "-hls_time", hls_time, "-hls_segment_type", "fmp4",
             "-hls_playlist_type", "event", "-hls_list_size", "0", "-hls_flags", "temp_file",
             "-hls_segment_options", "movflags=+frag_discont",   # absolute tfdt: segments land where they belong
             "-start_number", str(start_number), "-hls_fmp4_init_filename", "init.mp4",
             "-hls_segment_filename", "u%d.m4s", "index.m3u8"]   # relative: ffmpeg runs in out_dir
    return args


def _audio_encoders():
    if "audio" not in _enc:
        try:
            r = subprocess.run([media.ffmpeg(), "-hide_banner", "-encoders"], capture_output=True, text=True,
                               timeout=15, creationflags=NO_WINDOW)
            _enc["audio"] = {l.split()[1] for l in r.stdout.splitlines() if l.strip().startswith("A")
                             and len(l.split()) > 1}
        except Exception:
            _enc["audio"] = set()
    return _enc["audio"]


def _num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------------------------ jobs (one ffmpeg run)
class Job:
    def __init__(self, session, start_seg, pipeline):
        self.s = session
        self.start_seg = start_seg
        self.pipeline = pipeline
        self.n = session.next_job_no()
        self.dir = session.dir / f"job{self.n}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.units = []          # (unit number, path, seg)
        self.frontier = start_seg - 1   # highest complete segment
        self.done = False
        self.failed = None
        self.suspended = False
        self.progress = {}
        self.started = time.time()
        self.first_unit_at = None
        self.init = None
        self._scales = None
        self.start_time = session.seg_start(start_seg) + (0.002 if session.mode == "remux" and start_seg else 0)
        cmd = build_command(session.path, session.info, session.plan, self.start_time, 0, self.dir, pipeline,
                            session.seg_len)
        self.cmd = cmd
        self.log = open(self.dir / "ffmpeg.log", "wb")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=self.log, stdin=subprocess.DEVNULL,
                                     cwd=str(self.dir), creationflags=NO_WINDOW | BELOW_NORMAL)
        _tie_to_us(self.proc)
        threading.Thread(target=self._read_progress, daemon=True, name="mobile-ffmpeg-progress").start()
        threading.Thread(target=self._watch, daemon=True, name="mobile-ffmpeg-watch").start()

    def alive(self):
        return not self.done and self.proc.poll() is None

    def _read_progress(self):
        cur = {}
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            k, _, val = line.partition("=")
            cur[k] = val
            if k == "progress":
                self.progress = {"fps": _num(cur.get("fps")), "speed": _num((cur.get("speed") or "").rstrip("x")),
                                 "out_time": _num(cur.get("out_time_us")) / 1e6, "frame": int(_num(cur.get("frame")))}
                cur = {}

    def _watch(self):
        pl = self.dir / "index.m3u8"
        seen = 0
        while True:
            exited = self.proc.poll() is not None
            if self.init is None and (self.dir / "init.mp4").exists() and (names_ready := self._listed(pl)):
                try:  # the init segment is complete once ffmpeg has listed a unit after it
                    scales = media.init_info(self.dir / "init.mp4")
                    if scales["scales"] and scales["video"] is not None:
                        self._scales = scales
                        self.init = self.dir / "init.mp4"
                        self.s.init_ready(self.init)
                except Exception:
                    pass
            names = []
            try:
                names = [ln.strip() for ln in pl.read_text(encoding="utf-8", errors="replace").splitlines()
                         if ln.strip().endswith(".m4s")]
            except OSError:
                pass
            if len(names) > seen and self._scales:
                for name in names[seen:]:
                    p = self.dir / name
                    try:
                        t = media.unit_time(p, self._scales)
                    except OSError:
                        t = None
                    seg = self.s.seg_of(t) if t is not None else (self.units[-1][2] if self.units else self.start_seg)
                    if seg < self.start_seg:
                        continue   # ffmpeg started a keyframe early: that GOP belongs to the segment before
                    m = re.search(r"u(\d+)\.m4s$", name)
                    self.units.append((int(m.group(1)) if m else len(self.units), p, seg))
                    if self.first_unit_at is None:
                        self.first_unit_at = time.time()
                seen = len(names)
                # every segment before the newest unit's segment is complete
                if self.units:
                    self.s.units_complete(self, self.units[-1][2] - 1)
            if exited:
                rc = self.proc.returncode
                if self.units:
                    self.s.units_complete(self, self.units[-1][2] if rc == 0 else self.units[-1][2] - 1)
                if rc != 0 and not self.done:
                    try:
                        tail = (self.dir / "ffmpeg.log").read_text(encoding="utf-8", errors="replace")[-600:]
                    except OSError:
                        tail = ""
                    self.failed = tail.strip() or f"ffmpeg exited with {rc}"
                self.done = True
                try:
                    self.log.close()
                except Exception:
                    pass
                self.s.job_ended(self)
                return
            self.s.throttle(self)
            time.sleep(0.12)

    @staticmethod
    def _listed(pl):
        try:
            return ".m4s" in pl.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

    def segment_units(self, seg):
        return [p for _, p, s in self.units if s == seg]

    def pause(self, on):
        if self.alive() and on != self.suspended and _suspend(self.proc, on):
            self.suspended = on

    def kill(self):
        self.done = True
        try:
            if self.suspended:
                _suspend(self.proc, False)
            self.proc.kill()
        except Exception:
            pass


# ------------------------------------------------------------------ sessions
class Session:
    def __init__(self, item, path, info, plan, device, keyframes=None, start=0.0):
        self.id = secrets.token_hex(6)
        self.key = secrets.token_urlsafe(18)
        self.item = item            # {"id", "name", ...} (for Settings and stats)
        self.path = path
        self.info = info
        self.plan = plan
        self.mode = plan["mode"]
        self.device = device.get("id") if device else None
        self.device_name = device.get("name") if device else ""
        self.created = time.time()
        self.last_access = time.time()
        self.last_req = -1
        self.cond = threading.Condition(threading.RLock())
        self.job = None
        self.jobs = 0
        self.ready = {}             # seg -> [unit paths]
        self.init = None
        self.served = 0
        self.first_served_at = None
        self.restarts = 0
        self.errors = []
        self.dir = STREAMS / self.id
        self.seg_len = SEG
        self.kf = keyframes or [0.0]
        if self.mode == "remux":
            self.starts = self._group(self.kf, REMUX_SEG)
            self.seg_len = REMUX_SEG
        elif self.mode == "transcode":
            self.starts = [i * SEG for i in range(max(1, math.ceil((info["duration"] or SEG) / SEG)))]
        else:
            self.starts = []
        self.start_hint = (max(0, bisect.bisect_right(self.starts, start) - 1) if self.starts else 0)
        self.last_req = self.start_hint - 1

    @staticmethod
    def _group(kf, target):
        starts = [0.0]
        for t in kf:
            if t >= starts[-1] + target:
                starts.append(t)
        return starts

    def next_job_no(self):
        self.jobs += 1
        return self.jobs

    # timeline
    def seg_start(self, k):
        return self.starts[k] if 0 <= k < len(self.starts) else 0.0

    def seg_of(self, t):
        """The segment a unit belongs to, from the decode time of its first video frame."""
        if self.mode == "transcode":
            return max(0, min(len(self.starts) - 1, int(round(t / SEG))))
        # remux: a unit starts on a keyframe; the time read back from it is within a few frames of the keyframe's
        # time in the source (timestamps get rebuilt for B-frames, and shifted at the very start of a file)
        i = bisect.bisect_left(self.kf, t)
        near = [self.kf[j] for j in (i - 1, i) if 0 <= j < len(self.kf)]
        kf = min(near, key=lambda k: abs(k - t)) if near else t
        return max(0, bisect.bisect_right(self.starts, kf + 0.001) - 1)

    def playlist(self, base=""):
        dur = self.info["duration"]
        lens = [(self.starts[i + 1] if i + 1 < len(self.starts) else dur) - s for i, s in enumerate(self.starts)]
        lens = [max(0.1, x) for x in lens]
        out = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{math.ceil(max(lens))}",
               "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-INDEPENDENT-SEGMENTS",
               f'#EXT-X-MAP:URI="{base}init.mp4"']
        for i, d in enumerate(lens):
            out += [f"#EXTINF:{d:.6f},", f"{base}seg/{i}.m4s"]
        out.append("#EXT-X-ENDLIST")
        return "\n".join(out) + "\n"

    # jobs
    def _pipelines(self):
        enc = encoders()
        if self.mode != "transcode":
            return ["copy"]
        out = []
        cuda_ok = enc["cuda"] and (not self.plan.get("tonemap") or enc["tonemap_cuda"]) and (
            self.plan.get("burn") is None or enc["overlay_cuda"])
        if cuda_ok and (self.plan["encoder"].endswith("_nvenc")):
            out.append("cuda")
        out.append("sw")
        return out

    def _start_job(self, seg, pipeline=None):
        if self.job:
            self.job.kill()
            self.restarts += 1
        pipes = self._pipelines()
        pipeline = pipeline or pipes[0]
        STREAMS.mkdir(parents=True, exist_ok=True)
        self.job = Job(self, seg, pipeline)
        return self.job

    def init_ready(self, path):
        with self.cond:
            if self.init is None:
                self.init = path
            self.cond.notify_all()

    def units_complete(self, job, upto):
        with self.cond:
            if job is not self.job and job.done:
                pass
            for seg in range(max(job.start_seg, job.frontier + 1), upto + 1):
                units = job.segment_units(seg)
                if units and seg not in self.ready:
                    self.ready[seg] = units
            job.frontier = max(job.frontier, upto)
            self.cond.notify_all()

    def job_ended(self, job):
        with self.cond:
            if job.failed and job is self.job:
                pipes = self._pipelines()
                nxt = pipes[pipes.index(job.pipeline) + 1] if job.pipeline in pipes and \
                    pipes.index(job.pipeline) + 1 < len(pipes) else None
                if not job.units and nxt:   # e.g. the GPU couldn't decode this file: try the next way
                    print(f"mobile: {job.pipeline} failed ({job.failed[-200:]!r}), trying {nxt}", flush=True)
                    self.errors.append(job.failed[-300:])
                    self.job = None
                    self._start_job(job.start_seg, nxt)
                    self.plan["pipeline"] = nxt
                else:
                    self.errors.append(job.failed[-300:])
            self.cond.notify_all()

    def throttle(self, job):
        if job is not self.job:
            return
        ahead = job.frontier - self.last_req
        if not job.suspended and ahead > LEAD:
            job.pause(True)
        elif job.suspended and ahead <= LEAD - 1:
            job.pause(False)

    def ensure(self, seg):
        """Make sure something is producing `seg` (restart the encoder there if needed)."""
        j = self.job
        if j and not j.done and j.start_seg <= seg <= j.frontier + 1 + LOOKAHEAD:
            if j.suspended and seg > j.frontier - 1:
                j.pause(False)
            return
        if j and j.done and not j.failed and j.start_seg <= seg <= j.frontier:
            return
        self._start_job(seg)

    def get_init(self):
        self.last_access = time.time()
        deadline = time.time() + WAIT
        with self.cond:
            if self.init is None and (self.job is None or self.job.done):
                self.ensure(self.start_hint)
            while self.init is None:
                j = self.job   # after a failure job_ended() may already have started the next way
                if j and j.done and j.failed and not j.units:
                    raise RuntimeError("The stream couldn't start: " + (j.failed or "")[-200:])
                left = deadline - time.time()
                if left <= 0:
                    raise TimeoutError("The stream took too long to start")
                self.cond.wait(min(left, 0.5))
            return self.init

    def get_segment(self, seg):
        """Unit paths for a segment, waiting (and starting/restarting the encoder) as needed."""
        if not 0 <= seg < len(self.starts):
            raise KeyError(seg)
        self.last_access = time.time()
        deadline = time.time() + WAIT
        with self.cond:
            self.last_req = max(seg, self.last_req) if seg >= self.last_req - 2 else seg
            if seg in self.ready:
                self._prefetch(seg)
                return self.ready[seg]
            self.ensure(seg)
            while seg not in self.ready:
                j = self.job
                if j and j.done and (j.failed or j.frontier < seg):
                    if j.failed and not j.units:
                        raise RuntimeError("The stream stopped: " + (j.failed or "")[-200:])
                    self._start_job(seg)
                left = deadline - time.time()
                if left <= 0:
                    raise TimeoutError("The stream took too long")
                self.cond.wait(min(left, 0.5))
            if self.first_served_at is None:
                self.first_served_at = time.time()
            self.served += 1
            return self.ready[seg]

    def _prefetch(self, seg):
        """Already had it: keep the encoder going for what comes next."""
        nxt = seg + 1
        if nxt < len(self.starts) and nxt not in self.ready:
            j = self.job
            if not j or j.done or not (j.start_seg <= nxt <= j.frontier + 1 + LOOKAHEAD):
                self._start_job(nxt)
            elif j.suspended and j.frontier - self.last_req <= LEAD - 1:
                j.pause(False)

    def tidy(self):
        """Drop segments far behind the playhead (disk), stop an idle encoder."""
        now = time.time()
        with self.cond:
            if self.job and not self.job.done and now - self.last_access > IDLE_KILL:
                print(f"mobile: stream {self.id} idle, stopping ffmpeg", flush=True)
                self.job.kill()
            old = [k for k in self.ready if k < self.last_req - 30 or k > self.last_req + 400]
            for k in old:
                for p in self.ready.pop(k):
                    try:
                        p.unlink()
                    except OSError:
                        pass

    def stop(self):
        with self.cond:
            if self.job:
                self.job.kill()
            self.ready.clear()
        threading.Timer(1.5, _rmtree, args=(self.dir,)).start()

    def idle_for(self):
        return time.time() - self.last_access

    def active(self):
        """Counts toward the concurrent-stream limit."""
        return self.mode != "direct" and ((self.job and not self.job.done) or self.idle_for() < IDLE_KILL)

    def stats(self):
        j = self.job
        return {"id": self.id, "mode": self.mode, "item": self.item, "device": self.device_name,
                "quality": self.plan.get("label"), "encoder": self.plan.get("encoder"),
                "pipeline": (j.pipeline if j else self.plan.get("pipeline")), "reason": self.plan.get("reason"),
                "segments": len(self.starts), "ready": len(self.ready), "last_request": self.last_req,
                "running": bool(j and not j.done), "suspended": bool(j and j.suspended),
                "frontier": j.frontier if j else None, "progress": j.progress if j else {},
                "restarts": self.restarts, "served": self.served, "idle": round(self.idle_for(), 1),
                "first_unit_after": round(j.first_unit_at - j.started, 3) if j and j.first_unit_at else None,
                "pid": j.proc.pid if j else None, "errors": self.errors[-2:]}


def create(item, path, info, plan, device, keyframes=None, start=0.0):
    """A new session. Stops the device's previous ones. Raises OverflowError past the stream limit."""
    with _lock:
        _ensure_janitor()
        dev_id = device.get("id") if device else None
        for s in [s for s in sessions.values() if dev_id and s.device == dev_id]:
            _drop(s)
        if plan["mode"] != "direct":
            busy = [s for s in sessions.values() if s.active()]
            if len(busy) >= setting_max():
                raise OverflowError(f"{len(busy)} phones are already streaming from this PC (the limit is "
                                    f"{setting_max()}; change it in Settings > Atlas Mobile)")
            if plan["mode"] == "transcode":
                enc = encoders()
                plan["encoder"] = (enc["hevc"][0] if plan.get("hevc") and enc["hevc"] else enc["h264"][0])
        s = Session(item, path, info, plan, device, keyframes, start)
        sessions[s.id] = s
        if s.mode != "direct":
            s.dir.mkdir(parents=True, exist_ok=True)
            with s.cond:
                s.plan["pipeline"] = s._pipelines()[0]
                s.ensure(s.start_hint)   # warm start: the first segment is on its way before the player asks
        return s


def get(sid, key=None):
    s = sessions.get(sid)
    if not s or (key is not None and not secrets.compare_digest(s.key, key)):
        return None
    return s


def stop(sid):
    with _lock:
        s = sessions.get(sid)
        if s:
            _drop(s)
        return bool(s)


def stop_device(dev_id):
    with _lock:
        for s in [s for s in sessions.values() if s.device == dev_id]:
            _drop(s)


def _drop(s):
    sessions.pop(s.id, None)
    s.stop()


def stop_all():
    with _lock:
        for s in list(sessions.values()):
            _drop(s)


def active():
    return [s.stats() for s in sessions.values()]


def _ensure_janitor():
    global _janitor
    if _janitor and _janitor.is_alive():
        return
    if STREAMS.exists():  # left behind by a crash
        for d in STREAMS.iterdir():
            if d.name not in sessions:
                _rmtree(d)
    _janitor = threading.Thread(target=_janitor_loop, daemon=True, name="mobile-janitor")
    _janitor.start()


def _janitor_loop():
    while True:
        time.sleep(5)
        try:
            with _lock:
                for s in list(sessions.values()):
                    ttl = DIRECT_TTL if s.mode == "direct" else SESSION_TTL
                    if s.idle_for() > ttl:
                        _drop(s)
                    elif s.mode != "direct":
                        s.tidy()
            downloads.tick()
        except Exception as e:
            print("mobile janitor:", type(e).__name__, e)


# ------------------------------------------------------------------ downloads for offline
class Downloads:
    """"Download to phone": a 720p MP4 made in the background (one at a time, low priority), then fetched by the
    phone like any download. Kept for 3 days."""
    KEEP = 3 * 86400
    FILE = DOWNLOADS / "downloads.json"

    def __init__(self):
        self.lock = threading.RLock()
        self.items = None
        self.proc = None
        self.cur = None

    def _load(self):
        if self.items is None:
            try:
                self.items = json.loads(self.FILE.read_text(encoding="utf-8"))
            except Exception:
                self.items = {}
            for d in self.items.values():  # interrupted by a restart: queue again
                if d.get("state") == "working":
                    d["state"] = "queued"
        return self.items

    def _save(self):
        DOWNLOADS.mkdir(parents=True, exist_ok=True)
        tmp = self.FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, indent=1), encoding="utf-8")
        tmp.replace(self.FILE)

    def add(self, item, path, info, user_key, audio=None, quality="720"):
        with self.lock:
            items = self._load()
            for d in items.values():
                if d["item_id"] == item["id"] and d["user"] == user_key and d["state"] != "error" and \
                        d.get("quality") == quality:
                    return d
            did = secrets.token_hex(6)
            q = QUALITIES.get(quality, QUALITIES["720"])
            d = {"id": did, "item_id": item["id"], "title": item.get("title") or item.get("name"), "user": user_key,
                 "path": path, "audio": audio, "quality": quality, "label": q["label"], "state": "queued",
                 "progress": 0, "size": 0, "created": time.time(), "duration": info.get("duration") or 0,
                 "poster": item.get("poster")}
            items[did] = d
            self._save()
        self.tick()
        return d

    def list(self, user_key):
        with self.lock:
            return sorted([dict(d, path=None) for d in self._load().values() if d["user"] == user_key],
                          key=lambda d: -d["created"])

    def get(self, did):
        with self.lock:
            d = self._load().get(did)
            return dict(d) if d else None

    def file(self, did):
        return DOWNLOADS / f"{did}.mp4"

    def remove(self, did, user_key):
        with self.lock:
            d = self._load().get(did)
            if not d or d["user"] != user_key:
                return False
            if self.cur == did and self.proc:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.items.pop(did, None)
            self._save()
        for p in (self.file(did), self.file(did).with_suffix(".part.mp4")):
            try:
                p.unlink()
            except OSError:
                pass
        return True

    def tick(self):
        with self.lock:
            items = self._load()
            now = time.time()
            for did in [k for k, d in items.items() if now - d["created"] > self.KEEP]:
                self.items.pop(did, None)
                try:
                    self.file(did).unlink()
                except OSError:
                    pass
            if self.proc and self.proc.poll() is None:
                return
            nxt = next((d for d in sorted(items.values(), key=lambda d: d["created"]) if d["state"] == "queued"), None)
            if not nxt:
                return
            nxt["state"] = "working"
            self.cur = nxt["id"]
            self._save()
        threading.Thread(target=self._run, args=(nxt["id"],), daemon=True, name="mobile-download").start()

    def _run(self, did):
        d = self.get(did)
        try:
            info = media.probe(d["path"])["info"]
            caps = {"h264": True, "audio": {"aac": True}}
            plan = decide(info, caps, quality=d["quality"], audio=d.get("audio"), force="transcode")
            enc = encoders()
            plan["encoder"] = enc["h264"][0]
            plan["hevc"] = False
            pipe = "cuda" if enc["cuda"] and (not plan.get("tonemap") or enc["tonemap_cuda"]) and \
                plan["encoder"].endswith("_nvenc") else "sw"
            DOWNLOADS.mkdir(parents=True, exist_ok=True)
            part = self.file(did).with_suffix(".part.mp4")
            args = build_command(d["path"], info, plan, 0, 0, DOWNLOADS, pipe)
            # same pipeline as streaming, but one progressive MP4 instead of HLS
            cut = args.index("-max_muxing_queue_size")
            args = args[:cut] + ["-max_muxing_queue_size", "4096", "-movflags", "+faststart", "-y", str(part)]
            args = [a for a in args if a != "-copyts"]
            with open(DOWNLOADS / f"{did}.log", "wb") as log:
                self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=log, stdin=subprocess.DEVNULL,
                                             creationflags=NO_WINDOW | BELOW_NORMAL)
                _tie_to_us(self.proc)
                dur = info.get("duration") or 1
                for raw in self.proc.stdout:
                    k, _, v = raw.decode("utf-8", "replace").strip().partition("=")
                    if k == "out_time_us" and v.isdigit():
                        with self.lock:
                            if did in self.items:
                                self.items[did]["progress"] = min(0.999, int(v) / 1e6 / dur)
                rc = self.proc.wait()
            with self.lock:
                if did not in self.items:
                    return
                if rc == 0 and part.exists():
                    part.replace(self.file(did))
                    self.items[did].update(state="ready", progress=1, size=self.file(did).stat().st_size)
                else:
                    self.items[did].update(state="error", message="Couldn't make the file")
                self._save()
        except Exception as e:
            with self.lock:
                if did in self._load():
                    self.items[did].update(state="error", message=str(e)[:200])
                    self._save()
        finally:
            self.proc = None
            self.cur = None
            try:
                (DOWNLOADS / f"{did}.log").unlink()
            except OSError:
                pass
            self.tick()


downloads = Downloads()
