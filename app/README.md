# Marquee (the app)

Marquee is a Windows desktop player for your own films and shows: a Netflix-style front end on a
[Jellyfin](https://jellyfin.org) library that plays local files directly with [mpv](https://mpv.io), requests
through [Seerr](https://github.com/seerr-team/seerr), and shows the media stack's dashboard. What it does is
described in the [main README](../README.md); this page is about installing it on its own and working on it.

## Install

- **With the whole stack**: run `MediaStackSetup.exe` from the [Releases](../../../releases) page and keep
  Marquee ticked on *Choose your stack*. It's installed, given shortcuts and connected for you.
- **On its own**, for an existing Jellyfin server: download `Marquee-windows-x64.zip`, unzip it anywhere you can
  write to (so it can update itself) and run `Marquee.exe`. Sign in with your Jellyfin address and account, and
  your Seerr address if you use it.

Requirements: Windows 10 or 11 (64-bit) with Microsoft Edge WebView2 (Windows 11 has it). For RTX Super
Resolution, an NVIDIA RTX GPU with *RTX Video Enhancement* available in the NVIDIA App.

Settings live in `%APPDATA%\Marquee`; your Jellyfin token, Seerr session and keys are encrypted with Windows
DPAPI (only your Windows account can read them). Updates replace the program folder and never touch them.

Other programs on this PC (such as the Atlas assistant) can mark titles watched through a small token-protected
local API; see [docs/local-api.md](docs/local-api.md).

## Keyboard (player)

| Key | |
|---|---|
| Space / K | play or pause |
| ← / → | back / forward 5 seconds (10 in Settings) |
| J / L | back / forward 10 seconds |
| , / . | one frame back / forward |
| [ / ] / \ | slower / faster (0.05 steps) / normal speed |
| ↑ / ↓, mouse wheel | volume |
| M | mute |
| C | captions |
| F, double-click | fullscreen |
| P | picture-in-picture |
| N | next episode |
| B | bookmark (or undo a scene jump) |
| X | clip tool (I / O set in and out, Enter adds a slice) |
| A | Ask Atlas: pauses and asks about the picture |
| F8 (held) | talk to Marquee (when voice is on) |
| Hold the mouse on the picture | 2x while held |
| Esc / Backspace | close a menu / back to browsing |

## Optional downloads

Nothing heavy is in the program itself; these are fetched from pinned sources and checked against SHA-256
hashes, only when you turn them on in Settings:

| What | Size |
|---|---|
| Built-in AI: llama.cpp (Vulkan) + Qwen3-VL 4B, or 2B for weaker PCs (Apache-2.0) | about 3 GB (1.6 GB small) |
| Media index (scene search, chapters, intro/credits detection): ONNX Runtime + CLIP ViT-B/32 | about 340 MB |
| Voice: faster-whisper speech-to-text (+ NVIDIA CUDA runtime for GPU speed) | 72 MB + 141 MB model (+ 1.37 GB) |

The built-in AI only runs while it's needed, on 127.0.0.1, unloads after 5 idle minutes and never starts while a
video plays unless you ask it something. **Classic** mode (Settings > AI > Off) uses no AI at all.

## Sharing

**Share** uploads a film, episode or clip to your own Google Drive and gives you a private link: only people who
have it can open it, it isn't searchable, and you can make it expire, stop sharing or delete it from Marquee.
It needs a free Google OAuth client of your own (about 5 minutes, once): see
[docs/google-drive.md](docs/google-drive.md). Marquee only gets access to the files it uploads.

You are responsible for having the rights to anything you share. Google may limit or remove files it considers
copyrighted, and may temporarily block downloads of a file that many people open ("download quota exceeded").

## Run from source

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt pyinstaller
powershell -ExecutionPolicy Bypass -File tools\get_mpv.ps1   # libmpv into vendor\mpv
.venv\Scripts\python -m marquee
```

`build.ps1` makes `dist\Marquee\Marquee.exe` and `dist\Marquee-windows-x64.zip`. `tools\release.ps1 -Version x.y.z`
builds and publishes a release with its update manifest (see [docs/updates.md](docs/updates.md); try `-DryRun`
first). `requirements-ai.txt` installs the optional AI packages into the venv for development.

### How it's put together

| | |
|---|---|
| `marquee/app.py` | the window (pywebview + WebView2); starts everything |
| `marquee/server.py`, `details.py` | local web server (127.0.0.1 only): the UI and its JSON API; tokens never reach the page |
| `marquee/catalog.py` | the local copy of the library (SQLite) and image cache, so browsing and playback never wait on Jellyfin |
| `marquee/player.py`, `osc/` | mpv inside the window and its controls, drawn by mpv itself so they sit on the video |
| `marquee/jellyfin.py`, `seerr.py`, `live.py`, `arrivals.py` | the servers, live library updates, downloads that are ready before Jellyfin has them |
| `marquee/ai.py`, `localllm.py`, `scripted.py` | AI providers (Atlas, your key, built-in) and Classic mode's prepared lines |
| `marquee/assistant/` | voice and Ask Atlas: speech-to-text, vision, scene jumps, end-of-title comments |
| `marquee/index/` | the media index: scene search, chapters, intro/credits, sensitive-content ranges |
| `marquee/taste.py`, `chat.py`, `letterboxd.py` | the taste profile, Ask Marquee, Letterboxd import |
| `marquee/storage.py`, `smart.py` | request quality and size estimates, storage limit, clean-up, smart season requests |
| `marquee/mobile/`, `remote.py` | Marquee Mobile: phone browsing, streaming (direct, remux or NVENC transcode) and remote |
| `marquee/stack.py`, `share.py`, `updater.py` | Your stack (the Media Hub), Google Drive sharing, self-updates |
| `marquee/web/` | the browsing UI |

## Licence

GPL-3.0-or-later (Marquee ships libmpv, which is GPL). See [LICENSE](LICENSE).
