"""The brain hosts its own bus.

With `[bus] kind = "nats"` and a URL that points at this machine, the core
starts `nats-server` as a child process so the desktop is the one thing a
worker needs to reach. No Pi, no separate service, nothing to remember to
start before Alfred.

    nats-server -js -sd ~/.alfred/nats -a 0.0.0.0 -p 4222

Binding 0.0.0.0 is what lets the laptop in; the desktop's own processes still
talk to it over 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import subprocess
from pathlib import Path

from alfred.bus.nats_bus import host_port, is_local_host, reachable

log = logging.getLogger("alfred.bus")

DATA_DIR = Path("~/.alfred/nats").expanduser()
SEARCH_DIRS = [
    Path("~/.local/bin").expanduser(),
    Path("~/.alfred/bin").expanduser(),
    Path("/usr/local/bin"),
]
INSTALL_HINT = (
    "nats-server not found. Install it with `./install.sh` (downloads the single "
    "binary into ~/.local/bin), or from https://github.com/nats-io/nats-server/releases"
)

_child: subprocess.Popen | None = None


def find_binary() -> str | None:
    found = shutil.which("nats-server")
    if found:
        return found
    for folder in SEARCH_DIRS:
        candidate = folder / "nats-server"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def lan_address() -> str:
    """The address other machines on the LAN should use for this one.

    A UDP socket "connected" to a public address never sends anything; the
    kernel just tells us which interface it would have used.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())


def join_url(url: str) -> str:
    """The bus URL as seen from another machine: loopback swapped for LAN IP."""
    host, port = host_port(url)
    if is_local_host(host):
        host = lan_address()
    return f"nats://{host}:{port}"


def should_host(cfg: dict) -> bool:
    bus = cfg.get("bus", {})
    if bus.get("kind") != "nats":
        return False
    serve = bus.get("serve")
    if serve is not None:
        return bool(serve)
    return is_local_host(host_port(bus.get("url", "nats://127.0.0.1:4222"))[0])


async def ensure_server(cfg: dict) -> str | None:
    """Make sure a bus is listening at cfg["bus"]["url"]; start one if this
    machine is supposed to host it. Returns the URL workers should use, or
    None when the bus is not ours to run."""
    if not should_host(cfg):
        return None
    url = cfg["bus"].get("url", "nats://127.0.0.1:4222")
    _, port = host_port(url)

    if await reachable(url, timeout=1.0) is None:
        log.info("bus already listening at %s", url)
        return join_url(url)

    binary = find_binary()
    if binary is None:
        raise RuntimeError(INSTALL_HINT)

    data_dir = Path(cfg["bus"].get("data_dir", str(DATA_DIR))).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "nats-server.log"
    global _child
    with open(log_path, "ab") as log_fh:   # the child keeps its own copy of the fd
        _child = subprocess.Popen(
            [binary, "-js", "-sd", str(data_dir), "-a", "0.0.0.0", "-p", str(port)],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    log.info("started %s (pid %d, log %s)", binary, _child.pid, log_path)

    for _ in range(50):
        if _child.poll() is not None:
            raise RuntimeError(
                f"nats-server exited with code {_child.returncode}; see {log_path}"
            )
        if await reachable(url, timeout=0.5) is None:
            return join_url(url)
        await asyncio.sleep(0.2)
    raise RuntimeError(f"nats-server started but {url} never answered; see {log_path}")


def stop_server() -> None:
    global _child
    if _child is not None and _child.poll() is None:
        _child.terminate()
        try:
            _child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _child.kill()
    _child = None
