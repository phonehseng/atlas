"""The local catalog: Marquee's own copy of the library, so every page and every play works from this PC alone.

Jellyfin is only a background source. A snapshot of all films, shows, seasons and episodes (names, artwork
references, ratings, genres, cast, file paths, intro/credits segments) is kept in %APPDATA%\\Marquee\\catalog.db and
refreshed every 30 minutes and whenever Jellyfin reports a library change. Watch history (played, resume points,
favourites, last played) is owned here and synced both ways with Jellyfin, newest change winning; changes made while
Jellyfin is away wait in the sync queue (dirty rows) and go up as soon as it answers again.

Without Jellyfin (or in addition to it), `library_folders` from the settings are scanned: file names are read with
guessit, grouped into films / shows / seasons / episodes (ids start with "local:"), and matched to TMDB through
Seerr for artwork and descriptions when Seerr is reachable.

Artwork is served by the UI server from a local cache (cache\\images, least recently used pruned past ~1.5 GB):
URLs in cards always point at /img/..., which is filled from Jellyfin or TMDB the first time and from disk after.
"""
import hashlib
import json
import os
import random
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import settings

TICKS = 10_000_000
DB_FILE = settings.DIR / "catalog.db"
IMG_DIR = settings.DIR / "cache" / "images"
IMG_LIMIT = 1536 * 2**20
SNAPSHOT_EVERY = 30 * 60
SCAN_EVERY = 30 * 60
SEGMENTS_MAX_AGE = 7 * 86400
VIDEO = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".webm", ".wmv", ".mpg", ".mpeg"}
MIN_VIDEO = 20 * 2**20
SKIP_DIRS = {"extras", "featurettes", "behind the scenes", "deleted scenes", "interviews", "scenes", "shorts",
             "trailers", "samples", "sample", "other", "$recycle.bin", "system volume information"}
TMDB_IMG = "https://image.tmdb.org/t/p/"
SNAP_FIELDS = ("Overview,Genres,ProductionYear,OfficialRating,CommunityRating,RunTimeTicks,UserData,Path,ProviderIds,"
               "DateCreated,PremiereDate,SortName,Taglines,Studios,People,MediaStreams,ChildCount,CriticRating,RemoteTrailers")
SNAP_TYPES = "Movie,Series,Season,Episode"
GENRES = ["Action", "Comedy", "Science Fiction", "Animation", "Drama", "Adventure", "Thriller", "Fantasy"]

# The artwork sizes the UI asks for (keep in step with what the page shows; cache keys depend on them)
SLOTS = {
    "poster": {"fillHeight": 600, "quality": 90},
    "backdrop": {"fillWidth": 1920, "quality": 90},  # full HD at least: the hero and title page fill the window
    "thumb": {"fillWidth": 640, "quality": 85},   # an episode's own still
    "thumbart": {"fillWidth": 640},               # a film's/show's Thumb artwork
    "logo": {"maxWidth": 700},
    "person": {"fillHeight": 240},
}
IMG_PARAMS = {"fillHeight", "fillWidth", "maxWidth", "maxHeight", "quality"}


# ------------------------------------------------------------------ small helpers
def jf_img(item_id, kind, slot):
    if not item_id:
        return None
    return f"/img/Items/{item_id}/Images/{kind}?" + "&".join(f"{k}={v}" for k, v in SLOTS[slot].items())


def tmdb_img(url):
    """A TMDB artwork URL as a local cached one."""
    if url and url.startswith(TMDB_IMG):
        return "/img/tmdb/" + url[len(TMDB_IMG):]
    return url


def images_for(i):
    """(poster, backdrop, thumb, logo) local URLs for a Jellyfin item dict, the same choices the cards make."""
    tags = i.get("ImageTags") or {}
    series = i.get("SeriesId")
    poster = None
    if tags.get("Primary"):
        poster = jf_img(i["Id"], "Primary", "poster")
    elif series and i.get("SeriesPrimaryImageTag"):
        poster = jf_img(series, "Primary", "poster")
    backdrop = None
    if i.get("BackdropImageTags"):
        backdrop = jf_img(i["Id"], "Backdrop", "backdrop")
    elif i.get("ParentBackdropImageTags"):
        backdrop = jf_img(i.get("ParentBackdropItemId") or series, "Backdrop", "backdrop")
    if i.get("Type") == "Episode":
        thumb = jf_img(i["Id"], "Primary", "thumb") if tags.get("Primary") else backdrop
    else:
        thumb = jf_img(i["Id"], "Thumb", "thumbart") if tags.get("Thumb") else backdrop
    logo = None
    if tags.get("Logo"):
        logo = jf_img(i["Id"], "Logo", "logo")
    elif i.get("ParentLogoImageTag") and (i.get("ParentLogoItemId") or series):
        logo = jf_img(i.get("ParentLogoItemId") or series, "Logo", "logo")
    return poster, backdrop, thumb, logo


def ts(s):
    """Jellyfin date string -> epoch seconds (0 when missing)."""
    if not s:
        return 0.0
    try:
        t = datetime.fromisoformat(s.rstrip("Z")[:26]).replace(tzinfo=timezone.utc).timestamp()
        return t if t > 0 else 0.0
    except (ValueError, OverflowError, OSError):
        return 0.0


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ") if t else None


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def slug(s):
    return norm(s).replace(" ", "-") or "untitled"


def pathkey(p):
    return os.path.normcase(os.path.normpath(p)) if p else ""


def short_hash(s, n=16):
    return hashlib.sha1(s.encode("utf-8", "surrogatepass")).hexdigest()[:n]


def media_info(streams, path=None):
    """Quality facts from Jellyfin's MediaStreams: resolution, dynamic range (HDR10, HDR10+, Dolby Vision, HLG),
    and audio (Atmos, DTS:X, channels). {} when there's no video stream."""
    v = next((s for s in streams if s.get("Type") == "Video"), None)
    if not v:
        return {}
    rng = v.get("VideoRangeType") or v.get("VideoRange") or "SDR"
    audio = [s for s in streams if s.get("Type") == "Audio"]
    text = " ".join(((s.get("Profile") or "") + " " + (s.get("DisplayTitle") or "") + " " + (s.get("Title") or ""))
                    for s in audio).lower()
    return {"codec": (v.get("Codec") or "").lower(), "height": v.get("Height"), "width": v.get("Width"),
            "bits": v.get("BitDepth"), "range": rng, "hdr": rng not in ("SDR", "Unknown"),
            "dv": "DOVI" in rng, "hdr10plus": "HDR10Plus" in rng, "hlg": "HLG" in rng,
            "atmos": "atmos" in text, "dtsx": "dts:x" in text or "dts-x" in text,
            "channels": max([s.get("Channels") or 0 for s in audio] or [0]),
            "container": os.path.splitext(path or "")[1].lower().lstrip(".")}


def _ud_from_jf(u):
    u = u or {}
    return {"played": int(bool(u.get("Played"))), "position": round((u.get("PlaybackPositionTicks") or 0) / TICKS, 1),
            "favorite": int(bool(u.get("IsFavorite"))), "last_played": ts(u.get("LastPlayedDate")),
            "play_count": int(u.get("PlayCount") or 0)}


UD_KEYS = ("played", "position", "favorite", "last_played", "play_count")


