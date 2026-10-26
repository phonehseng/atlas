"""What's inside a video file, read fast and cached: streams (ffprobe), keyframe times (from the Matroska cue index or
the MP4 sync-sample table, so nothing reads the whole file), sidecar subtitles, and small fMP4 box helpers used to
stitch HLS segments together.
"""
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path

from .. import settings

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BELOW_NORMAL = 0x00004000
CACHE = settings.DIR / "cache" / "mobile"
SUBS_DIR = CACHE / "subs"
TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "ttml"}
IMAGE_SUBS = {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub", "dvb_subtitle", "xsub"}
SIDE_EXT = {".srt", ".vtt", ".ass", ".ssa"}
LANGS = {"eng": "English", "en": "English", "jpn": "Japanese", "ja": "Japanese", "spa": "Spanish", "es": "Spanish",
         "fre": "French", "fra": "French", "fr": "French", "ger": "German", "deu": "German", "de": "German",
         "ita": "Italian", "it": "Italian", "por": "Portuguese", "pt": "Portuguese", "rus": "Russian", "ru": "Russian",
         "chi": "Chinese", "zho": "Chinese", "zh": "Chinese", "kor": "Korean", "ko": "Korean", "ara": "Arabic",
         "hin": "Hindi", "dut": "Dutch", "nld": "Dutch", "swe": "Swedish", "nor": "Norwegian", "dan": "Danish",
         "fin": "Finnish", "pol": "Polish", "tur": "Turkish", "gre": "Greek", "ell": "Greek", "heb": "Hebrew",
         "tha": "Thai", "vie": "Vietnamese", "ind": "Indonesian", "may": "Malay", "msa": "Malay", "ukr": "Ukrainian",
         "cze": "Czech", "ces": "Czech", "hun": "Hungarian", "rum": "Romanian", "ron": "Romanian"}
AUDIO_NAMES = {"aac": "AAC", "ac3": "Dolby Digital", "eac3": "Dolby Digital+", "truehd": "TrueHD", "dts": "DTS",
               "flac": "FLAC", "opus": "Opus", "mp3": "MP3", "vorbis": "Vorbis", "pcm_s16le": "PCM", "pcm_s24le": "PCM",
               "alac": "ALAC", "mp2": "MP2"}

_probe_cache = {}
_kf_cache = {}
_lock = threading.Lock()


# ------------------------------------------------------------------ ffmpeg
_ff = {}


def ffmpeg():
    if "ffmpeg" not in _ff:
        path = None
        try:
            from ..index.deps import find_ffmpeg
            path = find_ffmpeg()
        except Exception:
            path = None
        path = path or (r"C:\Program Files\Jellyfin\Server\ffmpeg.exe"
                        if os.path.isfile(r"C:\Program Files\Jellyfin\Server\ffmpeg.exe") else shutil.which("ffmpeg"))
        _ff["ffmpeg"] = path
    return _ff["ffmpeg"]


def ffprobe():
    if "ffprobe" not in _ff:
        ff = ffmpeg()
        cand = os.path.join(os.path.dirname(ff), "ffprobe.exe") if ff else None
        _ff["ffprobe"] = cand if cand and os.path.isfile(cand) else shutil.which("ffprobe")
    return _ff["ffprobe"]


def filters():
    """Names of the filters this ffmpeg build has (cached)."""
    if "filters" not in _ff:
        names = set()
        try:
            r = subprocess.run([ffmpeg(), "-hide_banner", "-filters"], capture_output=True, text=True, timeout=15,
                               creationflags=NO_WINDOW)
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 3 and re.fullmatch(r"[TSC.]{2,3}", parts[0]):
                    names.add(parts[1])
        except Exception:
            pass
        _ff["filters"] = names
    return _ff["filters"]


def _key(path):
    st = os.stat(path)
    return f"{os.path.normcase(path)}|{st.st_size}|{int(st.st_mtime)}"


def _disk(kind, key):
    return CACHE / kind / (hashlib.sha1(key.encode("utf-8", "surrogatepass")).hexdigest()[:24] + ".json")


# ------------------------------------------------------------------ probe
def probe(path):
    """ffprobe's streams and format for a file (memory + disk cached), plus a summary under "info"."""
    key = _key(path)
    hit = _probe_cache.get(key)
    if hit:
        return hit
    f = _disk("probe", key)
    data = None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    if data is None:
        r = subprocess.run([ffprobe(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                            "-show_chapters", path], capture_output=True, timeout=60, creationflags=NO_WINDOW)
        data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
        if not data.get("streams"):
            raise RuntimeError("Couldn't read this file")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data), encoding="utf-8")
    data["info"] = summarize(data, path)
    with _lock:
        if len(_probe_cache) > 200:
            _probe_cache.clear()
        _probe_cache[key] = data
    return data


