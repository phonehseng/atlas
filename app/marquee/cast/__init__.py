"""Casting: play a title on a TV (Chromecast / Google Cast, AirPlay) while Atlas on this PC is the remote.

Parts:
  devices    finding TVs on the home network (mDNS: _googlecast._tcp, _airplay._tcp) and what each can play
  receivers  talking to a TV: Google Cast through pychromecast's Default Media Receiver, AirPlay through pyatv
  endpoint   the cast-only web server on this PC's home-network address that the TV pulls the video from
             (/c/<token>/..., a random token per cast that stops working when the cast ends)
  session    one cast: picks direct play / remux / transcode with Atlas Mobile's streaming engine
             (marquee.mobile.stream), resumes, saves progress, skips intros, plays the next episode
  firewall   is Windows Firewall letting the TV reach this PC, and the one-click rule that fixes it
  blueprint  /api/cast/* for the window (picker, casting bar, Settings) and the player's controls

AirPlay needs pyatv, which is large; the packaged app fetches it as pinned wheels the first time an AirPlay TV is
picked (marquee.wheels, group "airplay"). Google Cast is built in.
"""