# ------------------------------------------------------------------ image cache
class Images:
    """Artwork on disk. get_* return a local file path (fetching and caching it when it's missing), or None."""

    def __init__(self, catalog):
        self.cat = catalog
        self.dir = IMG_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.total = None
        self.http = requests.Session()

    @staticmethod
    def _safe(s):
        return re.sub(r"[^A-Za-z0-9._-]", "_", s)[:80]

    def _account(self, delta):
        with self.lock:
            if self.total is None:
                self.total = sum(f.stat().st_size for f in self.dir.iterdir() if f.is_file())
            self.total += delta
            over = self.total > IMG_LIMIT
        if over:
            self.prune()

    def prune(self, target=0.85):
        with self.lock:
            files = []
            for f in self.dir.iterdir():
                try:
                    st = f.stat()
                    files.append((st.st_mtime, st.st_size, f))
                except OSError:
                    pass
            total = sum(s for _, s, _ in files)
            files.sort()
            try:
                pinned = self.cat.pinned_images()
            except Exception:
                pinned = set()
            for _, size, f in files:
                if total <= IMG_LIMIT * target:
                    break
                if f.name in pinned:  # artwork of removed titles (their cards still show)
                    continue
                try:
                    f.unlink()
                    total -= size
                except OSError:
                    pass
            self.total = total

    def usage(self):
        if self.total is None:
            self._account(0)
        return self.total

    @staticmethod
    def _touch(p):
        try:
            if time.time() - p.stat().st_mtime > 3600:  # LRU by modification time (not on every hit)
                os.utime(p)
        except OSError:
            pass

    def _store(self, p, data):
        tmp = p.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(p)
        self._account(len(data))

    def _fallback(self, pattern):
        alts = sorted(self.dir.glob(pattern), key=lambda f: f.stat().st_size if f.exists() else 0, reverse=True)
        return alts[0] if alts else None

    def jf_name(self, item_id, kind, params):
        key = "&".join(f"{k}={params[k]}" for k in sorted(params))
        return f"{self._safe(item_id)}_{kind}_{short_hash(key, 10)}.img"

    def get_jf(self, item_id, kind, params, fetch=True):
        p = self.dir / self.jf_name(item_id, kind, params)
        if p.exists():
            self._touch(p)
            return p
        jf = self.cat.app.jf
        if fetch and jf.online and not item_id.startswith("local:"):
            try:
                r = self.http.get(f"{jf.base}/Items/{item_id}/Images/{kind}", params=params, timeout=jf.QUICK,
                                  headers={"Authorization": jf.auth_header()})
                if r.status_code == 200 and r.content:
                    self._store(p, r.content)
                    return p
            except requests.RequestException:
                pass
        return self._fallback(f"{self._safe(item_id)}_{kind}_*.img")  # another size of the same picture

    def get_tmdb(self, size, file, fetch=True):
        size, file = self._safe(size), self._safe(file.lstrip("/"))
        p = self.dir / f"tmdb_{file}_{size}.img"
        if p.exists():
            self._touch(p)
            return p
        if fetch:
            try:
                r = self.http.get(f"{TMDB_IMG}{size}/{file}", timeout=(3, 8))
                if r.status_code == 200 and r.content:
                    self._store(p, r.content)
                    return p
            except requests.RequestException:
                pass
        return self._fallback(f"tmdb_{file}_*.img")

    def get_url(self, url, fetch=True):
        """Resolve one of our own /img/... URLs."""
        m = re.match(r"/img/Items/([^/]+)/Images/(\w+)(?:\?(.*))?$", url or "")
        if m:
            params = dict(kv.split("=", 1) for kv in (m.group(3) or "").split("&") if "=" in kv)
            return self.get_jf(m.group(1), m.group(2), {k: v for k, v in params.items() if k in IMG_PARAMS}, fetch)
        m = re.match(r"/img/tmdb/([^/]+)/(.+)$", url or "")
        if m:
            return self.get_tmdb(m.group(1), m.group(2), fetch)
        return None

    @staticmethod
    def mimetype(p):
        with open(p, "rb") as f:
            head = f.read(12)
        if head[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return "image/webp"
        if head[:4] == b"\x00\x00\x00\x1c" or b"ftyp" in head:
            return "image/avif"
        if head[:3] == b"GIF":
            return "image/gif"
        return "application/octet-stream"


# ------------------------------------------------------------------ catalog
class Catalog:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.items = {}          # id -> row (dict)
        self.ud = {}             # id -> user data (played, position, favorite, last_played, play_count, changed, dirty)
        self.segments = {}       # id -> [{"type", "start", "end"}] (seconds)
        self.meta = {}
        self.wake = threading.Event()
        self.want_snapshot = True
        self.want_scan = True
        self.live_ids = set()
        self.live_events = {}
        self.on_updated = None   # fn(event, data): set by the UI server (tells the page; clears its caches)
        self.images = Images(self)
        self._guess_cache = {}
        self._positions_mtime = 0
        self._started = False
        self._open()
        self._load()
        try:  # Seerr cards for titles that were removed (watched) carry that state
            from .seerr import Seerr
            Seerr.removed_lookup = self.removed_flag
        except Exception:
            pass

    # ------------------------------------------------------------------ storage
    def _open(self):
        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(DB_FILE), check_same_thread=False, timeout=30, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, type TEXT, source TEXT, series_id TEXT,
                season_id TEXT, path TEXT, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS items_path ON items(path);
            CREATE TABLE IF NOT EXISTS userdata (id TEXT PRIMARY KEY, played INTEGER DEFAULT 0,
                position REAL DEFAULT 0, favorite INTEGER DEFAULT 0, last_played REAL DEFAULT 0,
                play_count INTEGER DEFAULT 0, changed REAL DEFAULT 0, dirty INTEGER DEFAULT 0, remote TEXT);
            CREATE TABLE IF NOT EXISTS segments (id TEXT PRIMARY KEY, data TEXT, fetched REAL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS tmdb (query TEXT PRIMARY KEY, data TEXT, fetched REAL);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, data TEXT, fetched REAL);
            CREATE TABLE IF NOT EXISTS mylist (key TEXT PRIMARY KEY, added REAL, data TEXT);
            CREATE TABLE IF NOT EXISTS removed (key TEXT PRIMARY KEY, kind TEXT, tmdb TEXT, season INTEGER,
                episode INTEGER, removed_at REAL, restored_at REAL, data TEXT);
        """)

    def _load(self):
        with self.lock:
            for rid, data in self.db.execute("SELECT id, data FROM items"):
                try:
                    self.items[rid] = json.loads(data)
                except ValueError:
                    pass
            for row in self.db.execute("SELECT id, played, position, favorite, last_played, play_count, changed, "
                                       "dirty, remote FROM userdata"):
                self.ud[row[0]] = {"played": row[1], "position": row[2], "favorite": row[3], "last_played": row[4],
                                   "play_count": row[5], "changed": row[6], "dirty": row[7],
                                   "remote": json.loads(row[8]) if row[8] else None}
            for rid, data, fetched in self.db.execute("SELECT id, data, fetched FROM segments"):
                self.segments[rid] = json.loads(data or "[]")
            self._seg_fetched = {r[0]: r[1] for r in self.db.execute("SELECT id, fetched FROM segments")}
            self.meta = {k: json.loads(v) for k, v in self.db.execute("SELECT key, value FROM meta")}
            self._index()

    def _set_meta(self, **kw):
        with self.lock:
            self.meta.update(kw)
            self.db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [(k, json.dumps(v)) for k, v in kw.items()])

    def _save_ud(self, ids):
        rows = []
        for i in ids:
            u = self.ud.get(i)
            if u:
                rows.append((i, u["played"], u["position"], u["favorite"], u["last_played"], u["play_count"],
                             u.get("changed") or 0, u.get("dirty") or 0,
                             json.dumps(u["remote"]) if u.get("remote") else None))
        if rows:
            self.db.executemany("INSERT OR REPLACE INTO userdata VALUES (?,?,?,?,?,?,?,?,?)", rows)

    def _index(self):
        """Lookup tables over self.items (call with the lock held after any change to items)."""
        self.by_path, self.seasons, self.eps, self.eps_by_season = {}, {}, {}, {}
        for r in self.items.values():
            if r.get("path"):
                self.by_path[pathkey(r["path"])] = r["id"]
            if r["type"] == "Season" and r.get("series_id"):
                self.seasons.setdefault(r["series_id"], []).append(r)
            elif r["type"] == "Episode":
                if r.get("series_id"):
                    self.eps.setdefault(r["series_id"], []).append(r)
                if r.get("season_id"):
                    self.eps_by_season.setdefault(r["season_id"], []).append(r)
        order = lambda r: (r.get("season_no") if r.get("season_no") is not None else 999, r.get("episode_no") or 0,
                           r.get("sort_name") or "")
        for d in (self.eps, self.eps_by_season):
            for v in d.values():
                v.sort(key=order)
        for v in self.seasons.values():
            v.sort(key=lambda r: r.get("season_no") if r.get("season_no") is not None else 999)

    def has_items(self):
        return bool(self.items)

    # ------------------------------------------------------------------ background work
    def start(self):
        if self._started:
            return
        self._started = True
        self.app.jf.listeners.append(self._connection_changed)
        threading.Thread(target=self._loop, daemon=True, name="catalog-sync").start()
        threading.Thread(target=self._warm_loop, daemon=True, name="catalog-images").start()

    def kick(self, snapshot=False, scan=False):
        if snapshot:
            self.want_snapshot = True
        if scan:
            self.want_scan = True
        self.wake.set()

    def _tell(self, event, data=None):
        if self.on_updated:
            try:
                self.on_updated(event, data or {})
            except Exception as e:
                print("catalog update notice failed:", e)

    def _connection_changed(self, up):
        print("jellyfin", "back online" if up else "unreachable: playing from the local catalog", flush=True)
        if up:
            self.kick(snapshot=True)
        self._tell("connection", {"offline": not up, "since": self.app.jf.down_since})

    def _loop(self):
        last_snap = self.meta.get("snapshot_at") or 0
        last_scan = 0
        folders_seen = None
        first = True
        while True:
            if not first:
                self.wake.wait(10)
            first = False
            self.wake.clear()
            jf = self.app.jf
            try:
                self._import_positions()
            except Exception as e:
                print("positions import failed:", e)
            if jf.configured:
                # down: re-check every 10 s; up: a heartbeat when nothing else has talked to it for 30 s
                up = jf.ping() if jf.down_since is not None else jf.fresh_online(30)
                if up:
                    try:
                        self.push()
                        if self.live_ids or self.live_events:
                            self._apply_live()
                        if self.want_snapshot or time.time() - last_snap > SNAPSHOT_EVERY:
                            self.want_snapshot = False
                            self.snapshot()
                            last_snap = time.time()
                            self.push()
                        self._fill_segments(limit=200)
                    except requests.RequestException as e:
                        print("catalog sync interrupted:", e)
                    except Exception as e:
                        print("catalog sync failed:", type(e).__name__, e)
            folders = tuple(settings.load().get("library_folders") or [])
            if folders != folders_seen or self.want_scan or time.time() - last_scan > SCAN_EVERY:
                folders_seen, self.want_scan, last_scan = folders, False, time.time()
                try:
                    if self.scan_folders(folders):
                        self._tell("library-changed", {"source": "folders"})
                except Exception as e:
                    print("folder scan failed:", type(e).__name__, e)

    # ------------------------------------------------------------------ Jellyfin snapshot
    def snapshot(self, quiet=False):
        """Copy the whole library from Jellyfin (one paged pass per library view), merge watch history."""
        jf = self.app.jf
        t0 = time.time()
        views = jf.call("GET", "/UserViews", params={"userId": jf.user}, timeout=30).get("Items", [])
        libs = []
        for v in views:
            name, kind = (v.get("Name") or "").lower(), v.get("CollectionType")
            lib = "anime" if "anime" in name else {"movies": "movies", "tvshows": "shows"}.get(kind, None if kind else "mixed")
            if lib:
                libs.append((0 if lib == "anime" else 1, lib, v["Id"]))
        new, remote_ud = {}, {}
        for _, lib, view in sorted(libs):
            start = 0
            while True:
                page = jf.call("GET", "/Items", timeout=120, params={
                    "userId": jf.user, "ParentId": view, "Recursive": "true", "IncludeItemTypes": SNAP_TYPES,
                    "Fields": SNAP_FIELDS, "EnableImageTypes": "Primary,Backdrop,Logo,Thumb", "StartIndex": start,
                    "Limit": 1000, "SortBy": "SortName"}) or {}
                batch = page.get("Items") or []
                for i in batch:
                    if i["Id"] in new:
                        continue
                    kind_lib = lib if lib != "mixed" else ("movies" if i.get("Type") == "Movie" else "shows")
                    new[i["Id"]] = self._row_from_jf(i, kind_lib)
                    remote_ud[i["Id"]] = _ud_from_jf(i.get("UserData"))
                start += len(batch)
                if not batch or start >= (page.get("TotalRecordCount") or 0):
                    break
        recs = []
        try:
            for rec in jf.recommendations() or []:
                ids = [x["Id"] for x in rec.get("Items") or [] if x["Id"] in new]
                if ids:
                    recs.append({"type": rec.get("RecommendationType"), "base": rec.get("BaselineItemName"), "ids": ids})
        except requests.RequestException:
            pass
        with self.lock:
            for rid, r in new.items():  # keep what was learned on the side (similar titles)
                old = self.items.get(rid)
                if old and old.get("extra", {}).get("similar"):
                    r["extra"]["similar"] = old["extra"]["similar"]
                    r["extra"]["similar_at"] = old["extra"].get("similar_at")
            gone = [rid for rid, r in self.items.items() if r.get("source") == "jellyfin" and rid not in new]
            # a file Jellyfin now knows replaces the folder scan's entry for it
            jf_paths = {pathkey(r["path"]) for r in new.values() if r.get("path")}
            gone += [rid for rid, r in self.items.items() if r.get("source") == "local" and r.get("path")
                     and pathkey(r["path"]) in jf_paths]
            for rid in gone:
                self.items.pop(rid, None)
            self.items.update(new)
            changed = [rid for rid, r in remote_ud.items() if self._merge_ud(rid, r)]
            self.db.execute("BEGIN")
            try:
                if gone:
                    self.db.executemany("DELETE FROM items WHERE id=?", [(g,) for g in gone])
                    self.db.executemany("DELETE FROM userdata WHERE id=?", [(g,) for g in gone if not g.startswith("local:")])
                self.db.executemany("INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?)",
                                    [self._item_tuple(r) for r in new.values()])
                self._save_ud(changed)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            for g in gone:
                self.ud.pop(g, None) if not g.startswith("local:") else None
            self._index()
            secs = round(time.time() - t0, 2)
            self._set_meta(snapshot_at=time.time(), snapshot_secs=secs, recommendations=recs,
                           counts={t: sum(1 for r in self.items.values() if r["type"] == t)
                                   for t in ("Movie", "Series", "Season", "Episode")})
        print(f"catalog: {len(new)} items from Jellyfin in {secs}s", flush=True)
        try:
            self.mylist_reconcile()
        except Exception as e:
            print("my list reconcile failed:", e)
        try:
            self.restore_removed()
        except Exception as e:
            print("removed titles check failed:", e)
        if (gone or changed) and not quiet:
            self._tell("library-changed", {"source": "snapshot"})
        return len(new)

    @staticmethod
    def _item_tuple(r):
        return (r["id"], r["type"], r.get("source"), r.get("series_id"), r.get("season_id"), r.get("path"),
                json.dumps(r, ensure_ascii=False))

    @staticmethod
    def _row_from_jf(i, lib):
        kind = i.get("Type")
        poster, backdrop, thumb, logo = images_for(i)
        media = media_info(i.get("MediaStreams") or [], i.get("Path"))
        people = []
        if kind in ("Movie", "Series"):
            ppl = i.get("People") or []
            crew = [p for p in ppl if p.get("Type") in ("Director", "Creator", "Writer")][:6]
            cast = [p for p in ppl if p.get("Type") in ("Actor", "GuestStar")][:18]
            people = [{"name": p.get("Name"), "role": p.get("Role"), "type": p.get("Type"), "id": p.get("Id"),
                       "photo": jf_img(p["Id"], "Primary", "person") if p.get("PrimaryImageTag") else None}
                      for p in cast + crew]
        trailers = [{"name": t.get("Name") or "Trailer", "url": t.get("Url")} for t in i.get("RemoteTrailers") or []
                    if "youtube.com/watch" in (t.get("Url") or "") or "youtu.be/" in (t.get("Url") or "")][:8]
        return {
            "id": i["Id"], "type": kind, "source": "jellyfin", "library": lib, "name": i.get("Name"),
            "sort_name": i.get("SortName") or i.get("Name"), "series_id": i.get("SeriesId"),
            "series_name": i.get("SeriesName"), "season_id": i.get("SeasonId") if kind == "Episode" else (
                i["Id"] if kind == "Season" else None),
            "season_no": i.get("IndexNumber") if kind == "Season" else i.get("ParentIndexNumber"),
            "episode_no": i.get("IndexNumber") if kind == "Episode" else None, "year": i.get("ProductionYear"),
            "overview": i.get("Overview") or "", "genres": i.get("Genres") or [], "rating": i.get("CommunityRating"),
            "age": i.get("OfficialRating"), "runtime": (i.get("RunTimeTicks") or 0) / TICKS, "path": i.get("Path"),
            "provider": i.get("ProviderIds") or {}, "added": ts(i.get("DateCreated")),
            "premiere": ts(i.get("PremiereDate")), "poster": poster, "backdrop": backdrop, "thumb": thumb,
            "logo": logo, "children": i.get("ChildCount"),
            "extra": {"people": people, "studios": [s.get("Name") for s in i.get("Studios") or []][:6],
                      "tagline": (i.get("Taglines") or [None])[0], "resolution": media.get("height"),
                      "hdr": media.get("hdr", False), "media": media, "critic": i.get("CriticRating"),
                      "trailers": trailers},
        }

    def _merge_ud(self, rid, remote):
        """Merge Jellyfin's user data for one item into ours. Newest wins: a local change that hasn't gone up yet
        stays unless Jellyfin saw a play after it. Returns True when our row changed."""
        u = self.ud.get(rid)
        if u is None:
            self.ud[rid] = {**remote, "changed": 0, "dirty": 0, "remote": remote}
            return True
        if u.get("dirty"):
            if remote["last_played"] and remote["last_played"] > (u.get("changed") or 0) + 1:
                u.update(remote, dirty=0, remote=remote)  # played elsewhere since: theirs is newer
                return True
            if u.get("remote") != remote:
                u["remote"] = remote
                return True
            return False
        if any(u.get(k) != remote[k] for k in UD_KEYS) or u.get("remote") != remote:
            u.update(remote, remote=remote)
            return True
        return False

    def push(self):
        """Send watch-history changes made here (also while Jellyfin was away) up to Jellyfin."""
        if os.environ.get("MARQUEE_DEV_KEY") and not os.environ.get("MARQUEE_DEV_PUSH"):
            return 0  # development runs use the real server: test plays must never reach its watch history
        jf = self.app.jf
        with self.lock:
            todo = [(rid, dict(u)) for rid, u in self.ud.items() if u.get("dirty") and not rid.startswith("local:")
                    and rid in self.items]
        sent = 0
        for rid, u in todo:
            body = {"Played": bool(u["played"]), "PlaybackPositionTicks": int((u["position"] or 0) * TICKS),
                    "IsFavorite": bool(u["favorite"]), "PlayCount": int(u["play_count"] or 0)}
            if u.get("last_played"):
                body["LastPlayedDate"] = iso(u["last_played"])
            try:
                jf.call("POST", f"/UserItems/{rid}/UserData", params={"userId": jf.user}, json=body, timeout=10)
            except requests.HTTPError as e:
                if getattr(e.response, "status_code", 0) not in (400, 404):
                    raise
                print("catalog: Jellyfin refused user data for", rid, e)
            with self.lock:
                cur = self.ud.get(rid)
                if cur and cur.get("changed") == u.get("changed"):  # not changed again meanwhile
                    cur["dirty"] = 0
                    cur["remote"] = {k: cur[k] for k in UD_KEYS}
                    self._save_ud([rid])
            sent += 1
        if sent:
            print(f"catalog: sent {sent} watch-history change(s) to Jellyfin", flush=True)
        return sent

    # ------------------------------------------------------------------ live events
    def on_live(self, kind, data):
        """Jellyfin's WebSocket said something changed (marquee.live)."""
        if kind == "UserDataChanged":
            with self.lock:
                for u in data.get("UserDataList") or []:
                    rid = (u.get("ItemId") or "").replace("-", "")
                    if rid in self.items:
                        self._merge_ud(rid, _ud_from_jf(u)) and self._save_ud([rid])
            self.live_events["userdata-changed"] = {"items": [(u.get("ItemId") or "").replace("-", "")
                                                              for u in data.get("UserDataList") or []]}
            self._tell("userdata-changed", self.live_events.pop("userdata-changed"))
        elif kind == "LibraryChanged":
            self.live_events["library-changed"] = {"added": data.get("ItemsAdded", []),
                                                   "removed": data.get("ItemsRemoved", []),
                                                   "updated": data.get("ItemsUpdated", [])}
            self.live_ids.update(data.get("ItemsAdded", []) + data.get("ItemsRemoved", []))
            self.wake.set()

    def _apply_live(self):
        time.sleep(3)  # a season pack imports many files in a burst: take them together
        self.live_ids.clear()
        events, self.live_events = self.live_events, {}
        self.snapshot(quiet=True)
        for event, data in events.items():
            self._tell(event, data)

    # ------------------------------------------------------------------ segments (skip intro offline)
    def _fill_segments(self, limit=200):
        jf = self.app.jf
        now = time.time()
        with self.lock:
            todo = [rid for rid, r in self.items.items() if r["type"] in ("Movie", "Episode")
                    and r.get("source") == "jellyfin" and now - self._seg_fetched.get(rid, 0) > SEGMENTS_MAX_AGE]
        for rid in todo[:limit]:
            if getattr(self.app.player, "playing", False):
                time.sleep(0.2)
            try:
                items = (jf.call("GET", f"/MediaSegments/{rid}", timeout=10) or {}).get("Items", [])
            except requests.HTTPError:
                items = []
            segs = [{"type": s.get("Type"), "start": s.get("StartTicks", 0) / TICKS, "end": s.get("EndTicks", 0) / TICKS}
                    for s in items]
            with self.lock:
                self.segments[rid] = segs
                self._seg_fetched[rid] = now
                self.db.execute("INSERT OR REPLACE INTO segments VALUES (?,?,?)", (rid, json.dumps(segs), now))

    # ------------------------------------------------------------------ artwork warm-up
    def _warm_loop(self):
        """Fill the image cache ahead of time so posters show even when Jellyfin is away: posters and backdrops
        of films and shows, stills of episodes. Slow and polite; pauses while a video plays."""
        time.sleep(20)
        while True:
            try:
                if (self.app.jf.online or any(r.get("source") == "local" for r in list(self.items.values()))):
                    self._warm_once()
            except Exception as e:
                print("image warm-up failed:", e)
            time.sleep(600)

    def _warm_once(self):
        with self.lock:
            rows = sorted(self.items.values(), key=lambda r: -(r.get("added") or 0))
        urls = []
        for r in rows:
            if r["type"] in ("Movie", "Series"):
                urls += [r.get("poster"), r.get("backdrop"), r.get("logo")]
            elif r["type"] == "Season":
                urls.append(r.get("poster"))
            elif r["type"] == "Episode":
                urls.append(r.get("thumb"))
        for r in rows:  # cast headshots last (the details page's cast row, offline)
            if r["type"] in ("Movie", "Series"):
                urls += [p.get("photo") for p in ((r.get("extra") or {}).get("people") or [])[:10]]
        seen = set()
        for u in urls:
            if not u or u in seen:
                continue
            seen.add(u)
            if self.images.usage() > IMG_LIMIT * 0.7:
                return
            while getattr(self.app.player, "playing", False):
                time.sleep(5)
            if u.startswith("/img/Items/") and not self.app.jf.online:
                continue
            self.images.get_url(u)

    # ------------------------------------------------------------------ local resume points (positions.json)
    def _import_positions(self):
        """Resume points the player kept by file path (files played outside Jellyfin) become catalog history."""
        f = settings.DIR / "positions.json"
        try:
            m = f.stat().st_mtime
        except OSError:
            return
        if m == self._positions_mtime:
            return
        self._positions_mtime = m
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            return
        for path, p in data.items():
            rid = self.by_path.get(pathkey(path))
            if not rid or not isinstance(p, dict):
                continue
            u = self.ud.get(rid) or {}
            at = p.get("at") or 0
            if at > max(u.get("changed") or 0, u.get("last_played") or 0) + 1:
                self.save_position(rid, p.get("pos") or 0, at=at)

    # ------------------------------------------------------------------ watch history (local, synced)
    def _resolve(self, item_id):
        if not item_id:
            return None
        if item_id in self.items:
            return item_id
        if item_id.startswith("file:"):
            item_id = item_id[5:]
        return self.by_path.get(pathkey(item_id))

    def _ud(self, rid):
        return self.ud.setdefault(rid, {"played": 0, "position": 0.0, "favorite": 0, "last_played": 0,
                                        "play_count": 0, "changed": 0, "dirty": 0, "remote": None})

    def save_position(self, item_id, seconds, played=None, at=None):
        """Where playback is/stopped (seconds). played=True marks it watched (resume point cleared); None decides
        from the runtime (90%). Stored locally at once and queued for Jellyfin."""
        with self.lock:
            rid = self._resolve(item_id)
            r = self.items.get(rid)
            if not r:
                return False
            runtime = r.get("runtime") or 0
            seconds = max(0.0, float(seconds or 0))
            if played is None:
                played = bool(runtime) and seconds >= runtime * 0.9
            now = at or time.time()
            u = self._ud(rid)
            if played:
                if not u["played"] or u["position"]:
                    u["play_count"] = (u["play_count"] or 0) + (0 if u["played"] else 1)
                u["played"], u["position"] = 1, 0.0
            else:
                u["position"] = round(seconds, 1) if seconds >= 5 else 0.0
            u["last_played"] = now
            u["changed"], u["dirty"] = now, 1
            self._save_ud([rid])
        self.wake.set()
        return True

    def set_played(self, item_id, played=True):
        """Mark watched/unwatched; a show or season marks all its episodes."""
        with self.lock:
            rid = self._resolve(item_id)
            r = self.items.get(rid)
            if not r:
                return False
            targets = [rid]
            if r["type"] == "Series":
                targets += [e["id"] for e in self.eps.get(rid, [])]
            elif r["type"] == "Season":
                targets += [e["id"] for e in self.eps_by_season.get(rid, [])]
            now = time.time()
            for t in targets:
                u = self._ud(t)
                if played and not u["played"]:
                    u["play_count"] = (u["play_count"] or 0) + 1
                    u["last_played"] = now
                u["played"], u["position"] = int(played), 0.0
                u["changed"], u["dirty"] = now, 1
            self._save_ud(targets)
        self.wake.set()
        return True

    def set_favorite(self, item_id, on=True):
        with self.lock:
            rid = self._resolve(item_id)
            if rid not in self.items:
                return False
            u = self._ud(rid)
            u["favorite"], u["changed"], u["dirty"] = int(on), time.time(), 1
            self._save_ud([rid])
        self.wake.set()
        return True

    def pending(self):
        return sum(1 for rid, u in self.ud.items() if u.get("dirty") and not rid.startswith("local:"))

    # ------------------------------------------------------------------ for the player
    def next_episode(self, item_id):
        """The episode after this one in the show (season 0 specials only follow specials), or None."""
        with self.lock:
            rid = self._resolve(item_id)
            r = self.items.get(rid)
            if not r or r["type"] != "Episode":
                return None
            eps = self.eps.get(r.get("series_id"), [])
            special = (r.get("season_no") or 0) == 0
            eps = [e for e in eps if ((e.get("season_no") or 0) == 0) == special]
            for k, e in enumerate(eps):
                if e["id"] == rid:
                    return eps[k + 1]["id"] if k + 1 < len(eps) else None
            return None

    def lookup(self, item_id):
        """What the player needs to play an item without asking Jellyfin, or None when it isn't in the catalog."""
        with self.lock:
            rid = self._resolve(item_id)
            r = self.items.get(rid)
            if not r:
                return None
            if r["type"] == "Series":  # a show: its next episode
                nxt = self.next_up_for(rid)
                return self.lookup(nxt["id"]) if nxt else None
            if r["type"] == "Season":
                eps = self.eps_by_season.get(rid) or []
                return self.lookup(eps[0]["id"]) if eps else None
            u = self.ud.get(rid) or {}
            path = r.get("path")
            return {
                "id": rid, "type": r["type"], "source": r.get("source"), "path": path,
                "exists": bool(path and os.path.exists(path)), "name": r.get("name"),
                "series": r.get("series_name"), "series_id": r.get("series_id"), "season": r.get("season_no"),
                "episode": r.get("episode_no"), "year": r.get("year"), "poster": r.get("poster"),
                "backdrop": r.get("backdrop"), "thumb": r.get("thumb"), "logo": r.get("logo"),
                "runtime": r.get("runtime") or 0, "position": u.get("position") or 0, "played": bool(u.get("played")),
                "genres": r.get("genres") or [], "anime": r.get("library") == "anime" or "anime" in (path or "").lower(),
                "segments": self.segments.get(rid), "next_episode_id": self.next_episode(rid),
                "provider": r.get("provider") or {},
            }

    # ------------------------------------------------------------------ cards and rows for the UI
    def card(self, r):
        """The same shape as marquee.server.card() makes from a Jellyfin item."""
        u = self.ud.get(r["id"]) or {}
        kind = r["type"]
        runtime = r.get("runtime") or 0
        pos = u.get("position") or 0
        played = bool(u.get("played"))
        unplayed = None
        if kind in ("Series", "Season"):
            eps = (self.eps.get(r["id"]) if kind == "Series" else self.eps_by_season.get(r["id"])) or []
            unplayed = sum(1 for e in eps if not (self.ud.get(e["id"]) or {}).get("played"))
            played = bool(eps) and not unplayed
        path = (r.get("path") or "").lower()
        return {
            "id": r["id"], "type": kind, "name": r.get("name"), "series": r.get("series_name"),
            "series_id": r.get("series_id"), "season": r.get("season_no"), "episode": r.get("episode_no"),
            "year": r.get("year"), "rating": r.get("rating"), "age": r.get("age"), "genres": r.get("genres") or [],
            "overview": r.get("overview") or "", "runtime": runtime, "position": pos,
            "progress": (pos / runtime) if runtime else 0, "played": played, "favorite": bool(u.get("favorite")),
            "unplayed": unplayed, "poster": r.get("poster"), "backdrop": r.get("backdrop"), "thumb": r.get("thumb"),
            "logo": r.get("logo"), "anime": r.get("library") == "anime" or "\\anime" in path or "/anime" in path,
            "added": iso(r.get("added")), "local": r.get("source") == "local",
            "premiere": iso(r.get("premiere")) if r.get("premiere") else None,
        }

    def cards(self, rows):
        return [self.card(r) for r in rows if r]

    def get(self, item_id):
        return self.items.get(self._resolve(item_id) or "")

    def _of(self, types, lib=None):
        types = set(types.split(",")) if isinstance(types, str) else set(types)
        return [r for r in self.items.values() if r["type"] in types and (lib is None or r.get("library") == lib)]

    def _series_last(self, sid):
        return max([(self.ud.get(e["id"]) or {}).get("last_played") or 0 for e in self.eps.get(sid, [])] or [0])

    def _played(self, r):
        if r["type"] == "Series":
            eps = self.eps.get(r["id"]) or []
            return bool(eps) and all((self.ud.get(e["id"]) or {}).get("played") for e in eps)
        return bool((self.ud.get(r["id"]) or {}).get("played"))

    def resume(self, limit=20):
        rows = [r for r in self._of("Movie,Episode")
                if (self.ud.get(r["id"]) or {}).get("position") and not (self.ud.get(r["id"]) or {}).get("played")]
        rows.sort(key=lambda r: -((self.ud.get(r["id"]) or {}).get("last_played") or 0))
        return rows[:limit]

    def next_up_for(self, sid, first_if_new=True):
        eps = [e for e in self.eps.get(sid, []) if (e.get("season_no") or 0) > 0] or self.eps.get(sid, [])
        if not eps:
            return None
        played = [k for k, e in enumerate(eps) if (self.ud.get(e["id"]) or {}).get("played")]
        if not played:
            return eps[0] if first_if_new else None
        last = max(played, key=lambda k: ((self.ud.get(eps[k]["id"]) or {}).get("last_played") or 0, k))
        for e in eps[last + 1:]:
            if not (self.ud.get(e["id"]) or {}).get("played"):
                return e
        return None

    def next_up(self, limit=20):
        """Next unwatched episode after the last one watched, per show, most recently watched shows first."""
        resuming = {r.get("series_id") for r in self.resume(100) if r["type"] == "Episode"}
        out = []
        for s in self._of("Series"):
            if s["id"] in resuming:
                continue
            nxt = self.next_up_for(s["id"], first_if_new=False)
            if nxt and not (self.ud.get(nxt["id"]) or {}).get("position"):
                out.append((self._series_last(s["id"]), nxt))
        out.sort(key=lambda x: -x[0])
        return [e for _, e in out[:limit]]

    def recently_watched(self, limit=20, types=("Movie", "Series")):
        rows = []
        for r in self._of(types):
            last = self._series_last(r["id"]) if r["type"] == "Series" else (self.ud.get(r["id"]) or {}).get("last_played")
            watched = (any((self.ud.get(e["id"]) or {}).get("played") for e in self.eps.get(r["id"], []))
                       if r["type"] == "Series" else (self.ud.get(r["id"]) or {}).get("played"))
            if watched and last:
                rows.append((last, r))
        rows.sort(key=lambda x: -x[0])
        return [r for _, r in rows[:limit]]

    def latest(self, lib, limit=24):
        """Newest additions; shows count by their newest episode."""
        rows = []
        for r in self.items.values():
            if r.get("library") != lib:
                continue
            if r["type"] == "Movie":
                rows.append((r.get("added") or 0, r))
            elif r["type"] == "Series":
                newest = max([e.get("added") or 0 for e in self.eps.get(r["id"], [])] or [r.get("added") or 0])
                rows.append((newest, r))
        rows.sort(key=lambda x: -x[0])
        return [r for _, r in rows[:limit]]

    def home_rows(self):
        rows = []
        libs = {r.get("library") for r in self.items.values()}

        def add(key, title, items, style="poster"):
            if items:
                rows.append({"key": key, "title": title, "items": self.cards(items), "style": style})

        with self.lock:
            add("continue", "Continue watching", self.resume(), "wide")
            add("nextup", "Next up", self.next_up(), "wide")
            add("recent", "Recently watched", self.recently_watched() or self.recently_watched(types=("Movie", "Episode")))
            if "movies" in libs:
                add("new-movies", "New movies", self.latest("movies"))
            if "shows" in libs:
                add("new-shows", "New episodes", self.latest("shows"))
            if "anime" in libs:
                add("anime", "Anime", random.sample(a := self._of("Series,Movie", "anime"), min(24, len(a))))
            for rec in (self.meta.get("recommendations") or [])[:3]:
                base = rec.get("base")
                title = {"SimilarToRecentlyPlayed": f"Because you watched {base}",
                         "SimilarToLikedItem": f"Because you liked {base}",
                         "HasDirectorFromRecentlyPlayed": f"More from {base}",
                         "HasActorFromRecentlyPlayed": f"More with {base}"}.get(rec.get("type"), "Picked for you")
                add("rec-" + (base or str(random.random())), title, [self.items.get(i) for i in rec["ids"]])
            pool = self._of("Movie,Series")
            unwatched = [r for r in pool if not self._played(r)]
            add("unwatched", "Haven't watched yet", random.sample(unwatched, min(24, len(unwatched))))
            add("top", "Highest rated in your library",
                sorted([r for r in pool if r.get("rating")], key=lambda r: -r["rating"])[:24])
            genres = GENRES[:]
            random.shuffle(genres)
            for g in genres[:3]:
                hits = [r for r in pool if g in (r.get("genres") or [])]
                add("genre-" + g, g, random.sample(hits, min(24, len(hits))))
        return rows

    def library_rows(self, kind):
        types = {"movies": "Movie", "shows": "Series", "anime": "Series,Movie"}[kind]
        with self.lock:
            items = self._of(types, kind)
            if not items:
                return []
            rows = [{"key": "all", "title": "Everything", "style": "poster",
                     "items": self.cards(sorted(items, key=lambda r: norm(r.get("sort_name") or r.get("name")))[:400])},
                    {"key": "new", "title": "Recently added", "style": "poster", "items": self.cards(self.latest(kind))}]
            for g in ["Action", "Comedy", "Drama", "Science Fiction", "Fantasy", "Adventure", "Animation", "Thriller"]:
                hits = [r for r in items if g in (r.get("genres") or [])]
                if len(hits) >= 3:
                    rows.append({"key": "g-" + g, "title": g, "style": "poster",
                                 "items": self.cards(random.sample(hits, min(24, len(hits))))})
        return rows

    def similar_local(self, r, limit=16):
        """Titles like this one from the library alone: shared genres and cast, a little rating."""
        if r["type"] not in ("Movie", "Series"):
            return []
        genres = set(r.get("genres") or [])
        cast = {p.get("name") for p in (r.get("extra") or {}).get("people") or []}
        scored = []
        for o in self._of("Movie,Series"):
            if o["id"] == r["id"]:
                continue
            g = len(genres & set(o.get("genres") or []))
            c = len(cast & {p.get("name") for p in (o.get("extra") or {}).get("people") or []})
            if g or c:
                scored.append((g * 2 + c * 3 + (o.get("rating") or 0) / 10 + (o["type"] == r["type"]), o))
        scored.sort(key=lambda x: -x[0])
        return [o for _, o in scored[:limit]]

    def remember_similar(self, item_id, ids):
        with self.lock:
            r = self.items.get(item_id)
            if not r:
                return
            r.setdefault("extra", {})["similar"] = [i for i in ids if i in self.items]
            r["extra"]["similar_at"] = time.time()
            self.db.execute("INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?)", self._item_tuple(r))

    def detail(self, item_id):
        """Everything the details page shows, from the catalog (works with Jellyfin away)."""
        with self.lock:
            r = self.get(item_id)
            if not r:
                return None
            out = self.card(r)
            extra = r.get("extra") or {}
            out["people"] = [{k: p.get(k) for k in ("name", "role", "type", "photo", "id")} for p in extra.get("people") or []]
            out["studios"] = extra.get("studios") or []
            out["tagline"] = extra.get("tagline")
            out["resolution"] = extra.get("resolution")
            out["hdr"] = bool(extra.get("hdr"))
            out["media"] = extra.get("media") or {}
            out["critic"] = extra.get("critic")
            out["trailers"] = extra.get("trailers") or []
            prov = r.get("provider") or {}
            out["tmdb"] = prov.get("Tmdb")
            out["imdb"] = prov.get("Imdb")
            out["source"] = r.get("source")
            sim = [self.items.get(i) for i in extra.get("similar") or []]
            out["similar"] = self.cards(sim) if any(sim) else self.cards(self.similar_local(r))
            if r["type"] == "Series":
                out["seasons"] = self.seasons_info(r["id"])
                nu = self.next_up_for(r["id"])
                out["next_up"] = self.card(nu) if nu else None
                if not out["media"]:  # a show's quality: what its episodes are
                    eps = self.eps.get(r["id"]) or []
                    best = max(eps, key=lambda e: ((e.get("extra") or {}).get("media") or {}).get("height") or 0,
                               default=None)
                    if best:
                        out["media"] = (best.get("extra") or {}).get("media") or {}
                        out["resolution"] = out["media"].get("height")
                        out["hdr"] = bool(out["media"].get("hdr"))
            out["available"] = bool(r.get("path") and os.path.exists(r["path"])) if r["type"] in ("Movie", "Episode") else None
        return out

    def seasons_info(self, series_id):
        """Seasons of a show with their overview, episode and watched counts (specials last)."""
        out = []
        for s in self.seasons.get(series_id, []):
            eps = self.eps_by_season.get(s["id"]) or []
            out.append({"id": s["id"], "name": s.get("name"), "number": s.get("season_no"),
                        "overview": s.get("overview") or "", "poster": s.get("poster"), "year": s.get("year"),
                        "episodes": len(eps),
                        "watched": sum(1 for e in eps if (self.ud.get(e["id"]) or {}).get("played")),
                        "premiere": iso(s.get("premiere")) if s.get("premiere") else None})
        out.sort(key=lambda x: (x["number"] == 0, x["number"] if x["number"] is not None else 999))
        return out

    @staticmethod
    def quality_label(r):
        m = (r.get("extra") or {}).get("media") or {}
        h = m.get("height") or 0
        res = "4K" if h >= 1600 else "1080p" if h >= 1000 else "720p" if h >= 700 else "SD" if h else ""
        rng = "DV" if m.get("dv") else "HDR10+" if m.get("hdr10plus") else "HDR" if m.get("hdr") else ""
        return " ".join(x for x in (res, rng) if x)

    def by_tmdb(self, kind, tmdb):
        """The library's film ("movie") or show ("tv") with this TMDB id, checked on our side, or None."""
        want = {"movie": "Movie", "tv": "Series"}.get(kind, kind)
        tmdb = str(tmdb)
        with self.lock:
            for r in self.items.values():
                if r["type"] == want and str((r.get("provider") or {}).get("Tmdb") or "") == tmdb:
                    return r
        return None

    def with_person(self, name, person_id=None, limit=30):
        """Films and shows in the library this person is in (by Jellyfin person id, else by name)."""
        key = norm(name)
        with self.lock:
            rows = [r for r in self._of("Movie,Series")
                    if any((person_id and p.get("id") == person_id) or (key and norm(p.get("name")) == key)
                           for p in (r.get("extra") or {}).get("people") or [])]
            rows.sort(key=lambda r: -(r.get("year") or 0))
            return self.cards(rows[:limit])

    # ------------------------------------------------------------------ small cache (ratings, people, TMDB extras)
    def kv_get(self, key, max_age=None):
        """(data, age_seconds) from the cache, or (None, None). max_age: older entries count as missing."""
        row = self.db.execute("SELECT data, fetched FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return None, None
        age = time.time() - (row[1] or 0)
        if max_age is not None and age > max_age:
            return None, age
        try:
            return json.loads(row[0]), age
        except ValueError:
            return None, None

    def kv_set(self, key, data):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)",
                        (key, json.dumps(data, ensure_ascii=False), time.time()))

    # ------------------------------------------------------------------ removed titles (deleted to save space)
    # Removing a file never erases its history: marquee.storage writes a record here first (ids, title, artwork,
    # runtime, genres, each user's played state), the artwork it points at is exempt from cache pruning, and when
    # the title comes back (re-requested and imported) the played state is put back in Jellyfin for those users.
    @staticmethod
    def removed_key(kind, tmdb=None, season=None, episode=None, fallback=None):
        if kind == "episode":
            return f"ep:{tmdb or fallback}:{season}:{episode}"
        return f"{kind}:{tmdb or fallback}"

    def record_removed(self, kind, data, reason=None, at=None):
        """Store a removed title. kind: movie | episode | series. data: {tmdb (the show's for episodes), tvdb, imdb,
        title, year, series, season, episode, poster, backdrop, thumb, runtime, genres, rating, favorite, users:
        {user_id: {played, last_played, play_count, position, favorite}}, size, jellyfin_id, series_jellyfin_id}.
        Returns the record's key."""
        tmdb = data.get("tmdb")
        key = self.removed_key(kind, tmdb, data.get("season"), data.get("episode"), data.get("jellyfin_id"))
        data = dict(data, reason=reason, kind=kind)
        users = data.get("users") or {}
        data["played"] = any(u.get("played") for u in users.values())
        data["last_played"] = max([u.get("last_played") or 0 for u in users.values()] or [0])
        imgs = set()
        for slot in ("poster", "backdrop", "thumb", "series_poster"):
            try:
                f = self.images.get_url(data.get(slot)) if data.get(slot) else None
                if f:
                    imgs.add(Path(f).name)
            except Exception:
                pass
        data["images"] = sorted(imgs)
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO removed VALUES (?,?,?,?,?,?,?,?)",
                            (key, kind, str(tmdb) if tmdb else None, data.get("season"), data.get("episode"),
                             at or time.time(), None, json.dumps(data, ensure_ascii=False)))
            self._pinned = None
        return key

    def _removed_rows(self, where="restored_at IS NULL", args=()):
        out = []
        for key, kind, tmdb, season, episode, at, restored, data in self.db.execute(
                "SELECT key, kind, tmdb, season, episode, removed_at, restored_at, data FROM removed WHERE " + where,
                args):
            try:
                d = json.loads(data or "{}")
            except ValueError:
                d = {}
            out.append(dict(d, key=key, kind=kind, tmdb=tmdb, season=season, episode=episode, removed_at=at,
                            restored_at=restored))
        return out

    def removed_items(self, kind=None):
        """Removed titles still gone, newest first. kind: movie | episode | series | None (all)."""
        rows = self._removed_rows("restored_at IS NULL" + (" AND kind=?" if kind else ""), (kind,) if kind else ())
        return sorted(rows, key=lambda r: -(r.get("removed_at") or 0))

    def removed_state(self, tmdb, season=None, episode=None, kind=None):
        """What we know about a removed film/show (tmdb id). For a show without season/episode: its removed
        episodes rolled up (played = every removed episode was watched). None when nothing of it was removed."""
        tmdb = str(tmdb)
        if season is not None and episode is not None:
            rows = self._removed_rows("restored_at IS NULL AND kind='episode' AND tmdb=? AND season=? AND episode=?",
                                      (tmdb, int(season), int(episode)))
            return rows[0] if rows else None
        rows = self._removed_rows("restored_at IS NULL AND tmdb=?" + (" AND kind=?" if kind else ""),
                                  (tmdb, kind) if kind else (tmdb,))
        if season is not None:
            rows = [r for r in rows if r.get("season") == int(season)]
        if not rows:
            return None
        movie = next((r for r in rows if r["kind"] == "movie"), None)
        if movie:
            return movie
        eps = [r for r in rows if r["kind"] == "episode"]
        base = next((r for r in rows if r["kind"] == "series"), None) or (eps[0] if eps else rows[0])
        return dict(base, kind="series", episodes=len(eps), played=bool(eps) and all(r.get("played") for r in eps),
                    any_played=any(r.get("played") for r in rows),
                    removed_at=max(r.get("removed_at") or 0 for r in rows),
                    last_played=max(r.get("last_played") or 0 for r in rows),
                    seasons=sorted({r.get("season") for r in eps if r.get("season") is not None}))

    def removed_flag(self, kind, tmdb):
        """Small state for a Seerr card (movie | tv, tmdb id): {removed, played, state, removed_at, watched_at}."""
        try:
            st = self.removed_state(tmdb)
        except Exception:
            return None
        if not st or (kind == "movie") != (st.get("kind") == "movie"):
            return None
        played = bool(st.get("played") or (kind == "tv" and st.get("any_played")))
        return {"removed": True, "played": played, "state": "removed_watched" if played else "removed",
                "removed_at": iso(st.get("removed_at")),
                "watched_at": iso(st.get("last_played")) if st.get("last_played") else None}

    def removed_cards(self, kind=None):
        """Removed films ("movie") / shows ("tv") as request-style cards for library pages ("Show removed"):
        TMDB id, artwork from the local cache, and removed / played / state flags."""
        out, seen = [], set()
        for r in self.removed_items():
            k = "movie" if r["kind"] == "movie" else "tv"
            if (kind and k != kind) or not r.get("tmdb") or (k, r["tmdb"]) in seen:
                continue
            seen.add((k, r["tmdb"]))
            st = self.removed_state(r["tmdb"]) or r
            played = bool(st.get("played") or (k == "tv" and st.get("any_played")))
            out.append({"id": int(r["tmdb"]) if str(r["tmdb"]).isdigit() else r["tmdb"], "type": k,
                        "title": r.get("series") if k == "tv" and r.get("series") else r.get("title"),
                        "year": str(r.get("year") or ""), "poster": r.get("series_poster") or r.get("poster"),
                        "backdrop": r.get("backdrop"), "genres": r.get("genres") or [], "rating": r.get("rating"),
                        "overview": r.get("overview") or "", "anime": bool(r.get("anime")), "removed": True,
                        "played": played, "state": "removed_watched" if played else "removed",
                        "removed_at": iso(st.get("removed_at")),
                        "watched_at": iso(st.get("last_played")) if st.get("last_played") else None,
                        "reason": r.get("reason"), "status": None})
        return out

    def removed_history(self):
        """Watch events of removed titles in the shape the taste profile reads ({id, type, name, series, genres,
        runtime, played, last_played, play_count, favorite, tmdb, removed_at}), so removed watched titles keep
        counting. Includes restored ones (their history is the same)."""
        out = []
        me = getattr(self.app.jf, "user", None) or ""
        for r in self._removed_rows("1=1"):
            mine = (r.get("users") or {}).get(me) or {}
            out.append({"id": r.get("jellyfin_id") or r["key"], "type": "Movie" if r["kind"] == "movie" else "Episode",
                        "name": r.get("title"), "series": r.get("series"), "genres": r.get("genres") or [],
                        "runtime": r.get("runtime") or 0, "played": bool(mine.get("played", r.get("played"))),
                        "last_played": mine.get("last_played") or r.get("last_played") or 0,
                        "play_count": mine.get("play_count") or 0, "favorite": bool(r.get("favorite")),
                        "tmdb": r.get("tmdb"), "removed_at": r.get("removed_at")})
        return out

    def forget_removed(self, key=None, tmdb=None):
        """Drop removed records ("Forget"): one key, or everything of a film/show by tmdb id."""
        with self.lock:
            if key:
                self.db.execute("DELETE FROM removed WHERE key=?", (key,))
            elif tmdb:
                self.db.execute("DELETE FROM removed WHERE tmdb=?", (str(tmdb),))
            self._pinned = None
        return True

    def pinned_images(self):
        if getattr(self, "_pinned", None) is None:
            names = set()
            for (data,) in self.db.execute("SELECT data FROM removed"):
                try:
                    names |= set(json.loads(data or "{}").get("images") or [])
                except ValueError:
                    pass
            self._pinned = names
        return self._pinned

    def restore_removed(self, mark=None):
        """Titles that came back (re-requested and imported): put their played state back, for this user here
        (synced like any change) and for the other users straight in Jellyfin, then mark the records restored.
        mark(user_id, item_id, record): replaces the Jellyfin call (tests)."""
        rows = self.removed_items()
        if not rows:
            return []
        done = []
        me = getattr(self.app.jf, "user", None)
        with self.lock:
            movies = {str((r.get("provider") or {}).get("Tmdb")): r for r in self._of("Movie")
                      if (r.get("provider") or {}).get("Tmdb")}
            shows = {str((r.get("provider") or {}).get("Tmdb")): r for r in self._of("Series")
                     if (r.get("provider") or {}).get("Tmdb")}
        for rec in rows:
            target = None
            if rec["kind"] == "movie":
                target = movies.get(str(rec.get("tmdb")))
            elif rec["kind"] == "episode":
                show = shows.get(str(rec.get("tmdb")))
                if show:
                    target = next((e for e in self.eps.get(show["id"], []) if e.get("season_no") == rec.get("season")
                                   and e.get("episode_no") == rec.get("episode")), None)
            elif rec["kind"] == "series":
                target = shows.get(str(rec.get("tmdb")))
            if not target or (target.get("path") and not os.path.exists(target["path"]) and rec["kind"] != "series"):
                continue
            for uid, st in (rec.get("users") or {}).items():
                if not st.get("played") or rec["kind"] == "series":
                    continue
                if uid in (me, "me"):
                    with self.lock:
                        u = self._ud(target["id"])
                        if not u["played"]:
                            u["played"], u["position"] = 1, 0.0
                            u["play_count"] = max(u["play_count"] or 0, st.get("play_count") or 1)
                            u["last_played"] = st.get("last_played") or time.time()
                            u["changed"], u["dirty"] = time.time(), 1
                            self._save_ud([target["id"]])
                    self.wake.set()
                else:
                    try:
                        if mark:
                            mark(uid, target["id"], rec)
                        else:
                            self.app.jf.call("POST", f"/UserPlayedItems/{target['id']}", params={
                                "userId": uid, "datePlayed": iso(st.get("last_played") or time.time())})
                    except Exception as e:
                        print("restore played state failed:", type(e).__name__, e)
            with self.lock:
                self.db.execute("UPDATE removed SET restored_at=? WHERE key=?", (time.time(), rec["key"]))
                self._pinned = None
            done.append(rec["key"])
        if done:
            print(f"catalog: {len(done)} removed title(s) are back; watch history restored", flush=True)
        return done

    # ------------------------------------------------------------------ My List
    # Owned titles are keyed "item:<id>", titles not in the library "movie:<tmdb>" / "tv:<tmdb>". With
    # mylist_sync_favorites on (the default) the owned part of the list IS the user's Jellyfin favourites:
    # adding/removing sets the favourite (queued while Jellyfin is away, like any watch-history change), and
    # favourites set elsewhere show up through the snapshot and live UserDataChanged events.
    @staticmethod
    def _sync_favs():
        return bool(settings.load().get("mylist_sync_favorites", True))

    def _ml_rows(self):
        return {k: (a, json.loads(d or "{}")) for k, a, d in self.db.execute("SELECT key, added, data FROM mylist")}

    def mylist_key(self, item_id=None, kind=None, tmdb=None):
        if item_id:
            rid = self._resolve(item_id)
            r = self.items.get(rid or "")
            if r and r["type"] in ("Episode", "Season") and r.get("series_id"):
                rid = r["series_id"]
            return f"item:{rid}" if rid else None
        if kind in ("movie", "tv") and str(tmdb or "").isdigit():
            owned = self.by_tmdb(kind, tmdb)
            return f"item:{owned['id']}" if owned else f"{kind}:{int(tmdb)}"
        return None

    def mylist_add(self, key, data=None):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO mylist VALUES (?,?,?)", (key, time.time(), json.dumps(data or {})))
        if key.startswith("item:") and self._sync_favs():
            self.set_favorite(key[5:], True)
        return True

    def mylist_remove(self, key):
        with self.lock:
            self.db.execute("DELETE FROM mylist WHERE key=?", (key,))
        if key.startswith("item:") and self._sync_favs():
            self.set_favorite(key[5:], False)
        return True

    def mylist_keys(self):
        return [k for k, _ in self._mylist_entries()]

    def mylist_reconcile(self):
        """Titles saved before they were in the library: once imported (TMDB id matched here, not trusting the
        server's filter), they become the library entry, and with sync on a Jellyfin favourite."""
        sync = self._sync_favs()
        for key, (added, data) in self._ml_rows().items():
            kind, _, tmdb = key.partition(":")
            if kind not in ("movie", "tv"):
                continue
            owned = self.by_tmdb(kind, tmdb)
            if not owned:
                continue
            with self.lock:
                self.db.execute("DELETE FROM mylist WHERE key=?", (key,))
                self.db.execute("INSERT OR REPLACE INTO mylist VALUES (?,?,?)",
                                (f"item:{owned['id']}", added, json.dumps(data)))
            if sync:
                self.set_favorite(owned["id"], True)
            print(f"catalog: My List entry {key} is now in the library", flush=True)

    def _mylist_entries(self):
        rows = self._ml_rows()
        out = {}
        with self.lock:
            if self._sync_favs():
                for k in [k for k in rows if k.startswith("item:")]:
                    if k[5:] in self.items and not (self.ud.get(k[5:]) or {}).get("favorite"):
                        rows.pop(k)  # un-favourited elsewhere
                for rid, u in self.ud.items():
                    r = self.items.get(rid)
                    if u.get("favorite") and r and r["type"] in ("Movie", "Series"):
                        k = f"item:{rid}"
                        out[k] = rows.pop(k, (u.get("changed") or r.get("added") or 0, {}))[0]
            for k, (added, _) in rows.items():
                if k.startswith("item:") and k[5:] not in self.items:
                    continue  # gone from the library
                out[k] = added
        return sorted(out.items(), key=lambda kv: -(kv[1] or 0))

    def mylist(self):
        """[(key, added, card_or_None, saved_data)] newest first; card is the library card for owned titles."""
        rows = self._ml_rows()
        out = []
        for k, added in self._mylist_entries():
            data = rows.get(k, (0, {}))[1]
            with self.lock:
                r = self.items.get(k[5:]) if k.startswith("item:") else None
                card = self.card(r) if r else None
            out.append((k, added, card, data))
        return out

    def episodes(self, series_id, season_id):
        with self.lock:
            eps = self.eps_by_season.get(season_id)
            if eps is None:  # a season number instead of an id
                eps = [e for e in self.eps.get(series_id, []) if str(e.get("season_no")) == str(season_id)]
            out = []
            for e in eps:
                c = self.card(e)
                c["quality"] = self.quality_label(e)
                c["available"] = bool(e.get("path") and os.path.exists(e["path"]))
                out.append(c)
            return out

    def search(self, q, types=("Movie", "Series", "Episode"), limit=40):
        words = norm(q).split()
        if not words:
            return []
        with self.lock:
            hits = []
            for r in self._of(types):
                name = norm(r.get("name"))
                hay = name + " " + norm(r.get("series_name")) + " " + str(r.get("year") or "")
                if all(w in hay for w in words):
                    nq = " ".join(words)
                    rank = 0 if name == nq else 1 if name.startswith(nq) else 2 if r["type"] != "Episode" else 3
                    hits.append((rank, r.get("sort_name") or "", r))
            hits.sort(key=lambda x: (x[0], norm(x[1])))
            return self.cards([r for _, _, r in hits[:limit]])

    def by_genres(self, genres, types="Movie,Series", limit=40):
        with self.lock:
            rows = [r for r in self._of(types) if set(genres) & set(r.get("genres") or [])]
            rows.sort(key=lambda r: -(r.get("rating") or 0))
            return self.cards(rows[:limit])

    def first_episode(self, series_id):
        eps = [e for e in self.eps.get(series_id, []) if (e.get("season_no") or 0) > 0] or self.eps.get(series_id, [])
        return eps[0] if eps else None

    def recent_played_rows(self, types, limit):
        rows = [r for r in self._of(types) if (self.ud.get(r["id"]) or {}).get("played")]
        rows.sort(key=lambda r: -((self.ud.get(r["id"]) or {}).get("last_played") or 0))
        return rows[:limit]

    def stats(self):
        try:
            size = DB_FILE.stat().st_size + sum(f.stat().st_size for f in DB_FILE.parent.glob("catalog.db-*"))
        except OSError:
            size = 0
        return {"items": len(self.items), "counts": self.meta.get("counts"), "snapshot_at": self.meta.get("snapshot_at"),
                "snapshot_secs": self.meta.get("snapshot_secs"), "pending_sync": self.pending(), "db_bytes": size,
                "image_bytes": self.images.usage(),
                "local_items": sum(1 for r in self.items.values() if r.get("source") == "local")}

    # ------------------------------------------------------------------ folder scanner
    def _guess(self, full, rel):
        try:
            st = os.stat(full)
        except OSError:
            return None
        key = (full, st.st_mtime, st.st_size)
        hit = self._guess_cache.get(key)
        if hit is None:
            from guessit import guessit
            g = dict(guessit(rel.replace("\\", "/")))
            if g.get("type") == "episode" and g.get("episode") is None:
                g2 = dict(guessit(os.path.basename(full)))
                if g2.get("episode") is not None:
                    g = g2
            hit = self._guess_cache[key] = (g, st)
        return hit

    def _tmdb_match(self, kind, title, year):
        """TMDB details through Seerr (cached in the catalog); {} when there's no match, None when Seerr is away."""
        q = f"{kind}:{norm(title)}:{year or ''}"
        row = self.db.execute("SELECT data FROM tmdb WHERE query=?", (q,)).fetchone()
        if row:
            return json.loads(row[0])
        sr = getattr(self.app, "seerr", None)
        if not sr or not sr.ready:
            return None
        try:
            results = [r for r in sr.search(f"{title} {year}" if year else title) if r["type"] == kind]
            if not results and year:
                results = [r for r in sr.search(title) if r["type"] == kind]
            if year:
                results.sort(key=lambda r: str(r.get("year")) != str(year))
            match = results[0] if results else {}
            if match:
                try:
                    d = sr.details(kind, match["id"])
                    match.update(genres=d.get("genres") or [], runtime=d.get("runtime"))
                except Exception:
                    pass
        except Exception:
            return None
        self.db.execute("INSERT OR REPLACE INTO tmdb VALUES (?,?,?)", (q, json.dumps(match), time.time()))
        return match

    def scan_folders(self, folders=None):
        """Read library_folders into the catalog (ids "local:..."). Returns True when the local part changed."""
        folders = [f for f in (folders if folders is not None else settings.load().get("library_folders") or [])
                   if f and os.path.isdir(f)]
        with self.lock:
            jf_paths = {pathkey(r["path"]) for r in self.items.values() if r.get("source") == "jellyfin" and r.get("path")}
        movies, shows = [], {}
        for root in folders:
            for dirpath, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_DIRS]
                for f in files:
                    full = os.path.join(dirpath, f)
                    if Path(f).suffix.lower() not in VIDEO or "sample" in f.lower() or pathkey(full) in jf_paths:
                        continue
                    got = self._guess(full, os.path.relpath(full, root))
                    if not got or got[1].st_size < MIN_VIDEO:
                        continue
                    g, st = got
                    title = g.get("title")
                    if not title:
                        continue
                    if isinstance(title, list):
                        title = title[0]
                    ep = g.get("episode")
                    ep = ep[0] if isinstance(ep, list) else ep
                    season = g.get("season")
                    season = season[0] if isinstance(season, list) else season
                    anime = "anime" in full.lower()
                    if g.get("type") == "episode" and ep is not None:
                        key = (norm(title), g.get("year"))
                        s = shows.setdefault(key, {"title": title, "year": g.get("year"), "anime": anime, "eps": []})
                        s["eps"].append({"path": full, "season": season if season is not None else 1, "episode": ep,
                                         "title": g.get("episode_title"), "added": st.st_mtime})
                    else:
                        movies.append({"path": full, "title": title, "year": g.get("year"), "anime": anime,
                                       "added": st.st_mtime, "part": g.get("part")})
        rows = {}
        for m in movies:
            q = f"{m['title']} {m['part']}" if m.get("part") else m["title"]
            match = self._tmdb_match("movie", q, m["year"]) or {}
            rid = "local:movie:" + short_hash(pathkey(m["path"]))
            rows[rid] = self._local_row(rid, "Movie", match.get("title") or q, m, match,
                                        library="anime" if m["anime"] else "movies")
        for (key, year), s in shows.items():
            match = self._tmdb_match("tv", s["title"], year) or {}
            sid = f"local:series:{slug(s['title'])}" + (f"-{year}" if year else "")
            name = match.get("title") or s["title"]
            lib = "anime" if s["anime"] or match.get("anime") else "shows"
            rows[sid] = self._local_row(sid, "Series", name, s, match, library=lib)
            rows[sid]["added"] = max(e["added"] for e in s["eps"])
            for n in sorted({e["season"] for e in s["eps"]}):
                season_id = f"{sid}:s{n}"
                rows[season_id] = {**rows[sid], "id": season_id, "type": "Season",
                                   "name": "Specials" if n == 0 else f"Season {n}", "sort_name": f"{n:04d}",
                                   "series_id": sid, "series_name": name, "season_id": season_id, "season_no": n,
                                   "path": None, "logo": rows[sid].get("logo")}
            for e in s["eps"]:
                eid = "local:ep:" + short_hash(pathkey(e["path"]))
                rows[eid] = {**rows[sid], "id": eid, "type": "Episode", "path": e["path"],
                             "name": e["title"] or f"Episode {e['episode']}", "sort_name": f"{e['season']:04d}{e['episode']:04d}",
                             "series_id": sid, "series_name": name, "season_id": f"{sid}:s{e['season']}",
                             "season_no": e["season"], "episode_no": e["episode"], "added": e["added"],
                             "thumb": rows[sid].get("backdrop"), "overview": "", "runtime": 0,
                             "extra": {}}
        with self.lock:
            old = {rid for rid, r in self.items.items() if r.get("source") == "local"}
            same = old == set(rows) and all(self.items[k] == v for k, v in rows.items())
            if same:
                return False
            for rid in old - set(rows):
                self.items.pop(rid, None)
            self.items.update(rows)
            self.db.execute("BEGIN")
            try:
                self.db.execute("DELETE FROM items WHERE source='local'")
                self.db.executemany("INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?)",
                                    [self._item_tuple(r) for r in rows.values()])
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            self._index()
        print(f"catalog: {len(rows)} items from library folders", flush=True)
        try:
            self.mylist_reconcile()
        except Exception as e:
            print("my list reconcile failed:", e)
        try:
            self.restore_removed()
        except Exception as e:
            print("removed titles check failed:", e)
        return True

    @staticmethod
    def _local_row(rid, kind, name, src, match, library):
        poster, backdrop = tmdb_img(match.get("poster")), tmdb_img(match.get("backdrop"))
        runtime = (match.get("runtime") or 0) * 60 if kind == "Movie" else 0
        year = match.get("year") or src.get("year")
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None
        return {
            "id": rid, "type": kind, "source": "local", "library": library, "name": name, "sort_name": name,
            "series_id": None, "series_name": None, "season_id": None, "season_no": None, "episode_no": None,
            "year": year, "overview": match.get("overview") or "", "genres": match.get("genres") or [],
            "rating": match.get("rating"), "age": None, "runtime": runtime, "path": src.get("path"),
            "provider": {"Tmdb": str(match["id"])} if match.get("id") else {}, "added": src.get("added") or 0,
            "premiere": 0, "poster": poster, "backdrop": backdrop, "thumb": backdrop, "logo": None, "children": None,
            "extra": {},
        }