def _lang(s):
    return (s.get("tags") or {}).get("language") or ""


def _title(s):
    return (s.get("tags") or {}).get("title") or ""


def summarize(data, path):
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    dur = float(fmt.get("duration") or 0) or float((v or {}).get("duration") or 0)
    size = int(fmt.get("size") or 0) or (os.path.getsize(path) if os.path.exists(path) else 0)
    bitrate = int(fmt.get("bit_rate") or 0) or (int(size * 8 / dur) if dur else 0)
    info = {"duration": dur, "bitrate": bitrate, "size": size, "start": float(fmt.get("start_time") or 0),
            "container": os.path.splitext(path)[1].lower().lstrip("."), "format": fmt.get("format_name", ""),
            "video": None, "audio": [], "subs": []}
    if v:
        transfer = v.get("color_transfer") or ""
        dovi = next((sd for sd in v.get("side_data_list") or [] if "DOVI" in (sd.get("side_data_type") or "")), None)
        fr = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
        try:
            num, den = fr.split("/")
            fps = float(num) / float(den) if float(den) else 0
        except (ValueError, ZeroDivisionError):
            fps = 0
        info["video"] = {
            "index": v["index"], "codec": v.get("codec_name"), "profile": v.get("profile") or "",
            "tag": v.get("codec_tag_string") or "", "width": v.get("width") or 0, "height": v.get("height") or 0,
            "pix_fmt": v.get("pix_fmt") or "", "bits": 10 if "10" in (v.get("pix_fmt") or "") else 8,
            "level": v.get("level"), "fps": fps,
            "hdr": transfer in ("smpte2084", "arib-std-b67") or bool(dovi and dovi.get("dv_bl_signal_compatibility_id") in (0,)),
            "hlg": transfer == "arib-std-b67", "transfer": transfer, "primaries": v.get("color_primaries") or "",
            "dv_profile": dovi.get("dv_profile") if dovi else None,
            "dv_compat": dovi.get("dv_bl_signal_compatibility_id") if dovi else None,
        }
    n = 0
    for s in streams:
        if s.get("codec_type") != "audio":
            continue
        lang = _lang(s)
        ch = s.get("channels") or 0
        layout = {1: "Mono", 2: "Stereo", 6: "5.1", 7: "6.1", 8: "7.1"}.get(ch, f"{ch} ch" if ch else "")
        name = LANGS.get(lang.lower(), lang.upper() if lang else "")
        title = _title(s) if len(_title(s)) < 48 else ""
        if title:
            label = title if (not name or name.lower() in title.lower()) else f"{name} · {title}"
        else:
            label = " · ".join(x for x in (name, AUDIO_NAMES.get(s.get("codec_name"), (s.get("codec_name") or "").upper()),
                                            layout) if x)
        info["audio"].append({"index": s["index"], "n": n, "codec": s.get("codec_name"), "profile": s.get("profile"),
                              "channels": ch, "lang": lang, "label": label or f"Track {n + 1}",
                              "default": bool((s.get("disposition") or {}).get("default"))})
        n += 1
    n = 0
    for s in streams:
        if s.get("codec_type") != "subtitle":
            continue
        codec = s.get("codec_name") or ""
        lang = _lang(s)
        disp = s.get("disposition") or {}
        title = _title(s)
        forced = bool(disp.get("forced")) or "forced" in title.lower()
        kind = "text" if codec in TEXT_SUBS else "image" if codec in IMAGE_SUBS else None
        if kind:
            name = LANGS.get(lang.lower(), lang.upper() if lang else "Unknown")
            extra = title if title and title.lower() not in (name.lower(), lang.lower()) else ""
            if extra and name.lower() in extra.lower():
                name, extra = extra, ""
            info["subs"].append({"id": str(s["index"]), "index": s["index"], "n": n, "codec": codec, "kind": kind,
                                 "lang": lang, "forced": forced, "default": bool(disp.get("default")),
                                 "label": " · ".join(x for x in (name, extra, "Forced" if forced and "forced" not in (name + extra).lower() else "",
                                                                  "image" if kind == "image" else "") if x),
                                 "width": s.get("width"), "height": s.get("height")})
        n += 1
    for k, sc in enumerate(sidecars(path)):
        info["subs"].append({"id": f"x{k}", "index": None, "n": None, "codec": sc["ext"].lstrip("."), "kind": "text",
                             "lang": sc["lang"], "forced": sc["forced"], "default": False, "external": True,
                             "path": sc["path"], "label": sc["label"]})
    info["chapters"] = [{"start": float(c.get("start_time") or 0), "end": float(c.get("end_time") or 0),
                         "title": (c.get("tags") or {}).get("title") or ""} for c in data.get("chapters") or []]
    return info


