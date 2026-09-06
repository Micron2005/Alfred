#!/usr/bin/env python3
"""Alfred core. DESKTOP ONLY. Never deploy this entry point to a worker node.

    python run_core.py --config configs/desktop.toml

With bus.kind = "local" this also starts an in-process worker, so the whole
system runs on one machine with no server and no network. That is phase 1,
and it stays useful forever as the fallback path.
"""

import argparse
import asyncio
import json
import logging

from alfred.bus import connect_core
from alfred.bus.discovery import beacon
from alfred.bus.hosting import stop_server
from alfred.bus.nats_bus import BusUnreachable
from alfred.capabilities import eligible_for
from alfred.contracts import Assignment
from alfred.config import load
from alfred.core.alfred import Alfred
from alfred.worker.runtime import WorkerRuntime


HELP = """/nodes                       machines seen, assigned and not
/probe <node_id>             full hardware profile
/adopt <node_id> [hint]      let Alfred propose a name and workload
/assign <node_id> <name> <caps>   set it yourself, comma separated
/memory                      what Alfred has learned about you
/forget <key>                make him forget one fact
/pending                     OS changes awaiting your approval
/approve <id> | /decline <id>
/help, /quit"""


async def handle_command(alfred, line: str) -> str:
    """Deterministic control path.

    Enrollment changes what the network will do, so it does not depend on a
    7B model correctly parsing intent out of conversation. Alfred still
    proposes; these commands are how a proposal becomes real.
    """
    parts = line.split()
    cmd, rest = parts[0].lower(), parts[1:]

    if cmd in {"/help", "/?"}:
        return HELP

    if cmd == "/memory":
        facts = alfred.state.known_facts("owner")
        if not facts:
            return "  (Alfred knows nothing about you yet)"
        return "  What Alfred knows about you:\n" + "\n".join(
            f"    {f['key'].replace('_',' ')}: {f['value']}"
            + ("  (inferred)" if f["confidence"] == "inferred" else "")
            for f in facts)

    if cmd == "/forget" and rest:
        key = "_".join(rest)
        return "  forgotten" if alfred.state.forget(key) else f"  no such fact: {key}"

    if cmd == "/pending":
        rows = alfred.state.pending_actions()
        if not rows:
            return "  no changes awaiting approval"
        return "\n".join(f"  #{r['id']}: {r['description']}" for r in rows)

    if cmd == "/approve" and rest:
        return "  " + await alfred.approve_action(int(rest[0]))

    if cmd == "/decline" and rest:
        return "  " + alfred.decline_action(int(rest[0]))

    if cmd == "/nodes":
        lines = []
        live = await alfred.live_workers_by_node()
        seen = {p.node_id: p for p in await alfred.bus.seen_nodes()}
        offline = [r for r in alfred.state.all_nodes() if r["node_id"] not in seen]
        for node_id in list(seen) + [r["node_id"] for r in offline]:
            known = alfred.state.known_node(node_id)
            name = (known or {}).get("name") or ""
            caps = json.loads((known or {}).get("capabilities") or "[]")
            worker = live.get(node_id)
            profile = seen.get(node_id)
            if worker is not None:
                status = f"{worker.worker_id}: {', '.join(worker.capabilities)}"
                status += "  (this machine)" if node_id == alfred.local_node_id else "  (online)"
            elif profile is None:
                status = f"{name}: {', '.join(caps)}  (OFFLINE)"
            elif caps:
                status = f"{name}: {', '.join(caps)}  (assigned, worker not heartbeating)"
            else:
                status = "UNASSIGNED  -- /adopt or /assign to give it a job"
            about = profile.describe() if profile else (known or {}).get("hostname") or "?"
            lines.append(f"  [{node_id}] {about}\n      {status}")
        if not lines:
            return ("  no machines announcing" if alfred.cfg["bus"]["kind"] == "nats"
                    else "  no machines announcing (bus.kind is 'local': other "
                         "machines cannot join; set kind = \"nats\" in the config)")
        return "\n".join(lines)

    if cmd == "/probe" and rest:
        profile = next((p for p in await alfred.bus.seen_nodes() if p.node_id == rest[0]), None)
        if not profile:
            return f"  no machine announcing with id {rest[0]}"
        return (f"  {profile.describe()}\n"
                f"  python {profile.python_version}\n"
                f"  binaries: {', '.join(profile.binaries) or 'none'}\n"
                f"  packages: {', '.join(profile.python_pkgs) or 'none'}\n"
                f"  eligible: {', '.join(eligible_for(profile)) or 'nothing yet'}")

    if cmd == "/adopt" and rest:
        proposal = await alfred.propose_for(rest[0], " ".join(rest[1:]))
        if proposal is None:
            return f"  no machine announcing with id {rest[0]}"
        if not proposal.capabilities:
            return f"  {proposal.note}"
        print(f"\n  proposed name : {proposal.name}"
              f"\n  capabilities  : {', '.join(proposal.capabilities)}"
              f"\n  concurrency   : {proposal.concurrency}"
              f"\n  reason        : {proposal.note}")
        answer = (await asyncio.to_thread(input, "  accept? [y/N] ")).strip().lower()
        if answer != "y":
            return "  not enrolled"
        return "  " + await alfred.enroll(proposal)

    if cmd == "/assign" and len(rest) >= 3:
        node_id, name, caps = rest[0], rest[1], rest[2].split(",")
        return "  " + await alfred.enroll(
            Assignment(node_id=node_id, name=name,
                       capabilities=[c.strip() for c in caps])
        )

    return f"  unknown command. {HELP}"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/desktop.toml")
    ap.add_argument("--project", default=None, help="existing project id")
    ap.add_argument("--new-project", default=None, help="name for a new project")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)-18s %(levelname)-7s %(message)s"
    )

    cfg = load(args.config)
    try:
        bus, join_url = await connect_core(cfg)
    except (BusUnreachable, RuntimeError) as exc:
        print(f"Alfred cannot start: {exc}")
        return

    alfred = Alfred(bus, cfg)
    project_id = args.project
    if args.new_project:
        project_id = alfred.state.create_project(args.new_project)
        print(f"created project {project_id}")

    background = [asyncio.create_task(alfred.supervise()),
                  asyncio.create_task(alfred.sight.run())]
    if join_url:
        background.append(asyncio.create_task(beacon(join_url)))
        print(f"Bus is up at {join_url}. Other machines join with:\n"
              f"    python run_node.py --bus {join_url}\n"
              f"  or, on the same network, simply:  python run_node.py --bus auto")

    # The desktop always runs a worker of its own. This is what turns
    # "if the Zenbook is unavailable, do it myself" into a scheduling
    # outcome rather than a branch in the request path.
    if cfg["worker"]["capabilities"]:
        background.append(asyncio.create_task(WorkerRuntime(bus, cfg).run()))

    print("Alfred is up. /nodes to list machines, /help for commands.\n")
    try:
        while True:
            message = (await asyncio.to_thread(input, "> ")).strip()
            if message.lower() in {"quit", "exit"}:
                break
            if message.startswith("/"):
                print("\n" + await handle_command(alfred, message) + "\n")
                continue
            print()
            print(await alfred.converse(message, project_id))
            print()
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        for task in background:
            task.cancel()
        await bus.close()
        stop_server()


if __name__ == "__main__":
    asyncio.run(main())
