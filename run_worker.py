#!/usr/bin/env python3
"""Worker node. THIS IS THE FILE THAT RUNS ON EVERY OTHER MACHINE.

    python run_worker.py --config configs/zenbook.toml
    python run_worker.py --config configs/macbook.toml
    python run_worker.py --config configs/pi.toml

Identical code on every box. The config decides which capabilities this
machine offers, and that is the only difference between them.

`[bus] url = "auto"` in the config (or `--bus auto` here) finds the desktop
by its LAN beacon instead of a typed-in address; `--bus nats://HOST:4222`
overrides whatever the file says.
"""

import argparse
import asyncio
import logging

from alfred.bus import build_bus, resolve_url
from alfred.config import load
from alfred.worker.runtime import WorkerRuntime


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--bus", default=None,
                    help="nats://HOST:4222 or 'auto'; overrides [bus] in the config")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)-18s %(levelname)-7s %(message)s"
    )
    cfg = load(args.config)
    if args.bus:
        cfg["bus"] = {"kind": "nats", "url": args.bus}
    if cfg["bus"]["kind"] != "nats":
        print("a worker on another machine needs [bus] kind = \"nats\" "
              "(kind = \"local\" never leaves this process)")
        return
    try:
        await resolve_url(cfg)
        await WorkerRuntime(build_bus(cfg), cfg).run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    asyncio.run(main())
