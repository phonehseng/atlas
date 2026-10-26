"""Just arrived: downloads become watchable the moment they finish, before Jellyfin has imported them.

Watches the download client (qBittorrent's Web API) and, optionally, extra folders. A finished video is identified
from its file name (guessit), matched to TMDB through Seerr for the poster and title, and offered as a playable card
that plays the file itself. As soon as Jellyfin has the same film/episode, the provisional card is dropped and the
real library entry takes over. Downloads still in progress are listed with their progress.
"""
import html
import os
import threading
import time
from pathlib import Path

import requests

from . import settings

VIDEO = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".webm", ".wmv"}
KEEP = 2 * 86400            # forget finished downloads after two days
MIN_SIZE = 40 * 2**20       # ignore samples/extras


def biggest_video(path):
    p = Path(path)
    if p.is_file():
        return p if p.suffix.lower() in VIDEO else None
    best = None
    for f in p.rglob("*") if p.is_dir() else []:
        if f.suffix.lower() in VIDEO and "sample" not in f.name.lower():
            try:
                if not best or f.stat().st_size > best.stat().st_size:
                    best = f
            except OSError:
                pass
    return best


class Arrivals:
    def __init__(self, app):
        self.app = app
        self.items = {}       # key -> card
        self.matched = {}     # name -> TMDB match (cache)
        self.lock = threading.Lock()
        self.observer = None

    # ------------------------------------------------------------------ sources
    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="arrivals").start()
        self._watch_folders()

    def _qbt(self):
        base = (settings.load().get("qbt_url") or "http://127.0.0.1:8080").rstrip("/")
        try:
            return requests.get(f"{base}/api/v2/torrents/info", timeout=5).json()
        except Exception:
            return None

    def _loop(self):
        while True:
            try:
                self.scan()
            except Exception as e:
                print("arrivals scan failed:", e)
            time.sleep(15)

    def _watch_folders(self):
        folders = [f for f in settings.load().get("watch_folders") or [] if os.path.isdir(f)]
        if not folders:
            return
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
        me = self

        class Handler(FileSystemEventHandler):
            def on_closed(self, event):
                if not event.is_directory and Path(event.src_path).suffix.lower() in VIDEO:
                    threading.Thread(target=me.add_file, args=(event.src_path, 1.0, "folder"), daemon=True).start()

            on_moved = on_closed

        self.observer = Observer()
        for f in folders:
            self.observer.schedule(Handler(), f, recursive=True)
        self.observer.start()

    # ------------------------------------------------------------------ identify
    def scan(self):
        torrents = self._qbt()
        if torrents is None:
            return
        now = time.time()
        seen = set()
        for t in torrents:
            done = t.get("progress", 0) >= 1
            if done and now - (t.get("completion_on") or 0) > KEEP:
                continue
            if not done and t.get("state") in ("pausedDL", "stoppedDL", "error", "missingFiles"):
                continue
            key = "qbt:" + t["hash"]
            seen.add(key)
            if key in self.items:
                self.items[key].update(progress=t.get("progress", 0), eta=t.get("eta"), done=done)
                if done and not self.items[key].get("path"):
                    f = biggest_video(t.get("content_path") or "")
                    self.items[key]["path"] = str(f) if f else None
                continue
            path = biggest_video(t.get("content_path") or "") if done else None
            card = self.identify(html.unescape(t["name"]), path, hint=self._hint(t.get("category")))
            if card:
                card.update(key=key, progress=t.get("progress", 0), eta=t.get("eta"), done=done, source="download",
                            added=t.get("completion_on") or t.get("added_on"))
                with self.lock:
                    self.items[key] = card
        with self.lock:
            for k in [k for k in self.items if k.startswith("qbt:") and k not in seen]:
                del self.items[k]
        self._drop_imported()
        self.sort_stuck()

    @staticmethod
    def _hint(category):
        c = (category or "").lower()
        return "movie" if c in ("radarr", "movies", "movie") else "episode" if c in ("sonarr", "tv", "anime") else None

    def add_file(self, path, progress=1.0, source="folder"):
        card = self.identify(Path(path).name, Path(path))
        if card:
            card.update(key="file:" + str(path), progress=progress, done=True, source=source, added=time.time())
            with self.lock:
                self.items[card["key"]] = card

    def identify(self, name, path=None, hint=None):
        from guessit import guessit
        opts = {"type": hint} if hint else {}
        g = guessit(Path(path).name if path else name, opts)
        if g.get("type") == "episode" and not g.get("season") and not g.get("episode"):
            g = guessit(name, opts)
        title = g.get("title")
        if not title:
            return None
        kind = "tv" if g.get("type") == "episode" else "movie"
        query = f"{title} {g['part']}" if g.get("part") else title
        card = {"provisional": True, "type": "Episode" if kind == "tv" else "Movie", "name": title,
                "series": title if kind == "tv" else None, "season": g.get("season"),
                "episode": g.get("episode") if not isinstance(g.get("episode"), list) else g["episode"][0],
                "year": g.get("year"), "path": str(path) if path else None, "quality": g.get("screen_size"),
                "poster": None, "backdrop": None, "tmdb": None, "id": None}
        match = self.matched.get(query)
        if match is None and self.app.seerr.ready:
            try:
                results = [r for r in self.app.seerr.search(query) if r["type"] == kind]
                if g.get("year"):
                    results.sort(key=lambda r: r.get("year") != str(g["year"]))
                match = results[0] if results else {}
            except Exception:
                match = {}
            self.matched[query] = match
        if match:
            card.update(poster=match.get("poster"), backdrop=match.get("backdrop"), tmdb=match.get("id"),
                        overview=match.get("overview", ""))
            if kind == "movie":
                card["name"] = match.get("title") or title
                card["year"] = match.get("year") or card["year"]
            else:
                card["series"] = match.get("title") or title
                card["name"] = g.get("episode_title") or f"Episode {card['episode']}"
        return card

    # ------------------------------------------------------------------ hand over to Jellyfin
    def _find(self, kind, tmdb):
        """Library items of this kind with this TMDB id (checked ourselves: the server may ignore the filter)."""
        hits = self.app.jf.items(Recursive="true", IncludeItemTypes=kind, AnyProviderIdEquals=f"tmdb.{tmdb}",
                                 HasTmdbId="true", Limit=50, Fields="Path,ProviderIds")
        return [h for h in hits if str((h.get("ProviderIds") or {}).get("Tmdb")) == str(tmdb)]

    def _drop_imported(self):
        """Once Jellyfin has the film/episode, the real entry replaces the provisional one."""
        jf = self.app.jf
        with self.lock:
            cards = [c for c in self.items.values() if c.get("done")]
        for c in cards:
            try:
                if c["type"] == "Movie" and c.get("tmdb"):
                    hit = self._find("Movie", c["tmdb"])
                elif c["type"] == "Episode" and c.get("tmdb"):
                    series = self._find("Series", c["tmdb"])
                    hit = series and [e for e in jf.get(f"/Shows/{series[0]['Id']}/Episodes", userId=jf.user,
                                                         season=c.get("season")).get("Items", [])
                                      if e.get("IndexNumber") == c.get("episode")]
                else:
                    continue
                if hit:
                    c["jellyfin_id"] = hit[0]["Id"]
            except Exception:
                pass

    # ------------------------------------------------------------------ fallback sorting
    def sort_stuck(self):
        """Downloads Jellyfin still hasn't picked up after a while get filed into the library by us: a hardlink
        (instant, no extra space, the torrent keeps seeding) into the show's/film's existing folder when Jellyfin
        knows the title, else a new folder named the way Jellyfin expects; then a scan of just that folder."""
        wait = settings.load().get("autosort_after", 120)
        if not wait:
            return
        now = time.time()
        with self.lock:
            stuck = [c for c in self.items.values() if c.get("done") and c.get("path") and not c.get("jellyfin_id")
                     and not c.get("sorted") and now - (c.get("added") or now) > wait * 60]
        for c in stuck:
            try:
                c["sorted"] = self._file_into_library(c)
            except Exception as e:
                c["sorted"] = f"failed: {e}"
                print("auto-sort failed:", c.get("path"), e)

    def _library_root(self, kind, anime):
        folders = self.app.jf.get("/Library/VirtualFolders") or []
        want = "movies" if kind == "Movie" else "tvshows"
        pick = None
        for f in folders:
            name = (f.get("Name") or "").lower()
            if anime and "anime" in name and f.get("Locations"):
                return f["Locations"][0 if kind == "Episode" else -1]
            if f.get("CollectionType") == want and f.get("Locations") and not pick:
                pick = f["Locations"][0]
        return pick

    def _file_into_library(self, c):
        jf = self.app.jf
        src = Path(c["path"])
        if os.stat(src).st_nlink > 1:  # Sonarr/Radarr already hardlinked it into the library; Jellyfin just hasn't caught up
            return "already imported by Sonarr/Radarr"
        clean =lambda s: "".join(ch for ch in (s or "") if ch not in '<>:"/\\|?*').strip()
        anime = "anime" in str(src).lower()
        existing = None
        if c.get("tmdb"):
            kind = "Movie" if c["type"] == "Movie" else "Series"
            hit = self._find(kind, c["tmdb"])
            existing = hit[0].get("Path") if hit else None
        if c["type"] == "Movie":
            folder = Path(existing).parent if existing and Path(existing).suffix else None
            if not folder:
                root = self._library_root("Movie", anime)
                if not root:
                    return "no films library in Jellyfin"
                folder = Path(root) / clean(f"{c['name']} ({c['year']})" if c.get("year") else c["name"])
            dest = folder / clean(f"{folder.name}{src.suffix}")
        else:
            show = Path(existing) if existing else None
            if not show:
                root = self._library_root("Episode", anime)
                if not root:
                    return "no shows library in Jellyfin"
                show = Path(root) / clean(c.get("series") or c["name"])
            season = c.get("season") or 1
            dest = show / f"Season {season:02d}" / clean(
                f"{show.name} - S{season:02d}E{(c.get('episode') or 0):02d}{src.suffix}")
        if dest.exists() or self._already_in(dest.parent, c):
            return "already there"
        if src.drive.lower() != dest.drive.lower():
            return "the download is on a different drive from the library (a copy would double the space): skipped"
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.link(src, dest)
        for sub in src.parent.glob(src.stem + "*"):  # subtitles that came with it
            if sub.suffix.lower() in (".srt", ".ass", ".ssa", ".vtt", ".sub") and not (dest.parent / (dest.stem + sub.name[len(src.stem):])).exists():
                os.link(sub, dest.parent / (dest.stem + sub.name[len(src.stem):]))
        jf.call("POST", "/Library/Media/Updated", json={"Updates": [{"Path": str(dest.parent), "UpdateType": "Created"}]})
        print("auto-sorted", src.name, "->", dest)
        self.app.notify_ui("toast", {"text": f"Filed {c.get('series') or c['name']} into your library"})
        return str(dest)

    @staticmethod
    def _already_in(folder, c):
        """The folder already holds this film (any video) or this episode (any SxxEyy name), under whatever name."""
        if not folder.is_dir():
            return False
        videos = [f.name.lower() for f in folder.iterdir() if f.suffix.lower() in VIDEO]
        if c["type"] == "Movie":
            return bool(videos)
        tag = f"s{(c.get('season') or 1):02d}e{(c.get('episode') or 0):02d}"
        return any(tag in v for v in videos)

    def list(self):
        with self.lock:
            cards = list(self.items.values())
        ready = sorted([c for c in cards if c.get("done") and c.get("path") and not c.get("jellyfin_id")],
                       key=lambda c: -(c.get("added") or 0))
        coming = sorted([c for c in cards if not c.get("done")], key=lambda c: -(c.get("progress") or 0))
        return {"ready": ready, "coming": coming}

    def get(self, key):
        return self.items.get(key)
