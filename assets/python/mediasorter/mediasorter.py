"""MediaSorter - sorts finished downloads into the Jellyfin library (part of Media Stack Setup).

  mediasorter.py import PATH [--category C] [--hash H] [--name N]
      Called by qBittorrent when a torrent finishes (PATH = content path).
  mediasorter.py watch
      Long-running: watches the Drop folder and periodically sweeps the
      completed-downloads folder for anything that was missed.
  mediasorter.py sweep        one pass of the above
  mediasorter.py status       show recent results
  mediasorter.py retry        re-process everything that failed

Movies go through Radarr and series through Sonarr (manual import with a forced
match), which rename/hardlink into the library and notify Bazarr for subtitles.
If they can't place a file, MediaSorter renames it itself.
"""
import argparse
import json
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import requests
from guessit import guessit

import arr
import stackconf
import tracks

if stackconf.WINDOWS:
    import msvcrt
else:
    import fcntl

HERE = Path(__file__).resolve().parent
DATA = None  # this process's state folder (set by setup_logging)
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts", ".webm", ".mov", ".wmv", ".mpg", ".mpeg", ".flv"}
SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup"}
EXTRA_DIRS = re.compile(r"^(extras?|featurettes?|behind the scenes|deleted scenes|interviews?|bonus|specials? features?|ncop|nced|menus?|samples?)$", re.I)
FANSUB_TAG = re.compile(r"^\s*\[[^\]]+\]")

log = logging.getLogger("mediasorter")


