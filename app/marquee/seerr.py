"""Seerr (Overseerr/Jellyseerr) client: search, trending, and requests sent with your remembered choices.

Signs in with the same Jellyfin login (Seerr's own Jellyfin sign-in) and keeps that session cookie."""
import os

import requests

from . import settings

IMG = "https://image.tmdb.org/t/p/"


class Seerr:
    def __init__(self):
        self.s = requests.Session()
        self.reload()

    def reload(self):
        st = settings.load()
        self.base = (os.environ.get("MARQUEE_DEV_SEERR") or st["seerr"] or "").rstrip("/")
        self.s.cookies.clear()
        self.s.headers.pop("X-Api-Key", None)
        self.s.headers.pop("X-Api-User", None)
        if os.environ.get("MARQUEE_DEV_SEERR_KEY"):  # development: API key acting as a user
            self.s.headers["X-Api-Key"] = os.environ["MARQUEE_DEV_SEERR_KEY"]
            self.s.headers["X-Api-User"] = os.environ.get("MARQUEE_DEV_SEERR_USER", "1")
        elif st["seerr_cookie"]:
            self.s.cookies.set("connect.sid", st["seerr_cookie"])

    @property
    def ready(self):
        return bool(self.base and (self.s.cookies.get("connect.sid") or "X-Api-Key" in self.s.headers))

    def call(self, method, path, **kw):
        kw.setdefault("timeout", 20)
        r = self.s.request(method, f"{self.base}/api/v1{path}", **kw)
        if r.status_code in (401, 403):
            raise PermissionError("Seerr sign-in expired")
        r.raise_for_status()
        return r.json() if r.content else None

    def login(self, server, user, password):
        base = server.rstrip("/")
        s = requests.Session()
        r = s.post(f"{base}/api/v1/auth/jellyfin", json={"username": user, "password": password}, timeout=20)
        r.raise_for_status()
        settings.save({"seerr": base, "seerr_cookie": s.cookies.get("connect.sid") or ""})
        self.reload()

    # ------------------------------------------------------------------ discovery
    @staticmethod
    def card(r):
        if r.get("mediaType") not in ("movie", "tv"):
            return None
        info = r.get("mediaInfo") or {}
        date = r.get("releaseDate") or r.get("firstAirDate") or ""
        anime = r.get("mediaType") == "tv" and r.get("originalLanguage") == "ja" and 16 in (r.get("genreIds") or [])
        return {"id": r["id"], "type": r["mediaType"], "title": r.get("title") or r.get("name"), "year": date[:4],
                "overview": r.get("overview") or "", "anime": anime,
                "poster": IMG + "w500" + r["posterPath"] if r.get("posterPath") else None,
                "backdrop": IMG + "original" + r["backdropPath"] if r.get("backdropPath") else None,
                "rating": r.get("voteAverage"), "status": info.get("status"), "genre_ids": r.get("genreIds") or [],
                "jellyfin": info.get("jellyfinMediaId"), **Seerr._removed(r["mediaType"], r["id"], info)}

    removed_lookup = None  # set by the catalog: fn(kind, tmdb) -> {removed, played, state, ...} for removed titles

    @staticmethod
    def _removed(kind, tmdb, info):
        """A title deleted to save space (and not back yet) keeps its watched state on its card."""
        fn = Seerr.removed_lookup
        if fn is None or info.get("status") in (4, 5):
            return {}
        try:
            return fn(kind, tmdb) or {}
        except Exception:
            return {}

    def cards(self, path, **params):
        data = self.call("GET", path, params=params) or {}
        return [c for c in map(self.card, data.get("results", [])) if c]

    def search(self, q):
        return self.cards(f"/search?query={requests.utils.quote(q, safe='')}&page=1")

    def discover(self):
        return {"trending": self.cards("/discover/trending"), "movies": self.cards("/discover/movies/upcoming"),
                "popular_movies": self.cards("/discover/movies"), "popular_tv": self.cards("/discover/tv")}

    def by_genre(self, genres, kind="any", limit=20):
        """Popular titles in these genres that aren't in the library yet (TMDB genre ids)."""
        from .ai import TMDB_MOVIE, TMDB_TV
        out = []
        for k, table, path in (("movie", TMDB_MOVIE, "/discover/movies"), ("tv", TMDB_TV, "/discover/tv")):
            if kind not in ("any", k):
                continue
            ids = sorted({str(table[g]) for g in genres if g in table})
            if ids:
                out += self.cards(path, genre=",".join(ids))
        return [c for c in out if c.get("status") != 5][:limit]

    def collection_rest(self, tmdb):
        """Other films in a film's collection (sequels, prequels) that aren't in the library yet."""
        d = self.call("GET", f"/movie/{tmdb}")
        col = (d.get("collection") or {}).get("id")
        if not col:
            return None, []
        c = self.call("GET", f"/collection/{col}")
        parts = [self.card({**p, "mediaType": "movie"}) for p in c.get("parts", [])]
        return c.get("name"), [p for p in parts if p and p.get("status") != 5 and p["id"] != int(tmdb)]

    def recommended(self, kind, tmdb, limit=12):
        data = self.call("GET", f"/{kind}/{tmdb}/recommendations") or {}
        cards = [self.card({**r, "mediaType": r.get("mediaType") or kind}) for r in data.get("results", [])]
        return [c for c in cards if c and c.get("status") != 5][:limit]

    def details(self, kind, tmdb):
        d = self.call("GET", f"/{kind}/{tmdb}")
        out = self.card({**d, "mediaType": kind, "genreIds": [g["id"] for g in d.get("genres", [])]})
        out["seasons"] = [{"number": s["seasonNumber"], "name": s.get("name"), "episodes": s.get("episodeCount")}
                          for s in d.get("seasons", []) if s.get("seasonNumber", 0) > 0]
        out["runtime"] = d.get("runtime")
        out["genres"] = [g["name"] for g in d.get("genres", [])]
        return out

    def options(self, kind):
        """Quality profiles and folders offered by the server Seerr sends this kind to."""
        service = "radarr" if kind == "movie" else "sonarr"
        servers = self.call("GET", f"/service/{service}") or []
        out = []
        for srv in servers:
            d = self.call("GET", f"/service/{service}/{srv['id']}")
            out.append({"id": srv["id"], "name": srv.get("name"), "is4k": srv.get("is4k"),
                        "default_profile": srv.get("activeProfileId"),
                        "anime_profile": srv.get("activeAnimeProfileId"),
                        "profiles": [{"id": p["id"], "name": p["name"]} for p in d.get("profiles", [])]})
        return out

    @staticmethod
    def request_body(kind, tmdb, profile=None, server=None, seasons="all", is4k=False, root_folder=None):
        body = {"mediaType": kind, "mediaId": int(tmdb), "is4k": bool(is4k)}
        if kind == "tv":
            body["seasons"] = seasons
        if profile:
            body["profileId"] = int(profile)
        if server is not None:
            body["serverId"] = int(server)
        if root_folder:
            body["rootFolder"] = str(root_folder)
        return body

    def request(self, kind, tmdb, profile=None, server=None, seasons="all", is4k=False, root_folder=None,
                dry_run=False):
        """Send a request to Seerr. dry_run: only build the body Seerr would get (nothing is sent)."""
        body = self.request_body(kind, tmdb, profile, server, seasons, is4k, root_folder)
        if dry_run:
            return {"dry_run": True, "body": body}
        return self.call("POST", "/request", json=body)

    def my_requests(self):
        me = self.call("GET", "/auth/me") or {}
        data = self.call("GET", "/request", params={"take": 30, "sort": "added", "requestedBy": me.get("id")}) or {}
        out = []
        for r in data.get("results", []):
            m = r.get("media") or {}
            info = {}
            try:
                d = self.call("GET", f"/{'movie' if r['type'] == 'movie' else 'tv'}/{m.get('tmdbId')}")
                info = self.card({**d, "mediaType": r["type"]}) or {}
            except Exception:
                pass
            out.append({"id": r["id"], "type": r["type"], "tmdb": m.get("tmdbId"), "status": m.get("status"),
                        "created": r.get("createdAt"), "title": info.get("title"), "poster": info.get("poster"),
                        "year": info.get("year")})
        return out
