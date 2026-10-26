"""Subtitle text around the playhead, for "what did they say" and summaries.

The player's subtitle_window(seconds) is used when it has it. Otherwise the video's text subtitle track is fetched
from Jellyfin (as SRT, once per video, kept in memory only) and cut to the window."""
import re
import threading

TEXT_CODECS = {"ass", "ssa", "subrip", "srt", "webvtt", "vtt", "mov_text", "text"}
_cache = {}
_inflight = {}  # item id -> Event while its subtitles are being fetched
_lock = threading.Lock()


def _norm_lines(raw):
    """Whatever the player hands back -> [{"start", "end", "text"}]."""
    out = []
    if isinstance(raw, dict):
        raw = raw.get("lines") or raw.get("items") or raw.get("subtitles") or []
    if isinstance(raw, str):
        raw = [raw]
    for x in raw or []:
        if isinstance(x, str):
            out.append({"start": None, "end": None, "text": x})
        elif isinstance(x, dict):
            out.append({"start": x.get("start", x.get("s")), "end": x.get("end", x.get("e")),
                        "text": x.get("text") or x.get("line") or ""})
        elif isinstance(x, (list, tuple)) and x:
            out.append({"start": x[0] if len(x) > 2 else None, "end": x[1] if len(x) > 2 else None, "text": str(x[-1])})
    return [ln for ln in out if clean(ln["text"])]


def clean(text):
    t = re.sub(r"\{[^}]*\}", "", text or "")       # ASS override tags
    t = re.sub(r"<[^>]+>", "", t)                  # <i>, <font>
    t = t.replace("\\N", " ").replace("\\n", " ")
    t = re.sub(r"\[[^\]]*\]|\([A-Z ]+\)", "", t)   # [music], (GRUNTS)
    t = re.sub(r"\s+", " ", t).strip(" -♪")
    return t.strip()


def _ts(s):
    h, m, rest = s.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def keep_cues(text):
    """Like clean(), but keeps the sound cues of SDH subtitles ([gunshot], (SCREAMING), SPEAKER:) for searching."""
    t = re.sub(r"\{[^}]*\}", "", text or "")
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("\\N", " ").replace("\\n", " ")
    t = re.sub(r"\s+", " ", t).strip(" -\u266a")
    return t.strip()


CUE = re.compile(r"\[[^\]]{2,}\]|\([A-Za-z][^)]{1,40}\)")


def parse_srt(text, keep=False):
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        m = re.search(r"(\d+:\d+:\d+[.,]\d+)\s*-->\s*(\d+:\d+:\d+[.,]\d+)[^\n]*\n(.*)", block, re.S)
        if m:
            t = (keep_cues if keep else clean)(m.group(3))
            if t:
                cues.append({"start": _ts(m.group(1)), "end": _ts(m.group(2)), "text": t})
    return cues


def _forced(s):
    """Forced (foreign-parts-only) tracks: flagged, or only named so ("Forced - English")."""
    return bool(s.get("IsForced") or s.get("forced")) or "forced" in " ".join(
        str(s.get(k) or "") for k in ("Title", "DisplayTitle", "title")).lower()


SDH_WORDS = re.compile(r"\b(sdh|cc|hearing[ -]?impaired|hi)\b|\.(sdh|hi|cc)\.", re.I)


def _sdh(s):
    """A subtitle track for deaf and hard-of-hearing viewers (sound cues and speaker names)."""
    label = " ".join(str(s.get(k) or "") for k in ("Title", "DisplayTitle", "Path", "title"))
    return bool(s.get("IsHearingImpaired") or s.get("hearing_impaired")) or bool(SDH_WORDS.search(label))


def _pick_sdh(item, prefs):
    """For searching: the SDH/CC track in the preferred language, else (None) - the caller then compares the
    full tracks by how many sound cues they carry. Never a forced track."""
    src = (item.get("MediaSources") or [{}])[0]
    full = [s for s in src.get("MediaStreams") or [] if s.get("Type") == "Subtitle"
            and (s.get("Codec") or "").lower() in TEXT_CODECS and not _forced(s)]
    for lang in prefs:
        same = [s for s in full if (s.get("Language") or "").lower().startswith(lang.lower()[:2])]
        if same:
            sdh = [s for s in same if _sdh(s)]
            return src, (sdh[0] if sdh else None), same
    return src, None, full


def _pick_stream(item, mpv, prefs):
    """The full dialogue track: the one showing if it's a full one, else the preferred language's (plain before
    SDH), never a forced track (those only cover foreign-language lines)."""
    src = (item.get("MediaSources") or [{}])[0]
    streams = [s for s in src.get("MediaStreams") or []
               if s.get("Type") == "Subtitle" and (s.get("Codec") or "").lower() in TEXT_CODECS]
    if not streams:
        return src, None
    full = [s for s in streams if not _forced(s)] or streams
    want = None
    try:  # the track that's showing, when it's one of the file's own full tracks
        cur = next((t for t in (mpv.track_list or []) if t.get("type") == "sub" and t.get("selected")), None)
        if cur and not _forced(cur):
            want = cur.get("lang")
            ff = cur.get("ff-index")
            hit = next((s for s in full if ff is not None and s.get("Index") == ff and not s.get("IsExternal")), None)
            if hit:
                return src, hit
    except Exception:
        pass
    langs = [x for x in ([want] if want else []) + prefs if x]
    for lang in langs:
        same = [s for s in full if (s.get("Language") or "").lower().startswith(lang.lower()[:2])]
        if same:
            plain = [s for s in same if "sdh" not in str(s.get("Title") or "").lower()]
            return src, (plain or same)[0]
    return src, full[0]


