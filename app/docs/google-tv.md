# Atlas on Google TV and Android TV

Atlas has a TV version: your library and the player, made for a TV remote. It's served by **Atlas Mobile** on
your PC (the same server the phone app uses), so the TV streams straight from the PC: it plays the file directly
when the TV can, or the PC converts it on the fly (with HDR tone-mapping for SDR TVs, and subtitles).

There are two ways to get it on a TV:

- **The Atlas app for Android TV** (`Atlas-AndroidTV.apk`): full screen, finds your PC by itself on the home
  network, keeps the screen on while you watch and works with the remote's media keys. Works on Google TV
  (Chromecast with Google TV, Google TV Streamer, Sony/TCL/Hisense TVs...), Android TV boxes and NVIDIA Shield.
- **Any TV browser**: open `http://<your PC's address>:8737/tv`.

## 1. Turn on Atlas Mobile on the PC

In Atlas on the PC, open **Settings › Phone & casting** and turn on **Atlas Mobile**. Then choose how the TV
reaches the PC (under *Advanced › Network*):

- **Home network + Tailscale**: the TV connects over your Wi-Fi/LAN, and the app finds the PC by itself.
- **Tailscale** only: install Tailscale on the TV too (see [Away from home](#away-from-home)).

The PC's addresses are shown at the top of that page (for example `http://192.168.1.20:8737` or
`http://my-pc.tailnet.ts.net:8737`). If Windows Firewall blocks the port, the page offers an **Allow Atlas**
button.

## 2. Install the app (sideloading)

The app isn't in the Play Store, so it's installed from the release page. Pick one way:

**With the Downloader app (easiest, nothing to plug in)**

1. On the TV, install **Downloader** (by AFTVnews) from the Play Store.
2. Allow it to install apps: *Settings › Apps › Security & restrictions › Unknown sources* (Google TV:
   *Settings › System › About*, press OK on *Android TV OS build* 7 times to become a developer first, then
   *Settings › Apps › Security & restrictions › Unknown sources › Downloader*).
3. In Downloader, enter this address and press Go:
   `https://github.com/phonehseng/atlas/releases/latest/download/Atlas-AndroidTV.apk`
4. Choose **Install**, then **Done**. You can delete the downloaded file when Downloader offers it.

**With a computer (adb)**

1. Turn on developer options on the TV (*Settings › System › About*, press OK on *Android TV OS build* 7
   times), then *Settings › System › Developer options › USB debugging* (and *Wireless debugging* on newer TVs).
2. From the PC: `adb connect <TV address>` (accept the prompt on the TV), then
   `adb install -r Atlas-AndroidTV.apk`.

Atlas then appears in the TV's apps row (on Google TV: *Apps › Your apps*; you can move it to Favourites).

Updates: install the newer APK the same way; it replaces the old one and keeps the pairing.

## 3. Connect and pair

1. Open Atlas on the TV. It looks for your PC on the home network: pick it, or type the PC's address (the TV's
   keyboard opens when you press OK on the box). `:8737` is added if you leave the port out.
2. The TV shows a six-letter code. On the PC, in **Settings › Phone & casting › Pair a TV**, type it and click
   **Pair TV**. The TV opens your library a second later.
   - Or the other way round: click **Show a code for the TV** on the PC and type that code on the TV (with the
     on-screen keys, or in the optional code box of the app's setup screen).
3. Each code works once, for 10 minutes. The TV now has its own sign-in; it's listed with your paired phones
   and can be removed there at any time (or unpaired from the TV's own Settings).

## Using it

| Remote | Browsing | Playing |
|---|---|---|
| Arrows | move between titles, rows and the menu (Left opens it) | ◀ ▶ jump 10 s (hold to go faster), ▲ shows the controls, ▼ opens subtitles, audio and quality |
| OK | open / pick | play / pause (or skip the intro, or start the next episode when offered) |
| Back | back one page, then to Home | leave the player (closes a menu first) |
| Play/Pause, ⏪ ⏩ | | play/pause, jump 30 s |

Search has on-screen letters, or press OK on the search box to use the TV's own keyboard (on most Google TV
remotes that includes voice typing with the microphone button). **Settings** on the TV sets the picture quality
(Auto picks what your network and TV can take) and whether the next episode starts by itself.

## Away from home

Atlas Mobile listens on your PC's [Tailscale](https://tailscale.com) address, so the TV can reach it from
anywhere (a holiday rental, a friend's place) without opening ports:

1. Install **Tailscale** on the TV from the Play Store (it has an Android TV app) and sign in with the same
   account as the PC (use *Sign in with another device* and scan the code with your phone).
2. In Atlas on the TV, choose *Settings › Change PC* and enter the PC's Tailscale address
   (`my-pc.tailnet.ts.net` or `100.x.y.z`), shown in Atlas's Settings on the PC.

Streams adapt to the connection: on *Auto* the PC lowers the quality when the network is slow.

## Troubleshooting

- **"Can't reach Atlas"**: the PC is asleep or off, Atlas isn't running, Atlas Mobile is off, or the TV is on a
  different network. Check the address in Settings › Phone & casting, and the firewall button there.
- **The PC isn't found automatically**: discovery only works on the same home network with *Home network +
  Tailscale* chosen; some routers block it (guest networks, "AP isolation"). Type the address instead.
- **No picture or stuttering**: set the quality to 1080p or 720p in the TV's Settings, or press ▼ while playing
  and pick a lower quality.
- **Moving to another PC**: Settings › Change PC on the TV.
