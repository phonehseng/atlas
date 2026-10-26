# Media Stack Setup

One program that builds a complete, automated home media server, one guided step at a time. Every step
explains what it's about to install, installs it, connects it to everything already installed, and tells
you what to do next.

Request a film or show from your phone, and a few minutes later it's in Jellyfin, named properly, with English
subtitles for anime, ready on your TV. It takes whatever copy is available first and keeps upgrading it (films
up to 4K, shows up to 1080p), and if a request is stuck, the Media Hub tells you why and fixes what it can.

- **Windows 10/11**: apps are installed natively and run in the background (Task Scheduler), no Docker needed.
- **Linux** (PCs, servers, Raspberry Pi 4/5 with a 64-bit OS): apps run as Docker containers.
  *Preview: the Linux version is newer and less tested than the Windows one.*

## What's in the stack

The wizard starts with **Choose your stack**: a checklist of every part below, with presets
*Recommended* (the default), *Minimal* (watch, request, download) and *Everything*. Ticking a part adds what
it needs; unticking one explains what depends on it. Only the chosen parts become steps, and you can add more
later. Sizes are rough (programs only, not media).

**Watch**

| Part | What it's for | Size |
|---|---|---|
| Media folders *(always)* | One folder for the library, downloads and every app's settings, easy to back up or move | - |
| **Jellyfin** *(always)* | Your own streaming service: films and shows on phones, TVs and browsers, at home or away | ~450 MB, ~500 MB RAM |
| Intro Skipper | A Netflix-style "Skip Intro" button on episodes | ~5 MB |
| **Marquee** *(Windows)* | A Netflix-style player for the PC with requests and the stack's dashboard in one app; plays files directly | ~250 MB, ~400 MB RAM |

**Find & organise**

| Part | What it's for | Size |
|---|---|---|
| **Prowlarr** | Keeps the search sites (indexers) in one place and shares them with Sonarr, Radarr and Lidarr | ~200 MB, ~150 MB RAM |
| **Sonarr** | Finds and downloads TV episodes and anime automatically, and upgrades them when better copies appear | ~250 MB, ~250 MB RAM |
| **Radarr** | The same for films: finds, downloads, renames and upgrades them | ~250 MB, ~250 MB RAM |
| Recyclarr | TRaSH Guides quality rules: any watchable copy first, then upgrades (films to 4K, shows to 1080p) | ~80 MB |
| Bazarr | English subtitles when the audio isn't English (anime, foreign films) | ~400 MB, ~200 MB RAM |
| MediaSorter | Sorts what you download yourself into the library and strips unwanted audio/subtitle languages | ~250 MB, ~60 MB RAM |
| Lidarr | Music: add an artist and it finds, downloads, tags and files their albums | ~250 MB, ~250 MB RAM |
| trawl, Byparr | Get Prowlarr past Cloudflare's "checking your browser" page | ~1.2 GB / ~700 MB |

**Download**

| Part | What it's for | Size |
|---|---|---|
| **VPN + qBittorrent** | Downloads torrents only ever through your VPN: if the VPN drops, downloads stop | ~120 MB, ~250 MB RAM |
| Unpackerr | Unpacks releases that arrive as .rar/.zip archives | ~20 MB |
| Cleanuparr | Removes stalled and fake downloads so requests don't sit stuck | ~150 MB, ~150 MB RAM |
| Qui | A faster qBittorrent page with seeding rules per tracker | ~40 MB |
| autobrr | Grabs new releases the moment private trackers announce them on IRC | ~50 MB |

**Request & share**

| Part | What it's for | Size |
|---|---|---|
| **Seerr** | The request page: search and click *Request*; friends can too (auto-approved) | ~1.5 GB on Windows (built from source), ~300 MB RAM |
| jfa-go | Invite links that create a Jellyfin account for a friend | ~30 MB |
| Requestrr | A Discord bot for requests | ~150 MB, ~100 MB RAM |

**Remote access**

| Part | What it's for | Size |
|---|---|---|
| Tailscale | Private access from phones, TVs and friends away from home, nothing opened to the internet | ~100 MB |

**Extras**

| Part | What it's for | Size |
|---|---|---|
| Media Hub | One dashboard: status, problems, requests, logs, hourly health check, a watchdog that restarts apps, a VPN leak check, and a request doctor that says why a request isn't here yet | ~30 MB, ~80 MB RAM |
| Nightly backups | Every app's settings and databases (not the media) at 3:30, with a Restore button | ~200 MB of backups |
| Phone alerts | "Ready to watch" and problems on your phone through the free ntfy app | - |

