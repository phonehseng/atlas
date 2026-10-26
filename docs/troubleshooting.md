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

**A step I want isn't in the list**: it wasn't chosen. Open **Choose your stack**, tick it, and press *Use this
selection*. Unticking something already installed only hides its step.

**"Update the installer" failed**: the download didn't match the release's checksum or couldn't be saved next
to the program. Download the new version from the Releases page instead; your install is unaffected.

**Update my stack: "Marquee is open"**: close Marquee (and its tray icon), then run *Update my stack* again.

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

**A request has been "Searching" for days**: read its line on the Media Hub's *Requests* card. "No indexer
has it" means none of your indexers carry it: add more in Prowlarr. "None usable: no one is sharing it" means it
exists but nobody seeds it; try again later or add indexers. "Blocked by the quality rules" names the rule; for
a one-off, use *Interactive Search* in Sonarr/Radarr and grab a release yourself.

**A film downloaded but it's the wrong length**: the request doctor removes film files far shorter than the
film (they go to the recycle bin, `data/media/.recycle-bin`, kept 14 days) and blocklists that release.

**A download finished but isn't imported**: Sonarr/Radarr → *Activity → Queue* shows why (the orange icon).
Releases whose folder name confused them are imported automatically by the health check; others need
*Manual Import* there.

**A manual download wasn't sorted**: check the Media Hub's *Recently sorted* card. It must have the category
*movies*, *tv* or *anime* (or be in the *drop* folder). Use *Retry failed imports* after fixing the cause.

**The VPN leak check says torrents use your home address**: stop qBittorrent (Media Hub → restart, or close it),
then: Windows: run the *VPN for downloads* and *qBittorrent* steps again with the VPN connected. Linux:
`docker compose logs gluetun`. The check runs again within 10 minutes.

**Lidarr has no indexers**: Prowlarr only shares indexers that return music results with Lidarr. Add a site
with music in Prowlarr.

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

**No "Skip Intro" button**: Intro Skipper scans episodes in the background (Jellyfin → *Dashboard → Scheduled
Tasks*). Some apps don't show the button yet; Jellyfin's web page and official apps do. If the plugin shows
"Restart" in *Dashboard → Plugins*, restart Jellyfin (the health check does it once nobody is watching).

**Subtitles sit too high or too low**: in the Jellyfin app, *Settings → Subtitles → Vertical position*.

## Marquee

**Marquee asks for the Jellyfin address**: it was already signed in to another Jellyfin server, so setup left
its settings alone (the step's log says so). In Marquee: *Settings → Sign out*, then sign in with
`http://localhost:8096`.

**Marquee's "Your stack" page says it can't find the Media Hub**: check that the Media Hub task is running
(Task Scheduler → *Media Stack* → *Media Hub*). Run the *Marquee* step again to rewrite Marquee's settings
for this stack (your other Marquee settings are kept).

**Marquee Mobile doesn't load on the phone**: the phone must be on Tailscale; check that the firewall rule
*Marquee Mobile (Tailscale)* exists (*Windows Defender Firewall → Inbound Rules*) and run the
*Phones, TVs & away from home* step again with the firewall box ticked if it doesn't.

## Access and network

**The Tailscale install failed (Windows)**: its installer log is `<folder>\logs\tailscale-install.log`.
Run the step again, or install Tailscale from tailscale.com yourself: the step then only signs it in.

**"Tailscale sign-in didn't finish"**: sign in from the Tailscale icon in the taskbar (or `tailscale up` on
Linux), then run the step again; once Tailscale is connected it just reads this computer's name and address.
If Tailscale was set up earlier with other options, the step doesn't change them.

**Tailscale won't sign in while NordVPN is connected (Windows)**: add
`C:\Program Files\Tailscale\tailscaled.exe` to NordVPN's split tunnelling, then reconnect NordVPN.

**Some websites (for example Google) stop loading while the VPN is on**: the VPN's DNS servers may be giving
a wrong answer. In the VPN app, set *Custom DNS* to `1.1.1.1` and `1.0.0.1`, then reconnect.

**The Media Hub says "only available on your own network and over Tailscale"**: you opened it from the
internet or (Windows) from another device on your home network. On Windows, open it on the PC or over
Tailscale.

## Apps

**An app shows "Down" in the Media Hub**: the watchdog restarts it within 5 minutes and records an incident
saying what its log reported (Overview → *Incidents*). To restart it yourself: press its restart button, or
Windows: Task Scheduler → *Media Stack* → right-click the app → *Run*; Linux: `docker compose restart <app>`.

**The watchdog says an app is "still down"**: a restart didn't help, so read the incident's diagnosis (and the
app's log in *Errors & logs*). The common causes are another program on its port, a full drive, a damaged
database, or another app's API key having changed (run that app's setup step again). The watchdog won't
restart the same app more than once every 30 minutes.

**Sonarr/Radarr ask for a login on this PC (Windows)**: they shouldn't on the PC itself. Run their step
again to reapply the settings.

**Bazarr finds no subtitles**: add a free OpenSubtitles.com account in Bazarr → *Settings → Providers*.

**The apps don't start after a reboot (Windows)**: they start when you sign in. Turn on automatic sign-in on
a dedicated server PC.

## Backups

**Restore a backup**: Media Hub → Overview → *Backups* → **Restore** (on the server itself). The current settings
are saved as a backup first ("before restore"), so you can go back. From a command line:
Windows `<folder>\apps\python-env\Scripts\python.exe <folder>\apps\mediastack-py\mediasorter\backup.py restore <zip> [apps]`;
Linux `docker compose exec hub python /app/mediasorter/backup.py restore <zip> [apps]`.

**"The last settings backup is N days old"**: Windows: Task Scheduler → your install's folder → *Backup* → *Run*,
and check its log in `<folder>\logs\backup.log`. Linux: the hub container makes them; `docker compose logs hub`.

## Starting over

Run a step again to redo it. To start completely fresh, uninstall (see the [Windows](windows.md#uninstalling)
or [Linux](linux.md#uninstalling) guide) and run the program again with a new folder.
