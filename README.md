# Media Stack Setup

One program that builds a complete, automated home media server, one guided step at a time. Every step
explains what it's about to install, installs it, connects it to everything already installed, and tells
you what to do next.

Request a film or show from your phone, and a few minutes later it's in Jellyfin, named properly, in 4K when
there's a 4K release, with English subtitles for anime, ready on your TV.

- **Windows 10/11**: apps are installed natively and run in the background (Task Scheduler), no Docker needed.
- **Linux** (PCs, servers, Raspberry Pi 4/5 with a 64-bit OS): apps run as Docker containers.
  *Preview: the Linux version is newer and less tested than the Windows one.*

## What you get

| | App | Job |
|---|---|---|
| Watch | **Jellyfin** | Your own streaming service for phones, TVs and browsers |
| Request | **Seerr** | Search and click *Request*; friends can too (auto-approved) |
| Find | **Prowlarr**, **Sonarr**, **Radarr** | Search your indexers, grab releases, rename and file them |
| Quality | **Recyclarr** | TRaSH Guides profiles: best 4K first, 1080p when no 4K exists |
| Download | **qBittorrent** + your **VPN** | Torrents only ever go through the VPN |
| Subtitles | **Bazarr** | English subtitles when the audio isn't English |
| Tidy | **Unpackerr**, **Cleanuparr** | Unpack archives; remove stalled, dead and fake downloads |
| Sort | **MediaSorter** | Files your own downloads into the library; strips unwanted audio/subtitle languages |
| Overview | **Media Hub** | One dashboard: status, problems, requests, downloads, logs, health checks |
| Away from home | **Tailscale** | Private access from your phone, TV and friends, nothing opened to the internet |
| Optional | **jfa-go**, **Requestrr**, **autobrr**, **trawl**, **Byparr** | Invite links, Discord requests, IRC auto-grabs, Cloudflare helpers |

## Download

Get the file for your computer from the [Releases](../../releases) page:

| File | For |
|---|---|
| `MediaStackSetup.exe` | Windows 10/11 (64-bit) |
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
Python apps (`assets/python`) are built into the program.

Command-line options (run with `--help`):

```
--root <folder>     install into (or continue) that folder
--port-offset <n>   add n to every app's port (test next to an existing install)
--name <name>       name of the install (Task Scheduler folder / Docker Compose project)
--listen <address>  where the wizard listens (default 127.0.0.1; 0.0.0.0 on a Linux server without a screen)
--port <n>          the wizard's own port (default 8590)
--no-browser        don't open a browser
```

## Disclaimer

This project installs and configures third-party open-source software. You're responsible for what you
download and for following the laws where you live.
