"""Atlas's settings: one JSON file in %APPDATA%\\Marquee. Secrets (Jellyfin token, Seerr session) are
encrypted with Windows DPAPI, so only this Windows account can read them."""
import base64
import ctypes
import ctypes.wintypes
import json
import os
import threading
from pathlib import Path

DIR = Path(os.environ.get("MARQUEE_HOME") or Path(os.environ.get("APPDATA", Path.home())) / "Marquee")
FILE = DIR / "settings.json"
_lock = threading.Lock()

DEFAULTS = {
    "server": "",            # Jellyfin address, e.g. http://localhost:8096
    "user_id": "",
    "user_name": "",
    "token": "",             # encrypted
    "seerr": "",             # Seerr address
    "seerr_cookie": "",      # encrypted
    "theme": "dark",         # dark (default) | light (Clear Home)
    "quality": "original",   # original | 2160 | 1080 | 720 | 480
    "upscaler": "auto",      # auto (anime: Anime4K, live action: NVIDIA RTX VSR) | nvidia | anime4k | hq | off
    "rtx_hdr": False,
    "stable_volume": True,
    "autoplay_next": True,
    "fullscreen_on_play": True,  # playing a video makes the window fullscreen (back to a window when it ends)
    "skip_intros": False,    # skip automatically (the Skip Intro button shows either way)
    "previews": True,
    "sub_lang": "eng",
    "audio_lang_anime": "jpn",
    "request_prefs": {},     # remembered Seerr request choices per kind (movie / tv / anime)
    "base_volume": 80,       # volume every video starts at
    "hwdec": True,           # hardware video decoding (GPU); off = software
    "passthrough": False,    # bitstream Dolby/DTS to a receiver or soundbar
    "audio_device": "auto",  # mpv audio-device name ("auto" = Windows default)
    "night_mode": False,     # dynamic-range compression: quiet parts up, loud parts down
    "dialogue_boost": False, # emphasise speech (centre channel)
    "seek_step": 5,          # seconds the arrow keys jump (5 or 10)
    "voice_focus": "off",    # off | dialogue (dialogue boost) | isolate (AI voice isolation, RNNoise)
    "sdr_to_hdr": "auto",    # auto (RTX Video HDR / inverse tone mapping when the display is in HDR) | off
    "credits_mini": True,    # when the credits roll: the video shrinks to a corner, "Up next" shows behind it
    "interpolation": False,  # smooth motion: display-resample + frame interpolation
    "pip_bounds": None,      # last mini-player place and size [x, y, w, h]
    "ai_provider": "auto",   # auto (own key, else Atlas, else built-in, else classic) | custom | atlas | builtin |
                             # classic (no AI at all: prepared lines). Old values: openai = custom, off = classic
    "ai_url": "",            # any OpenAI-compatible endpoint, e.g. https://api.openai.com/v1
    "ai_key": "",            # encrypted
    "ai_model": "",
    "ai_builtin_size": "standard",  # built-in AI model: standard (Qwen3-VL 4B) | small (Qwen3-VL 2B, weaker PCs)
    "ai_builtin_gpu": True,  # built-in AI on the graphics card (off: CPU only)
    "ai_while_watching": False,  # background AI while a video plays (off: only questions you ask)
    "ai_vram_gb": 0,  # most graphics memory the built-in AI may use, in GB (0: as much as it needs)
    "library_folders": [],   # folders of films/shows to play without Jellyfin (scanned into the local catalog)
    "remote": False,         # phone remote (LAN listener, paired by QR code)
    "remote_port": 8737,
    "mobile": False,         # Atlas Mobile: the phone app (watch on the phone over Tailscale, or remote control)
    "mobile_bind": "tailscale",  # where the phone server listens: tailscale (this PC's Tailscale address) | lan+tailscale
    "mobile_max_streams": 2, # phones converting video at the same time (remux/transcode; direct play doesn't count)
    "mobile_quality": "auto",  # default phone quality: auto | original | 1080 | 720 | 480
    "mobile_jellyfin_signin": False,  # friends may sign in on the phone app with their own Jellyfin account
    "mobile_https": False,   # HTTPS through `tailscale serve` (set by the Settings button, not by hand)
    "cast": True,            # casting to TVs (Chromecast / Google Cast, AirPlay): the Cast buttons and the picker
    "cast_quality": "auto",  # auto (the best the TV plays) | original (never convert for size) | 1080 | 720
    "cast_4k_hdr": True,     # send 4K and HDR to TVs that play them (off: at most 1080p, HDR tone-mapped)
    "cast_port": 8738,       # the port TVs pull the video from (this PC's home-network address only)
    "mylist_sync_favorites": True,  # My List <-> Jellyfin favourites, both ways (off: My List stays on this PC)
    "details_autoplay": True,  # the details page plays a silent preview of the title behind its artwork
    "gdrive_client_id": "",      # Share to Google Drive: the user's own OAuth "Desktop app" client (docs/google-drive.md)
    "gdrive_client_secret": "",  # encrypted (Google requires it for desktop clients; it isn't truly confidential)
    "gdrive_token": "",          # encrypted Google refresh token (scope drive.file)
    "share_speed_cap": 0,        # upload speed cap for sharing, Mbit/s (0 = no cap)
    "launchpad_done": False,     # the first-run Launchpad (taste questions) was answered or skipped
    "taste_learning": True,      # the taste profile learns from watch history (off: only what you tell it)
    "storage_limit_gb": 0,       # media storage limit in GB (0 = no limit), measured over the library folders
    "storage_enforce": "warn",   # over the limit: warn | block (refuse new requests) | auto (free space by the cleanup rules)
    "shrink_4k_watched": False,  # re-encode a 4K film to 1080p once you've watched it (marquee.shrink)
    "delete_after_watch": False, # remove titles once everyone has watched them (after delete_after_watch_days)
    "delete_after_watch_days": 3,
    "delete_whole_seasons": False,  # TV: remove a season only once it's fully watched (off: watched episodes one by one)
    "delete_unwatched_days": 0,  # remove titles nobody has started N days after they were added (0 = off)
    "delete_torrents": False,    # also remove the seeding torrent (qBittorrent) once no library file uses it
    "keep_ids": [],              # titles marked "Keep": never removed by the cleanup rules
    "radarr_url": "",            # Radarr/Sonarr for storage and cleanup (else asked of Seerr when signed in as admin)
    "radarr_key": "",            # encrypted
    "sonarr_url": "",
    "sonarr_key": "",            # encrypted
    "sdh_fetch": True,           # get hearing-impaired (SDH) English subtitles from Bazarr for scene search (search only)
    "bazarr_url": "",            # Bazarr for that (else mediastack.json's bazarr entry)
    "bazarr_key": "",            # encrypted
    "smart_request": True,       # shows requested "smart": the next season is requested as you watch
    "smart_request_at": 50,      # ...once this % of the season being watched is watched (30% for 4K / big seasons)
    "stack_config": "",          # <root>/config/shared/mediastack.json (written by Media Stack Setup): "Your stack"
    "hub_url": "",               # the Media Hub's address when it isn't found by itself (default http://127.0.0.1:8585)
    "hub_token": "",             # encrypted: the hub's API token, for a hub on another computer (GET /api/token there)
    "update_check": "auto",      # updates (marquee.updater): auto (download in the background, ask to restart) |
                                 # notify (only say a new version is out) | off
    "update_channel": "stable",  # stable | beta (also offers pre-releases)
    "update_repo": "phonehseng/atlas",    # GitHub owner/repo the releases come from (not settable from the page)
    "update_skip": "",           # a version the user chose to skip ("Skip this version")
    "local_api_token": "",       # encrypted: the local API's token for other programs on this PC (docs/local-api.md)
}
SECRET = ("token", "seerr_cookie", "ai_key", "gdrive_client_secret", "gdrive_token", "radarr_key", "sonarr_key",
          "hub_token", "bazarr_key", "local_api_token")


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    src = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _seal(text: str) -> str:
    return base64.b64encode(_dpapi(text.encode(), True)).decode() if text else ""


