"""Marquee Mobile: the phone web app (browse and stream from this PC, or remote-control the PC player).

Parts:
  net     where the phone server listens (Tailscale address by default), `tailscale serve` HTTPS, firewall check
  auth    paired devices (one cookie each, revocable), one-time pairing tokens, Jellyfin sign-in for friends
  users   watch state for friends who signed in with their own Jellyfin account
  media   ffprobe, keyframe tables (Matroska cues / MP4 sync samples), fMP4 box helpers, subtitles
  stream  direct play, remux and transcode sessions (ffmpeg -> fMP4 HLS), downloads for offline
  api     the /r/api/* routes the phone page uses

The web server itself (waitress, the pairing guard, the pages) lives in marquee.remote.
"""
