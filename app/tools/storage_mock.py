"""A mock media stack for testing marquee.storage's deletion path without touching a real library.

One Flask app on 127.0.0.1:<port> answers as Radarr (/radarr), Sonarr (/sonarr), qBittorrent (/qbt), Jellyfin
(/jf) and Seerr (/seerr), over small dummy files it creates in <root> (a library folder plus a "downloads" folder
whose files are hardlinks, like a seeding torrent). Every write call is recorded in CALLS; DELETE of a file moves
it into <root>/recycle (like the apps' recycle bin).

    python tools/storage_mock.py <root> [port]      (or import and call build(root) / serve(root, port))
"""
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request

CALLS = []
U1, U2 = "user-one", "user-two"
DAY = 86400


def build(root):
    root = Path(root)
    lib, dl, rec = root / "Media", root / "Downloads", root / "recycle"
    for d in (lib / "Movies", lib / "TV Shows", dl, rec):
        d.mkdir(parents=True, exist_ok=True)

    def make(p, size):
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            with open(p, "wb") as f:
                f.write(os.urandom(1024) * (size // 1024))
        return str(p)
    now = time.time()
    mA = make(lib / "Movies" / "Mock Movie (2020)" / "Mock Movie (2020) [Bluray-1080p].mkv", 3 * 2**20)
    seedA = dl / "Mock.Movie.2020.1080p" / "Mock.Movie.2020.1080p.mkv"
    seedA.parent.mkdir(parents=True, exist_ok=True)
    if not seedA.exists():
        os.link(mA, seedA)  # the seeding copy: same file, second name
    mB = make(lib / "Movies" / "Half Watched (2021)" / "Half Watched (2021) [WEBDL-1080p].mkv", 2 * 2**20)
    mC = make(lib / "Movies" / "Never Started (2019)" / "Never Started (2019) [WEBDL-1080p].mkv", 2 * 2**20)
    show = lib / "TV Shows" / "Mock Show (2018)"
    eps = {}
    for s, e in ((1, 1), (1, 2), (2, 1), (2, 2)):
        eps[(s, e)] = make(show / f"Season {s:02d}" / f"Mock Show - S{s:02d}E{e:02d} [WEBDL-1080p].mkv", 1 * 2**20)
    S = {
        "movies": [
            {"id": 1, "title": "Mock Movie", "year": 2020, "tmdbId": 9001, "path": str(Path(mA).parent), "runtime": 100,
             "monitored": True, "hasFile": True, "qualityProfileId": 1, "added": "2026-01-01T00:00:00Z",
             "movieFile": {"id": 11, "path": mA, "size": os.path.getsize(mA), "dateAdded": "2026-01-01T00:00:00Z",
                           "quality": {"quality": {"id": 7, "name": "Bluray-1080p", "resolution": 1080, "source": "bluray"}},
                           "mediaInfo": {"runTime": "1:40:00"}}},
            {"id": 2, "title": "Half Watched", "year": 2021, "tmdbId": 9002, "path": str(Path(mB).parent), "runtime": 90,
             "monitored": True, "hasFile": True, "qualityProfileId": 1, "added": "2026-01-01T00:00:00Z",
             "movieFile": {"id": 12, "path": mB, "size": os.path.getsize(mB), "dateAdded": "2026-01-01T00:00:00Z",
                           "quality": {"quality": {"id": 3, "name": "WEBDL-1080p", "resolution": 1080, "source": "web"}},
                           "mediaInfo": {"runTime": "1:30:00"}}},
            {"id": 3, "title": "Never Started", "year": 2019, "tmdbId": 9003, "path": str(Path(mC).parent), "runtime": 95,
             "monitored": True, "hasFile": True, "qualityProfileId": 1, "added": "2026-01-01T00:00:00Z",
             "movieFile": {"id": 13, "path": mC, "size": os.path.getsize(mC), "dateAdded": "2026-01-01T00:00:00Z",
                           "quality": {"quality": {"id": 3, "name": "WEBDL-1080p", "resolution": 1080, "source": "web"}},
                           "mediaInfo": {"runTime": "1:35:00"}}},
        ],
        "series": [{"id": 1, "title": "Mock Show", "year": 2018, "tmdbId": 9100, "tvdbId": 8100, "path": str(show),
                    "runtime": 30, "monitored": True, "qualityProfileId": 1, "seriesType": "standard",
                    "statistics": {"episodeFileCount": 4, "episodeCount": 4, "sizeOnDisk": 4 * 2**20}}],
        "episodes": [{"id": 100 + k, "seriesId": 1, "seasonNumber": s, "episodeNumber": e, "episodeFileId": 200 + k,
                      "hasFile": True, "monitored": True} for k, (s, e) in enumerate(sorted(eps))],
        "files": [{"id": 200 + k, "seriesId": 1, "seasonNumber": s, "path": eps[(s, e)], "size": os.path.getsize(eps[(s, e)]),
                   "dateAdded": "2026-01-01T00:00:00Z",
                   "quality": {"quality": {"id": 3, "name": "WEBDL-1080p", "resolution": 1080, "source": "web"}},
                   "mediaInfo": {"runTime": "30:00"}} for k, (s, e) in enumerate(sorted(eps))],
        "torrents": [{"hash": "a" * 40, "name": "Mock.Movie.2020.1080p", "save_path": str(dl), "size": os.path.getsize(mA),
                      "files": ["Mock.Movie.2020.1080p/Mock.Movie.2020.1080p.mkv"]}],
        "recycle": str(rec), "lib": str(lib), "dl": str(dl),
        "paths": {"A": mA, "B": mB, "C": mC, **{f"S{s}E{e}": p for (s, e), p in eps.items()}},
    }
    # Jellyfin watch state: user one watched A, the show's S1 and S2E1; user two watched A, is halfway through B
    old = "2026-09-01T20:00:00.0000000Z"
    played = {U1: {mA: True, eps[(1, 1)]: True, eps[(1, 2)]: True, eps[(2, 1)]: True}, U2: {mA: True}}
    pos = {U2: {mB: 2700 * 10**7}}
    S["jf_items"] = {}
    ids = {mA: "jf-a", mB: "jf-b", mC: "jf-c", **{p: f"jf-s{s}e{e}" for (s, e), p in eps.items()}}
    for u in (U1, U2):
        S["jf_items"][u] = [{"Id": ids[p], "Name": Path(p).stem, "Path": p, "Type": "Episode" if "Mock Show" in p else "Movie",
                             "SeriesId": "jf-show" if "Mock Show" in p else None,
                             "SeasonId": (f"jf-season{Path(p).parent.name[-1]}" if "Mock Show" in p else None),
                             "UserData": {"Played": played.get(u, {}).get(p, False),
                                          "PlaybackPositionTicks": pos.get(u, {}).get(p, 0),
                                          "LastPlayedDate": old if played.get(u, {}).get(p) else None,
                                          "IsFavorite": False}} for p in ids]
    S["ids"] = ids
    return S


def app_for(S):
    app = Flask("storage_mock")

    def rec():
        CALLS.append({"method": request.method, "path": request.path, "args": dict(request.args),
                      "json": request.get_json(silent=True), "form": dict(request.form)})

    def recycle(path):
        dest = Path(S["recycle"]) / Path(path).name
        shutil.move(path, dest)

    # ---- Radarr
    @app.get("/radarr/api/v3/rootfolder")
    def r_root():
        return jsonify([{"path": os.path.join(S["lib"], "Movies"), "freeSpace": shutil.disk_usage(S["lib"]).free}])

    @app.get("/radarr/api/v3/movie")
    def r_movies():
        out = []
        for m in S["movies"]:
            m = dict(m)
            if not m.get("movieFile"):
                m.pop("movieFile", None)
            m["sizeOnDisk"] = (m.get("movieFile") or {}).get("size", 0)
            out.append(m)
        return jsonify(out)

    @app.put("/radarr/api/v3/movie/editor")
    def r_editor():
        rec()
        for m in S["movies"]:
            if m["id"] in request.json["movieIds"]:
                m["monitored"] = request.json["monitored"]
        return jsonify([])

    @app.delete("/radarr/api/v3/moviefile/<int:fid>")
    def r_delfile(fid):
        rec()
        for m in S["movies"]:
            if (m.get("movieFile") or {}).get("id") == fid:
                recycle(m["movieFile"]["path"])
                m["movieFile"] = None
                m["hasFile"] = False
        return "", 200

    @app.get("/radarr/api/v3/history/movie")
    def r_hist():
        mid = int(request.args["movieId"])
        return jsonify([{"eventType": "downloadFolderImported", "downloadId": "A" * 40}] if mid == 1 else [])

    # ---- Sonarr
    @app.get("/sonarr/api/v3/rootfolder")
    def s_root():
        return jsonify([{"path": os.path.join(S["lib"], "TV Shows"), "freeSpace": shutil.disk_usage(S["lib"]).free}])

    @app.get("/sonarr/api/v3/series")
    def s_series():
        for se in S["series"]:
            fs = [f for f in S["files"] if f["seriesId"] == se["id"]]
            se["statistics"].update(episodeFileCount=len(fs), sizeOnDisk=sum(f["size"] for f in fs))
        return jsonify(S["series"])

    @app.get("/sonarr/api/v3/episode")
    def s_eps():
        return jsonify([e for e in S["episodes"] if e["seriesId"] == int(request.args["seriesId"])])

    @app.get("/sonarr/api/v3/episodefile")
    def s_files():
        return jsonify([f for f in S["files"] if f["seriesId"] == int(request.args["seriesId"])])

    @app.put("/sonarr/api/v3/episode/monitor")
    def s_mon():
        rec()
        for e in S["episodes"]:
            if e["id"] in request.json["episodeIds"]:
                e["monitored"] = request.json["monitored"]
        return jsonify([])

    @app.put("/sonarr/api/v3/series/editor")
    def s_editor():
        rec()
        return jsonify([])

    @app.delete("/sonarr/api/v3/episodefile/<int:fid>")
    def s_del(fid):
        rec()
        f = next(f for f in S["files"] if f["id"] == fid)
        recycle(f["path"])
        S["files"].remove(f)
        for e in S["episodes"]:
            if e["episodeFileId"] == fid:
                e["episodeFileId"], e["hasFile"] = 0, False
        return "", 200

    @app.get("/sonarr/api/v3/history/series")
    def s_hist():
        return jsonify([])

    # ---- shared *arr bits
    for prefix in ("radarr", "sonarr"):
        def mk(prefix):
            @app.get(f"/{prefix}/api/v3/queue", endpoint=f"{prefix}_queue")
            def queue():
                return jsonify({"records": []})

            @app.get(f"/{prefix}/api/v3/qualitydefinition", endpoint=f"{prefix}_qd")
            def qd():
                return jsonify([{"quality": {"id": 3, "name": "WEBDL-1080p", "resolution": 1080, "source": "web"},
                                 "minSize": 4, "maxSize": None, "preferredSize": None},
                                {"quality": {"id": 7, "name": "Bluray-1080p", "resolution": 1080, "source": "bluray"},
                                 "minSize": 4, "maxSize": None, "preferredSize": None}])

            @app.get(f"/{prefix}/api/v3/qualityprofile", endpoint=f"{prefix}_qp")
            def qp():
                return jsonify([{"id": 1, "name": "HD-1080p", "upgradeAllowed": False, "cutoff": 3,
                                 "items": [{"quality": {"id": 3, "name": "WEBDL-1080p"}, "allowed": True},
                                           {"quality": {"id": 7, "name": "Bluray-1080p"}, "allowed": True}]}])

            @app.get(f"/{prefix}/api/v3/config/mediamanagement", endpoint=f"{prefix}_mm")
            def mm():
                return jsonify({"recycleBin": S["recycle"], "recycleBinCleanupDays": 7})

            @app.get(f"/{prefix}/api/v3/downloadclient", endpoint=f"{prefix}_dc")
            def dc():
                port = request.host.split(":")[1]
                return jsonify([{"implementation": "QBittorrent", "enable": True, "fields": [
                    {"name": "host", "value": "127.0.0.1"}, {"name": "port", "value": int(port)},
                    {"name": "useSsl", "value": False}, {"name": "urlBase", "value": "/qbt"}]}])
        mk(prefix)

    # ---- qBittorrent
    @app.get("/qbt/api/v2/torrents/info")
    def q_info():
        h = (request.args.get("hashes") or "").lower()
        return jsonify([{k: t[k] for k in ("hash", "name", "save_path", "size")} for t in S["torrents"]
                        if not h or t["hash"] == h])

    @app.get("/qbt/api/v2/torrents/files")
    def q_files():
        t = next((t for t in S["torrents"] if t["hash"] == request.args["hash"].lower()), None)
        return jsonify([{"name": n} for n in (t or {}).get("files", [])])

    @app.post("/qbt/api/v2/torrents/delete")
    def q_del():
        rec()
        h = request.form["hashes"].lower()
        for t in list(S["torrents"]):
            if t["hash"] == h:
                if request.form.get("deleteFiles") == "true":
                    shutil.rmtree(os.path.join(t["save_path"], t["name"]), ignore_errors=True)
                S["torrents"].remove(t)
        return "Ok."

    # ---- Jellyfin
    @app.get("/jf/System/Info/Public")
    def j_info():
        return jsonify({"Version": "10.10.0"})

    @app.get("/jf/Users")
    def j_users():
        return jsonify([{"Id": U1, "Name": "one"}, {"Id": U2, "Name": "two"}])

    @app.get("/jf/Items")
    def j_items():
        u = request.args.get("userId")
        if request.args.get("IncludeItemTypes") == "Series":
            return jsonify({"Items": []})
        alive = [i for i in S["jf_items"].get(u, []) if os.path.exists(i["Path"])]
        return jsonify({"Items": alive})

    @app.get("/jf/Library/VirtualFolders")
    def j_vf():
        return jsonify([{"Name": "Movies", "CollectionType": "movies", "Locations": [os.path.join(S["lib"], "Movies")]},
                        {"Name": "Shows", "CollectionType": "tvshows", "Locations": [os.path.join(S["lib"], "TV Shows")]}])

    @app.post("/jf/Library/Media/Updated")
    def j_updated():
        rec()
        return "", 204

    @app.post("/jf/UserPlayedItems/<item>")
    def j_played(item):
        rec()
        return jsonify({"Played": True})

    # ---- Seerr
    @app.get("/seerr/api/v1/settings/<svc>")
    def sr_settings(svc):
        return jsonify({"message": "admin only"}), 403

    @app.get("/seerr/api/v1/movie/<int:tmdb>")
    def sr_movie(tmdb):
        return jsonify({"id": tmdb, "title": "x", "runtime": 100, "mediaInfo": {"id": 500 + tmdb % 100, "status": 5}})

    @app.get("/seerr/api/v1/tv/<int:tmdb>")
    def sr_tv(tmdb):
        return jsonify({"id": tmdb, "name": "Mock Show", "seasons": [], "mediaInfo": {"id": 700, "status": 4}})

    @app.delete("/seerr/api/v1/media/<int:mid>")
    def sr_del(mid):
        rec()
        return "", 204

    @app.post("/seerr/api/v1/request")
    def sr_request():
        rec()
        return jsonify({"id": 4242, "status": 2})

    @app.get("/seerr/api/v1/request")
    def sr_requests():
        return jsonify({"results": []})

    @app.get("/seerr/api/v1/auth/me")
    def sr_me():
        return jsonify({"id": 7})

    @app.get("/calls")
    def calls():
        return jsonify(CALLS)
    return app


def serve(root, port=28600):
    S = build(root)
    app = app_for(S)
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return S, srv


if __name__ == "__main__":
    S, srv = serve(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 28600)
    print("mock stack on", srv.server_port)
    threading.Event().wait()