def _open(blob: str) -> str:
    try:
        return _dpapi(base64.b64decode(blob), False).decode() if blob else ""
    except Exception:
        return ""


def load() -> dict:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    s = {**DEFAULTS, **data}
    for k in SECRET:
        s[k] = _open(s.get(k, ""))
    return s


def save(changes: dict) -> dict:
    with _lock:
        s = load()
        s.update(changes)
        out = dict(s)
        for k in SECRET:
            out[k] = _seal(out.get(k, ""))
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=1), encoding="utf-8")
        tmp.replace(FILE)
        return s


def public(s: dict) -> dict:
    """Settings the web UI may see (no secrets)."""
    return {k: v for k, v in s.items() if k not in SECRET} | {"signed_in": bool(s.get("token")),
                                                               "seerr_signed_in": bool(s.get("seerr_cookie")),
                                                               "ai_key_set": bool(s.get("ai_key")),
                                                               "gdrive_client_secret_set": bool(s.get("gdrive_client_secret")),
                                                               "gdrive_signed_in": bool(s.get("gdrive_token")),
                                                               "radarr_key_set": bool(s.get("radarr_key")),
                                                               "bazarr_key_set": bool(s.get("bazarr_key")),
                                                               "sonarr_key_set": bool(s.get("sonarr_key"))}
