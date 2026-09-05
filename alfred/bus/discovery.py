"""Finding the brain on the LAN without typing its IP.

The core broadcasts a small UDP beacon every few seconds. A node started with
`--bus auto` listens for one and joins whatever bus it names. DHCP handing the
desktop a new address, or the laptop moving between Wi-Fi and Ethernet,
stops mattering.

Beacon payload: `ALFRED-BUS <url>`. The host part of the URL is the core's
LAN address; if a beacon ever carries a loopback host, the receiver uses the
sender's address instead.
"""

from __future__ import annotations

import asyncio
import logging
import socket

from alfred.bus.nats_bus import host_port, is_local_host

log = logging.getLogger("alfred.bus")

PORT = 42422
MAGIC = "ALFRED-BUS"
INTERVAL_S = 3.0


async def beacon(url: str, interval_s: float = INTERVAL_S) -> None:
    """Announce the bus URL to the local network until cancelled."""
    payload = f"{MAGIC} {url}".encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    try:
        while True:
            try:
                sock.sendto(payload, ("255.255.255.255", PORT))
            except OSError as exc:
                log.debug("beacon send failed: %s", exc)
            await asyncio.sleep(interval_s)
    finally:
        sock.close()


class _Listener(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.found: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        text = data.decode(errors="replace").strip()
        if not text.startswith(MAGIC) or self.found.done():
            return
        url = text[len(MAGIC):].strip()
        host, port = host_port(url)
        if is_local_host(host) and addr[0] not in {"127.0.0.1", "::1"}:
            url = f"nats://{addr[0]}:{port}"
        self.found.set_result(url)


async def discover(timeout_s: float | None = None) -> str | None:
    """Wait for a beacon; the bus URL it named, or None after timeout_s."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", PORT))
    transport, protocol = await loop.create_datagram_endpoint(_Listener, sock=sock)
    try:
        return await asyncio.wait_for(protocol.found, timeout_s)
    except asyncio.TimeoutError:
        return None
    finally:
        transport.close()
