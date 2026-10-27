# Atlas's local API

Other programs on the same PC can call a small part of Atlas's API. The separate LocalAI assistant uses it, for example to
handle "mark Dune as watched", and you can call it from your own scripts. It listens only on `127.0.0.1` and works
only while Atlas is running.

## Finding Atlas: the discovery file

Atlas picks a random port each time it starts, so it writes where it is to a file:

```
%APPDATA%\Marquee\local-api.json        (or %MARQUEE_HOME%\local-api.json when MARQUEE_HOME is set)
%APPDATA%\Marquee\atlas-local-api.json  (the same content under the app's new name)
```

```json
{
 "port": 51234,
 "token": "q3l9...",
 "pid": 4242,
 "version": "1.4.5",
 "base": "http://127.0.0.1:51234",
 "routes": ["DELETE /api/watched", "GET /api/watched", "POST /api/watched"],
 "started": "2026-10-26T18:02:11Z"
}
```

- The file is rewritten each time Atlas starts and removed when it exits. If Atlas crashed, a stale file can be
  left behind. If `pid` isn't running, or the port refuses the connection, Atlas isn't running.
- `token` is random for each install and stays the same across restarts. It's stored encrypted (Windows DPAPI) in
  Atlas's settings. Treat it like a password: any program that has it can mark titles watched.
- Atlas runs outside any app container, so this is the real `%APPDATA%`. A program packaged as MSIX (such as a
  Store app) sees a redirected AppData of its own, so it should read the file by its full real path
  (`C:\Users\<you>\AppData\Roaming\Marquee\local-api.json`).

## Calling it

Send every request with the header `X-Marquee-Token: <token>` (`X-Atlas-Token: <token>` works the same).
The names keep "Marquee", the app's name before it became Atlas, so programs written for it keep working;
the data folder is still `%APPDATA%\Marquee`.

| Request | Result |
|---|---|
| with the right token, from a program (no `Origin` header) | allowed |
| no token | `403` |
| wrong token | `403` |
| token sent from a web page (it has an `Origin` or a cross-site `Sec-Fetch-Site` header) | `403` |
| `POST` / `DELETE` with a body that isn't JSON | `415` |

Only routes marked as local API accept the token. Every other route of Atlas's server is for its own window only.

## Watched

Mark any film or show as watched: one in your library, one Seerr can request, or neither. Library titles are marked
in Jellyfin. Other titles are kept on this PC, and when one of them is downloaded later its watched state is carried
over to Jellyfin. Either way, Atlas's taste profile learns from it the same way it learns from a title you
finish, and Home stops recommending it.

### `GET /api/watched?kind=&q=`

Lists everything watched, newest first, one entry per title. It merges library titles played in Jellyfin, titles
marked here (in Atlas, the chat, another program such as the LocalAI assistant, or a Letterboxd import) and titles watched and then removed to save space.
`kind` is `movie` or `tv`; `q` filters by title.

```json
{"ok": true, "count": 2, "counts": {"movie": 1, "tv": 1}, "items": [
  {"key": "movie:872585", "kind": "movie", "tmdb": 872585, "item_id": null, "title": "Oppenheimer", "year": 2023,
   "poster": "/img/tmdb/w500/....jpg", "backdrop": "...", "genres": ["Drama", "History"], "runtime": 10800,
   "watched_at": "2026-10-26T19:40:02.000000Z", "source": "atlas", "sources": ["atlas"],
   "owned": false, "removed": false, "complete": true, "seasons": [], "episodes": null, "rating": 0},
  {"key": "tv:136315", "kind": "tv", "tmdb": 136315, "item_id": "a1b2...", "title": "The Bear", "year": 2022,
   "source": "library", "sources": ["library"], "owned": true, "complete": false, "seasons": [1],
   "episodes": {"watched": 8, "total": 18}, "watched_at": "...", "card": {"...": "the library card"}}
]}
```

- `source` is one of `library`, `manual`, `atlas`, `chat`, `letterboxd` or `removed`. `sources` lists every place
  that says the title was watched.
- `complete: false` means only part of a show was watched. `seasons` lists the seasons watched in full, and for
  library shows `episodes` gives the episode counts.
- `rating` is your 1 to 5 stars, or 0 if you haven't rated it.
- Image paths are relative to `base`.

### `POST /api/watched`

The body names the title in one of these ways:

