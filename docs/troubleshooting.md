# Troubleshooting

Start with the **Media Hub**: its Overview lists every problem the apps report (with fix buttons), and
**Errors & logs** shows warnings and errors from every app in one place. The hourly health check fixes the
common problems below by itself and reports the rest in *Activity*.

Logs: Windows `<folder>\logs\<app>.log` and `<folder>\config\<app>\logs`; Linux `docker compose logs <app>`.

## The setup wizard

**"This page has expired"**: the wizard was restarted. Use the address it just printed (or run the program
again; it reopens its page).

**A step failed**: read the last lines of its log. Most steps can simply be run again; finished parts are
skipped. If setup was restarted, the wizard asks for your admin password again.

**"These ports are already used by other programs"**: another media server (or an older install) is
running. Stop it, or uninstall it, then press *Check* again. To try this installer next to an existing setup,
start it with `--root D:\Test --port-offset 20000 --name "Media Stack Test"`.

**Windows: "winget is missing"**: install *App Installer* from the Microsoft Store.

**A Windows permission prompt was declined**: press the step's button again and choose *Yes*.

**SmartScreen blocks the program**: *More info → Run anyway* (the program isn't code-signed).

**Seerr's build fails (Windows)**: it needs about 4 GB of free memory and 3 GB of disk space. Close other
programs and run the step again.

## Downloads

**Nothing downloads, "No indexers available"**: add indexers in Prowlarr ([after setup](after-setup.md#1-add-your-indexers-required)).

**qBittorrent shows "firewalled"**: normal behind most VPNs (no port forwarding). Downloads still work; very
rare torrents with only "firewalled" seeders may not connect.

**A download stays on "Downloading metadata"**: the torrent's few seeders can't be reached. Cleanuparr gives
up after about 30 minutes, blocklists that release, and Sonarr/Radarr try another. To skip the wait:
Sonarr/Radarr → *Activity → Queue* → remove it with *Blocklist release* and *search again* ticked.

**All downloads stopped / "qBittorrent has no network"**: the VPN is disconnected. Reconnect it.
- Windows: qBittorrent is locked to the VPN's network connection on purpose. After reconnecting, MediaSorter
  restarts qBittorrent within about a minute if it doesn't pick the VPN up again by itself.
- Linux: `docker compose logs gluetun`. Wrong credentials show as authentication failures; run the
  *VPN for downloads* step again with the right ones.

**Downloads failed during an internet/VPN outage and were blocklisted**: Sonarr/Radarr → *Activity →
Blocklist* → remove them, then search again. The health check warns about this.

**Indexer errors mentioning Cloudflare, or "unable to connect" on a Cloudflare site**: give the indexer the
`trawl` tag in Prowlarr. If it worked before and stopped (often after the VPN reconnected to a different
server), restart trawl from the Media Hub. The health check does this automatically for failed `trawl`
indexers.

**A download finished but isn't imported**: Sonarr/Radarr → *Activity → Queue* shows why (the orange icon).
Releases whose folder name confused them are imported automatically by the health check; others need
*Manual Import* there.

**A manual download wasn't sorted**: check the Media Hub's *Recently sorted* card. It must have the category
*movies*, *tv* or *anime* (or be in the *drop* folder). Use *Retry failed imports* after fixing the cause.

## Jellyfin

**A new download isn't in Jellyfin**: the Media Hub checks 90 seconds after each import and runs a library
scan if it's missing. You can also use Jellyfin → *Dashboard → Libraries → Scan All Libraries*.

**The Jellyfin app can't connect**:
- Use port **8096** (Jellyfin), not 8585 (Media Hub).
- At home: use the server's home-network address (Media Hub → *Server addresses*). Windows: run the
  *Phones, TVs & away from home* step with the firewall option ticked.
- Away from home: Tailscale must be on, on that device, signed in to the same account (or a shared one).
  If `http://<name>:8096` doesn't work, use `http://100.x.y.z:8096`.

**Playback stutters on a phone/TV**: the file is being converted. Turn on hardware acceleration (Jellyfin →
*Dashboard → Playback → Transcoding*), or use a quality profile with smaller files.

**Subtitles sit too high or too low**: in the Jellyfin app, *Settings → Subtitles → Vertical position*.

## Access and network

**Tailscale won't sign in while NordVPN is connected (Windows)**: add
`C:\Program Files\Tailscale\tailscaled.exe` to NordVPN's split tunnelling, then reconnect NordVPN.

**Some websites (for example Google) stop loading while the VPN is on**: the VPN's DNS servers may be giving
a wrong answer. In the VPN app, set *Custom DNS* to `1.1.1.1` and `1.0.0.1`, then reconnect.

**The Media Hub says "only available on your own network and over Tailscale"**: you opened it from the
internet or (Windows) from another device on your home network. On Windows, open it on the PC or over
Tailscale.

## Apps

**An app shows "Down" in the Media Hub**: press its restart button. Windows: Task Scheduler → *Media Stack* →
right-click the app → *Run*. Linux: `docker compose restart <app>`. If it keeps stopping, check its log.

**Sonarr/Radarr ask for a login on this PC (Windows)**: they shouldn't on the PC itself. Run their step
again to reapply the settings.

**Bazarr finds no subtitles**: add a free OpenSubtitles.com account in Bazarr → *Settings → Providers*.

**The apps don't start after a reboot (Windows)**: they start when you sign in. Turn on automatic sign-in on
a dedicated server PC.

## Starting over

Run a step again to redo it. To start completely fresh, uninstall (see the [Windows](windows.md#uninstalling)
or [Linux](linux.md#uninstalling) guide) and run the program again with a new folder.