def sidecars(path):
    """Subtitle files next to the video: "<name>.srt", "<name>.en.srt", "<name>.eng.forced.srt"."""
    p = Path(path)
    out = []
    try:
        entries = list(p.parent.iterdir())
    except OSError:
        return out
    stem = p.stem.lower()
    for f in sorted(entries):
        if f.suffix.lower() not in SIDE_EXT or not f.name.lower().startswith(stem):
            continue
        middle = f.name[len(p.stem):-len(f.suffix)].strip(".").lower()
        parts = [x for x in re.split(r"[.\s_-]+", middle) if x]
        lang = next((x for x in parts if x in LANGS), "")
        forced = "forced" in parts
        sdh = "sdh" in parts or "hi" in parts or "cc" in parts
        label = " · ".join(x for x in (LANGS.get(lang, lang.upper() if lang else "Subtitles"),
                                        "Forced" if forced else "", "SDH" if sdh else "", "file") if x)
        out.append({"path": str(f), "ext": f.suffix.lower(), "lang": lang, "forced": forced, "label": label})
    return out


# ------------------------------------------------------------------ keyframes
def keyframes(path, info=None):
    """Sorted keyframe times (seconds) of the video track, from the file's own index, or None when there's none
    (then stream copy can't be cut at predictable places and the stream gets transcoded instead)."""
    key = _key(path)
    if key in _kf_cache:
        return _kf_cache[key]
    f = _disk("keyframes", key)
    kf = None
    try:
        kf = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext in (".mkv", ".webm", ".mk3d"):
                kf = mkv_keyframes(path)
            elif ext in (".mp4", ".m4v", ".mov"):
                kf = mp4_keyframes(path)
        except Exception as e:
            print("keyframe index unreadable:", os.path.basename(path), type(e).__name__, e)
            kf = None
        if kf is not None:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(kf), encoding="utf-8")
    if kf:
        dur = (info or {}).get("duration") or 0
        gaps = [b - a for a, b in zip(kf, kf[1:])]
        if (dur and kf[-1] < dur - 30) or (gaps and max(gaps) > 15) or len(kf) < 3:
            kf = None  # a partial or very sparse index: not good enough to cut segments on
    _kf_cache[key] = kf
    return kf


# EBML / Matroska
_SEGMENT, _SEEKHEAD, _SEEK, _SEEKID, _SEEKPOS = 0x18538067, 0x114D9B74, 0x4DBB, 0x53AB, 0x53AC
_INFO, _TSCALE, _TRACKS, _TRACKENTRY, _TRACKNUM, _TRACKTYPE = 0x1549A966, 0x2AD7B1, 0x1654AE6B, 0xAE, 0xD7, 0x83
_CUES, _CUEPOINT, _CUETIME, _CUETRACKPOS, _CUETRACK, _CLUSTER = 0x1C53BB6B, 0xBB, 0xB3, 0xB7, 0xF7, 0x1F43B675


