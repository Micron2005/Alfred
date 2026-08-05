#!/usr/bin/env python3
"""Worker node. THIS IS THE FILE THAT RUNS ON EVERY OTHER MACHINE.

    python run_worker.py --config configs/zenbook.toml
    python run_worker.py --config configs/macbook.toml
    python run_worker.py --config configs/pi.toml

Identical code on every box. The config decides which capabilities this
machine offers, and that is the only difference between them.
"""

import argparse
import asyncio
import logging

from alfred.bus import build_bus
from alfred.config import load
from alfred.worker.runtime import WorkerRuntime


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)-18s %(levelname)-7s %(message)s"
    )
    cfg = load(args.config)
    bus = build_bus(cfg)
    try:
        await WorkerRuntime(bus, cfg).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    asyncio.run(main())
