"""Where the phone server listens, and the Tailscale helpers.

Bind modes (setting "mobile_bind"):
  "tailscale"      (default) this PC's Tailscale address only (100.64.0.0/10), plus 127.0.0.1 for `tailscale serve`
  "lan+tailscale"  also the Wi-Fi/Ethernet address (phones at home without Tailscale)
Never 0.0.0.0: binding named addresses keeps the server off public adapters (and off their firewall prompts).
When Tailscale isn't up yet the server listens on 127.0.0.1 only and checks again every 30 seconds.

HTTPS: `tailscale serve --bg --https=8443 http://127.0.0.1:<port>` gives https://<pc>.<tailnet>.ts.net:8443 with a real
certificate (needs MagicDNS and HTTPS certificates turned on for the tailnet). Needed for installing the PWA and for
some media features on iOS. Turned off with `tailscale serve --https=8443 off`.
"""
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

CGNAT = ipaddress.ip_network("100.64.0.0/10")
HTTPS_PORT = 8443
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_cache = {}


def _cached(key, seconds, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def forget():
    _cache.clear()


# ------------------------------------------------------------------ Tailscale
def tailscale_exe():
    for c in (shutil.which("tailscale"), r"C:\Program Files\Tailscale\tailscale.exe",
              r"C:\Program Files (x86)\Tailscale\tailscale.exe"):
        if c and os.path.isfile(c):
            return c
    return None


def _ts(*args, timeout=6):
    exe = tailscale_exe()
    if not exe:
        return None
    try:
        return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _iface_cgnat():
    """A 100.64.0.0/10 address on any interface (Tailscale's), without the CLI."""
    found = []
    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family == socket.AF_INET and ipaddress.ip_address(a.address) in CGNAT:
                    found.append(a.address)
    except Exception:
        try:
            found = [a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                     if ipaddress.ip_address(a[4][0]) in CGNAT]
        except OSError:
            pass
    return sorted(set(found))


def tailscale_ip():
    """This PC's Tailscale IPv4 address, or None while Tailscale is off."""
    def find():
        r = _ts("ip", "-4", timeout=4)
        if r is not None and r.returncode == 0:
            for line in r.stdout.split():
                try:
                    if ipaddress.ip_address(line.strip()) in CGNAT:
                        return line.strip()
                except ValueError:
                    continue
        ips = _iface_cgnat()
        return ips[0] if ips else None
    return _cached("ip", 20, find)


def tailscale_status():
    """{installed, running, ip, name (MagicDNS name), host, tailnet, https (status of our serve), https_url}."""
    def find():
        out = {"installed": bool(tailscale_exe()), "running": False, "ip": None, "name": None, "host": None}
        r = _ts("status", "--json")
        if r is not None and r.returncode == 0:
            try:
                d = json.loads(r.stdout)
                me = d.get("Self") or {}
                out["running"] = d.get("BackendState") == "Running"
                out["name"] = (me.get("DNSName") or "").rstrip(".") or None
                out["host"] = me.get("HostName")
                out["tailnet"] = d.get("MagicDNSSuffix")
                out["cert_domains"] = d.get("CertDomains") or []
            except ValueError:
                pass
        out["ip"] = tailscale_ip() if out["running"] or not out["installed"] else None
        if out["ip"] and not out["installed"]:
            out["running"] = True
        return out
    return _cached("status", 20, find)


def serve_status(local_port):
    """Is `tailscale serve` forwarding HTTPS_PORT to us? -> {on, url, other} (other: it serves something else there)."""
    st = tailscale_status()
    r = _ts("serve", "status", "--json")
    out = {"on": False, "url": None, "other": False, "available": bool(st.get("running"))}
    if r is None or r.returncode != 0:
        return out
    try:
        cfg = json.loads(r.stdout or "{}") or {}
    except ValueError:
        return out
    for hostport, web in (cfg.get("Web") or {}).items():
        if not hostport.endswith(f":{HTTPS_PORT}"):
            continue
        for _path, h in (web.get("Handlers") or {}).items():
            proxy = (h or {}).get("Proxy") or ""
            if re.search(rf"(127\.0\.0\.1|localhost):{local_port}\b", proxy):
                out["on"] = True
                out["url"] = f"https://{hostport.rsplit(':', 1)[0]}:{HTTPS_PORT}"
            else:
                out["other"] = True
    return out


def serve_command(local_port, on=True):
    exe = tailscale_exe() or "tailscale"
    if on:
        return [exe, "serve", "--bg", f"--https={HTTPS_PORT}", f"http://127.0.0.1:{local_port}"]
    return [exe, "serve", f"--https={HTTPS_PORT}", "off"]


def serve_set(local_port, on):
    """Turn the HTTPS front door on/off. Returns (ok, message)."""
    st = tailscale_status()
    if not st.get("installed"):
        return False, "Tailscale isn't installed on this PC"
    if not st.get("running"):
        return False, "Tailscale isn't running"
    cur = serve_status(local_port)
    if on and cur["other"]:
        return False, f"Tailscale already serves something else on port {HTTPS_PORT}"
    if not on and not cur["on"]:
        return True, "HTTPS is off"
    r = _ts(*serve_command(local_port, on)[1:], timeout=20)
    if r is None:
        return False, "Couldn't run tailscale"
    text = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        if "https" in text.lower() and ("enable" in text.lower() or "not enabled" in text.lower()):
            return False, ("HTTPS certificates aren't enabled for your tailnet yet: turn on MagicDNS and HTTPS in the "
                           "Tailscale admin console (DNS page), then try again")
        return False, text.splitlines()[-1] if text else "tailscale serve failed"
    forget()
    return True, "HTTPS is on" if on else "HTTPS is off"


# ------------------------------------------------------------------ LAN
def lan_ip():
    """This PC's address on the home network (a physical adapter's private address)."""
    def find():
        cands = []
        try:
            import psutil
            stats = psutil.net_if_stats()
            for name, addrs in psutil.net_if_addrs().items():
                low = name.lower()
                if not stats.get(name) or not stats[name].isup or any(
                        w in low for w in ("tailscale", "vethernet", "virtual", "vpn", "nordlynx", "wireguard",
                                           "loopback", "vmware", "hyper-v", "zerotier", "hamachi", "bluetooth")):
                    continue
                for a in addrs:
                    if a.family != socket.AF_INET:
                        continue
                    ip = ipaddress.ip_address(a.address)
                    if ip.is_private and not ip.is_link_local and not ip.is_loopback and ip not in CGNAT:
                        cands.append(a.address)
        except Exception:
            pass
        if not cands:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("10.255.255.255", 1))  # picks the outgoing interface; no packet is sent
                ip = s.getsockname()[0]
                if ipaddress.ip_address(ip).is_private and ipaddress.ip_address(ip) not in CGNAT:
                    cands.append(ip)
            except OSError:
                pass
            finally:
                s.close()
        rank = lambda a: (0 if a.startswith("192.168.") else 1 if a.startswith("10.") else 2)  # noqa: E731
        return sorted(set(cands), key=rank)[0] if cands else None
    return _cached("lan", 60, find)


