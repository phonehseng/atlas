# How it works

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
           Media Hub: status, problems, logs, activity, hourly health check, 5-minute watchdog
```

## Folders

```
<folder>/
├─ data/
│  ├─ media/        Movies, TV Shows, Anime, Anime Movies
│  ├─ downloads/    incomplete/, complete/<category>/, watch/
│  └─ drop/         drop video files here to have them sorted
├─ config/<app>/    each app's settings and database
├─ config/shared/mediastack.json   the whole install described for MediaSorter, the Media Hub and the health check
├─ apps/            Windows: the programs (plus MediaStack.exe, the background launcher)
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

Every app is downloaded from its official source at install time, so you get the current version:
GitHub releases (Sonarr, Radarr, Prowlarr, Bazarr, Recyclarr, Unpackerr, Cleanuparr, autobrr, jfa-go,
Requestrr, uv), repo.jellyfin.org, nodejs.org (checksum-verified), winget (qBittorrent, MKVToolNix,
Tailscale), and official Docker images on Linux. trawl is pinned to a tested version because the installer
patches it; its browser add-on is checksum-verified.
