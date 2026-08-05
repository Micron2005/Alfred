"""Enrolling new machines. Desktop only.

The flow:

    1. A node boots with only a bus address, probes itself, announces.
    2. Alfred's supervisor loop notices an unassigned machine and files a
       notice. He mentions it on your next turn — he does not interrupt.
    3. You tell him what it is. He proposes a capability set.
    4. On confirmation the assignment is written durably, and the node picks
       it up within a heartbeat.

The important property: `propose()` never lets a model choose freely. The
eligible set is computed from the manifest against the probed hardware, and
the model only ranks within it. A 7B model cannot assign FreeCAD work to a
machine without FreeCAD, because that option is not on the table.
"""

from __future__ import annotations

import json
import logging

from alfred import llm
from alfred.capabilities import MANIFEST, eligible_for, explain
from alfred.contracts import Assignment, NodeProfile

log = logging.getLogger("alfred.enrollment")

PROPOSAL_SYSTEM = (
    "You allocate work to machines in a small home network. Return JSON only. "
    "Prefer giving a machine few capabilities it does well over many it does "
    "badly. A weak or intermittently available machine should get light, "
    "non-urgent work that nothing else waits on."
)


async def propose(
    profile: NodeProfile, hint: str, cfg: dict, taken: dict[str, list[str]] | None = None
) -> Assignment:
    """Suggest a name and a workload. Nothing is applied until confirmed."""
    eligible = eligible_for(profile)
    if not eligible:
        return Assignment(
            node_id=profile.node_id, name="", capabilities=[],
            note="nothing this machine can currently do; install FreeCAD, "
                 "httpx/pypdf, or paho-mqtt to give it a job",
        )

    existing = ""
    if taken:
        existing = "\nAlready covered elsewhere:\n" + "\n".join(
            f"  {name}: {', '.join(caps)}" for name, caps in taken.items()
        )

    prompt = f"""A new machine has joined the network.

Hardware: {profile.describe()}
Python {profile.python_version}, packages: {', '.join(profile.python_pkgs) or 'none'}
Can reach a language model: {profile.can_reach_model}

What the owner says about it: {hint or "(nothing yet)"}
{existing}

It is eligible for exactly these capabilities:
{chr(10).join('  ' + explain(c) for c in eligible)}

Return JSON:
  "name": short lowercase identifier, no spaces, e.g. "garage-pi"
  "capabilities": a subset of the eligible list above
  "concurrency": 1 to 4, how many tasks at once
  "claim_delay_s": 0 normally; 10 to 20 if this should be a fallback that
                   only takes work nobody else wanted
  "note": one sentence on why"""

    try:
        raw = await llm.complete_json(prompt, cfg, system=PROPOSAL_SYSTEM)
        if isinstance(raw, str):
            raw = json.loads(raw)          # some models double-encode
        if isinstance(raw, list):
            raw = {"capabilities": raw}    # some return only the list
        if not isinstance(raw, dict):
            raise ValueError(f"expected an object, got {type(raw).__name__}")
    except Exception as exc:
        log.warning("proposal failed (%s); offering everything eligible", exc)
        return Assignment(
            node_id=profile.node_id,
            name=profile.hostname.split(".")[0].lower(),
            capabilities=eligible,
            note="fallback proposal: everything this machine is capable of",
        )

    # Filter against eligibility again. The model's output is a suggestion,
    # not an authority — anything outside the eligible set is dropped silently
    # rather than assigned and left to fail on the node.
    chosen = [c for c in raw.get("capabilities", []) if c in eligible]
    return Assignment(
        node_id=profile.node_id,
        name=(raw.get("name") or profile.hostname.split(".")[0]).strip().lower(),
        capabilities=chosen or eligible,
        concurrency=max(1, min(4, int(raw.get("concurrency", 1) or 1))),
        claim_delay_s=max(0.0, min(60.0, float(raw.get("claim_delay_s", 0) or 0))),
        note=(raw.get("note") or "").strip(),
    )


def validate(profile: NodeProfile, capabilities: list[str]) -> tuple[list[str], list[str]]:
    """Split a requested capability list into what will work and what will not.

    Used by the manual path, where you name capabilities directly and are
    entitled to be told plainly that one of them is impossible here.
    """
    eligible = set(eligible_for(profile))
    good = [c for c in capabilities if c in eligible]
    bad = []
    for cap in capabilities:
        if cap in eligible:
            continue
        spec = MANIFEST.get(cap)
        if spec is None:
            bad.append(f"{cap} (no such capability)")
        else:
            missing = [b for b in spec.binaries if b not in profile.binaries]
            missing += [p for p in spec.python_pkgs if p not in profile.python_pkgs]
            reason = f"missing {', '.join(missing)}" if missing else (
                f"needs {spec.min_ram_gb}GB RAM, has {profile.ram_gb}GB"
                if profile.ram_gb < spec.min_ram_gb else "no reachable model"
            )
            bad.append(f"{cap} ({reason})")
    return good, bad