def bind_hosts(mode):
    """The addresses to listen on for a bind mode. 127.0.0.1 is always there (tailscale serve proxies to it)."""
    hosts = ["127.0.0.1"]
    ts = tailscale_ip()
    if ts:
        hosts.append(ts)
    if mode in ("lan+tailscale", "lan"):
        lan = lan_ip()
        if lan and lan not in hosts:
            hosts.append(lan)
    return hosts


# ------------------------------------------------------------------ Windows Firewall
def program_path():
    """The executable that actually owns the listening socket (a venv's python.exe starts the base interpreter)."""
    try:
        import psutil
        return psutil.Process().exe()
    except Exception:
        return sys.executable


def firewall_status(port):
    """Does an inbound rule let other devices reach this program on Private networks (Tailscale's category)?
    -> {checked, allowed, blocked, program}. Read-only."""
    def find():
        prog = program_path()
        ps = ("$p = '" + prog.replace("'", "''") + "'; $out = @(); "
              "foreach ($f in Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | "
              "Where-Object { $_.Program -eq $p }) { $r = $f | Get-NetFirewallRule; "
              "if ($r.Enabled -eq 'True' -and $r.Direction -eq 'Inbound') { $out += ($r.Action.ToString() + '|' + "
              "$r.Profile.ToString()) } }; "
              f"foreach ($pf in Get-NetFirewallPortFilter -Protocol TCP -ErrorAction SilentlyContinue | "
              f"Where-Object {{ $_.LocalPort -eq '{int(port)}' }}) {{ $r = $pf | Get-NetFirewallRule; "
              "if ($r.Enabled -eq 'True' -and $r.Direction -eq 'Inbound') { $out += ($r.Action.ToString() + '|' + "
              "$r.Profile.ToString()) } }; $out -join ';'")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                               text=True, timeout=20, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return {"checked": False, "program": prog}
        allowed = blocked = False
        for entry in filter(None, (r.stdout or "").strip().split(";")):
            action, _, profile = entry.partition("|")
            if "Private" in profile or "Any" in profile:
                allowed |= action == "Allow"
                blocked |= action == "Block"
        return {"checked": True, "allowed": allowed and not blocked, "blocked": blocked, "program": prog}
    return _cached(f"fw{port}", 300, find)


def firewall_fix_command(port):
    """What "Allow through Windows Firewall" runs (elevated, after a UAC prompt): one rule, Private profile only."""
    return ["netsh", "advfirewall", "firewall", "add", "rule", "name=Marquee Mobile", "dir=in", "action=allow",
            "protocol=TCP", f"localport={int(port)}", "profile=private", f"program={program_path()}"]


def firewall_fix(port):
    """Ask Windows (UAC) to add the rule. Returns True when the prompt was shown."""
    import ctypes
    cmd = firewall_fix_command(port)
    params = " ".join(f'"{a}"' if " " in a else a for a in cmd[1:])
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", cmd[0], params, None, 0)
    forget()
    return rc > 32