| Body | |
|---|---|
| `{"tmdb": 872585, "kind": "movie"}` | by TMDB id (`kind` is `movie` or `tv`) |
| `{"item_id": "<jellyfin id>"}` | a library item (a film, a show, a season or an episode) |
| `{"title": "Dune", "year": 2021, "kind": "movie"}` | by name. `year` and `kind` are optional but help |
| `{"key": "movie:872585"}` | the `key` of a list entry |

These fields are optional:

- `"watched_at"`: an ISO date (`"2024-05-01"` or `"2024-05-01T20:00:00Z"`) or epoch seconds. It defaults to now.
- `"rating"`: 1 to 5 stars, which also go into the taste profile.
- `"season"` or `"seasons"`: `1` or `[1, 2]`. For a show, marks only those seasons.
- `"source"`: one of `manual`, `atlas`, `chat` or `letterboxd`. It defaults to `atlas` when the request has the
  token, and to `manual` when it comes from Atlas's own window.

`200` returns `{"ok": true, "watched": true, "item": <the list entry>, "taste": {"moved": {"Drama": [0.12, 0.21]}},
"rating": null}`.

When you send a title, Atlas looks it up in your library and through Seerr's search, matching on the year. If
more than one title fits, it marks nothing and answers `409`:

```json
{"ok": false, "ambiguous": true, "message": "More than one title is called “Dune”: send its tmdb and kind",
 "candidates": [{"kind": "movie", "tmdb": 438631, "item_id": null, "title": "Dune", "year": 2021, "poster": "...", "owned": false},
                {"kind": "movie", "tmdb": 841, "item_id": null, "title": "Dune", "year": 1984, "poster": "...", "owned": false}]}
```

Ask the user which one they mean, then send its `tmdb` and `kind`. A title that can't be found answers `404`, and a
bad field answers `400`. Every error has a `message`.

### `DELETE /api/watched`

`DELETE` takes the same ways of naming a title, as a JSON body or as query parameters. It also takes
`season` / `seasons`. What it does depends on where the title is:

- In the library, the title is marked unplayed in Jellyfin.
- Marked here, the record is deleted.
- Watched and then removed, the watched state is cleared.

In every case the taste profile forgets the remembered title, but what it already learned from it (genres, people)
stays. The response is `{"ok": true, "watched": false, "kind": ..., "tmdb": ..., "item_id": ..., "dropped": [keys]}`.

## Examples

PowerShell:

```powershell
$api = Get-Content "$env:APPDATA\Marquee\local-api.json" -Raw | ConvertFrom-Json
$h = @{ 'X-Marquee-Token' = $api.token }
Invoke-RestMethod "$($api.base)/api/watched?kind=movie" -Headers $h
Invoke-RestMethod "$($api.base)/api/watched" -Method Post -Headers $h -ContentType 'application/json' `
  -Body '{"title": "Oppenheimer", "year": 2023, "rating": 5}'
```

curl (Git Bash, with `jq`):

```sh
F="$APPDATA/Marquee/local-api.json"
BASE=$(jq -r .base "$F"); TOKEN=$(jq -r .token "$F")
curl -s -H "X-Marquee-Token: $TOKEN" "$BASE/api/watched?q=dune"
curl -s -H "X-Marquee-Token: $TOKEN" -H 'Content-Type: application/json' \
     -d '{"tmdb": 438631, "kind": "movie"}' "$BASE/api/watched"
curl -s -X DELETE -H "X-Marquee-Token: $TOKEN" -H 'Content-Type: application/json' \
     -d '{"tmdb": 438631, "kind": "movie"}' "$BASE/api/watched"
```

Python:

```python
import json, os, requests
api = json.load(open(os.path.expandvars(r"%APPDATA%\Marquee\local-api.json")))
s = requests.Session()
s.headers["X-Marquee-Token"] = api["token"]
r = s.post(api["base"] + "/api/watched", json={"title": "Dune", "kind": "movie"})
if r.status_code == 409:
    print([f"{c['title']} ({c['year']})" for c in r.json()["candidates"]])
```

## For Atlas developers

To add a route to the local API, put `@local_api("METHOD /path")` under the Flask route decorator
(`marquee/localapi.py`). Routes without it never accept the token. Atlas's own window reaches local API routes
because WebView2 marks its same-origin requests with `Sec-Fetch-Site: same-origin`. A request with an `Origin` from
another site is refused, as on Atlas's other guarded routes.
