"""Bus implementations and the factory that picks one from config."""

from alfred.bus.base import Bus


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


__all__ = ["Bus", "build_bus"]
