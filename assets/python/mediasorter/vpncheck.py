"""VPN leak check: proves qBittorrent's traffic leaves through the VPN. Part of Media Stack Setup.

Compares the internet address qBittorrent's peers and trackers see (qBittorrent reports it) with this
server's own address outside the VPN, and looks up where each one is. If they match, torrent traffic is
leaking past the VPN.
  * Windows (VPN app): "home" is a request sent the normal way and "VPN" one sent out through the VPN
    adapter. When the VPN app sends everything through the VPN (no split tunnelling) the two are the same,
    so the home address seen last time is used, and qBittorrent being locked to the VPN adapter is checked.
  * Linux (Gluetun): qBittorrent shares the VPN container's network, so "home" is simply what the Media Hub
    container sees.
Used by the health check (warns on a leak) and the Media Hub (Overview chip, alert on a leak). Results are
cached for 10 minutes in vpncheck.json in the hub's state folder.
"""
import http.client
import json
import ssl
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import stackconf  # noqa: E402

MAX_AGE = 600


def ip_via(source=None):
    """Our public IPv4, optionally sent from a local address (i.e. out through that adapter)."""
    conn = http.client.HTTPSConnection("api.ipify.org", timeout=10, context=ssl.create_default_context(),
                                       source_address=(source, 0) if source else None)
    try:
        conn.request("GET", "/?format=json")
        return json.loads(conn.getresponse().read())["ip"]
    finally:
        conn.close()


def where(ip, known):
    if not ip:
        return None
    if ip in known:
        return known[ip]
    try:
        d = requests.get(f"https://ipinfo.io/{ip}/json", timeout=10).json()
        info = {"ip": ip, "country": d.get("country"), "city": d.get("city"), "org": d.get("org")}
    except Exception:
        info = {"ip": ip}
    known[ip] = info
    return info


def adapter_address(name):
    import psutil
    for a in psutil.net_if_addrs().get(name or "", []):
        if a.family.name == "AF_INET":
            return a.address
    return None


def check(cfg=None, force=False):
    cfg = cfg or stackconf.load()
    cache = stackconf.own_state(cfg, "hub_state_dir") / "vpncheck.json"
    try:
        old = json.loads(cache.read_text(encoding="utf-8"))
    except Exception:
        old = {}
    if not force and old.get("time") and time.time() - old["time"] < MAX_AGE:
        return old
    vpn = cfg.get("vpn") or {}
    res = {"time": time.time(), "status": "unknown", "text": ""}
    if not stackconf.has(cfg, "qbittorrent") or vpn.get("mode") not in ("app", "gluetun"):
        res.update(status="none", text="no VPN is set up for qBittorrent")
        return res
    known = {i["ip"]: i for i in (old.get("torrent"), old.get("vpn"), old.get("home"), old.get("last_home"))
             if i and i.get("country")}
    try:
        torrent = stackconf.qbt_get(cfg, "/sync/maindata", params={"rid": 0})["server_state"].get("last_external_address_v4") or None
        bound = stackconf.qbt_get(cfg, "/app/preferences").get("current_interface_address") or None
    except Exception:
        torrent = bound = None
        res["text"] = "qBittorrent isn't answering"
    try:
        home = ip_via()
    except Exception:
        home = None
    if vpn["mode"] == "gluetun":
        vpn_src, vpn_ip, locked = True, None, True  # qBittorrent has no network but the VPN container's
        full_tunnel, real_home = False, home
    else:
        vpn_src = adapter_address(vpn.get("adapter"))
        try:
            vpn_ip = ip_via(vpn_src) if vpn_src else None
        except Exception:
            vpn_ip = None
        locked = bool(vpn_src and bound == vpn_src)
        full_tunnel = bool(home and vpn_ip and home == vpn_ip)
        real_home = (old.get("last_home") or {}).get("ip") if full_tunnel else home
    res.update(torrent=where(torrent, known), vpn=where(vpn_ip, known), home=where(real_home, known),
               full_tunnel=full_tunnel, last_home=where(real_home, known) or old.get("last_home"), locked=locked)
    if not vpn_src:
        res.update(status="off", text="the VPN is disconnected, so qBittorrent has no internet (the safety lock working)")
    elif not torrent:
        res.update(status="unknown", text=res["text"] or "qBittorrent hasn't learned its address from peers yet")
    elif real_home and torrent == real_home:
        res.update(status="leak", text=f"torrents are using your home address {torrent} - they are NOT going through the VPN")
    elif not locked:
        res.update(status="warn", text=f"qBittorrent isn't locked to the VPN adapter (bound to {bound or 'any'}): "
                                       "if the VPN drops, torrents could use your home connection")
    else:
        t = res["torrent"]
        place = ", ".join(x for x in (t.get("city"), t.get("country")) if x)
        res.update(status="ok", text=f"torrents appear from {torrent}" + (f" ({place})" if place else "") +
                   (", not your home address" if real_home else
                    "; everything on this computer goes through the VPN right now, and qBittorrent is locked to it"))
    cache.write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


if __name__ == "__main__":
    print(json.dumps(check(force=True), indent=1))
