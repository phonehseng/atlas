# After setup

## 1. Add your indexers (required)

Sonarr and Radarr can only find things on the sites (indexers) you add. Nothing is added for you.

1. Open **Prowlarr** (Media Hub → Prowlarr, or `http://localhost:9696`).
2. *Indexers* → *Add Indexer* → search for a site you use → fill in what it asks (private trackers need your
   account's API key or cookie) → *Test* → *Save*.
3. Within a minute they appear in Sonarr and Radarr automatically (*Settings → Indexers* there).

**Sites behind Cloudflare** ("checking your browser"): install the **trawl** step (and optionally **Byparr**),
then edit the indexer in Prowlarr, add the tag `trawl` (or `byparr`), and *Test* again.

## 2. Request something

Open **Seerr** (`http://<server>:5055`), sign in with your Jellyfin login, search, and click **Request**.

- Requests are approved immediately.
- Anime is detected and goes to the *Anime* folders with the anime quality profile.
- Follow progress in the Media Hub (*Downloading*, *Activity*). It appears in Jellyfin a minute or two after
  the download finishes, and you get a notification (Windows, and your phone if phone alerts are on).
- Not there yet? The Media Hub's *Requests* card says why for each request (see [the request doctor](#the-request-doctor)).

You can also request from the Media Hub's **Request** tab, or add shows/films directly in Sonarr/Radarr.

## 3. Watch on phones and TVs

Install the **Jellyfin** app (Android, iPhone/iPad, Android TV / Google TV, Fire TV, Roku, LG/Samsung TVs,
or *Swiftfin* on Apple TV) and enter the server address:

- **At home:** `http://<server's address>:8096` (the *All done* step and the Media Hub's *Server addresses*
  card show it).
- **Away from home:** install **Tailscale** on the device, sign in with the same account, and use
  `http://<server's Tailscale name>:8096`. If the name doesn't work, use the `100.x.y.z` address instead.

The Jellyfin app needs Jellyfin's address (port **8096**). The Media Hub (port 8585) is a web page, not a
Jellyfin server; the app can't connect to it.

## 4. Friends and family

1. **Accounts**: open **jfa-go** (`http://<server>:8056`), sign in with your Jellyfin admin login,
   *Invites* → *Create*, and send the link. They choose a username and password and get their own Jellyfin
   account, which also works in Seerr.
2. **Access from their homes**: in the Tailscale admin console (login.tailscale.com) → *Machines* → your
   server → *Share* → send the link. They install Tailscale, sign in with their own account and accept. Then
   they use `http://<server's Tailscale name>:8096` in the Jellyfin app and `:5055` for requests.

### Requests from Discord (Requestrr)

1. Open Requestrr (`http://<server>:4545`); the first visit asks you to create its login.
2. Create a bot: discord.com/developers → *New Application* → *Bot* → *Reset Token* (copy it) → turn on
   *Message Content Intent* → *OAuth2 → URL Generator* (scopes *bot* and *applications.commands*) → open
   the link to add the bot to your server.
3. In Requestrr: paste the bot token; for requests choose *Overseerr* with Seerr's address and API key
   (Seerr → *Settings → General*).

### IRC auto-grabs (autobrr)

For private trackers that announce new releases on IRC: autobrr → *Settings → Indexers* → add your tracker,
then *Filters* → create a filter that sends matches to Sonarr or Radarr (already connected).

## Downloading things yourself

- In qBittorrent, choose the category **movies**, **tv** or **anime** when adding a torrent: MediaSorter
  identifies it, names it and files it into the library when it finishes.
- Or drop `.torrent` files into `<folder>/data/downloads/watch`.
- Or drop video files into `<folder>/data/drop`; they're sorted after a minute.

## Quality and file size

The rule is **get whatever is available first, then upgrade**, so a request never waits for a perfect copy:

| | Profile | Takes first | Upgrades up to |
|---|---|---|---|
| Films | *Remux 2160p (Combined)* | any watchable release (even a small encode) | a 4K remux (40-80 GB) |
| Shows | *1080p* | any watchable release | 1080p (web or Blu-ray) |
| Anime | *[Anime] Remux-1080p* | any English-subtitled release | the best 1080p release from the anime groups |

Low-quality encodes (YTS-style), x265 and upscales are only taken when nothing better exists. Disc images,
extras, 3D, dubbed-only versions, raw (unsubtitled) anime and anime subtitled only in another language (the
*Foreign subs only* custom format: titles like "Legendado" or "Sub ITA" that don't mention English, Multi-Subs
or "Multiple Subtitle") are never taken. Sonarr and Radarr only look for
upgrades in brand-new releases, so the Media Hub's request doctor also searches a few older items for better
copies every hour.

Want a show in 4K? Edit it in Sonarr and pick *Remux 2160p (Combined)*. Want smaller films? Pick *UHD Bluray +
WEB* or *HD Bluray + WEB* in Radarr (or as the default in Seerr → *Settings → Services*).

## Music (Lidarr)

If you installed **Lidarr**: open it → *Add New* → search for an artist. Albums go to `data/media/Music`, which
Jellyfin shows as the *Music* library. Lidarr only gets the Prowlarr indexers that have music; if it says it has
no indexers, add a site with music in Prowlarr.

## Qui

If you installed **Qui**: it's a faster qBittorrent page (sign in with your admin login). *Automations* sets
rules per tracker: how long to seed, when to pause or remove. Torrents without a rule keep qBittorrent's own
limits.

## Skip Intro

With **Intro Skipper** (the Jellyfin step's checkbox), episodes get a *Skip Intro* button once the plugin has
scanned them. It scans new episodes as they arrive and everything else in its daily scheduled task (Jellyfin →
*Dashboard → Scheduled Tasks*). Supported by Jellyfin's own apps and web page.

## Audio and subtitle languages

MediaSorter removes audio and subtitle tracks in languages you didn't choose from new library files
(losslessly; nothing is re-encoded). The default is English audio and subtitles, plus Japanese audio for
anime. Films with no audio in your languages keep all their audio. Change the languages by running the
*MediaSorter* step again.

## Updating

Run the setup program again and press **Run again** on a step to update that app. Your settings are kept.

## Your dashboard

The **Media Hub** (`http://localhost:8585` on Windows, `http://<server>:8585` on Linux) shows:

- every app's status and every problem the apps report, with fix buttons;
- what's downloading, recent requests (approve/decline), recently sorted files, what's playing;
- a combined error log from all apps;
- the addresses to use on phones and TVs.

A health check runs every hour: it restarts stopped apps, re-tests failed indexers, imports downloads that
got stuck on a folder-name mix-up, rescans Jellyfin when it's missing items, restarts Jellyfin to finish a
plugin install when nobody is watching, runs the request doctor, checks the backups and the VPN, and tells you
about anything it can't fix.

### The request doctor

Every request that isn't fully available is followed into Sonarr/Radarr and the download queue. The *Requests*
card says, in plain words, where it stands:

- **Not out yet**: still in cinemas, or the next episode hasn't aired. Nothing to do; it downloads on release.
- **Downloading**: how far along it is.
- **Searching**: it searched the indexers (each item at most every 6 hours). If a release qualifies it's
  grabbed; if none do, it says why every release was turned down: no seeders, too small to be real,
  blocklisted after an earlier failure, or no indexer has it at all (only more indexers can help then).
- **Stuck**: searched repeatedly with nothing usable. The health check tells you once, after three days.

What it fixes by itself: a request Seerr never handed to Sonarr/Radarr (sent again), an anime profile on a show
that isn't anime, unmonitored items, a downloaded film whose file is far shorter than the film (blocklisted and
moved to the recycle bin so a proper copy is found), and Seerr not noticing a finished download. Press **Why
not here?** on the card to run it now.

### Backups

Every night at 3:30 the settings and databases of every app (not the media) are zipped into
`<folder>/backups`; the newest 7 are kept. *Back up now* makes one immediately. **Restore** (on the server
itself) stops the apps, puts that backup's files back and starts them again; the current settings are saved as
a backup first, so a restore can be undone. A backup is about 10 MB. Copy the `backups` folder to another drive
or cloud storage now and then: a backup on the same disk doesn't survive that disk failing.

### VPN leak check

Every 10 minutes the hub compares the internet address qBittorrent's peers see (qBittorrent reports it) with
your home address, and shows where torrents appear from on the Overview (*Torrents via VPN · city*). If they
ever match, torrents are leaking past the VPN: you get an alert, and the problem shows until it's fixed. It
also checks that qBittorrent is still locked to the VPN's network connection (Windows).

### Phone alerts

Overview → *Phone alerts* → **Turn on**, then install the free **ntfy** app (App Store / Google Play) and
subscribe to the topic the card shows. You'll get "ready to watch" (with the poster) and problem alerts, even away
from home. The topic name is the only key, so keep it private; the alerts go through the public ntfy.sh server.
To use your own ntfy server, add `"ntfy_server": "https://..."` to `config/hub/hub.json`.

### The watchdog

Every 5 minutes the Media Hub's **watchdog** checks every app. When one has gone down it:

1. restarts it (at most once every 30 minutes per app, so a broken app isn't restarted in a loop);
2. records an **incident** with what the app's own log says, translated into plain words when it recognises
   the cause (port in use, disk full, locked database, wrong API key, network unreachable, out of memory...);
3. notifies you if the app is still down at the next check, because that needs a person;
4. marks the incident resolved when the app is back.

Incidents are on the Overview (*What's happening → Incidents*) and the Activity page. Click the **Watchdog**
chip on the Overview to turn it off or on.

Optional: if you run [Ollama](https://ollama.com) on the server, the watchdog can have a local AI model write
the diagnosis. In `<folder>/config/shared/mediastack.json`, set `"watchdog": {"ollama_host":
"http://127.0.0.1:11434", "ollama_model": "<a model you have>"}` and restart the Media Hub. The same section
has `minutes` (how often it checks) and `auto_restart`.