def cues_for(app, item, sdh=False):
    """All subtitle cues of the item (cached per item), or None when it has no text subtitles.
    sdh=True (scene search, loudness): the SDH/CC track when there is one (else the full track with the most sound
    cues), with its [sound cues] kept in the text. What's shown on screen is not affected."""
    key = (item["Id"], "sdh") if sdh else item["Id"]
    with _lock:
        if key in _cache:
            return _cache[key]
        ev = _inflight.get(key)
        mine = ev is None
        if mine:
            ev = _inflight[key] = threading.Event()
    if not mine:  # someone (the prefetch) is already fetching it: wait for that instead of asking twice
        ev.wait(95)
        with _lock:
            if key in _cache:
                return _cache[key]
        raise RuntimeError("subtitles still loading")
    try:
        return _fetch(app, item, key, sdh)
    finally:
        with _lock:
            _inflight.pop(key, None)
        ev.set()


def _get_track(app, item, src, stream, keep):
    jf = app.jf
    r = jf.s.get(f"{jf.base}/Videos/{item['Id']}/{src.get('Id')}/Subtitles/{stream['Index']}/0/Stream.srt",
                 headers={"Authorization": jf.auth_header()}, timeout=90)  # first time: Jellyfin extracts it
    r.raise_for_status()
    return parse_srt(r.content.decode("utf-8-sig", "replace"), keep=keep)


def _fetch(app, item, key, sdh=False):
    from .. import settings
    jf = app.jf
    prefs = [x for x in settings.load()["sub_lang"].split(",") + ["eng", "en"] if x]
    if not item.get("MediaSources"):
        item = jf.item(item["Id"])
    cues = None
    side = None
    if sdh:  # a hearing-impaired file next to the video (e.g. fetched by Bazarr) wins
        try:
            from .sdh_fetch import sidecar, sound_cues
            side = sidecar(item)
            if side is not None:
                marked, total = sound_cues(side)
                if not total or marked < max(5, total * 0.03):
                    side = None  # labelled hearing-impaired but without sound cues: no better than the rest
        except Exception:
            side = None
    if side is not None:
        text = side.read_text(encoding="utf-8-sig", errors="replace")
        if side.suffix.lower() in (".ass", ".ssa"):
            from .ending import _parse_ass
            cues = [{**c, "text": keep_cues(c["text"])} for c in _parse_ass(text)]
        else:
            cues = parse_srt(text, keep=True)
    elif sdh:
        src, stream, same = _pick_sdh(item, prefs)
        if stream is not None:
            cues = _get_track(app, item, src, stream, keep=True)
        else:  # no labelled SDH: the full track with the most [sound cues] (at most 3 looked at)
            best = None
            for st in [x for x in same if (x.get("Codec") or "").lower() in TEXT_CODECS][:3]:
                try:
                    got = _get_track(app, item, src, st, keep=True)
                except Exception:
                    continue
                n = sum(1 for c in got if CUE.search(c["text"]))
                if best is None or n > best[0]:
                    best = (n, got)
            cues = best[1] if best else None
    else:
        src, stream = _pick_stream(item, getattr(app.player, "mpv", None), prefs)
        if stream is not None:
            cues = _get_track(app, item, src, stream, keep=False)
    with _lock:
        if len(_cache) > 8:
            _cache.clear()
        _cache[key] = cues
    return cues


def prefetch(app):
    """Fetch the playing video's subtitles in the background (first use of the assistant during a video), so a
    question a moment later doesn't wait for Jellyfin to extract them."""
    player = getattr(app, "player", None)
    item = getattr(player, "item", None)
    if not item or callable(getattr(player, "subtitle_window", None)):
        return
    with _lock:
        if item["Id"] in _cache:
            return

    def run():
        try:
            cues_for(app, item)
        except Exception as e:
            print("subtitle prefetch failed:", e)
    threading.Thread(target=run, daemon=True).start()


def window(app, seconds, position=None, item=None):
    """Subtitle lines from `seconds` before the playhead up to it: (lines, why_empty)."""
    player = getattr(app, "player", None)
    if callable(getattr(player, "subtitle_window", None)):
        try:
            lines = _norm_lines(player.subtitle_window(seconds))
            if lines:
                return lines, None
        except Exception as e:
            print("subtitle_window failed:", e)
    item = item or getattr(player, "item", None)
    if not item:
        return [], "Nothing is playing"
    if position is None:
        try:
            position = player.mpv.time_pos or getattr(player, "_pos", 0)
        except Exception:
            position = getattr(player, "_pos", 0) or 0
    try:
        cues = cues_for(app, item)
    except Exception as e:
        print("subtitles from Jellyfin failed:", e)
        cues = None
    if cues is None:
        return [], "This video has no text subtitles I can read"
    lo = position - seconds
    lines = [c for c in cues if c["end"] >= lo and c["start"] <= position + 0.5]
    return lines, (None if lines else "Nobody said anything in that stretch")


def as_text(lines, limit=7000):
    out, seen = [], None
    for ln in lines:
        t = clean(ln["text"])
        if t and t != seen:
            out.append(t)
            seen = t
    text = "\n".join(out)
    return text[-limit:] if len(text) > limit else text