**Minimal** = Jellyfin, Seerr, Sonarr, Radarr, Prowlarr, VPN + qBittorrent. **Recommended** adds Intro
Skipper, Recyclarr, Bazarr, MediaSorter, Unpackerr, Cleanuparr, the Media Hub with nightly backups, Tailscale
and (on Windows) Marquee.

## Marquee

Marquee is the stack's desktop app for Windows: a Netflix-style player for your Jellyfin library that plays
the files directly, requests through Seerr, and the Media Hub's dashboard (*Your stack*) in one window, with
Marquee Mobile for watching on a phone over Tailscale. On *Choose your stack*, pick **Use Marquee as your stack
app (recommended)** or **Media Hub only** (the browser dashboard, with Jellyfin as the player). The installer
downloads Marquee, adds Start menu and desktop shortcuts, and fills in its settings for the stack, so the
first launch only asks for your Jellyfin password. The Media Hub keeps running in the background either way.
Marquee is Windows-only for now; on Linux the Media Hub is the stack app.

## Download

Get the file for your computer from the [Releases](../../releases) page:

| File | For |
|---|---|
| `MediaStackSetup.exe` | Windows 10/11 (64-bit), about 11 MB |
| `MediaStackSetup-linux-x64` | Linux on Intel/AMD |
| `MediaStackSetup-linux-arm64` | Linux on ARM (Raspberry Pi 4/5, ARM servers) |

## Quick start

**Windows**: double-click `MediaStackSetup.exe`. The wizard opens in your browser. Keep the small
"Media Stack Setup" window open while you work through the steps.

**Linux**:

```bash
chmod +x MediaStackSetup-linux-x64
sudo ./MediaStackSetup-linux-x64
```

On a server without a screen, it prints an address with a one-time key; open it from any computer on your
network.

Then follow the steps. You can close the wizard at any time and run it again later: finished steps are
remembered, and running a step again updates that app to its newest version.

## Updating

When a new version is released, the wizard offers it at the top (**Update the installer**: downloaded,
checksum-verified, and restarted into). Then **Update my stack** on the first page (or `--update`) brings the
installed stack up to date: the MediaSorter / Media Hub files, pinned apps, Marquee, and optionally every app
with a newer release. It never touches settings or media, and ends with a summary of what changed.

## Guides

- [Installing on Windows](docs/windows.md)
- [Installing on Linux](docs/linux.md)
- [After setup: adding indexers, requesting, phones and TVs, friends](docs/after-setup.md)
- [How it works: folders, background apps, security](docs/how-it-works.md)
- [Troubleshooting](docs/troubleshooting.md)

## What you need

- A PC or server that stays on, with plenty of disk space (4K films are 40-80 GB each).
- A VPN subscription for downloading (NordVPN, Proton VPN, Mullvad, Surfshark, PIA and others work).
- The indexers (search sites) you want to use. The wizard shows you where to add them; it never picks any for you.
- About an hour for the full setup, most of it waiting for downloads.

## Building from source

Needs [Go](https://go.dev/dl/) 1.27 or newer; no C compiler.

```bash
go vet ./... && go test ./...
GOOS=windows GOARCH=amd64 go build -ldflags "-s -w -H windowsgui" -o dist/MediaStackSetup.exe .
GOOS=linux   GOARCH=amd64 go build -ldflags "-s -w" -o dist/MediaStackSetup-linux-x64 .
GOOS=linux   GOARCH=arm64 go build -ldflags "-s -w" -o dist/MediaStackSetup-linux-arm64 .
```

On Windows, `build.ps1` does all of that. The wizard page (`assets/web`) and the MediaSorter / Media Hub
Python apps (`assets/python`) are built into the program; everything else (Marquee included) is downloaded
when its step runs, so the program stays about 11 MB. Before a release, set Marquee's download in
`internal/steps/marquee.go` (`MarqueeVersion`, `MarqueeURL`, `MarqueeSHA256`, `MarqueeSize`) and publish
`marquee-update.json` with the release (format in [How it works](docs/how-it-works.md#updates)).

Command-line options (run with `--help`):

```
--root <folder>     install into (or continue) that folder
--port-offset <n>   add n to every app's port (test next to an existing install)
--name <name>       name of the install (Task Scheduler folder / Docker Compose project)
--listen <address>  where the wizard listens (default 127.0.0.1; 0.0.0.0 on a Linux server without a screen)
--port <n>          the wizard's own port (default 8590)
--no-browser        don't open a browser
--update            open "Update my stack" straight away
```

## Disclaimer

This project installs and configures third-party open-source software. You're responsible for what you
download and for following the laws where you live.
