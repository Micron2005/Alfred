"""Alfred inside WSL: a Linux brain living in a Windows house.

The owner runs Alfred under WSL (Ubuntu on Windows). Two things follow:

1. The Windows side is reachable: `powershell.exe` is on the PATH inside WSL
   and acts on the real desktop -- screenshots, later mouse and keyboard.
   Everything Windows-facing goes through `powershell()` here.

2. In WSL's default (NAT) networking the Linux VM has a private address
   (172.x) that no other machine on the LAN can reach, and UDP broadcasts
   never leave it. So the laptop cannot see the bus the desktop hosts -- the
   exact symptom the owner hit -- unless Windows forwards the port:

       netsh interface portproxy add v4tov4 listenport=4222 \
             connectaddress=<WSL ip> connectport=4222
       New-NetFirewallRule -DisplayName 'Alfred bus' -Direction Inbound \
             -Protocol TCP -LocalPort 4222 -Action Allow

   Both need administrator rights, and the WSL address changes on reboot,
   so `bridge()` (re)writes the rule through one UAC prompt and
   `bridge_status()` tells the panel whether the door is currently open.
   Mirrored networking (Windows 11, `.wslconfig` networkingMode=mirrored)
   needs none of this and is detected as such.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import platform
import socket

log = logging.getLogger("alfred.wsl")

FIREWALL_RULE = "Alfred bus"


def is_wsl() -> bool:
    return "microsoft" in platform.uname().release.lower()


def encode(script: str) -> str:
    """-EncodedCommand form: the script crosses the WSL/Windows argv boundary
    untouched, whatever quotes it contains."""
    return base64.b64encode(script.encode("utf-16-le")).decode()


async def powershell(script: str, timeout: float = 20) -> tuple[int, str]:
    """Run a PowerShell snippet on the Windows side; (exit code, stdout)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-EncodedCommand", encode(script),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return 127, f"powershell.exe not runnable: {exc}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "powershell timed out"
    return proc.returncode or 0, out.decode(errors="replace").strip()


def wsl_ip() -> str:
    """This machine's own outward address (172.x for a NAT'd WSL VM; the
    host's LAN IP when mirrored or not in WSL at all). A UDP socket
    "connected" to a public address never sends anything; the kernel just
    tells us which interface it would have used."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


async def windows_lan_ip() -> str | None:
    """The address the rest of the house sees Windows at. Adapters without a
    default gateway (the WSL/Hyper-V vEthernet ones) are skipped."""
    code, out = await powershell(
        "(Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and "
        "$_.NetAdapter.Status -eq 'Up' } | Select-Object -First 1)"
        ".IPv4Address.IPAddress"
    )
    ip = out.splitlines()[0].strip() if code == 0 and out else ""
    return ip or None


def parse_portproxy(text: str, port: int) -> str | None:
    """The connect address of the v4tov4 rule listening on `port`, if any.

        Listen on ipv4:             Connect to ipv4:
        Address         Port        Address         Port
        --------------- ----------  --------------- ----------
        0.0.0.0         4222        172.28.5.3      4222
    """
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[1] == str(port) and parts[0][0].isdigit():
            return parts[2]
    return None


async def bridge_status(port: int) -> dict:
    """Is Windows letting the LAN through to the bus in this VM?"""
    inner = wsl_ip()
    windows = await windows_lan_ip()
    if windows and windows == inner:
        return {"mode": "mirrored", "windows_ip": windows, "wsl_ip": inner,
                "ok": True, "auto_works": True,
                "detail": "mirrored networking: WSL shares the Windows address"}

    code, out = await powershell("netsh interface portproxy show v4tov4")
    proxied = parse_portproxy(out, port) if code == 0 else None
    code, out = await powershell(
        f"if (Get-NetFirewallRule -DisplayName '{FIREWALL_RULE}' "
        "-ErrorAction SilentlyContinue) { 'yes' } else { 'no' }"
    )
    firewall = code == 0 and out.strip().lower() == "yes"

    ok = proxied == inner and firewall
    if ok:
        detail = f"Windows forwards port {port} to WSL ({inner})"
    elif proxied and proxied != inner:
        detail = (f"Windows forwards port {port} to {proxied}, but WSL is now "
                  f"{inner} (it changes on reboot)")
    elif not proxied:
        detail = f"Windows is not forwarding port {port} to WSL"
    else:
        detail = f"the firewall rule '{FIREWALL_RULE}' is missing"
    return {"mode": "nat", "windows_ip": windows, "wsl_ip": inner,
            "proxied_to": proxied, "firewall": firewall,
            "ok": ok, "auto_works": False, "detail": detail}


async def bridge(port: int) -> tuple[bool, str]:
    """Open the door: (re)point the port forward at this VM and allow the
    port through the firewall. Windows shows one UAC prompt; the owner
    clicking No is a normal outcome, reported as such."""
    inner = wsl_ip()
    script = (
        f"netsh interface portproxy delete v4tov4 listenport={port} "
        "listenaddress=0.0.0.0 | Out-Null; "
        f"netsh interface portproxy add v4tov4 listenport={port} "
        f"listenaddress=0.0.0.0 connectport={port} connectaddress={inner} | Out-Null; "
        f"if (-not (Get-NetFirewallRule -DisplayName '{FIREWALL_RULE}' "
        "-ErrorAction SilentlyContinue)) { "
        f"New-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -Direction Inbound "
        f"-Protocol TCP -LocalPort {port} -Action Allow | Out-Null }}"
    )
    code, out = await powershell(
        "Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden "
        f"-ArgumentList '-NoProfile','-EncodedCommand','{encode(script)}'",
        timeout=120,
    )
    if code != 0:
        if "canceled" in out.lower() or "cancelled" in out.lower():
            return False, "Windows asked for permission and it was declined"
        return False, f"could not change Windows networking: {out[-200:]}"
    status = await bridge_status(port)
    return bool(status["ok"]), status["detail"]
