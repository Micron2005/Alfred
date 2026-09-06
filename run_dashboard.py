#!/usr/bin/env python3
"""Alfred's household status board — the cockpit beside the conversation.

Open a SECOND terminal and run:

    python3 run_dashboard.py

It reads the heartbeat the core writes each tick (~/.alfred/status.json) and
redraws a live view: Alfred's own vitals, every machine (online / offline /
working, with how long since last heard), and jobs in flight right now.
Purely a viewer — it changes nothing, so it is safe to open and close anytime.
"""

import json
import time
import pathlib
import sys

STATUS = pathlib.Path.home() / ".alfred" / "status.json"

R = "\033[0m"; DIM = "\033[2m"; B = "\033[1m"
GREEN = "\033[32m"; RED = "\033[31m"; YEL = "\033[33m"; CYAN = "\033[36m"
CLEAR = "\033[2J\033[H"


def dot(online, working):
    if working: return f"{CYAN}\u25cf working{R}"
    if online:  return f"{GREEN}\u25cf online{R}"
    return f"{RED}\u25cb offline{R}"


def draw(s):
    out = [CLEAR]
    out.append(f"{B}{CYAN}  ALFRED \u2014 HOUSEHOLD STATUS{R}")
    age = time.time() - s.get("ts", 0)
    fresh = f"{GREEN}live{R}" if age < 10 else f"{YEL}{int(age)}s old{R}"
    out.append(f"  {DIM}heartbeat: {fresh}{R}\n")

    a = s.get("alfred", {})
    out.append(f"  {B}Alfred{R}")
    out.append(f"    model     {a.get('model','?')}")
    out.append(f"    bus       {a.get('bus','?')}")
    out.append(f"    projects  {a.get('projects',0)}")
    pa = a.get("pending_actions", 0)
    pac = YEL if pa else DIM
    out.append(f"    pending   {pac}{pa} awaiting approval{R}\n")

    out.append(f"  {B}Machines{R}")
    nodes = s.get("nodes", [])
    if not nodes:
        out.append(f"    {DIM}none enrolled yet{R}")
    for n in nodes:
        last = n.get("last_seen_s")
        seen = f"{DIM}last heard {int(last)}s ago{R}" if last is not None and not n["online"] else ""
        caps = n.get("capabilities") or []
        more = "…" if len(caps) > 4 else ""
        capline = f"{DIM}{', '.join(caps[:4])}{more}{R}" if caps else f"{DIM}unassigned{R}"
        out.append(f"    {dot(n['online'], n.get('working'))}  {B}{n['name']}{R}  {seen}")
        out.append(f"        {capline}")
    out.append("")

    flight = s.get("in_flight", [])
    out.append(f"  {B}In flight{R}")
    if not flight:
        out.append(f"    {DIM}idle \u2014 no jobs running{R}")
    for t in flight:
        who = t.get("assigned_to") or "?"
        out.append(f"    {CYAN}\u2192{R} {t.get('capability','?')}  {DIM}on {who} ({t.get('status')}){R}")

    out.append(f"\n  {DIM}Ctrl+C to close this board. Alfred keeps running.{R}")
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()


def main():
    while True:
        try:
            if STATUS.exists():
                draw(json.loads(STATUS.read_text()))
            else:
                sys.stdout.write(CLEAR + f"{DIM}  waiting for Alfred to start\u2026{R}\n"
                                 f"  {DIM}(run him in another terminal: python3 run_core.py){R}\n")
                sys.stdout.flush()
        except Exception as e:
            sys.stdout.write(f"{DIM}  (reading\u2026 {e}){R}\n")
        try:
            time.sleep(2)
        except KeyboardInterrupt:
            sys.stdout.write("\n  board closed.\n"); return


if __name__ == "__main__":
    main()
