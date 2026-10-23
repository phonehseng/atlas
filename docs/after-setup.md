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
  the download finishes, and you get a notification (Windows).

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

The default profiles prefer **4K remuxes** (best quality, 40-80 GB per film) and fall back to 1080p when no
4K exists. To use less space, pick a different profile in Sonarr/Radarr (*Edit* the show/film, or change the
default in Seerr → *Settings → Services*): *WEB-2160p*, *UHD Bluray + WEB* or *HD Bluray + WEB* (1080p) are
also installed.

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
got stuck on a folder-name mix-up, rescans Jellyfin when it's missing items, and tells you about anything it
can't fix.