def _vint(f, as_id):
    b = f.read(1)
    if not b:
        raise EOFError
    first = b[0]
    length, mask = 1, 0x80
    while length <= 8 and not first & mask:
        mask >>= 1
        length += 1
    if length > 8:
        raise ValueError("bad EBML")
    rest = f.read(length - 1)
    if as_id:
        return int.from_bytes(bytes([first]) + rest, "big"), length
    v = first & (mask - 1)
    for x in rest:
        v = (v << 8) | x
    return (None if v == (1 << (7 * length)) - 1 else v), length


def _children(buf):
    """(id, payload bytes) of the elements in a bytes buffer."""
    f = io.BytesIO(buf)
    n = len(buf)
    while f.tell() < n:
        try:
            eid, _ = _vint(f, True)
            size, _ = _vint(f, False)
        except EOFError:
            return
        if size is None:
            return
        yield eid, f.read(size)


def _uint(b):
    return int.from_bytes(b, "big") if b else 0


def mkv_keyframes(path):
    with open(path, "rb") as f:
        eid, _ = _vint(f, True)
        size, _ = _vint(f, False)
        if eid != 0x1A45DFA3:
            return None
        f.seek(size, 1)
        eid, _ = _vint(f, True)
        _seg_size, _ = _vint(f, False)
        if eid != _SEGMENT:
            return None
        base = f.tell()
        where = {}   # element id -> absolute offset
        seen_heads = set()

        def read_seekhead(pos):
            if pos in seen_heads:
                return
            seen_heads.add(pos)
            f.seek(pos)
            i, _ = _vint(f, True)
            s, _ = _vint(f, False)
            if i != _SEEKHEAD or s is None or s > 1 << 20:
                return
            for cid, payload in _children(f.read(s)):
                if cid != _SEEK:
                    continue
                sid = spos = None
                for k, v in _children(payload):
                    if k == _SEEKID:
                        sid = _uint(v)
                    elif k == _SEEKPOS:
                        spos = _uint(v)
                if sid is not None and spos is not None:
                    if sid == _SEEKHEAD:
                        read_seekhead(base + spos)
                    else:
                        where.setdefault(sid, base + spos)

        # top level up to the first cluster
        f.seek(base)
        for _ in range(64):
            pos = f.tell()
            try:
                i, _ = _vint(f, True)
                s, _ = _vint(f, False)
            except EOFError:
                break
            if i == _CLUSTER or s is None:
                break
            if i == _SEEKHEAD:
                read_seekhead(pos)
                f.seek(pos)
                _vint(f, True)
                _vint(f, False)
            elif i in (_INFO, _TRACKS, _CUES):
                where.setdefault(i, pos)
            f.seek(s, 1)

        def element(eid_):
            pos = where.get(eid_)
            if pos is None:
                return None
            f.seek(pos)
            i, _ = _vint(f, True)
            s, _ = _vint(f, False)
            if i != eid_ or s is None or s > 256 << 20:
                return None
            return f.read(s)

        scale = 1_000_000
        info = element(_INFO)
        if info:
            for k, v in _children(info):
                if k == _TSCALE:
                    scale = _uint(v) or scale
        video_tracks = set()
        tracks = element(_TRACKS)
        if tracks:
            for k, v in _children(tracks):
                if k != _TRACKENTRY:
                    continue
                num = typ = None
                for kk, vv in _children(v):
                    if kk == _TRACKNUM:
                        num = _uint(vv)
                    elif kk == _TRACKTYPE:
                        typ = _uint(vv)
                if typ == 1 and num is not None:
                    video_tracks.add(num)
        cues = element(_CUES)
        if not cues or not video_tracks:
            return None
        vt = min(video_tracks)
        times = []
        for k, v in _children(cues):
            if k != _CUEPOINT:
                continue
            t = None
            tracks_here = set()
            for kk, vv in _children(v):
                if kk == _CUETIME:
                    t = _uint(vv)
                elif kk == _CUETRACKPOS:
                    for k3, v3 in _children(vv):
                        if k3 == _CUETRACK:
                            tracks_here.add(_uint(v3))
            if t is not None and vt in tracks_here:
                times.append(round(t * scale / 1e9, 4))
        times = sorted(set(times))
        return times or None


