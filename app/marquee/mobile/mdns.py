"""Lets TVs on the home network find Atlas by themselves: an mDNS (Bonjour) `_atlas._tcp` service for the phone
server, announced only while it listens on the home network ("Home network + Tailscale"). The Android TV app looks
for it on its setup screen; the TXT record says where the TV page is.
"""
import os
import socket
import threading

from .. import __version__

TYPE = "_atlas._tcp.local."
_lock = threading.Lock()
_zc = None
_info = None
_key = None


def _name():
    pc = (os.environ.get("COMPUTERNAME") or socket.gethostname() or "PC").split(".")[0]
    return f"Atlas on {pc.title()}"[:60]


def announce(ip, port):
    """Announce (or move) the service to ip:port; ip None stops it. Runs in the background (it takes a moment)."""
    threading.Thread(target=_apply, args=(ip, port), daemon=True, name="marquee-mdns").start()


def _apply(ip, port):
    global _zc, _info, _key
    with _lock:
        key = (ip, port) if ip else None
        if key == _key:
            return
        _stop_locked()
        if not key:
            return
        try:
            from zeroconf import ServiceInfo, Zeroconf
            name = _name()
            info = ServiceInfo(TYPE, f"{name}.{TYPE}", addresses=[socket.inet_aton(ip)], port=int(port),
                               properties={"path": "/r/tv", "version": __version__, "name": name},
                               server=f"atlas-{socket.gethostname().split('.')[0].lower()}.local.")
            zc = Zeroconf(interfaces=[ip])
            zc.register_service(info, allow_name_change=True)
            _zc, _info, _key = zc, info, key
            print(f"Atlas Mobile: announced to TVs on {ip}:{port}", flush=True)
        except Exception as e:  # no multicast on this network, zeroconf missing...: TVs can still type the address
            print("Atlas Mobile: couldn't announce on the home network:", type(e).__name__, e, flush=True)


def _stop_locked():
    global _zc, _info, _key
    zc, info, _zc, _info, _key = _zc, _info, None, None, None
    if zc is not None:
        try:
            if info is not None:
                zc.unregister_service(info)
            zc.close()
        except Exception:
            pass


def stop():
    with _lock:
        _stop_locked()
