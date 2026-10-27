# Atlas

A Netflix-style home theatre for your own films and shows, and one guided installer that builds the whole
media server behind it.

- **Atlas** (Windows): rows of posters instead of folders, previews that play when you rest on a poster, and a
  player that plays your files directly (4K HDR, every audio track, full anime subtitles) with nothing in between
  to buffer. Requests, recommendations, your phone and the server's health live in the same app.
- **The installer**: one small program (about 11 MB) that installs and connects everything else, one explained
  step at a time: Jellyfin, Seerr, Sonarr, Radarr, Prowlarr, qBittorrent behind a VPN, subtitles, backups,
  Tailscale and more. You pick the parts on a checklist.

Request a film or show from your phone, and a few minutes later it's in your library, named properly, with
English subtitles for anime. It takes whatever copy is available first and keeps upgrading it (films up to 4K,
shows up to 1080p), and if a request is stuck, Atlas tells you why and fixes what it can.

- **Windows 10/11**: apps are installed natively and run in the background (Task Scheduler), no Docker needed.
- **Linux** (PCs, servers, Raspberry Pi 4/5 with a 64-bit OS): apps run as Docker containers, with the Media Hub
  as the dashboard (Atlas is Windows-only for now). *Preview: the Linux version is newer and less tested.*

## Atlas, the app

**Browsing**
- Home is rows, not folders: continue watching, next up, My List, because-you-watched, sequels and recommended
  requests, genres, and "Just arrived" downloads before Jellyfin has even imported them.
- Liquid-glass posters show what's yours (clear), what you can request (dark) and what's on its way (white).
- Title pages like a streaming service: a 1080p preview with subtitles, IMDb and Rotten Tomatoes scores, cast,
  seasons and episodes, trailers, collections, and your own 5-star rating.
- A taste profile that learns from what you finish, rate and say ("Is this for you?" on every title), started by
  a first-launch question or your Letterboxd, and fully visible and editable in Settings.
- **Ask Atlas**: a chat that recommends, opens titles, plays the next episode, requests "seasons 5-8 of Doctor
  Who" (you confirm first) and explains why something isn't working.

**Playing**
- mpv inside the window: hardware decoding, Dolby Atmos / DTS:X passthrough, NVIDIA RTX Video Super Resolution
  and RTX HDR, Anime4K for anime, night mode, dialogue boost and AI voice isolation, stable volume.
- YouTube-style controls: red progress bar with thumbnails, hold for 2x, 0.5-3x speed without pitch change,
  frame stepping, chapters, bookmarks, a clip tool, picture-in-picture you can resize from any corner.
- Skip intro/credits, and when the credits roll the video shrinks to a corner with what to watch next.
- **Ask Atlas** (press A): "who's that actor?", "is that the guy from The Walking Dead?", "is there a jumpscare
  soon?", "go to the part where...". It looks at the picture and checks the soundtrack ahead; answers appear
  quietly at the top while the film keeps playing.
- Keeps playing with Jellyfin down: everything on your PC plays from a local copy of the library.

**Around it**
- Requests with a quality picker and size estimate, "smart" season-by-season requests, a storage limit and
  optional clean-up of watched or forgotten titles (they stay in your history).
- **Atlas Mobile**: browse and stream to your phone over Tailscale, or use it as a remote.
- **Your stack**: the Media Hub's dashboard inside Atlas, with Fix buttons.
- Share a clip or file to your own Google Drive as a private link.
- Updates itself from this page's releases (automatic, notify or off) and rolls back if an update won't start.

AI is optional and local-first: Atlas (a local assistant on the same PC), any OpenAI-compatible key, or the
**built-in AI** (a small model downloaded on demand, no account). **Classic** mode turns all of it off and uses
hand-written lines and rules instead. Details and the developer guide: [app/README.md](app/README.md).

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
| **Atlas** *(Windows)* | A Netflix-style player for the PC with requests and the stack's dashboard in one app; plays files directly | ~250 MB, ~400 MB RAM |

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
and (on Windows) Atlas.

## Atlas or Media Hub only

On *Choose your stack*, pick **Use Atlas as your stack
app (recommended)** or **Media Hub only** (the browser dashboard, with Jellyfin as the player). The installer
downloads Atlas, adds Start menu and desktop shortcuts, and fills in its settings for the stack, so the
first launch only asks for your Jellyfin password. The Media Hub keeps running in the background either way.
Atlas is Windows-only for now; on Linux the Media Hub is the stack app.

## Download

Get the file for your computer from the [Releases](../../releases) page:

| File | For |
|---|---|
| `MediaStackSetup.exe` | Windows 10/11 (64-bit), about 11 MB: installs the stack and Atlas |
| `Atlas-windows-x64.zip` | Atlas on its own, for an existing Jellyfin server: unzip and run `Atlas.exe` |
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
installed stack up to date: the MediaSorter / Media Hub files, pinned apps, Atlas, and optionally every app
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

The installer is the Go program at the top of this repository; Atlas is in [`app/`](app) (Python, see its
README).

Needs [Go](https://go.dev/dl/) 1.27 or newer; no C compiler.

```bash
go vet ./... && go test ./...
GOOS=windows GOARCH=amd64 go build -ldflags "-s -w -H windowsgui" -o dist/MediaStackSetup.exe .
GOOS=linux   GOARCH=amd64 go build -ldflags "-s -w" -o dist/MediaStackSetup-linux-x64 .
GOOS=linux   GOARCH=arm64 go build -ldflags "-s -w" -o dist/MediaStackSetup-linux-arm64 .
```

On Windows, `build.ps1` does all of that. The wizard page (`assets/web`) and the MediaSorter / Media Hub
Python apps (`assets/python`) are built into the program; everything else (Atlas included) is downloaded
when its step runs, so the program stays about 11 MB. Before a release, set Atlas's download in
`internal/steps/marquee.go` (`MarqueeVersion`, `MarqueeSHA256`, `MarqueeSize`; `MarqueeURL` follows the
version) and publish `atlas-update.json` and `marquee-update.json` with the release (format in
[How it works](docs/how-it-works.md#updates)).

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

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).

## Disclaimer

This project installs and configures third-party open-source software. You're responsible for what you
download and for following the laws where you live.
