"""'Is there a jumpscare coming?' / 'does it get loud?': read the next few minutes of the soundtrack ahead of the
playhead (ffmpeg's EBU R128 momentary loudness, decoded faster than real time, audio only) and find sudden jumps
over the running level, plus anything the media index flagged in that stretch. Cached per file and position."""
import re
import subprocess
import threading
import time

TICKS = 10_000_000
_cache = {}
_lock = threading.Lock()


def _source(app, item):
    path = (item.get("MediaSources") or [{}])[0].get("Path") or item.get("Path")
    import os
    if path and os.path.exists(path):
        return path
    jf = app.jf
    if str(item.get("Id", "")).startswith("file:"):
        return None
    return jf.stream_url(item, "original")


def momentary(src, start, span=300, audio_index=None):
    """[(t_absolute, momentary LUFS)] every 0.1 s from start to start+span."""
    from ..index import deps
    ff = deps.find_ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg not found")
    cmd = [ff, "-v", "error", "-ss", f"{max(0.0, start):.2f}", "-t", str(span), "-i", src,
           "-map", f"0:{audio_index}" if audio_index is not None else "0:a:0", "-vn", "-sn", "-dn",
           "-af", "ebur128=metadata=1,ametadata=mode=print:key=lavfi.r128.M:file=-", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=180, creationflags=0x08000000 | 0x00004000)
    out, t = [], None
    for line in r.stdout.splitlines():
        if line.startswith(b"frame:"):
            m = re.search(rb"pts_time:([\d.]+)", line)
            t = float(m.group(1)) if m else None
        elif line.startswith(b"lavfi.r128.M=") and t is not None:
            try:
                v = float(line.split(b"=", 1)[1])
            except ValueError:
                continue
            out.append((start + t, max(-70.0, v)))
    if not out and r.returncode:
        raise RuntimeError(r.stderr.decode(errors="replace")[-200:])
    return out


def find_jumps(series, rise=15.0, quiet=3.0, floor=-20.0):
    """Sudden jumps: a 0.5 s bin at least `rise` LU over the level of the `quiet` seconds before it, which stayed
    calm (no loud moment) - the shape of a jump scare. Also sustained loud stretches."""
    if not series:
        return []
    bins = {}
    t0 = series[0][0] + 1.0  # the first ~0.4 s of a momentary meter is still filling up
    for t, v in series:
        if t < t0:
            continue
        v = max(v, -45.0)  # silence counts as "quiet", not as -70
        k = int(t * 2)
        bins[k] = max(bins.get(k, -70.0), v)
    keys = sorted(bins)
    events = []
    hist = int(quiet * 2)
    for i, k in enumerate(keys):
        if i < hist:
            continue
        before = [bins[x] for x in keys[i - hist:i]]
        base = sorted(before)[len(before) // 2]
        calm = max(before) <= base + 8
        v = bins[k]
        if v >= floor and v - base >= rise and calm:
            events.append({"t": k / 2, "kind": "spike", "rise": round(v - base, 1), "level": round(v, 1)})
    # sustained loud: 3 s above -16 LUFS momentary
    run = 0
    for k in keys:
        run = run + 1 if bins[k] >= -16 else 0
        if run == 6:
            events.append({"t": (k - 5) / 2, "kind": "loud", "level": round(bins[k], 1)})
    events.sort(key=lambda e: e["t"])
    merged = []
    for e in events:
        if merged and e["t"] - merged[-1]["t"] < 6:
            if e["kind"] == "spike" and merged[-1]["kind"] != "spike":
                merged[-1] = e
            continue
        merged.append(e)
    return merged


def index_flags(item_id, start, end):
    """Ranges the media index flagged (violence/gore/...) in [start, end], if this title is already indexed."""
    try:
        from ..index import analysis, api, store
        d = store.load((item_id or "").lower().replace("-", ""))
        if d is None or not d["meta"].get("complete"):
            return []
        d = api._with_sens(d)
        return [r for r in analysis.sensitive_ranges(d) if r["end"] >= start and r["start"] <= end]
    except Exception:
        return []


def scan(app, item, position, span=300, audio_index=None):
    """Events in the next `span` seconds after `position`: [{"t", "kind", ...}] (absolute times)."""
    src = _source(app, item)
    if not src:
        raise RuntimeError("no file to listen to")
    key = (item.get("Id"), audio_index, int(position // 20))
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < 3600:
        return [e for e in hit[1] if e["t"] >= position]
    events = find_jumps(momentary(src, position, span, audio_index))
    with _lock:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = (time.time(), events)
    return events


def clock(s):
    s = int(max(0, s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def answer(app, item, position, span=300, audio_index=None):
    """A short, spoiler-light line about the next few minutes, measured from `position` (when it was asked)."""
    events = scan(app, item, position, span, audio_index)
    flags = index_flags(item.get("Id"), position, position + span)
    mins = span // 60

    def when(t):
        return f"in about {clock(t - position)} (around {clock(t)})"
    spikes = [e for e in events if e["kind"] == "spike"]
    loud = [e for e in events if e["kind"] == "loud"]
    parts = []
    if spikes:
        if len(spikes) == 1:
            parts.append(f"Heads up: a sudden loud moment {when(spikes[0]['t'])}")
        else:
            parts.append(f"Heads up: {len(spikes)} sudden loud moments, the first {when(spikes[0]['t'])}"
                         + (f", the next in about {clock(spikes[1]['t'] - position)}" if len(spikes) > 1 else ""))
    if loud and (not spikes or loud[0]["t"] + 6 < spikes[0]["t"] or loud[0]["t"] > spikes[0]["t"] + 6):
        parts.append(f"it gets loud for a while {when(loud[0]['t'])}")
    if flags:
        cats = sorted({f["category"] for f in flags})
        parts.append(f"the index also flags {' and '.join(cats)} {when(flags[0]['start'])}")
    if not parts:
        return f"Nothing sudden in the next {mins} minutes - the sound stays fairly even.", events, flags
    text = "; ".join(parts) + "."
    return text[0].upper() + text[1:], events, flags
