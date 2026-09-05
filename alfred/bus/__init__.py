"""Bus implementations and the factory that picks one from config."""

from __future__ import annotations

import logging

from alfred.bus.base import Bus

log = logging.getLogger("alfred.bus")

AUTO = "auto"


def build_bus(cfg: dict) -> Bus:
    """The only place in the codebase that knows which transport is in use.

    kind = "local"  -> everything in one process, no server needed
    kind = "nats"   -> workers on other machines
    """
    kind = cfg.get("bus", {}).get("kind", "local")
    if kind == "local":
        from alfred.bus.local import SHARED
        return SHARED
    if kind == "nats":
        from alfred.bus.nats_bus import NatsBus
        return NatsBus(cfg["bus"].get("url", "nats://127.0.0.1:4222"))
    raise ValueError(f"unknown bus kind: {kind!r}")


async def resolve_url(cfg: dict) -> None:
    """Turn `url = "auto"` into a real URL by listening for the core's beacon.

    Waits as long as it takes: a worker booted before the desktop is the
    normal case, not an error.
    """
    if cfg.get("bus", {}).get("url") != AUTO:
        return
    from alfred.bus.discovery import discover

    log.info("looking for Alfred's bus on the LAN (the core broadcasts it)...")
    while True:
        url = await discover(timeout_s=20)
        if url:
            log.info("found the bus at %s", url)
            cfg["bus"]["url"] = url
            return
        log.info("still looking; is the core running on the same network?")


async def connect_core(cfg: dict) -> tuple[Bus, str | None]:
    """Bring up the bus for the brain: host nats-server if that is ours to do,
    connect, and return the URL other machines should join with (None on the
    local bus)."""
    from alfred.bus.hosting import ensure_server

    join = await ensure_server(cfg)
    bus = build_bus(cfg)
    await bus.connect()
    return bus, join


__all__ = ["AUTO", "Bus", "build_bus", "connect_core", "resolve_url"]