# MP4 (moov/trak/stbl: stss + stts + ctts + elst)
def _boxes(buf, start=0, end=None):
    end = len(buf) if end is None else end
    p = start
    while p + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[p:p + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", buf[p + 8:p + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - p
        if size < hdr:
            return
        yield typ.decode("latin-1"), p + hdr, p + size
        p += size


def _top_box(f, want):
    """Bytes of a top-level box (moov) in a file, read by walking box headers."""
    f.seek(0, 2)
    end = f.tell()
    p = 0
    while p + 8 <= end:
        f.seek(p)
        hdr = f.read(16)
        size, typ = struct.unpack(">I4s", hdr[:8])
        h = 8
        if size == 1:
            size = struct.unpack(">Q", hdr[8:16])[0]
            h = 16
        elif size == 0:
            size = end - p
        if size < h:
            return None
        if typ == want:
            if size > 512 << 20:
                return None
            f.seek(p)
            return f.read(size)
        p += size
    return None


def _find(buf, path, start=0, end=None):
    """First box along a path like ["mdia", "minf"] -> (start, end) of its payload."""
    s, e = start, end if end is not None else len(buf)
    for name in path:
        for typ, a, b in _boxes(buf, s, e):
            if typ == name:
                s, e = a, b
                break
        else:
            return None
    return s, e


def mp4_keyframes(path):
    with open(path, "rb") as f:
        moov = _top_box(f, b"moov")
    if not moov:
        return None
    for typ, a, b in _boxes(moov, 8):
        if typ != "trak":
            continue
        hdlr = _find(moov, ["mdia", "hdlr"], a, b)
        if not hdlr or moov[hdlr[0] + 8:hdlr[0] + 12] != b"vide":
            continue
        mdhd = _find(moov, ["mdia", "mdhd"], a, b)
        ver = moov[mdhd[0]]
        timescale = struct.unpack(">I", moov[mdhd[0] + (20 if ver == 1 else 12):][:4])[0]
        stbl = _find(moov, ["mdia", "minf", "stbl"], a, b)
        if not stbl:
            return None
        tables = {t: (x, y) for t, x, y in _boxes(moov, *stbl)}
        if "stts" not in tables:
            return None
        if "stss" not in tables:
            return None  # every sample is a sync sample (intra-only): no useful cut table
        x = tables["stts"][0]
        n = struct.unpack(">I", moov[x + 4:x + 8])[0]
        stts = [struct.unpack(">II", moov[x + 8 + 8 * i:x + 16 + 8 * i]) for i in range(n)]
        x = tables["stss"][0]
        n = struct.unpack(">I", moov[x + 4:x + 8])[0]
        sync = [struct.unpack(">I", moov[x + 8 + 4 * i:x + 12 + 4 * i])[0] for i in range(n)]
        ctts = {}
        if "ctts" in tables:
            x = tables["ctts"][0]
            ver = moov[x]
            n = struct.unpack(">I", moov[x + 4:x + 8])[0]
            want = set(sync)
            sample = 1
            for i in range(n):
                cnt, off = struct.unpack(">I" + ("i" if ver else "I"), moov[x + 8 + 8 * i:x + 16 + 8 * i])
                for s_ in range(sample, sample + cnt):
                    if s_ in want:
                        ctts[s_] = off
                sample += cnt
        shift = 0
        elst = _find(moov, ["edts", "elst"], a, b)
        if elst:
            x = elst[0]
            ver = moov[x]
            if struct.unpack(">I", moov[x + 4:x + 8])[0] >= 1:
                mt = (struct.unpack(">q", moov[x + 16:x + 24])[0] if ver == 1 else
                      struct.unpack(">i", moov[x + 12:x + 16])[0])
                shift = max(0, mt)
        out = []
        it = iter(sorted(sync))
        target = next(it, None)
        sample, dts = 1, 0
        for cnt, delta in stts:
            while target is not None and target < sample + cnt:
                t = dts + (target - sample) * delta
                out.append(round((t + ctts.get(target, 0) - shift) / timescale, 4))
                target = next(it, None)
            sample += cnt
            dts += cnt * delta
            if target is None:
                break
        return sorted(set(x for x in out if x >= 0)) or None
    return None


# ------------------------------------------------------------------ fMP4 helpers (HLS units)
def init_info(init_path):
    """{"scales": {track_id: timescale}, "video": video track id, "shift": {track_id: edit-list start (s)}} of an
    fMP4 init segment."""
    buf = Path(init_path).read_bytes()
    out = {"scales": {}, "video": None, "shift": {}}
    moov = _find(buf, ["moov"])
    if not moov:
        return out
    for typ, a, b in _boxes(buf, *moov):
        if typ != "trak":
            continue
        tkhd = _find(buf, ["tkhd"], a, b)
        ver = buf[tkhd[0]]
        tid = struct.unpack(">I", buf[tkhd[0] + (20 if ver == 1 else 12):][:4])[0]
        mdhd = _find(buf, ["mdia", "mdhd"], a, b)
        mv = buf[mdhd[0]]
        scale = struct.unpack(">I", buf[mdhd[0] + (20 if mv == 1 else 12):][:4])[0]
        out["scales"][tid] = scale
        hdlr = _find(buf, ["mdia", "hdlr"], a, b)
        if hdlr and buf[hdlr[0] + 8:hdlr[0] + 12] == b"vide":
            out["video"] = tid
        elst = _find(buf, ["edts", "elst"], a, b)
        if elst and scale:
            x = elst[0]
            ev = buf[x]
            n = struct.unpack(">I", buf[x + 4:x + 8])[0]
            for k in range(n):   # the first non-empty edit's media time
                if ev == 1:
                    mt = struct.unpack(">q", buf[x + 16 + 20 * k:x + 24 + 20 * k])[0]
                else:
                    mt = struct.unpack(">i", buf[x + 12 + 12 * k:x + 16 + 12 * k])[0]
                if mt >= 0:
                    out["shift"][tid] = mt / scale
                    break
    return out


def unit_time(path, init):
    """Presentation time (s) of the first video frame in an fMP4 media segment: tfdt + the first sample's composition
    offset - the edit list's start."""
    with open(path, "rb") as f:
        head = f.read(256 * 1024)
    scales, video = init["scales"], init["video"]
    for typ, a, b in _boxes(head):
        if typ != "moof":
            continue
        b = min(b, len(head))
        for t2, a2, b2 in _boxes(head, a, b):
            if t2 != "traf":
                continue
            tid = tfdt = None
            cto = 0
            for t3, a3, b3 in _boxes(head, a2, b2):
                if t3 == "tfhd":
                    tid = struct.unpack(">I", head[a3 + 4:a3 + 8])[0]
                elif t3 == "tfdt":
                    ver = head[a3]
                    tfdt = (struct.unpack(">Q", head[a3 + 4:a3 + 12])[0] if ver == 1 else
                            struct.unpack(">I", head[a3 + 4:a3 + 8])[0])
                elif t3 == "trun":
                    ver = head[a3]
                    flags = int.from_bytes(head[a3 + 1:a3 + 4], "big")
                    p = a3 + 8
                    p += 4 if flags & 0x1 else 0
                    p += 4 if flags & 0x4 else 0
                    p += 4 if flags & 0x100 else 0
                    p += 4 if flags & 0x200 else 0
                    p += 4 if flags & 0x400 else 0
                    if flags & 0x800:
                        cto = struct.unpack(">i" if ver else ">I", head[p:p + 4])[0]
            if tid == video and tfdt is not None and scales.get(tid):
                return (tfdt + cto) / scales[tid] - init["shift"].get(tid, 0.0)
        break
    return None


def media_offset(path):
    """Where the first moof starts in a segment file (anything before it, like styp, is dropped when stitching)."""
    with open(path, "rb") as f:
        head = f.read(8192)
    p = 0
    while p + 8 <= len(head):
        size, typ = struct.unpack(">I4s", head[p:p + 8])
        if typ in (b"moof", b"prft", b"emsg"):
            return p
        if size == 1:
            size = struct.unpack(">Q", head[p + 8:p + 16])[0]
        if size < 8:
            return 0
        p += size
    return 0


# ------------------------------------------------------------------ subtitles -> WebVTT
_sub_jobs = {}    # file key -> {"state": "running"|"done"|"error", "started"}
_sub_lock = threading.Lock()


def _vtt_path(path, sid):
    return SUBS_DIR / (hashlib.sha1(_key(path).encode("utf-8", "surrogatepass")).hexdigest()[:20] + f"_{sid}.vtt")


def subtitle_vtt(path, sub):
    """(state, vtt path): state "ready", "pending" (extraction running in the background) or "error"."""
    out = _vtt_path(path, sub["id"])
    if out.exists() and out.stat().st_size > 0:
        return "ready", out
    SUBS_DIR.mkdir(parents=True, exist_ok=True)
    if sub.get("external"):
        tmp = out.with_suffix(".part.vtt")
        r = subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", sub["path"], "-f", "webvtt",
                            str(tmp)], capture_output=True, timeout=60, creationflags=NO_WINDOW)
        if r.returncode != 0 or not tmp.exists():
            return "error", None
        tmp.replace(out)
        return "ready", out
    start_extraction(path)
    with _sub_lock:
        job = _sub_jobs.get(_key(path)) or {}
    if out.exists() and out.stat().st_size > 0:
        return "ready", out
    return ("error", None) if job.get("state") == "error" else ("pending", None)


