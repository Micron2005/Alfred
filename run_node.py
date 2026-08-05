#!/usr/bin/env python3
"""UNIVERSAL NODE. Drop this on any machine. No config, no capability list.

    python run_node.py --bus nats://alfredpi.local:4222

That is the entire setup. The machine probes itself, announces what it is,
and waits. Alfred will mention it to you on your next turn; you tell him what
it is; he names it and gives it a workload it can actually handle.

A machine already enrolled skips all of that and goes straight back to work —
the assignment is stored on Alfred's side and keyed to a stable node id, so
reboots, IP changes and month-long absences do not require re-enrolling.

If you would rather be explicit, --capabilities bypasses enrollment entirely:

    python run_node.py --bus nats://alfredpi.local:4222 \
        --name garage-pi --capabilities hw.mqtt,hw.sensor
"""

import argparse
import asyncio
import logging

from alfred.bus import build_bus
from alfred.config import load
from alfred.probe import probe
from alfred.worker.runtime import WorkerRuntime


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bus", default="nats://alfredpi.local:4222")
    ap.add_argument("--config", default=None, help="optional; overrides the flags")
    ap.add_argument("--name", default=None, help="skip enrollment, use this id")
    ap.add_argument("--capabilities", default="", help="skip enrollment, comma separated")
    ap.add_argument("--artifacts", default="/mnt/alfred",
                    help="shared artifact mount, same path on every machine")
    ap.add_argument("--model-host", default=None,
                    help="e.g. http://alfreddesktop.local:11434 if this box runs no model")
    ap.add_argument("--probe-only", action="store_true", help="print the profile and exit")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)-16s %(levelname)-7s %(message)s"
    )

    cfg = load(args.config) if args.config else load(None)
    cfg["bus"] = {"kind": "nats", "url": args.bus}
    cfg["core"]["artifact_dir"] = args.artifacts
    if args.model_host:
        cfg["core"]["ollama_url"] = args.model_host
    cfg["worker"]["capabilities"] = [
        c.strip() for c in args.capabilities.split(",") if c.strip()
    ]
    if args.name:
        cfg["worker"]["id"] = args.name

    if args.probe_only:
        profile = probe(cfg)
        print(profile.describe())
        print(f"  id        : {profile.node_id}")
        print(f"  binaries  : {', '.join(profile.binaries) or 'none'}")
        print(f"  packages  : {', '.join(profile.python_pkgs) or 'none'}")
        from alfred.capabilities import eligible_for
        print(f"  could run : {', '.join(eligible_for(profile)) or 'nothing yet'}")
        return

    try:
        await WorkerRuntime(build_bus(cfg), cfg).run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    asyncio.run(main())
