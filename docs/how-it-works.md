# How it works

## Choosing the parts

The *Choose your stack* page lists every part with a one-line description, a rough size and an OS badge. The
choice is saved in `stack.json` (`parts`), and only the chosen parts' steps appear in the wizard. Each part
declares what it needs (Seerr needs Jellyfin, Sonarr and Radarr; Marquee needs Jellyfin and Seerr; the Media
Hub needs MediaSorter...): ticking a part adds its needs, and unticking one explains what depends on it before
removing those too. Media folders and Jellyfin are always included. In this version Recyclarr, Bazarr,
Unpackerr, Cleanuparr, MediaSorter, Seerr and autobrr work with Sonarr and Radarr together, so a films-only or
TV-only stack isn't offered yet.

## The flow

```
 You / friends ──► Seerr (request) ──► Sonarr (TV, anime) / Radarr (films)
                                          │  quality rules: Recyclarr (TRaSH Guides)
                                          ▼
                         Prowlarr ──► your indexers   (Cloudflare sites via trawl / Byparr)
                                          │
                                          ▼
                         qBittorrent ── only through the VPN ──► internet
                                          │ finished
                                          ▼
           Sonarr/Radarr import (hardlink + rename) ◄── Unpackerr (archives), Cleanuparr (stuck/fake)
                                          │
                                          ▼
           data/media/...  ──► Jellyfin ──► phones, TVs, browsers (home network or Tailscale)
              ▲    Bazarr adds subtitles; MediaSorter sorts your own downloads and trims languages
              │
           Media Hub: status, problems, logs, activity, hourly health check, 5-minute watchdog,
                      request doctor, nightly backups, VPN leak check, phone alerts (ntfy)
              ▲
           Marquee (Windows, optional): the player for this PC, requests, and the hub's dashboard
                      ("Your stack"); Marquee Mobile serves phones over Tailscale
```

With Marquee as the stack app, the Media Hub's `hub.json` has `"ui": "marquee"`: the hub's own page then only
points to Marquee (with a link to open it anyway), while its API and every background job keep running exactly
as before, so monitoring works with Marquee closed.

Lidarr (optional) does the same for music: you add artists in Lidarr itself, and albums land in
`data/media/Music` (Jellyfin's *Music* library).

## Folders

```
<folder>/
├─ data/
│  ├─ media/        Movies, TV Shows, Anime, Anime Movies
│  ├─ downloads/    incomplete/, complete/<category>/, watch/
│  └─ drop/         drop video files here to have them sorted
├─ config/<app>/    each app's settings and database
├─ config/shared/mediastack.json   the whole install described for MediaSorter, the Media Hub and the health check
├─ backups/         nightly zips of every app's settings and databases (newest 7)
├─ apps/            Windows: the programs (plus MediaStack.exe, the background launcher; Marquee in apps/Marquee)
├─ logs/            Windows: output of the background apps
├─ docker-compose.yml, .env   Linux
└─ stack.json       what setup has done (versions, API keys; never passwords)
```

Downloads and the library share one `data` folder on one drive on purpose: imports are **hardlinks**, which
are instant and take no extra space while the torrent keeps seeding. Seeding stops at ratio 1.0, after 12
hours, or after 1 hour with nobody downloading, and Sonarr/Radarr then clean up the download copy.

## Background apps

- **Windows**: each app is a Task Scheduler task in the *Media Stack* folder: it starts at sign-in, restarts
  within a minute if it crashes, and needs no admin rights. Console apps run through `MediaStack.exe run`, a
  hidden launcher that writes their output to `logs\` and stops the app when the task is ended.
- **Linux**: a Docker Compose project (`docker compose ps` in the folder). qBittorrent shares the Gluetun
  VPN container's network: no VPN, no network.

## Security

- **One admin login** for Jellyfin and every admin page. Setup keeps the password in memory only; each app
  stores its own copy, most as a one-way hash. jfa-go keeps it as-is (it needs it to create accounts), and on
  Linux so does the Media Hub's qBittorrent connection; those files are readable by their owner only.
- **Windows**: Sonarr, Radarr, Prowlarr, qBittorrent, autobrr, trawl and Byparr only listen on the PC itself.
  Bazarr, Cleanuparr and Requestrr ask for a login. Jellyfin, Seerr, jfa-go and the Media Hub listen on the
  network; the Media Hub only lets in this PC (no login) and Tailscale devices (Jellyfin admin login).
- **Linux**: admin pages always require the login; trawl and Byparr only listen on the server itself; the Media
  Hub lets in the home network and Tailscale after a Jellyfin admin login.
- Nothing is opened to the internet. Remote access is through Tailscale.
- trawl runs with its extra features off (no MCP server, no proxying, no paid captcha services).
- The wizard only listens on `127.0.0.1` (or, on a headless Linux server, the network with a random one-time
  key) and checks that requests come from its own page.

## Where things come from

Nothing big is built into the program: every app is downloaded from its official source when its step runs,
so you get the current version: GitHub releases (Sonarr, Radarr, Prowlarr, Bazarr, Recyclarr, Unpackerr,
Cleanuparr, autobrr, jfa-go, Requestrr, uv), repo.jellyfin.org, nodejs.org (checksum-verified), winget
(qBittorrent, MKVToolNix), and official Docker images on Linux. Pinned to a tested version and checked against
a SHA-256 written into the program:

- **trawl** (the installer patches it) and its browser add-on;
- **Tailscale for Windows**: the official MSI from `pkgs.tailscale.com/stable`, installed silently with
  `msiexec /qn`. Tailscale keeps itself up to date afterwards;
- **Marquee**: its release zip (version, address, size and SHA-256 are set when a release is made).

On Linux, Tailscale is installed on the server itself with Tailscale's official script, which adds Tailscale's
signed package repository (so it updates with the system), rather than as a container next to the stack: every
app already publishes its port on the server, so one Tailscale on the host reaches all of them with no extra
networking, and the Media Hub and the wizard read the server's Tailscale name and address from it. A
container would need host networking, `/dev/net/tun` and its own state volume to do the same.

## Updates

Each release publishes an update list, `marquee-update.json`, next to its downloads
(`https://github.com/phonehseng/marquee/releases/latest/download/marquee-update.json`; the
`MEDIASTACK_UPDATE_MANIFEST` environment variable points the installer at another address or a local file
for testing). The installer reads three entries:

```json
{
  "schema": 1,
  "installer":    { "version": "1.4.1", "notes": "...",
                    "files": { "windows-x64": { "url": "...", "sha256": "...", "size": 0 },
                               "linux-x64":   { "url": "...", "sha256": "...", "size": 0 },
                               "linux-arm64": { "url": "...", "sha256": "...", "size": 0 } } },
  "stack-assets": { "version": "1.4.1", "min_installer": "1.4.1", "notes": "..." },
  "marquee":      { "version": "1.0.1", "notes": "...",
                    "files": { "windows-x64": { "url": "...", "sha256": "...", "size": 0 } } }
}
```

- `installer`: a newer version is offered at the top of the wizard; the download must match `sha256` and
  `size`, then the program replaces itself and restarts.
- `stack-assets`: the version of the MediaSorter / Media Hub / health check files the newest installer
  carries. They are built into the installer, so a newer version here means "update the installer first".
- `marquee`: *Update my stack* installs it when it's newer than the installed Marquee. An entry may also give
  `url`, `sha256` and `size` directly instead of `files`.

Only `https` downloads are accepted, except from a local test manifest.
