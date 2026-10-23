# Installing on Linux

On Linux every app is a Docker container in one Docker Compose project. qBittorrent runs *inside* a VPN
container (Gluetun), so it has no network at all unless the VPN is connected.

## Before you start

- A 64-bit Linux PC or server (Ubuntu, Debian, Fedora, Raspberry Pi OS 64-bit...). Docker can be installed by
  the first step.
- `/dev/net/tun` must exist for the VPN container (it does on normal installs; on a VM or LXC container,
  enable TUN for that machine).
- Your VPN provider's **manual setup credentials** (see below).

## Running the wizard

```bash
chmod +x MediaStackSetup-linux-x64     # or -linux-arm64 on a Raspberry Pi / ARM server
sudo ./MediaStackSetup-linux-x64
```

- On a desktop, your browser opens.
- On a server without a screen, it prints an address like `http://192.168.1.20:8590/?t=...`. Open it from
  any computer on your network. The `t=` part is a one-time key; don't share it.
- Keep the program running (use `tmux` or `screen` over SSH) until you're done. Press Ctrl+C to stop it.

The apps run as the user who ran `sudo` (their files belong to you, not root). The default folder is
`/srv/mediastack`.

## VPN credentials

Gluetun signs in with your provider's credentials for manual setups, which are **not** your website login:

| Provider | Where | Type |
|---|---|---|
| NordVPN | nordaccount.com → NordVPN → *Set up NordVPN manually* → *Service credentials* | OpenVPN |
| Proton VPN | account.proton.me → VPN → *OpenVPN / IKEv2 username* | OpenVPN |
| Mullvad | mullvad.net account → *WireGuard configuration* → generate a key; copy the private key and the address | WireGuard |
| Surfshark | my.surfshark.com → VPN → *Manual setup* → *Router or other* → *Credentials* | OpenVPN |
| PIA | Your PIA username/password | OpenVPN |
| Others | Look for "manual setup", "OpenVPN credentials" or "WireGuard key" | |

They're saved only in `/srv/mediastack/.env`, readable by root only. The step waits until the VPN container is
connected and shows the public address your downloads will use.

## The steps

They're the same as on Windows (see the [Windows guide](windows.md#the-steps)), except:

- **VPN for downloads** creates the Gluetun container instead of using a VPN app.
- Nothing installs system-wide and there are no permission prompts.
- **Phones, TVs & away from home** installs Tailscale on the server with its official script and prints a
  sign-in link.
- Admin pages (Sonarr, Radarr...) always ask for your login, even on your home network, because a container
  can't tell "this computer" apart from other devices.

## Day to day

```bash
cd /srv/mediastack
sudo docker compose ps                 # what's running
sudo docker compose logs -f sonarr     # follow one app's log
sudo docker compose restart radarr     # restart one app
sudo docker compose pull && sudo docker compose up -d   # update every app
```

Or run the setup program again and press **Run again** on a step to update that app.

Containers restart by themselves after a crash or reboot (`restart: unless-stopped`).

## Hardware video conversion

If the server has Intel or AMD graphics (`/dev/dri` exists), it's passed into the Jellyfin container. Turn it
on in Jellyfin → Dashboard → Playback → Transcoding → *Hardware acceleration*: **Intel QuickSync (QSV)** for
Intel, **VAAPI** for AMD. NVIDIA needs the NVIDIA Container Toolkit; see Jellyfin's documentation.

## Uninstalling

```bash
cd /srv/mediastack && sudo docker compose down --rmi local
sudo rm -rf /srv/mediastack      # deletes your library too - move media out first
```
