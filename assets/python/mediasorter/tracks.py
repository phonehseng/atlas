"""Strips unwanted audio/subtitle languages from library files (lossless MKV remux).

Rules come from mediastack.json ("tracks"), chosen in the setup wizard. The defaults:
  * Movies & TV: keep English audio and English subtitles.
  * Anime (Anime / Anime Movies folders): keep English + Japanese audio, English subtitles.
  * Never leave a file silent: if no audio track is in the wanted languages (a foreign-language
    film), all audio is kept. Untagged ("und") tracks are kept since they may be wanted.
  * Video, chapters and attachments (anime subtitle fonts) are always kept.

The cleaned file replaces the library path only after it verifies. Library files are hardlinks
of the torrent's copy, so replacing the library entry leaves the seeding copy untouched.
"""
import json
import os
import subprocess
import time
from pathlib import Path

import requests

import stackconf

log = None  # set by mediasorter

# IETF tags some muxers write instead of ISO 639-2 codes.
IETF = {"en": "eng", "ja": "jpn", "es": "spa", "fr": "fre", "de": "ger", "it": "ita", "pt": "por", "ru": "rus",
        "ko": "kor", "zh": "chi", "nl": "dut", "sv": "swe", "no": "nor", "da": "dan", "fi": "fin", "pl": "pol",
        "hi": "hin", "ar": "ara", "tr": "tur"}
# ISO 639-2 has two codes for some languages ("fre"/"fra"); treat them as one.
SYNONYMS = {"fra": "fre", "deu": "ger", "zho": "chi", "nld": "dut", "ces": "cze", "fas": "per", "ell": "gre"}


def lang(track):
    p = track["properties"]
    code = (p.get("language") or "und").lower()
    ietf = (p.get("language_ietf") or "").lower().split("-")[0]
    if ietf in IETF:
        return IETF[ietf]
    return SYNONYMS.get(code, code)


def rules(cfg):
    t = cfg.get("tracks") or {}
    norm = lambda xs: {SYNONYMS.get(x, x) for x in xs}
    return (bool(t.get("enabled", True)), norm(t.get("audio") or ["eng"]), norm(t.get("anime_audio") or ["eng", "jpn"]),
            norm(t.get("subtitles") or ["eng"]))


def plan(info, anime, cfg):
    """Which track ids to keep. Returns None when nothing would change."""
    _, audio_langs, anime_langs, sub_langs = rules(cfg)
    tracks = info.get("tracks", [])
    audio = [t for t in tracks if t["type"] == "audio"]
    subs = [t for t in tracks if t["type"] == "subtitles"]
    wanted_audio = anime_langs if anime else audio_langs
    keep_audio = [t for t in audio if lang(t) in wanted_audio or lang(t) == "und"]
    if not any(lang(t) in wanted_audio for t in audio):
        keep_audio = audio  # foreign-language film: don't strip its only soundtrack
    keep_subs = [t for t in subs if lang(t) in sub_langs or lang(t) == "und"]
    if len(keep_audio) == len(audio) and len(keep_subs) == len(subs):
        return None
    # If the default audio track is being removed, make the first kept one the default.
    default = next((t for t in keep_audio if t["properties"].get("default_track")), None)
    return {"audio": keep_audio, "subs": keep_subs, "new_default": None if default else (keep_audio[0] if keep_audio else None),
            "removed_audio": len(audio) - len(keep_audio), "removed_subs": len(subs) - len(keep_subs),
            "audio_total": len(audio), "subs_total": len(subs)}


def tag_ns(track):
    """A track's length in ns from its statistics tag ("02:36:31.758000000"), or 0 if absent."""
    d = track["properties"].get("tag_duration")
    if not d:
        return 0
    h, m, s = d.split(":")
    return int((int(h) * 3600 + int(m) * 60 + float(s)) * 1e9)


def mkvmerge(cfg):
    return (cfg.get("tracks") or {}).get("mkvmerge") or "mkvmerge"


def identify(cfg, path):
    out = subprocess.run([mkvmerge(cfg), "-J", str(path)], capture_output=True, text=True, encoding="utf-8",
                         creationflags=stackconf.NO_WINDOW)
    return json.loads(out.stdout)


def available(cfg):
    try:
        subprocess.run([mkvmerge(cfg), "--version"], capture_output=True, creationflags=stackconf.NO_WINDOW, timeout=30)
        return True
    except Exception:
        return False