def start_extraction(path):
    """One background pass that pulls every embedded text subtitle track out as WebVTT (the whole file has to be
    read, since subtitles are interleaved with the video; low priority)."""
    try:
        info = probe(path)["info"]
    except Exception:
        return
    subs = [s for s in info["subs"] if s["kind"] == "text" and not s.get("external")
            and not (_vtt_path(path, s["id"]).exists() and _vtt_path(path, s["id"]).stat().st_size > 0)]
    if not subs:
        return
    key = _key(path)
    with _sub_lock:
        job = _sub_jobs.get(key)
        if job and (job["state"] == "running" or (job["state"] == "error" and time.time() - job["started"] < 300)):
            return
        _sub_jobs[key] = {"state": "running", "started": time.time()}
    SUBS_DIR.mkdir(parents=True, exist_ok=True)

    def run():
        args = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", path]
        parts = []
        for s in subs:
            part = _vtt_path(path, s["id"]).with_suffix(".part.vtt")
            parts.append((part, _vtt_path(path, s["id"])))
            args += ["-map", f"0:{s['index']}", "-c:s", "webvtt", "-f", "webvtt", str(part)]
        t0 = time.time()
        try:
            r = subprocess.run(args, capture_output=True, timeout=1800, creationflags=NO_WINDOW | BELOW_NORMAL)
            ok = r.returncode == 0
        except Exception:
            ok = False
        for part, final in parts:
            if part.exists() and part.stat().st_size > 0:
                part.replace(final)
            elif part.exists():
                part.unlink()
        with _sub_lock:
            _sub_jobs[key] = {"state": "done" if ok else "error", "started": t0}
        print(f"mobile: subtitles extracted from {os.path.basename(path)} in {time.time() - t0:.1f}s", flush=True)

    threading.Thread(target=run, daemon=True, name="mobile-subs").start()


_off_cache = {}


def offset_cached(path):
    k = str(path)
    v = _off_cache.get(k)
    if v is None:
        v = media_offset(path)
        if len(_off_cache) > 20000:
            _off_cache.clear()
        _off_cache[k] = v
    return v
