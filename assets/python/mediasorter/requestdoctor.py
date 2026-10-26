"""Request doctor: works out why each Seerr request isn't watchable yet, and fixes what it safely can.

Part of Media Stack Setup. Runs from the hourly health check and the Media Hub's "Why not here?" button. It follows every
request that isn't fully available into Sonarr/Radarr and the download queue:
  * not in Sonarr/Radarr at all      -> asks Seerr to send it again
  * not released yet                 -> says when it will be (nothing to fix)
  * an anime profile on a show that isn't anime -> moves it to the normal profile
  * not monitored                    -> monitors it
  * downloaded but Seerr hasn't noticed -> runs Seerr's Jellyfin scan
  * downloading                      -> how far along it is (stalled downloads are Cleanuparr's job); a film
                                        waiting in qBittorrent's queue is moved to the front
  * nothing downloading              -> searches the indexers (each item at most every 6 hours). If a
                                        release qualifies it's grabbed; if none do, it explains why every
                                        release was turned down (no seeders, too small, blocklisted...)
It also keeps upgrades going: Sonarr/Radarr only look for better versions in new RSS posts, so each run
searches a few items that are below their quality target (each at most once a day).

Writes requests.json in the Media Hub's state folder. Usage: requestdoctor.py [--quiet]
"""
import collections
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import arr  # noqa: E402
import stackconf  # noqa: E402

RESEARCH_AFTER = 6 * 3600   # search a stuck item again after this long
UPGRADE_AFTER = 24 * 3600   # look for a better version of the same item at most this often
SEARCH_BUDGET = 6           # indexer test searches per run (each can take a minute)
TEST_TIME = 5 * 60          # ...and no new test searches after this long; background searches instead
COMMAND_BUDGET = 8          # background searches started per run
PER_ITEM = 2                # searches per request per run, so one big show can't starve the rest
MAX_SEARCHES = 2            # searches an app may have queued/running at once (it runs 3 commands at a time)
SEARCH_COMMANDS = {"SeriesSearch", "SeasonSearch", "EpisodeSearch", "MoviesSearch", "MissingEpisodeSearch",
                   "MissingMoviesSearch", "CutoffUnmetEpisodeSearch", "CutoffUnmetMoviesSearch"}
UPGRADE_BUDGET = 4          # upgrade searches per app per run
SEARCH_TIMEOUT = 150
JAPANESE = {"Japanese"}
NOTHING = "no indexer has it - only more indexers (Prowlarr) can help"
NONE_USABLE = "none usable"

# Sonarr/Radarr rejection texts -> plain words.
REASONS = [
    (r"Not enough seeders", "no one is sharing it (0 seeders)"),
    (r"smaller than minimum allowed", "file too small to be real"),
    (r"larger than maximum allowed", "file too big"),
    (r"blocklist", "blocklisted after an earlier download failed"),
    (r"Custom Formats? (.*) ha(?:s|ve) score", None),  # handled below: names the formats
    (r"(\S+) is not wanted in profile", None),
    (r"not an upgrade|equal or higher (preference|quality)|Existing file", "not better than the copy you have"),
    (r"already in queue|in queue already|Release in queue", "already downloading"),
    (r"Unable to identify|Unknown (Series|Movie)|Wrong (series|movie)|doesn't match|mapping", "release name doesn't match"),
    (r"Language", "wrong language"),
    (r"not (yet )?(available|released)|minimum availability", "not released yet"),
    (r"retention|older than", "too old for the indexer"),
    (r"(Episode|Season).*(wasn't|not) (requested|wanted|monitored)|Full season pack|Season pack", "covers episodes that aren't wanted"),
]


def plain(rejection):
    for pattern, text in REASONS:
        m = re.search(pattern, rejection, re.I)
        if not m:
            continue
        if text:
            return text
        if "Custom Format" in pattern:
            names = (m.group(1) or "").strip(" ,")
            return f"blocked by the quality rules ({names})" if names else "below the quality rules' minimum score"
        return f"quality not wanted ({m.group(1)})"
    return rejection[:80]


