"""Thin clients for the Sonarr / Radarr v3 APIs, plus the bits of matching logic
that decide which series/movie a release belongs to."""
import difflib
import re
import time
from pathlib import Path

import requests

log = None  # set by mediasorter.setup_logging


def norm(s):
    s = (s or "").lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def similarity(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


# Rejections meaning the library already holds this file at equal/better quality.
SKIP_REASONS = r"not an upgrade|already imported|existing file|same as existing"


class ArrError(Exception):
    pass


class ArrClient:
    def __init__(self, name, cfg):
        """cfg is the app's entry in mediastack.json: {"url": ..., "key": ...}."""
        self.name = name
        self.url = (cfg.get("url") or "").rstrip("/")
        self.key = cfg.get("key") or cfg.get("api_key")
        self.s = requests.Session()
        self.s.headers["X-Api-Key"] = self.key or ""

    def available(self):
        if not self.key:
            return False
        try:
            self.get("system/status", timeout=5)
            return True
        except Exception:
            return False

    def _req(self, method, path, timeout=60, **kw):
        r = self.s.request(method, f"{self.url}/api/v3/{path}", timeout=timeout, **kw)
        if r.status_code >= 400:
            raise ArrError(f"{self.name} {method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else None

    def get(self, path, **kw):
        return self._req("GET", path, **kw)

    def post(self, path, body, **kw):
        return self._req("POST", path, json=body, **kw)

    def put(self, path, body, **kw):
        return self._req("PUT", path, json=body, **kw)

    def command(self, body, wait=True, timeout=1800):
        cmd = self.post("command", body)
        if not wait:
            return cmd
        deadline = time.time() + timeout
        while time.time() < deadline:
            cmd = self.get(f"command/{cmd['id']}")
            if cmd["status"] in ("completed", "failed", "aborted", "cancelled", "orphaned"):
                return cmd
            time.sleep(2)
        raise ArrError(f"{self.name} command {body['name']} timed out")

    def quality_profile_id(self):
        profiles = self.get("qualityprofile")
        for p in profiles:
            if p["name"].lower() == "any":
                return p["id"]
        return profiles[0]["id"]

    def ensure_root(self, path):
        roots = self.get("rootfolder")
        for r in roots:
            if r["path"].rstrip("/\\").lower() == str(path).rstrip("/\\").lower():
                return r["path"]
        return self.post("rootfolder", {"path": path})["path"]


def pick_best(results, title, year, title_keys=("title",), anime_hint=False):
    """Choose a lookup result. The *arrs return results in relevance order, so the
    first one wins unless a later one is a clearly better title/year match."""
    def sim(r):
        names = [r.get(k) for k in title_keys if r.get(k)]
        names += [a.get("title") for a in r.get("alternateTitles") or [] if a.get("title")]
        return max((similarity(title, n) for n in names), default=0.0)

    def year_ok(r):
        return not year or not r.get("year") or abs(int(r["year"]) - int(year)) <= 1

    top = results[:10]
    if not top:
        return None
    first, first_sim = top[0], sim(top[0])
    # Romaji/alias names ("Sousou no Frieren") score poorly against the English
    # title, so only override the search engine's pick on strong evidence.
    for r in top[1:]:
        s = sim(r)
        if s >= 0.9 and first_sim < 0.9 and year_ok(r):
            return r
        if year and not year_ok(first) and year_ok(r) and s >= 0.6:
            return r
    # Romaji names ("Shingeki no Kyojin") share no words with the English title,
    # so for anime releases trust the search engine when its pick is an anime.
    if plausible(title, first, title_keys) or (anime_hint and looks_anime(first)):
        return first
    return None


def plausible(title, r, title_keys):
    """Reject search hits that share nothing with what we searched for (the *arrs
    always return *something*, even for gibberish)."""
    names = [r.get(k) for k in title_keys if r.get(k)]
    names += [a.get("title") for a in r.get("alternateTitles") or [] if a.get("title")]
    q = norm(title)
    words = {w for w in q.split() if len(w) >= 4}
    for n in map(norm, names):
        if similarity(q, n) >= 0.5 or q.replace(" ", "") in n.replace(" ", ""):
            return True
        if words & {w for w in n.split() if len(w) >= 4}:
            return True
    return False


def looks_anime(item):
    genres = [g.lower() for g in item.get("genres") or []]
    lang = ((item.get("originalLanguage") or {}).get("name") or "").lower()
    if "anime" in genres:
        return True
    return "animation" in genres and lang == "japanese"


class Sonarr(ArrClient):
    def __init__(self, cfg):
        super().__init__("Sonarr", cfg)

    def lookup(self, title, year=None, anime_hint=False):
        m = re.fullmatch(r"(.+) (US|UK|AU|CA|NZ)", title)
        if m:
            # Regional versions are titled "Show (US)" on TVDB; the original usually has no suffix.
            base, code = m.groups()
            res = self.get("series/lookup", params={"term": base}) or []
            tagged = [r for r in res if f"({code})" in r["title"]]
            untagged = [r for r in res if not re.search(r"\((US|UK|AU|CA|NZ)\)", r["title"])]
            for cands in (tagged, untagged if code == "UK" else []):
                best = pick_best(cands, base, year, anime_hint=anime_hint)
                if best:
                    return best
        for term in ([f"{title} {year}"] if year else []) + [title]:
            res = self.get("series/lookup", params={"term": term})
            best = pick_best(res or [], title, year, anime_hint=anime_hint)
            if best:
                return best
        return None

    def ensure_series(self, found, anime, roots):
        existing = {s["tvdbId"]: s for s in self.get("series")}
        if found["tvdbId"] in existing:
            return existing[found["tvdbId"]], False
        root = self.ensure_root(roots["anime"] if anime else roots["tv"])
        body = dict(found)
        body.update({
            "rootFolderPath": root,
            "qualityProfileId": self.quality_profile_id(),
            "seriesType": "anime" if anime else "standard",
            "seasonFolder": True,
            "monitored": True,
            "addOptions": {"monitor": "none", "searchForMissingEpisodes": False,
                           "searchForCutoffUnmetEpisodes": False},
        })
        body.pop("id", None)
        series = self.post("series", body)
        log.info("Sonarr: added series %r (tvdb %s, %s)", series["title"], series["tvdbId"], body["seriesType"])
        # Wait until episodes are populated so manual import can map files.
        for _ in range(60):
            if self.get("episode", params={"seriesId": series["id"]}):
                break
            time.sleep(2)
        return series, True

    def import_files(self, series, files, mode):
        """Manual-import `files` into `series`. Returns the set of files that imported."""
        Path(series["path"]).mkdir(parents=True, exist_ok=True)  # Sonarr 500s if it is missing
        wanted = {str(f).lower() for f in files}
        items = []
        # Note: passing seriesId here would make Sonarr scan the series folder instead.
        for f in files:
            items += [i for i in self.get("manualimport", params={"folder": str(f), "filterExistingFiles": "false"},
                                          timeout=300) or [] if i["path"].lower() in wanted]
        # Re-evaluate anything Sonarr didn't tie to our series (romaji names etc.)
        # against the series we picked - this is what its manual-import UI does.
        redo = [i for i in items if (i.get("series") or {}).get("id") != series["id"] or not i.get("episodes")]
        if redo:
            fixed = self.post("manualimport", [{
                "id": i["id"], "path": i["path"], "seriesId": series["id"], "seasonNumber": None,
                "episodeIds": [], "releaseGroup": i.get("releaseGroup"), "quality": i.get("quality"),
                "languages": i.get("languages") or [], "downloadId": "", "indexerFlags": 0,
                "releaseType": i.get("releaseType", "unknown"),
            } for i in redo]) or []
            by_path = {i["path"].lower(): i for i in fixed}
            items = [by_path.get(i["path"].lower(), i) for i in items]
        payload, handled = [], set()
        for it in items:
            reasons = " | ".join(r.get("reason", "") for r in it.get("rejections") or [])
            if not it.get("episodes"):
                log.warning("Sonarr: no episode match for %s (%s)", it["path"], reasons)
                continue
            if re.search(SKIP_REASONS, reasons, re.I):
                # Library already has this episode at equal/better quality - nothing to do.
                log.info("Sonarr: skipping %s (%s)", it["path"], reasons)
                handled.add(it["path"].lower())
                continue
            payload.append({
                "path": it["path"], "folderName": it.get("folderName", ""),
                "seriesId": series["id"], "episodeIds": [e["id"] for e in it["episodes"]],
                "quality": it.get("quality"), "languages": it.get("languages") or [],
                "releaseGroup": it.get("releaseGroup") or "", "indexerFlags": it.get("indexerFlags", 0),
                "releaseType": "multiEpisode" if len(it["episodes"]) > 1 else "singleEpisode", "downloadId": "",
            })
        if not payload:
            return handled
        cmd = self.command({"name": "ManualImport", "importMode": mode, "files": payload})
        if cmd["status"] != "completed":
            log.error("Sonarr ManualImport %s: %s", cmd["status"], cmd.get("message"))
        # Verify which episodes now have files.
        eps = {e["id"]: e for e in self.get("episode", params={"seriesId": series["id"]})}
        ok = handled
        for p in payload:
            if all(eps.get(i, {}).get("hasFile") for i in p["episodeIds"]):
                ok.add(p["path"].lower())
        return ok


class Radarr(ArrClient):
    def __init__(self, cfg):
        super().__init__("Radarr", cfg)

    def lookup(self, title, year=None, anime_hint=False):
        for term in ([f"{title} {year}"] if year else []) + [title]:
            res = self.get("movie/lookup", params={"term": term})
            best = pick_best(res or [], title, year, title_keys=("title", "originalTitle"), anime_hint=anime_hint)
            if best:
                return best
        return None

    def ensure_movie(self, found, anime, roots):
        existing = {m["tmdbId"]: m for m in self.get("movie")}
        if found["tmdbId"] in existing:
            return existing[found["tmdbId"]], False
        root = self.ensure_root(roots["anime_movies"] if anime else roots["movies"])
        body = dict(found)
        body.update({
            "rootFolderPath": root,
            "qualityProfileId": self.quality_profile_id(),
            "monitored": True, "minimumAvailability": "released",
            "addOptions": {"searchForMovie": False, "monitor": "movieOnly"},
        })
        body.pop("id", None)
        movie = self.post("movie", body)
        log.info("Radarr: added movie %r (%s, tmdb %s)", movie["title"], movie.get("year"), movie["tmdbId"])
        return movie, True

    def import_file(self, movie, f, mode):
        Path(movie["path"]).mkdir(parents=True, exist_ok=True)
        # As with Sonarr, movieId would make Radarr scan the movie folder instead of `f`.
        items = self.get("manualimport", params={"folder": str(f), "filterExistingFiles": "false"}, timeout=300) or []
        it = next((i for i in items if i["path"].lower() == str(f).lower()), None)
        if it is None:
            log.warning("Radarr: manual import did not list %s", f)
            return False
        reasons = " | ".join(r.get("reason", "") for r in it.get("rejections") or [])
        if re.search(SKIP_REASONS, reasons, re.I):
            log.info("Radarr: skipping %s (%s)", f, reasons)
            return True  # already in the library at equal/better quality
        quality = it.get("quality") or self.get("parse", params={"title": Path(f).name}).get("parsedMovieInfo", {}).get("quality")
        cmd = self.command({"name": "ManualImport", "importMode": mode, "files": [{
            "path": it["path"], "folderName": it.get("folderName", ""), "movieId": movie["id"],
            "quality": quality, "languages": it.get("languages") or [],
            "releaseGroup": it.get("releaseGroup") or "", "indexerFlags": it.get("indexerFlags", 0),
            "downloadId": "",
        }]})
        if cmd["status"] != "completed":
            log.error("Radarr ManualImport %s: %s", cmd["status"], cmd.get("message"))
        return bool(self.get(f"movie/{movie['id']}").get("hasFile"))
