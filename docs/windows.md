# Installing on Windows

Everything installs natively, with no Docker, and runs in the background whenever you're signed in to Windows.

## Before you start

- Windows 10 or 11, 64-bit, with **App Installer** (it provides `winget`; it's built into Windows 11. On
  Windows 10, get it from the Microsoft Store if the first step says it's missing).
- A drive with lots of free space for the library.
- Your VPN app installed and connected (for example NordVPN with the *NordLynx* protocol).
- Close any older media-server apps that use the same ports (8096, 8080, 8989, 7878, 9696, 5055, 6767,
  11011, 7474, 8056, 4545, 8190, 8191, 8585). The first step checks this for you.

## Running the wizard

1. Download `MediaStackSetup.exe` from the Releases page and double-click it.
2. Windows SmartScreen may say "Windows protected your PC" because the program isn't code-signed: click
   **More info → Run anyway**.
3. A small window appears and the wizard opens in your browser. Keep the window open; closing it stops setup.
   If you close the browser tab, run the program again and it reopens the same page.

## The steps

| Step | What happens | What you do |
|---|---|---|
| Check this computer | Internet, winget, graphics card and ports are checked | Fix anything it lists, then *Check* again |
| Choose a folder | Creates `<drive>:\MediaServer` (or your choice) with the library and download folders | Pick a big drive |
| Your admin login | One username/password for Jellyfin and all admin pages | Choose a strong password |
| Jellyfin | Downloads Jellyfin, runs its first-time setup, creates the libraries, turns on NVIDIA transcoding if you have NVIDIA graphics, adds Intro Skipper ("Skip Intro" button) | Untick Intro Skipper if you don't want it |
| VPN for downloads | Locks qBittorrent to your VPN's network connection | Connect your VPN app, press **Refresh**, pick it (NordVPN: *NordLynx*) |
| qBittorrent | Installed system-wide (click **Yes** on the Windows prompt), folders, categories and seeding limits set | NordVPN split tunnelling: see below |
| Qui (optional) | A faster qBittorrent page with per-tracker seeding rules, signed in with your admin login | - |
| Prowlarr, Sonarr, Radarr | Installed and connected to qBittorrent, Jellyfin and each other | Add your indexers in Prowlarr (see [after setup](after-setup.md)) |
| Lidarr (optional) | Music: connected to qBittorrent, Prowlarr and Jellyfin (a *Music* library) | Add artists in Lidarr |
| Quality profiles | TRaSH Guides profiles synced weekly, set to take what's available first and upgrade (films to 4K, shows to 1080p) | - |
| trawl / Byparr (optional) | Cloudflare helpers for Prowlarr | Only if an indexer is behind Cloudflare |
| Bazarr | English subtitles when the audio isn't English | Optional: add an OpenSubtitles.com account |
| Unpackerr, Cleanuparr | Archive extraction, stuck/fake download removal | - |
| Seerr | Built from source (10-15 minutes), signed in to Jellyfin, connected to Sonarr/Radarr | - |
| jfa-go, Requestrr, autobrr (optional) | Invites, Discord requests, IRC grabs | See [after setup](after-setup.md) |
| MediaSorter | Python and MKVToolNix installed (click **Yes**), qBittorrent hands it every finished download | Choose which audio/subtitle languages to keep |
| Media Hub | The dashboard, an hourly health check, a nightly settings backup (3:30), a desktop shortcut | Optional: turn on phone alerts |
| Phones, TVs & away from home | Windows Firewall rules (click **Yes**), Tailscale installed and signed in | Sign in to Tailscale in the browser tab it opens |
| All done | A table of the addresses to use | Your to-do list |

### Windows prompts you'll see

- **"Do you want to allow this app to make changes?"** (User Account Control): qBittorrent, MKVToolNix and
  Tailscale install system-wide, and the firewall step adds rules. Click **Yes**.
- **"Windows Defender Firewall has blocked some features of this app"**: the first time Jellyfin, Seerr
  (Node.js), jfa-go and the Media Hub/Bazarr (Python) start. Click **Allow access** so phones and TVs on
  your network can connect.

### NordVPN split tunnelling

If you use NordVPN's *split tunnelling* ("VPN only for selected apps"), add these programs to it, then
reconnect NordVPN:

- `C:\Program Files\qBittorrent\qbittorrent.exe` (after the qBittorrent step)
- `C:\Program Files\Tailscale\tailscaled.exe` (after the Tailscale step), otherwise Tailscale can't sign in
  while NordVPN is connected.

Without split tunnelling, NordVPN covers the whole PC and nothing extra is needed.

## Where everything is

- **Task Scheduler → Task Scheduler Library → Media Stack**: one task per app. They start when you sign in,
  and restart within a minute if they crash. Right-click a task to stop or start it.
- `<folder>\apps`: the programs. `<folder>\config`: each app's settings. `<folder>\logs`: background apps' output.
- The Media Hub (desktop shortcut) shows everything in one place.

If this PC is a dedicated server, turn on automatic sign-in so the apps start after a reboot. Search the web
for "Windows automatic sign-in" (Sysinternals *Autologon* is the easy way).

## Updating

Run the program again and press **Run again** on a step: that app is updated to its newest version and its
settings are kept. Sonarr, Radarr and Prowlarr don't update themselves on Windows (so they always restart in
the background properly); use this instead.

## Uninstalling

1. Task Scheduler → delete the **Media Stack** folder's tasks (select them → Delete), then the folder.
2. Delete `<folder>` (this deletes your library and the `backups` folder too; move them out first if you want to keep them).
3. Optional: uninstall qBittorrent, MKVToolNix and Tailscale in *Settings → Apps*, and remove the
   "Media Stack - ..." rules in *Windows Defender Firewall → Inbound Rules*.