def parse_time(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def nice_date(ts):
    return datetime.fromtimestamp(ts).strftime("%b %d").replace(" 0", " ")


def paths(cfg):
    """(report, state, lock) in the hub's state folder, where the hub reads the report."""
    d = stackconf.own_state(cfg, "hub_state_dir")
    return d / "requests.json", d / "requestdoctor-state.json", d / "requestdoctor.lock"


class Doctor:
    def __init__(self, cfg, quiet=False):
        global OUT, STATE, LOCK
        OUT, STATE, LOCK = paths(cfg)
        self.cfg = cfg
        self.sonarr = arr.Sonarr(stackconf.app(cfg, "sonarr")) if stackconf.has(cfg, "sonarr") else None
        self.radarr = arr.Radarr(stackconf.app(cfg, "radarr")) if stackconf.has(cfg, "radarr") else None
        self.seerr_url = stackconf.url(cfg, "seerr", "/api/v1")
        self.seerr_h = {"X-Api-Key": stackconf.key(cfg, "seerr")}
        self.quiet = quiet
        try:
            self.state = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            self.state = {}
        self.state.setdefault("searched", {})
        self.state.setdefault("upgraded", {})
        self.state.setdefault("stuck_since", {})
        self.budget, self.commands = SEARCH_BUDGET, COMMAND_BUDGET
        self.test_until = time.time() + TEST_TIME
        self.fixed, self.searches, self.items, self.problems = [], [], [], []
        self.seen, self.recycle_bin, self.slots = set(), False, {}
        self.seerr_scan = False

    def log(self, *a):
        if not self.quiet:
            print(*a)

    def seerr(self, method, path, **kw):
        r = requests.request(method, self.seerr_url + path, headers=self.seerr_h, timeout=kw.pop("timeout", 30), **kw)
        r.raise_for_status()
        return r.json() if r.content else None

    def fix(self, item, text):
        item["fixed"].append(text)
        self.fixed.append(f"{item['title']}: {text}")
        self.log("  FIXED:", text)

    # ------------------------------------------------------------------ searching
    def due(self, key, every=RESEARCH_AFTER, table="searched"):
        return time.time() - self.state[table].get(key, 0) > every

    def test_search(self, client, params):
        """An interactive search: what the indexers have and why each release was or wasn't taken.
        Returns None when the indexers are too slow to answer."""
        try:
            return client.get("release", params=params, timeout=SEARCH_TIMEOUT)
        except Exception as e:
            self.log("  test search failed:", e)
            return None

    @staticmethod
    def explain(releases):
        reasons = collections.Counter()
        for r in releases:
            for why in {plain(x) for x in r.get("rejections") or []}:
                reasons[why] += 1
        top = ", ".join(f"{why} ({n})" for why, n in reasons.most_common(3))
        return f"{len(releases)} release{'s' if len(releases) != 1 else ''} found, none usable: {top}"

    @staticmethod
    def free_slots(client):
        """How many more searches this app should be given now. Sonarr/Radarr run three commands at once,
        and a long search holds one for hours; one slot always stays free for imports and RSS."""
        try:
            busy = sum(1 for c in client.get("command") if c.get("status") in ("queued", "started")
                       and c.get("name") in SEARCH_COMMANDS)
        except Exception:
            return 0
        return max(0, MAX_SEARCHES - busy)

    @staticmethod
    def best(releases):
        ok = [r for r in releases if not r.get("rejected") and r.get("downloadAllowed", True)]
        # Sonarr/Radarr's own order: quality, then custom format score, then seeders.
        return max(ok, key=lambda r: (r.get("qualityWeight", 0), r.get("customFormatScore", 0),
                                      r.get("seeders") or 0), default=None)

    def search(self, item, client, key, params, command, label=""):
        """Search for a stuck item: a test search (grab the best usable release, or say why there's none)
        while the run's time allows, otherwise a normal background search in Sonarr/Radarr."""
        why = self.state.setdefault("why", {})
        if not self.due(key):
            ago = (time.time() - self.state["searched"][key]) / 3600
            return f"{why.get(key, 'searched')} (checked {f'{ago:.0f} h ago' if ago >= 1 else 'just now'})"
        testing = self.budget > 0 and time.time() < self.test_until
        can_queue = self.commands > 0 and self.slots.get(client.name, 0) > 0
        if (not testing and not can_queue) or item.get("_turns", 0) >= PER_ITEM:
            return why.get(key, "not searched yet") + " (its turn comes on a later check)"
        item["_turns"] = item.get("_turns", 0) + 1  # share the run's searches between requests
        self.state["searched"][key] = time.time()
        releases = None
        if testing:
            self.budget -= 1
            releases = self.test_search(client, params)
            if releases is None:
                self.test_until = 0  # indexers are slow today: background searches for the rest
        if releases is None and not can_queue:
            self.state["searched"][key] = time.time() - RESEARCH_AFTER + 3600  # try again in an hour
            why[key] = f"the indexers were too slow to answer and {client.name} is busy searching; trying again later"
        elif releases is None:
            self.commands -= 1
            self.slots[client.name] -= 1
            client.command(command, wait=False)
            self.searches.append(f"{item['title']}{label}")
            why[key] = "searching in the background" + (" (the indexers were too slow for a test search)" if testing else "")
        elif self.best(releases):
            pick = self.best(releases)
            try:
                client.post("release", {"guid": pick["guid"], "indexerId": pick["indexerId"]}, timeout=60)
                self.fix(item, f"grabbed {pick['title'][:70]}{label}")
                why[key] = f"grabbed {pick['title'][:60]}"
            except Exception as e:  # usually qBittorrent busy for a moment
                self.state["searched"][key] = time.time() - RESEARCH_AFTER + 1800  # retry in 30 min
                why[key] = f"found {pick['title'][:50]} but couldn't hand it to the download client ({str(e)[:80]})"
        elif not releases:
            why[key] = NOTHING
        else:
            why[key] = self.explain(releases)
        return why[key]
    # ------------------------------------------------------------------ movies
    def movie(self, item, req, queue, profiles):
        if not self.radarr:
            item.update(state="stuck", reason="Radarr isn't installed")
            return
        tmdb = req["media"]["tmdbId"]
        found = self.radarr.get("movie", params={"tmdbId": tmdb})
        if not found:
            self.resend(item, req)
            return
        m = found[0]
        item["title"], item["year"] = m["title"], m.get("year")
        item["profile"] = profiles.get(m.get("qualityProfileId"))
        if m.get("hasFile") and self.bad_file(m, item):
            m["hasFile"] = False
        if m.get("hasFile"):
            q = ((m.get("movieFile") or {}).get("quality") or {}).get("quality", {}).get("name", "")
            item.update(state="available", reason=f"downloaded ({q}); Seerr marks it available after its next scan")
            self.seerr_scan = True
            return
        if not m.get("monitored"):
            self.radarr.put("movie/editor", {"movieIds": [m["id"]], "monitored": True})
            self.fix(item, "it wasn't monitored, so Radarr would never download it; now it is")
        if not m.get("isAvailable"):
            item["state"] = "waiting"
            digital = [parse_time(m.get(k)) for k in ("digitalRelease", "physicalRelease")]
            future = sorted(t for t in digital if t and t > time.time())
            cinema = parse_time(m.get("inCinemas"))
            if future:
                item["reason"] = f"not out for home viewing yet: released digitally around {nice_date(future[0])}"
            elif cinema and cinema <= time.time():
                item["reason"] = (f"only in cinemas so far (since {nice_date(cinema)}); it downloads once a home "
                                  "release comes out")
            elif cinema:
                item["reason"] = f"not released yet: in cinemas {nice_date(cinema)}"
            else:
                item["reason"] = "not released yet (no release date announced)"
            return
        q = [r for r in queue["radarr"] if r.get("movieId") == m["id"]]
        if q:
            waiting = [r["downloadId"] for r in q if (r.get("status") or "").lower() == "queued" and r.get("downloadId")]
            if waiting and self.qbt_top(waiting):
                self.fix(item, "it was waiting behind other downloads; moved it to the front of qBittorrent's queue")
            self.downloading(item, q)
            return
        item["state"] = "searching"
        item["reason"] = self.search(item, self.radarr, f"movie:{m['id']}", {"movieId": m["id"]},
                                     {"name": "MoviesSearch", "movieIds": [m["id"]]})
        if item["fixed"] and item["reason"].startswith("grabbed"):
            item["state"] = "downloading"
        self.settle(item, [item["reason"]])

    def bad_file(self, m, item=None):
        """A film file far shorter than the film (half a movie, a trailer, a fake) gets blocklisted and sent
        to Radarr's recycle bin, so Radarr finds a proper copy. Returns True if it was removed."""
        f = m.get("movieFile") or {}
        rt = re.match(r"(?:(\d+):)?(\d+):(\d+)", (f.get("mediaInfo") or {}).get("runTime") or "")
        if not rt or (m.get("runtime") or 0) < 60:
            return False
        minutes = int(rt.group(1) or 0) * 60 + int(rt.group(2)) + int(rt.group(3)) / 60
        if minutes >= 0.7 * m["runtime"]:
            return False
        what = f"the file was only {minutes:.0f} of the film's {m['runtime']} minutes"
        if not self.recycle_bin:
            text = f"{what} - delete it in Radarr so a proper copy is found"
            if item:
                item["note"] = text
            else:
                self.problems.append(f"{m['title']}: {text}")
            return False
        for h in self.radarr.get("history/movie", params={"movieId": m["id"]}):
            if h["eventType"] == "grabbed" and h.get("sourceTitle") == f.get("sceneName"):
                self.radarr.post(f"history/failed/{h['id']}", {})
                break
        self.radarr._req("DELETE", f"moviefile/{f['id']}")
        text = f"{what}; blocklisted that release and moved the file to Radarr's recycle bin"
        if item:
            self.fix(item, text)
        else:
            self.fixed.append(f"{m['title']}: {text}")
        return True

    def recent_files(self):
        """The same check for every film imported in the last three days, requested or not."""
        for m in self.radarr.get("movie"):
            added = parse_time((m.get("movieFile") or {}).get("dateAdded"))
            if m.get("hasFile") and added and time.time() - added < 3 * 86400 and f"movie:{m.get('tmdbId')}" not in self.seen:
                try:
                    self.bad_file(m)
                except Exception as e:
                    self.log("file check failed:", m.get("title"), e)

    # ------------------------------------------------------------------ shows
    def show(self, item, req, queue, profiles, defaults):
        if not self.sonarr:
            item.update(state="stuck", reason="Sonarr isn't installed")
            return
        tvdb = req["media"].get("tvdbId")
        found = self.sonarr.get("series", params={"tvdbId": tvdb}) if tvdb else []
        if not found:
            self.resend(item, req)
            return
        s = found[0]
        item["title"], item["year"] = s["title"], s.get("year")
        lang = (s.get("originalLanguage") or {}).get("name")
        anime_profile = defaults.get("anime")
        if lang and lang not in JAPANESE and (s.get("seriesType") == "anime" or s.get("qualityProfileId") == anime_profile):
            s["seriesType"], s["qualityProfileId"] = "standard", defaults.get("standard") or s["qualityProfileId"]
            self.sonarr.put(f"series/{s['id']}", s)
            self.fix(item, f"it's a {lang} show but was set up as anime (anime-only release rules); "
                           f"moved it to the {profiles.get(s['qualityProfileId'], 'normal')} profile")
        elif lang in JAPANESE and s.get("seriesType") != "anime":
            item["note"] = "looks like anime but is set up as a normal show (anime releases may be missed)"
        item["profile"] = profiles.get(s.get("qualityProfileId"))
        if not s.get("monitored"):
            s["monitored"] = True
            self.sonarr.put(f"series/{s['id']}", s)
            self.fix(item, "the show wasn't monitored; now it is")

        wanted = {x["seasonNumber"] for x in req.get("seasons") or []} or None
        now = time.time()
        episodes = [e for e in self.sonarr.get("episode", params={"seriesId": s["id"]})
                    if e["seasonNumber"] > 0 and (wanted is None or e["seasonNumber"] in wanted)]
        aired = [e for e in episodes if (parse_time(e.get("airDateUtc")) or now + 1) <= now]
        missing = [e for e in aired if not e.get("hasFile") and e.get("monitored")]
        unmonitored = [e for e in aired if not e.get("hasFile") and not e.get("monitored")]
        upcoming = sorted(parse_time(e["airDateUtc"]) for e in episodes
                          if e.get("airDateUtc") and parse_time(e["airDateUtc"]) > now)
        have = sum(1 for e in aired if e.get("hasFile"))
        item["progress"] = f"{have} of {len(aired)} aired episodes"
        if not missing:
            if unmonitored:
                item.update(state="stuck", reason=f"{len(unmonitored)} aired episode(s) aren't monitored - "
                                                  "monitor them in Sonarr if you want them")
            elif upcoming:
                item.update(state="airing", reason=f"every aired episode is here; the next one airs {nice_date(upcoming[0])}")
            else:
                item.update(state="available", reason="every episode is here; Seerr catches up after its next scan")
                self.seerr_scan = True
            return
        q = [r for r in queue["sonarr"] if r.get("seriesId") == s["id"]]
        queued = {r.get("episodeId") for r in q}
        by_season = collections.defaultdict(list)
        for e in missing:
            if e["id"] not in queued:
                by_season[e["seasonNumber"]].append(e)
        if not by_season:
            self.downloading(item, q)
            return
        item["state"] = "downloading" if q else "searching"
        details, whys = [], []
        due = [n for n in by_season if self.due(f"season:{s['id']}:{n}")]
        if len(due) > 3 and self.commands > 0 and self.slots.get(self.sonarr.name, 0) > 0 and s.get("seriesType") != "anime":
            # Lots of seasons to find: one whole-show search in Sonarr instead of a test search per season.
            # (Not for anime: Sonarr searches anime episode by episode, which ties it up for hours.)
            self.commands -= 1
            self.slots[self.sonarr.name] -= 1
            self.sonarr.command({"name": "SeriesSearch", "seriesId": s["id"]}, wait=False)
            self.searches.append(f"{item['title']} (every missing season)")
            for n in due:
                self.state["searched"][f"season:{s['id']}:{n}"] = time.time()
                self.state.setdefault("why", {})[f"season:{s['id']}:{n}"] = "searching in the background"
        for season in sorted(by_season):
            eps = by_season[season]
            label = f" for season {season}"
            whole = len(eps) == sum(1 for e in aired if e["seasonNumber"] == season and not e.get("hasFile"))
            command = ({"name": "SeasonSearch", "seriesId": s["id"], "seasonNumber": season} if whole or len(eps) > 3
                       else {"name": "EpisodeSearch", "episodeIds": [e["id"] for e in eps]})
            why = self.search(item, self.sonarr, f"season:{s['id']}:{season}",
                              {"seriesId": s["id"], "seasonNumber": season}, command, label)
            details.append(f"Season {season}: {len(eps)} missing - {why}")
            whys.append(why)
        item["details"] = details
        total = sum(map(len, by_season.values()))
        if q:
            item["reason"] = f"{len(q)} episode(s) downloading; {total} more still to find"
        elif len(details) == 1:
            item["reason"] = details[0]
        else:
            item["reason"] = f"{total} episodes missing across {len(details)} seasons"
        if any(w.startswith("grabbed") for w in whys):
            item["state"] = "downloading"
        self.settle(item, whys)

    # ------------------------------------------------------------------ shared
    def qbt_top(self, hashes):
        """Requested films jump the queue: a show with hundreds of episodes shouldn't hold them up."""
        if not stackconf.has(self.cfg, "qbittorrent"):
            return False
        try:
            stackconf.qbt_post(self.cfg, "/torrents/topPrio", data={"hashes": "|".join(h.lower() for h in hashes)})
            return True
        except Exception:
            return False

    def resend(self, item, req):
        if req.get("status") == 4 or self.due(f"resend:{req['id']}", 3600):
            self.state["searched"][f"resend:{req['id']}"] = time.time()
            try:
                self.seerr("POST", f"/request/{req['id']}/retry")
                self.fix(item, "Seerr never got it to Sonarr/Radarr; sent it again")
                item.update(state="searching", reason="sent to Sonarr/Radarr again just now")
                return
            except Exception as e:
                item.update(state="stuck", reason=f"Seerr couldn't send it to Sonarr/Radarr: {e}")
                return
        item.update(state="stuck", reason="Seerr hasn't handed it to Sonarr/Radarr (retried within the hour)")

    @staticmethod
    def downloading(item, q):
        pct = [round(100 * (1 - r["sizeleft"] / r["size"])) for r in q if r.get("size")]
        warn = next((m for r in q for s in r.get("statusMessages") or [] for m in s.get("messages") or []), None)
        state = {r.get("trackedDownloadState") for r in q}
        if state & {"importPending", "importBlocked", "importing"}:
            reason = "downloaded, being imported" + (f" ({warn})" if warn else "")
        else:
            reason = f"downloading ({min(pct) if pct else 0}%)" if len(q) == 1 else \
                f"{len(q)} downloads in progress (slowest {min(pct) if pct else 0}%)"
            if warn:
                reason += f" - {warn}"
        item.update(state="downloading", reason=reason)

    def settle(self, item, why):
        """Mark an item stuck when every search came back empty or unusable, and note since when,
        so the health check only mentions it once."""
        stuck = not item["fixed"] and item["state"] == "searching" and why and \
            all(NONE_USABLE in w or w.startswith(NOTHING[:20]) for w in why)
        key = item["key"]
        if stuck:
            item["state"] = "stuck"
            self.state["stuck_since"].setdefault(key, time.time())
        else:
            self.state["stuck_since"].pop(key, None)

    # ------------------------------------------------------------------ upgrades
    def upgrades(self):
        done = []
        for client, kind in ((self.radarr, "movie"), (self.sonarr, "series")):
            if not client:
                continue
            if self.slots.get(client.name, 0) <= 0:
                continue  # busy with request searches: upgrades can wait for a quieter hour
            self.slots[client.name] -= 1  # one command each: several films, or one season
            try:
                recs = client.get("wanted/cutoff", params={"pageSize": 200, "monitored": "true"})["records"]
            except Exception as e:
                self.log("upgrade list failed:", e)
                continue
            if kind == "movie":
                todo = [r for r in recs if self.due(f"m{r['id']}", UPGRADE_AFTER, "upgraded")][:UPGRADE_BUDGET]
                if todo:
                    client.command({"name": "MoviesSearch", "movieIds": [r["id"] for r in todo]}, wait=False)
                    for r in todo:
                        self.state["upgraded"][f"m{r['id']}"] = time.time()
                    done.append(f"looking for better versions of {len(todo)} film(s)")
            else:
                seasons = []
                for r in recs:
                    k = f"s{r['seriesId']}:{r['seasonNumber']}"
                    if k not in seasons and self.due(k, UPGRADE_AFTER, "upgraded"):
                        seasons.append(k)
                for k in seasons[:1]:
                    sid, season = k[1:].split(":")
                    client.command({"name": "SeasonSearch", "seriesId": int(sid), "seasonNumber": int(season)}, wait=False)
                    self.state["upgraded"][k] = time.time()
                if seasons:
                    done.append("looking for a better version of 1 season")
        return done

    # ------------------------------------------------------------------ main
    def run(self):
        """One check at a time (the hub button and the hourly check can overlap)."""
        LOCK.parent.mkdir(exist_ok=True)
        if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 30 * 60:
            raise RuntimeError("a request check is already running")
        LOCK.write_text(str(time.time()))
        try:
            return self._run()
        finally:
            LOCK.unlink(missing_ok=True)

    def _run(self):
        reqs = self.seerr("GET", "/request", params={"take": 200, "filter": "all", "sort": "added"})["results"]
        reqs = [r for r in reqs if r.get("status") in (2, 4) and (r.get("media") or {}).get("status") != 5]
        sonarr_cfg = (self.seerr("GET", "/settings/sonarr") or [{}])[0] if self.sonarr else {}
        defaults = {"standard": sonarr_cfg.get("activeProfileId"), "anime": sonarr_cfg.get("activeAnimeProfileId")}
        clients = [(n, c) for n, c in (("sonarr", self.sonarr), ("radarr", self.radarr)) if c]
        profiles = {"sonarr": {}, "radarr": {}}
        profiles.update({n: {p["id"]: p["name"] for p in c.get("qualityprofile")} for n, c in clients})
        queue = {"sonarr": [], "radarr": []}
        queue.update({n: c.get("queue", params={"pageSize": 500, "includeEpisode": "true"})["records"]
                      for n, c in clients})
        self.slots = {c.name: self.free_slots(c) for c in (self.sonarr, self.radarr) if c}
        self.recycle_bin = bool(self.radarr and self.radarr.get("config/mediamanagement").get("recycleBin"))
        seen = self.seen
        for req in reqs:
            media = req["media"]
            key = f"{req['type']}:{media.get('tmdbId')}"
            if key in seen:
                continue
            seen.add(key)
            item = {"key": key, "type": req["type"], "title": f"TMDB {media.get('tmdbId')}", "year": None,
                    "requested_by": (req.get("requestedBy") or {}).get("displayName"),
                    "requested": parse_time(req.get("createdAt")), "state": "searching", "reason": "",
                    "fixed": [], "request_id": req["id"], "tmdb": media.get("tmdbId")}
            self.log(f"- {req['type']} {media.get('tmdbId')}")
            try:
                if req["type"] == "movie":
                    self.movie(item, req, queue, profiles["radarr"])
                else:
                    self.show(item, req, queue, profiles["sonarr"], defaults)
            except Exception as e:
                item.update(state="error", reason=f"couldn't check it: {e}"[:200])
            item.pop("_turns", None)
            self.log(f"  {item['title']}: [{item['state']}] {item['reason']}")
            self.items.append(item)
        if self.seerr_scan:
            try:
                self.seerr("POST", "/settings/jobs/jellyfin-recently-added-scan/run")
            except Exception:
                pass
        if self.radarr:
            self.recent_files()
        upgrades = self.upgrades()
        now = time.time()
        for k in list(self.state["searched"]):
            if now - self.state["searched"][k] > 7 * 86400:
                del self.state["searched"][k]
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps(self.state), encoding="utf-8")
        report = {"time": now, "items": self.items, "fixed": self.fixed, "searches": self.searches, "upgrades": upgrades,
                  "problems": self.problems,
                  "counts": dict(collections.Counter(i["state"] for i in self.items))}
        OUT.write_text(json.dumps(report, indent=1), encoding="utf-8")
        return report

    def newly_stuck(self, hours=72):
        """Items stuck for more than `hours`, reported once (the health check tells the user)."""
        told = self.state.setdefault("told", {})
        out = []
        for i in self.items:
            since = self.state["stuck_since"].get(i["key"])
            if i["state"] == "stuck" and since and time.time() - since > hours * 3600 and i["key"] not in told:
                told[i["key"]] = time.time()
                out.append(f"{i['title']}: {i['reason']}")
        for k in list(told):
            if k not in self.state["stuck_since"]:
                del told[k]
        STATE.write_text(json.dumps(self.state), encoding="utf-8")
        return out


def main():
    cfg = stackconf.load()
    if not stackconf.key(cfg, "seerr"):
        sys.exit("Seerr isn't set up yet")
    rep = Doctor(cfg, quiet="--quiet" in sys.argv).run()
    print(f"\n{len(rep['items'])} request(s) not available yet: {rep['counts']}")
    for f in rep["fixed"]:
        print("fixed:", f)
    for u in rep["upgrades"]:
        print("upgrades:", u)
    for s in rep["searches"]:
        print("searching:", s)
    for p in rep["problems"]:
        print("problem:", p)


if __name__ == "__main__":
    main()
