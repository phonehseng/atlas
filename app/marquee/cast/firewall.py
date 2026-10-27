"""Can a TV on the home network reach this PC? (Windows Firewall, read-only check + the one-click fix.)

Casting needs two things inbound from the home network: TCP to the cast port (the TV pulls the video) and UDP 5353
(mDNS answers, to find TVs). Windows sorts each network into Private or Public (or Domain) and applies rules per
category, so the check looks up the category of the adapter that holds the home-network address and evaluates the
inbound rules for this program (or any program) on that category: a matching Block wins, then an Allow, then the
profile's default (Block unless changed). Nothing here changes a rule; fix() asks Windows (UAC prompt) to add two
narrow rules: this program only, these ports only, the home network's category only, local subnet only.
"""
import json
import subprocess
import threading
import time

from ..mobile import net

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MDNS_PORT = 5353
RULE_NAME = "Marquee Casting"
_cache = {}
_busy = set()


def _ps(script, timeout=40):
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                           text=True, timeout=timeout, creationflags=NO_WINDOW)
        return r.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _query(port, ip):
    prog = net.program_path()
    q = lambda s: "'" + str(s).replace("'", "''") + "'"  # noqa: E731
    script = (
        f"$p = {q(prog)}; $ip = {q(ip or '')}; $port = '{int(port)}'; "
        "$alias = $null; if ($ip) { $alias = (Get-NetIPAddress -IPAddress $ip -ErrorAction SilentlyContinue | "
        "Select-Object -First 1).InterfaceAlias }; "
        "$cat = $null; if ($alias) { $cat = (Get-NetConnectionProfile -InterfaceAlias $alias -ErrorAction "
        "SilentlyContinue | Select-Object -First 1).NetworkCategory }; "
        "$pn = if ($null -ne $cat) { ($cat.ToString() -replace 'DomainAuthenticated','Domain') } else { 'Private' }; "
        "$prof = Get-NetFirewallProfile -Name $pn -ErrorAction SilentlyContinue; "
        "$rules = @{}; "
        "foreach ($f in Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | Where-Object { $_.Program "
        "-eq $p }) { $r = $f | Get-NetFirewallRule; $rules[$r.Name] = $r }; "
        "foreach ($f in Get-NetFirewallPortFilter -Protocol TCP -ErrorAction SilentlyContinue | Where-Object { "
        "@($_.LocalPort) -contains $port }) { $r = $f | Get-NetFirewallRule; $rules[$r.Name] = $r }; "
        "foreach ($f in Get-NetFirewallPortFilter -Protocol UDP -ErrorAction SilentlyContinue | Where-Object { "
        f"@($_.LocalPort) -contains '{MDNS_PORT}' }}) {{ $r = $f | Get-NetFirewallRule; $rules[$r.Name] = $r }}; "
        "$out = @(); foreach ($r in $rules.Values) { if ($r.Enabled.ToString() -ne 'True' -or "
        "$r.Direction.ToString() -ne 'Inbound') { continue }; $af = $r | Get-NetFirewallApplicationFilter; "
        "$pf = $r | Get-NetFirewallPortFilter; $out += [pscustomobject]@{ name = $r.DisplayName; action = "
        "$r.Action.ToString(); profile = $r.Profile.ToString(); program = [string]$af.Program; protocol = "
        "[string]$pf.Protocol; port = (@($pf.LocalPort) -join ',') } }; "
        "[pscustomobject]@{ alias = $alias; category = $pn; enabled = [string]$prof.Enabled; inbound = "
        "[string]$prof.DefaultInboundAction; rules = $out } | ConvertTo-Json -Depth 4 -Compress")
    raw = _ps(script).strip()
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def evaluate(data, port, program):
    """-> {checked, category, tcp, mdns (True: allowed), blocked_by (names of Block rules)}. Pure: testable."""
    cat = data.get("category") or "Private"
    rules = data.get("rules") or []
    if isinstance(rules, dict):
        rules = [rules]
    prog = (program or "").lower()

    def hits(proto, p):
        allow, block = [], []
        for r in rules:
            if (r.get("program") or "Any").lower() not in ("any", prog):
                continue
            if (r.get("protocol") or "Any") not in ("Any", proto):
                continue
            ports = [x.strip() for x in str(r.get("port") or "Any").split(",")]
            if "Any" not in ports and str(p) not in ports:
                continue
            prof = r.get("profile") or "Any"
            if "Any" not in prof and cat not in prof:
                continue
            (block if r.get("action") == "Block" else allow if r.get("action") == "Allow" else []).append(
                r.get("name") or "rule")
        return allow, block

    off = str(data.get("enabled")).lower() == "false"
    default_allow = str(data.get("inbound")).lower() == "allow"
    out = {"checked": True, "category": cat, "interface": data.get("alias"), "firewall_off": off}
    blocked_by = []
    for key, proto, p in (("tcp", "TCP", port), ("mdns", "UDP", MDNS_PORT)):
        allow, block = hits(proto, p)
        blocked_by += block
        out[key] = off or (not block and (bool(allow) or default_allow))
    out["blocked_by"] = sorted(set(blocked_by))
    out["allowed"] = out["tcp"] and out["mdns"]
    return out


def status(port, wait=False):
    """Cached (5 minutes). The first check runs in the background unless wait: {"checked": False, "pending": True}."""
    ip = net.lan_ip()
    key = (int(port), ip)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]

    def run():
        try:
            data = _query(port, ip)
            prog = net.program_path()
            res = evaluate(data, port, prog) if data else {"checked": False, "error": "Couldn't read the firewall"}
            res.update(program=prog, port=int(port), lan_ip=ip)
            _cache[key] = (time.time(), res)
        finally:
            _busy.discard(key)
    if wait:
        run()
        return _cache.get(key, (0, {"checked": False}))[1]
    if key not in _busy:
        _busy.add(key)
        threading.Thread(target=run, daemon=True, name="cast-firewall-check").start()
    return {"checked": False, "pending": True, "lan_ip": ip}


def forget():
    _cache.clear()


def fix_commands(port, category="Private"):
    """The two rules "Allow Marquee" adds (run elevated). category: the home network's (Private / Public / Domain)."""
    prog = net.program_path()
    prof = {"public": "public", "domain": "domain"}.get((category or "").lower(), "private")
    base = ["netsh", "advfirewall", "firewall", "add", "rule", "dir=in", "action=allow", f"profile={prof}",
            "remoteip=localsubnet", f"program={prog}"]
    return [base[:5] + [f"name={RULE_NAME}", "protocol=TCP", f"localport={int(port)}"] + base[5:],
            base[:5] + [f"name={RULE_NAME} (find TVs)", "protocol=UDP", f"localport={MDNS_PORT}"] + base[5:]]


def _quote(a):
    if "=" in a and " " in a:   # netsh wants name="Marquee Casting", program="C:\Program Files\..."
        k, _, v = a.partition("=")
        return f'{k}="{v}"'
    return f'"{a}"' if " " in a else a


def command_line(port, category="Private"):
    return " & ".join(" ".join(_quote(a) for a in cmd) for cmd in fix_commands(port, category))


def fix(port, category="Private"):
    """Ask Windows (UAC) to add the rules. Returns True when the prompt was shown."""
    import ctypes
    line = command_line(port, category)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", f"/c {line}", None, 0)
    forget()
    return rc > 32