def setup_logging(cfg, verbose=False):
    global DATA
    DATA = stackconf.own_state(cfg, "state_dir")
    (DATA / "logs").mkdir(exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(DATA / "logs" / "mediasorter.log", maxBytes=5_000_000,
                                              backupCount=5, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s [%(process)d] %(levelname)s %(message)s")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout and sys.stdout.isatty() or verbose:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.setLevel(logging.INFO)
    arr.log = log
    tracks.log = log


def load_config():
    return stackconf.load()


# --------------------------------------------------------------------------- state

def db():
    DATA.mkdir(exist_ok=True)
    con = sqlite3.connect(DATA / "state.db", timeout=60)
    con.execute("""CREATE TABLE IF NOT EXISTS items(
        key TEXT PRIMARY KEY, path TEXT, category TEXT, status TEXT, detail TEXT, ts REAL)""")
    return con


def item_key(path, torrent_hash=None):
    return (torrent_hash or "").lower() or str(Path(path).resolve()).lower()


def record(key, path, category, status, detail=""):
    with db() as con:
        con.execute("REPLACE INTO items VALUES(?,?,?,?,?,?)", (key, str(path), category, status, detail, time.time()))


def seen(key):
    with db() as con:
        row = con.execute("SELECT status FROM items WHERE key=?", (key,)).fetchone()
    return row and row[0] in ("done", "partial", "ignored")


@contextmanager
def global_lock():
    """Serialise imports - qBittorrent can start several of us at once."""
    DATA.mkdir(exist_ok=True)
    fh = open(DATA / "import.lock", "a+")
    while True:
        try:
            fh.seek(0)
            if stackconf.WINDOWS:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            time.sleep(1)
    try:
        yield
    finally:
        fh.seek(0)
        if stackconf.WINDOWS:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


# --------------------------------------------------------------------------- parsing

def video_files(path, min_mb):
    path = Path(path)
    files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
    out = []
    for f in files:
        if f.suffix.lower() not in VIDEO_EXT:
            continue
        rel_parts = f.relative_to(path).parts[:-1] if path.is_dir() else ()
        if any(EXTRA_DIRS.match(p) for p in rel_parts):
            continue
        if re.search(r"(^|[\W_])sample([\W_]|$)", f.stem, re.I) and f.stat().st_size < 500 * 2**20:
            continue
        if f.stat().st_size < min_mb * 2**20:
            continue
        out.append(f)
    return sorted(out)


def classify(f, root_name, category):
    """Return (kind, title, year, is_anime_hint) for one video file."""
    g = guessit(f.name)
    gt = guessit(root_name) if root_name else {}
    title = g.get("title")
    # File names like "01.mkv" or "Episode 5.mkv" carry no title - use the torrent name.
    if not title or len(arr.norm(title)) < 2 or re.fullmatch(r"(episode|ep|e)?\s*\d+", arr.norm(title)):
        title = gt.get("title") or Path(root_name or f.stem).stem
    year = g.get("year") or gt.get("year")
    kind = g.get("type") or gt.get("type") or "movie"
    if gt.get("type") == "episode" or gt.get("season") is not None:
        kind = "episode"
    if category == "movies":
        kind = "movie"
    elif category == "tv":
        kind = "episode"
    anime_hint = category == "anime" or bool(FANSUB_TAG.match(f.name) or FANSUB_TAG.match(root_name or ""))
    if isinstance(title, list):
        title = title[0]
    country = g.get("country") or gt.get("country")
    if kind == "episode" and country:
        code = str(getattr(country, "alpha2", country))
        title = f"{title} {'UK' if code == 'GB' else code}"  # "The.Office.US" -> "The Office US"
    if kind == "movie" and g.get("part"):
        title = f"{title} Part {g['part']}"  # "Dune.Part.Two" parses as title=Dune, part=2
    if isinstance(year, list):
        year = year[0]
    return kind, str(title), year, anime_hint, g


# --------------------------------------------------------------------------- fallback renamer

def safe(name):
    name = re.sub(r'[<>:"/\\|?*]', "", name).strip().rstrip(".")
    return re.sub(r"\s+", " ", name)


def place(src, dst, mode):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        log.info("Fallback: %s already exists, skipping", dst)
        return dst
    if mode == "move":
        shutil.move(str(src), str(dst))
    else:
        try:
            os.link(src, dst)  # hardlink: no extra space, torrent keeps seeding
        except OSError:
            shutil.copy2(src, dst)
    # Carry matching subtitle files along (e.g. Movie.en.srt next to Movie.mkv).
    for sub in src.parent.glob(src.stem + "*"):
        if sub.suffix.lower() in SUB_EXT:
            sdst = dst.with_name(dst.stem + sub.name[len(src.stem):])
            if not sdst.exists():
                (shutil.move if mode == "move" else shutil.copy2)(str(sub), str(sdst))
    log.info("Fallback: %s -> %s", src, dst)
    return dst


def fallback_movie(f, title, year, anime, cfg, mode, movie=None):
    if movie:
        folder = Path(movie["path"])
        title, year = movie["title"], movie.get("year")
    else:
        base = cfg["library"]["anime_movies" if anime else "movies"]
        folder = Path(base) / safe(f"{title} ({year})" if year else title)
    name = safe(f"{title} ({year})" if year else title) + f.suffix.lower()
    return place(f, folder / name, mode)


def fallback_episode(f, g, title, anime, cfg, mode, series=None):
    season = g.get("season")
    episode = g.get("episode")
    if isinstance(season, list):
        season = season[0]
    if episode is None:
        return None
    if season is None:
        season = 1  # absolute-numbered anime: Jellyfin's anime matching copes with S01Exxx
    eps = episode if isinstance(episode, list) else [episode]
    if series:
        folder = Path(series["path"])
        title = series["title"]
    else:
        folder = Path(cfg["library"]["anime" if anime else "tv"]) / safe(title)
    ep_tag = "".join(f"E{e:02d}" for e in eps)
    name = safe(f"{title} - S{int(season):02d}{ep_tag}") + f.suffix.lower()
    return place(f, folder / f"Season {int(season):02d}" / name, mode)


# --------------------------------------------------------------------------- import

class Sorter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sonarr = arr.Sonarr(stackconf.app(cfg, "sonarr"))
        self.radarr = arr.Radarr(stackconf.app(cfg, "radarr"))
        self.sonarr_ok = self.sonarr.available()
        self.radarr_ok = self.radarr.available()
        self.fallback_used = False  # Sonarr/Radarr notify Jellyfin themselves; we only do so for our own placements
        if not self.sonarr_ok:
            log.warning("Sonarr not reachable - using built-in renamer for series")
        if not self.radarr_ok:
            log.warning("Radarr not reachable - using built-in renamer for movies")

    def run(self, path, category="", torrent_hash="", name="", mode="copy"):
        path = Path(path)
        self.fallback_used = False
        category = (category or "").strip().lower()
        if category in self.cfg.get("managed_categories", []):
            log.info("Skipping %s: category %r is imported by Sonarr/Radarr themselves", path, category)
            return
        if category not in ("movies", "tv", "anime"):
            category = ""
        key = item_key(path, torrent_hash)
        if not path.exists():
            log.error("Path does not exist: %s", path)
            record(key, path, category, "failed", "path missing")
            return
        root_name = name or path.name
        files = video_files(path, self.cfg["min_video_mb"])
        log.info("=== Importing %s (category=%s, %d video files, mode=%s)", path, category or "auto", len(files), mode)
        if not files and path.is_dir() and any(p.suffix.lower() in (".rar", ".zip", ".7z") or re.fullmatch(r"\.r\d\d", p.suffix.lower())
                                               for p in path.rglob("*")):
            # Unpackerr extracts these in place; leave it un-recorded so the sweeper retries.
            log.info("Only archives in %s - waiting for Unpackerr to extract them", path)
            return
        if not files:
            record(key, path, category, "ignored", "no video files")
            log.info("No video files in %s - ignored", path)
            return

        groups = {}
        for f in files:
            kind, title, year, hint, g = classify(f, root_name, category)
            groups.setdefault((kind, arr.norm(title), year if kind == "movie" else None), []).append((f, title, year, hint, g))

        done, failed = [], []
        for (kind, _, _), members in groups.items():
            try:
                if kind == "episode":
                    ok, bad = self.import_series(members, mode)
                else:
                    ok, bad = self.import_movies(members, mode)
            except Exception as e:
                log.exception("Import error: %s", e)
                ok, bad = [], [m[0] for m in members]
            done += ok
            failed += bad

        status = "done" if not failed else ("partial" if done else "failed")
        detail = f"{len(done)} imported" + (f", {len(failed)} failed: " + "; ".join(p.name for p in failed) if failed else "")
        record(key, path, category, status, detail)
        log.info("=== %s: %s", status.upper(), detail)
        if mode == "move" and path.is_dir() and not failed:
            shutil.rmtree(path, ignore_errors=True)
        if self.fallback_used:
            self.refresh_jellyfin()

    def import_series(self, members, mode):
        f0, title, year, hint, g0 = members[0]
        files = [m[0] for m in members]
        series, anime = None, hint
        if self.sonarr_ok:
            found = self.sonarr.lookup(title, year, hint)
            if found:
                anime = hint or arr.looks_anime(found)
                series, _ = self.sonarr.ensure_series(found, anime, self.cfg["library"])
                if series.get("seriesType") == "anime":
                    anime = True
                log.info("Matched %r -> %s (%s)", title, series["title"], "anime" if anime else "tv")
                ok = self.sonarr.import_files(series, files, mode)
                done = [f for f in files if str(f).lower() in ok]
                rest = [m for m in members if str(m[0]).lower() not in ok]
            else:
                log.warning("Sonarr found nothing for %r", title)
                done, rest = [], members
        else:
            done, rest = [], members
        bad = []
        for f, t, _, _, g in rest:
            self.fallback_used = True
            if fallback_episode(f, g, t, anime, self.cfg, mode, series):
                done.append(f)
            else:
                bad.append(f)
        if series and rest:
            self.sonarr.command({"name": "RescanSeries", "seriesId": series["id"]}, wait=False)
        return done, bad

    def import_movies(self, members, mode):
        done, bad = [], []
        for f, title, year, hint, g in members:
            movie, anime = None, hint
            if self.radarr_ok:
                found = self.radarr.lookup(title, year, hint)
                if found:
                    anime = hint or arr.looks_anime(found)
                    movie, _ = self.radarr.ensure_movie(found, anime, self.cfg["library"])
                    log.info("Matched %r -> %s (%s)", title, movie["title"], movie.get("year"))
                    if self.radarr.import_file(movie, f, mode):
                        done.append(f)
                        continue
                else:
                    log.warning("Radarr found nothing for %r (%s)", title, year)
            fallback_movie(f, title, year, anime, self.cfg, mode, movie)
            self.fallback_used = True
            done.append(f)
            if movie:
                self.radarr.command({"name": "RescanMovie", "movieId": movie["id"]}, wait=False)
        return done, bad

    def refresh_jellyfin(self):
        if not stackconf.key(self.cfg, "jellyfin"):
            return  # Jellyfin's real-time folder monitoring picks changes up on its own
        try:
            # Jellyfin 12 rejects the legacy X-Emby-Token header.
            requests.post(stackconf.url(self.cfg, "jellyfin", "/Library/Refresh"),
                          headers=stackconf.jellyfin_headers(self.cfg), timeout=10)
        except Exception as e:
            log.warning("Jellyfin refresh failed: %s", e)


# --------------------------------------------------------------------------- sweep / watch

def candidates(cfg):
    """(path, category, mode) for every top-level item awaiting import."""
    out = []
    root = Path(cfg["downloads_complete"])
    for cat_dir in root.iterdir() if root.exists() else []:
        if cat_dir.is_dir():
            cat = cat_dir.name.lower()
            if cat in cfg.get("managed_categories", []):
                continue
            for p in cat_dir.iterdir():
                out.append((p, cat if cat in ("movies", "tv", "anime") else "", "copy"))
    drop = Path(cfg["drop_folder"])
    settle = cfg["drop_settle_seconds"]
    for p in drop.iterdir() if drop.exists() else []:
        newest = max([p.stat().st_mtime] + [c.stat().st_mtime for c in p.rglob("*")] if p.is_dir() else [p.stat().st_mtime])
        if time.time() - newest >= settle:
            cat = ""
            if p.is_dir() and p.name.lower() in ("movies", "tv", "anime"):
                for c in p.iterdir():
                    out.append((c, p.name.lower(), "move"))
                continue
            out.append((p, cat, "move"))
    return out


def sweep(cfg):
    todo = [(p, c, m) for p, c, m in candidates(cfg) if m == "move" or not seen(item_key(p))]
    if not todo:
        return
    with global_lock():
        sorter = Sorter(cfg)
        for p, cat, mode in todo:
            if p.exists() and (mode == "move" or not seen(item_key(p))):
                sorter.run(p, cat, mode=mode)


def cmd_import(args, cfg):
    p = Path(args.path)
    with global_lock():
        if seen(item_key(p)):
            log.info("Already handled by the sweeper: %s", p)
            return
        Sorter(cfg).run(p, args.category, args.hash, args.name, mode="copy")
    # Also mark the bare path as handled so the sweeper doesn't redo it.
    with db() as con:
        row = con.execute("SELECT status, detail FROM items WHERE key=?", (item_key(p, args.hash),)).fetchone()
    if row and args.hash:
        record(item_key(p), p, args.category, row[0], row[1])


class VpnWatchdog:
    """Windows: qBittorrent is bound to the VPN app's network adapter. Windows can renumber that adapter
    when VPN adapters are recreated (keep the binding right), and if the VPN connects after qBittorrent
    started (e.g. at boot), libtorrent can stay stuck with no peers; a restart makes it attach properly."""

    def __init__(self, cfg):
        self.up_since = None
        self.last_restart = 0
        vpn = cfg.get("vpn") or {}
        self.alias = vpn.get("adapter") if vpn.get("mode") == "app" else None

    @staticmethod
    def adapter_name(alias):
        """Windows' internal adapter name (e.g. iftype53_32771) - what qBittorrent binds to."""
        import ctypes
        iphlp = ctypes.WinDLL("iphlpapi")
        luid = ctypes.c_uint64()
        if iphlp.ConvertInterfaceAliasToLuid(ctypes.c_wchar_p(alias), ctypes.byref(luid)) != 0:
            return None
        buf = ctypes.create_unicode_buffer(256)
        iphlp.ConvertInterfaceLuidToNameW(ctypes.byref(luid), buf, 256)
        return buf.value

    def fix_binding(self, cfg, psutil):
        """Keep qBittorrent locked to the VPN adapter (by adapter and by its tunnel address)."""
        name = self.adapter_name(self.alias)
        addrs = [a.address for a in psutil.net_if_addrs().get(self.alias, []) if a.family == 2]
        if not name or not addrs:
            return
        try:
            prefs = stackconf.qbt_get(cfg, "/app/preferences", timeout=5)
        except Exception:
            return
        want = {"current_network_interface": name, "current_interface_address": addrs[0]}
        if all(prefs.get(k) == v for k, v in want.items()):
            return
        log.warning("qBittorrent was bound to %s/%s; re-binding to %s %s/%s", prefs.get("current_network_interface"),
                    prefs.get("current_interface_address"), self.alias, name, addrs[0])
        stackconf.qbt_post(cfg, "/app/setPreferences", data={"json": json.dumps(want)}, timeout=5)

    def check(self, cfg):
        if not stackconf.WINDOWS or not self.alias or not stackconf.has(cfg, "qbittorrent"):
            return
        import psutil  # only the watcher needs it
        nic = psutil.net_if_stats().get(self.alias)
        if not (nic and nic.isup):
            self.up_since = None  # VPN off: torrents are meant to be idle
            return
        self.fix_binding(cfg, psutil)
        self.up_since = self.up_since or time.time()
        if time.time() - self.up_since < 60 or time.time() - self.last_restart < 600:
            return
        try:
            info = stackconf.qbt_get(cfg, "/transfer/info", timeout=5)
        except Exception:
            return  # qBittorrent closed or its API is off: nothing to fix here
        if info.get("dht_nodes", 0) > 0 or info.get("connection_status") == "connected":
            return
        log.warning("VPN is up but qBittorrent has no peers - restarting qBittorrent to re-attach to the VPN")
        try:
            stackconf.qbt_post(cfg, "/app/shutdown", timeout=5)
            time.sleep(6)
        except Exception:
            pass
        stackconf.kill_own(cfg, "qbittorrent.exe")  # only this install's copy, never another qBittorrent
        stackconf.restart_task(cfg, "qBittorrent")
        self.last_restart = time.time()
        stackconf.hub_hook(cfg, "Reconnected qBittorrent to the VPN",
                           "The VPN came up after qBittorrent started, so qBittorrent was restarted to use it.")


def track_cleaner_loop():
    """Background thread: strip unwanted languages from new library files (remuxes can take minutes,
    so this runs beside the import sweep rather than inside it)."""
    while True:
        try:
            tracks.sweep(load_config(), db)
        except Exception:
            log.exception("Track cleanup sweep failed")
        time.sleep(300)


def cmd_watch(cfg):
    log.info("Watcher started (drop=%s, sweep every %ss)", cfg["drop_folder"], cfg["sweep_interval_seconds"])
    last_sweep, last_retry = 0, time.time()
    vpn = VpnWatchdog(cfg)
    threading.Thread(target=track_cleaner_loop, daemon=True).start()
    while True:
        try:
            cfg = load_config()
            # The drop folder is checked every 15s, the download folders on the sweep interval.
            drop = Path(cfg["drop_folder"])
            if time.time() - last_sweep >= cfg["sweep_interval_seconds"]:
                sweep(cfg)
                last_sweep = time.time()
            elif drop.exists() and any(drop.iterdir()):
                sweep(cfg)
            # The Media Hub's "Retry failed imports" button and the health check leave this flag;
            # failed imports are also retried every 6 hours on their own.
            flag = DATA / "retry.flag"
            if flag.exists() or time.time() - last_retry > 6 * 3600:
                flag.unlink(missing_ok=True)
                last_retry = time.time()
                cmd_retry(cfg)
        except Exception:
            log.exception("Sweep failed")
        try:
            vpn.check(cfg)
        except Exception:
            log.exception("VPN watchdog failed")
        time.sleep(15)


def cmd_status(n=30):
    with db() as con:
        rows = con.execute("SELECT ts, status, category, path, detail FROM items ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
    for ts, st, cat, path, detail in rows:
        print(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))}  {st:8} {cat or 'auto':7} {Path(path).name}  [{detail}]")


def cmd_retry(cfg):
    with db() as con:
        rows = con.execute("SELECT key, path, category FROM items WHERE status IN ('failed','partial')").fetchall()
    if not rows:
        return
    with global_lock():
        sorter = Sorter(cfg)
        for key, path, cat in rows:
            sorter.run(path, cat, key if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", key) else "")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("import")
    pi.add_argument("path")
    pi.add_argument("--category", default="")
    pi.add_argument("--hash", default="")
    pi.add_argument("--name", default="")
    sub.add_parser("watch")
    sub.add_parser("sweep")
    sub.add_parser("status")
    sub.add_parser("retry")
    pt = sub.add_parser("tracks", help="strip audio & subtitle languages you don't keep (see mediastack.json)")
    pt.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    setup_logging(cfg, verbose=args.cmd in ("sweep", "retry", "tracks"))
    try:
        if args.cmd == "import":
            cmd_import(args, cfg)
        elif args.cmd == "watch":
            cmd_watch(cfg)
        elif args.cmd == "sweep":
            sweep(cfg)
        elif args.cmd == "status":
            cmd_status()
        elif args.cmd == "retry":
            cmd_retry(cfg)
        elif args.cmd == "tracks":
            for f, result in tracks.sweep(cfg, db, dry_run=args.dry_run) or []:
                print(f"{'[dry run] ' if args.dry_run else ''}{f.name}: {result or 'already clean'}")
    except Exception:
        log.exception("Fatal error")
        raise


if __name__ == "__main__":
    main()