def clean(cfg, path, anime, dry_run=False):
    """Returns a short description of what happened, or None if the file was already clean."""
    path = Path(path)
    info = identify(cfg, path)
    p = plan(info, anime, cfg)
    if not p:
        return None
    summary = (f"removed {p['removed_audio']} of {p['audio_total']} audio and "
               f"{p['removed_subs']} of {p['subs_total']} subtitle tracks")
    if dry_run:
        return summary
    # Same drive as the library (so the final rename is instant), outside Jellyfin's folders.
    work = Path(cfg["downloads_complete"]).parent / ".remux"
    work.mkdir(parents=True, exist_ok=True)
    tmp = work / (path.stem + ".remux.mkv")
    cmd = [mkvmerge(cfg), "-q", "-o", str(tmp)]
    cmd += ["--audio-tracks", ",".join(str(t["id"]) for t in p["audio"])] if p["audio"] else ["--no-audio"]
    cmd += ["--subtitle-tracks", ",".join(str(t["id"]) for t in p["subs"])] if p["subs"] else ["--no-subtitles"]
    if p["new_default"]:
        cmd += ["--default-track-flag", f"{p['new_default']['id']}:1"]
    cmd.append(str(path))
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", creationflags=stackconf.NO_WINDOW)
    if r.returncode not in (0, 1):  # 1 = finished with warnings
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"mkvmerge failed: {(r.stdout or r.stderr)[-300:]}")
    out = identify(cfg, tmp)
    want = len(p["audio"]) + len(p["subs"])
    got = sum(1 for t in out.get("tracks", []) if t["type"] in ("audio", "subtitles"))
    dur_out = out.get("container", {}).get("properties", {}).get("duration") or 0
    # A file is as long as its longest track, and a removed dub can run minutes past the movie,
    # so compare against the longest track that was kept (video included).
    kept_ids = {t["id"] for t in p["audio"] + p["subs"]} | {t["id"] for t in info["tracks"] if t["type"] == "video"}
    kept_durs = [tag_ns(t) for t in info["tracks"] if t["id"] in kept_ids]
    dur_in = max(kept_durs) if kept_durs and all(kept_durs) else (info.get("container", {}).get("properties", {}).get("duration") or 0)
    if got != want or (dur_in and abs(dur_out - dur_in) > 2_000_000_000):  # >2 s difference
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"verification failed ({got}/{want} tracks, duration {dur_in}->{dur_out})")
    os.replace(tmp, path)  # swaps the library entry only; the torrent's hardlinked copy is untouched
    return summary


def notify_apps(path, cfg):
    """Tell Sonarr/Radarr (media info) and Jellyfin (track list) that the file changed."""
    import arr
    path = Path(path)
    for app_id, client_cls, kind in (("sonarr", arr.Sonarr, "series"), ("radarr", arr.Radarr, "movie")):
        if not stackconf.has(cfg, app_id):
            continue
        client = client_cls(stackconf.app(cfg, app_id))
        try:
            for item in client.get(kind):
                if item.get("path") and Path(item["path"]) in path.parents:
                    name = "RescanSeries" if kind == "series" else "RescanMovie"
                    client.post("command", {"name": name, f"{kind}Id": item["id"]})
                    break
        except Exception:
            pass
    if stackconf.key(cfg, "jellyfin"):
        try:
            requests.post(stackconf.url(cfg, "jellyfin", "/Library/Media/Updated"), timeout=10,
                          headers=stackconf.jellyfin_headers(cfg),
                          json={"Updates": [{"Path": str(path), "UpdateType": "Modified"}]})
        except Exception:
            pass


def sweep(cfg, db, dry_run=False):
    """Process every library MKV that hasn't been processed in its current form."""
    enabled = rules(cfg)[0]
    if not enabled or not available(cfg):
        return []
    lib = cfg["library"]
    anime_roots = [Path(lib["anime"]), Path(lib["anime_movies"])]
    con = db()
    con.execute("CREATE TABLE IF NOT EXISTS tracks(path TEXT PRIMARY KEY, size INTEGER, mtime REAL, result TEXT, ts REAL)")
    con.commit()
    done = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, size, mtime FROM tracks")}
    results = []
    for root in map(Path, lib.values()):
        for f in sorted(root.rglob("*.mkv")) if root.exists() else []:
            st = f.stat()
            key = str(f).lower()
            if done.get(key) == (st.st_size, st.st_mtime) or time.time() - st.st_mtime < 180:
                continue  # already handled, or still being written
            anime = any(r in f.parents for r in anime_roots)
            try:
                result = clean(cfg, f, anime, dry_run)
            except PermissionError:
                continue  # in use (e.g. being played) - try again next pass
            except Exception as e:
                result = f"error: {e}"
                if log:
                    log.warning("Track cleanup failed for %s: %s", f, e)
            results.append((f, result))
            if dry_run:
                continue
            if result and not result.startswith("error"):
                if log:
                    log.info("Cleaned tracks in %s: %s", f.name, result)
                notify_apps(f, cfg)
                stackconf.hub_hook(cfg, f"Removed unwanted languages: {f.stem}", result)
            st = f.stat()
            con.execute("REPLACE INTO tracks VALUES(?,?,?,?,?)",
                        (key, st.st_size, st.st_mtime, result or "already clean", time.time()))
            con.commit()
    con.close()
    return results
