# Installing on Windows

Everything installs natively, with no Docker, and runs in the background whenever you're signed in to Windows.

## Before you start

- Windows 10 or 11, 64-bit, with **App Installer** (it provides `winget`; it's built into Windows 11. On
  Windows 10, get it from the Microsoft Store if the first step says it's missing).
- A drive with lots of free space for the library.
- Your VPN app installed and connected (for example NordVPN with the *NordLynx* protocol).
- Close any older media-server apps that use the same ports (8096, 8080, 8989, 7878, 9696, 5055, 6767,
  11011, 7474, 8056, 4545, 8190, 8191, 8585). The first step checks this for you.
- The program itself is small (about 11 MB): each part is downloaded from its official source when its step
  runs, so the full install needs a working internet connection.

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
| Choose your stack | A checklist of every part with what it's for and roughly what it takes; presets *Recommended* (default), *Minimal* and *Everything*; ticking a part adds what it needs, unticking one says what depends on it; *Atlas* or *Media Hub only* as your stack app | Pick the parts. Only those appear as steps below |
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
| Media Hub | The dashboard, an hourly health check, a nightly settings backup (3:30) if chosen, phone alerts if chosen, a desktop shortcut (not with Atlas) | With phone alerts: subscribe to the topic it shows in the ntfy app |
| Atlas | Downloads Atlas (about 75 MB, checksum-verified), installs it in `<folder>\apps\Atlas`, adds Start menu and desktop shortcuts, fills in its settings for this stack, and makes Atlas's *Your stack* page the Media Hub's dashboard | Open Atlas and sign in with your Jellyfin login |
| Phones, TVs & away from home | Tailscale: the official installer from pkgs.tailscale.com (pinned version, checksum-verified, installed silently), or nothing if Tailscale is already installed. Windows Firewall rules for phones and TVs, plus *Atlas Mobile (Tailscale)* when Atlas is installed. One **Yes** prompt for all of it | Sign in to Tailscale in the browser window it opens |
| All done | A table of the addresses to use, and *Change what's installed* | Your to-do list |
| Update my stack | Shown once something is installed: brings the stack up to date without touching settings or media (see [Updating](#updating)) | - |

### Windows prompts you'll see

- **"Do you want to allow this app to make changes?"** (User Account Control): qBittorrent, MKVToolNix and
  Tailscale install system-wide, and the firewall step adds rules (Tailscale and the rules share one prompt).
  Click **Yes**.
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
- Atlas (Start menu and desktop) or the Media Hub (desktop shortcut) shows everything in one place.

If this PC is a dedicated server, turn on automatic sign-in so the apps start after a reboot. Search the web
for "Windows automatic sign-in" (Sysinternals *Autologon* is the easy way).

## Updating

- **The installer**: when a newer Media Stack Setup is published, the wizard shows *Media Stack Setup x.y is
  available* at the top. **Update the installer** downloads it (checked against the release's SHA-256),
  replaces the program and restarts into it.
- **Your stack**: run the program and press **Update my stack** on the first page (or start it with
  `--update`). It puts the newest MediaSorter / Media Hub / health check files in place (only if they
  changed), moves pinned apps to their tested version, updates Atlas to its newest release, starts
  anything that should be running, and ends with a summary. Tick *Also update every app...* to update
  Jellyfin, Sonarr, Radarr, Prowlarr and the other release-based apps too (only those with a newer release
  are touched). Settings, logins, requests and media are never changed.
- **One app**: press **Run again** on its step: it's updated to its newest version and its settings are kept.
  Sonarr, Radarr and Prowlarr don't update themselves on Windows (so they always restart in the background
  properly); use one of the above instead.

To add parts later, open **Choose your stack** (or *Change what's installed* on the last page), tick them, and
run their new steps. Unticking a part that is already installed hides its step but doesn't uninstall it.

## Uninstalling

1. Task Scheduler → delete the **Media Stack** folder's tasks (select them → Delete), then the folder.
2. Delete `<folder>` (this deletes your library and the `backups` folder too; move them out first if you want to keep them).
3. Atlas: delete its *Atlas* shortcuts (Start menu and desktop) and `%APPDATA%\Marquee` (its settings);
   the program itself is in `<folder>\apps\Atlas`. Media Stack Setup doesn't add entries to *Settings → Apps*
   for anything it installs into the folder (Atlas included); only the system-wide installs below have one.
4. Optional: uninstall qBittorrent, MKVToolNix and Tailscale in *Settings → Apps*, and remove the
   "Media Stack - ..." and "Atlas Mobile (Tailscale)" rules in *Windows Defender Firewall → Inbound Rules*.
