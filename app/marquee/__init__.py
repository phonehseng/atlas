"""Marquee: a local, Netflix-style player for your Jellyfin library."""
import os
import subprocess

__version__ = "1.5.0"

if os.name == "nt":
    # Marquee is a windowed app: every helper it starts (ffmpeg, ffprobe, tailscale, powershell, llama-server...)
    # would otherwise flash a console window on the desktop. Default them all to no window, unless a caller asks for
    # a console of its own (CREATE_NEW_CONSOLE) or none at all (DETACHED_PROCESS).
    _CREATE_NO_WINDOW, _OWN_CONSOLE = 0x08000000, 0x00000010 | 0x00000008
    _popen_init = subprocess.Popen.__init__

    def _quiet_init(self, *args, **kwargs):
        flags = kwargs.get("creationflags") or 0
        if not flags & _OWN_CONSOLE:
            kwargs["creationflags"] = flags | _CREATE_NO_WINDOW
        _popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _quiet_init
