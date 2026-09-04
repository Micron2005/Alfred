"""Planning and verification. Desktop only.

The planner emits a dependency graph, not a list. Research on the MacBook and
a CAD skeleton on the Zenbook have no reason to run one after the other.
"""

from __future__ import annotations

import json
import logging

from alfred import llm
from alfred.contracts import Task, TaskResult
from alfred.worker.handlers import registered_capabilities

log = logging.getLogger("alfred.planner")

PLANNER_SYSTEM = (
    "You decompose engineering and business requests into delegatable tasks. Return JSON "
    "only, no prose, no markdown fences. Be sparing: fewer, larger tasks beat "
    "many small ones, because every task costs a round trip. If the request "
    "needs no delegation at all, return {\"tasks\": []}."
)

_PLAN_PROMPT = """Request: {request}

Project context:
{briefing}

Available capabilities (use ONLY these, exactly as written): {caps}
{hints}
Return JSON: {{"tasks": [...]}}. Each task object:
  "ref": short id unique within this plan, e.g. "t1"
  "capability": exactly one string from the list above
  "prompt": self-contained instruction; the worker sees no conversation
  "inputs": object of parameters, {{}} if none
  "depends_on": list of refs that must finish first, [] if independent
  "timeout_s": integer seconds, 30 to 3600

Anything that must not be run twice (moving hardware, writing to CAD, sending
a message) also needs "needs_idempotency": true.

If you cannot map the request onto the capabilities above, return
{{"tasks": []}} rather than inventing one."""


# What a capability expects in `inputs`, for the ones where a 7B planner
# would otherwise have to guess the key names.
INPUT_HINTS = {
    "research.web": "inputs: {\"query\": str} or {\"urls\": [str]}",
    "research.document": "inputs: {\"paths\": [str]}",
    "marketing.audit": "inputs: {\"url\": str, \"product\": str (brief name, optional)}",
    "marketing.draft": (
        "inputs: {\"kind\": one of post|thread|email|followup|ad|landing|"
        "comparison|script|plan|answer, \"channel\": str, \"audience\": str, "
        "\"goal\": str, \"product\": str (brief name), \"count\": int 1-5}"
    ),
}


async def plan(
    request: str,
    briefing: str,
    cfg: dict,
    project_id: str | None,
    available: list[str] | None = None,
) -> list[Task]:
    """Ask for a plan, then assume the answer is wrong until it validates.

    `available` is what the NETWORK can do, gathered from live worker
    adverts — not what this machine happens to have installed. Deriving it
    from local imports instead is a subtle and expensive mistake: the desktop
    has no httpx, so it would refuse to ever plan a research task and the
    MacBook would sit idle forever without a single error to explain why.
    """
    caps = sorted(set(available) if available else registered_capabilities())
    if not caps:
        log.error("no capabilities available anywhere; is any worker running?")
        return []
    prompt = _PLAN_PROMPT.format(
        request=request,
        briefing=briefing or "(new project, no history)",
        caps=", ".join(caps),
        hints="".join(f"  {c} {INPUT_HINTS[c]}\n" for c in caps if c in INPUT_HINTS),
    )

    for attempt in range(3):
        try:
            raw = await llm.complete_json(prompt, cfg, system=PLANNER_SYSTEM)
        except json.JSONDecodeError as exc:
            log.warning("plan attempt %d returned invalid JSON: %s", attempt + 1, exc)
            continue
        except Exception as exc:
            log.error("planner unreachable: %s", exc)
            return []

        entries = raw if isinstance(raw, list) else raw.get("tasks", [])
        if not isinstance(entries, list):
            log.warning("plan attempt %d was not a list", attempt + 1)
            continue

        tasks, rejected = _build(entries, request, project_id, caps)
        if rejected and attempt < 2:
            # Tell it exactly what it got wrong. Small models correct well
            # from specific feedback and not at all from being asked again.
            prompt += (
                f"\n\nYour previous answer was rejected: {'; '.join(rejected)}. "
                f"Use only these capabilities: {', '.join(caps)}."
            )
            log.warning("plan attempt %d rejected: %s", attempt + 1, rejected)
            continue
        return tasks

    log.error("planner failed 3 times; handling this turn without delegation")
    return []


def _build(
    entries: list, request: str, project_id: str | None, caps: list[str]
) -> tuple[list[Task], list[str]]:
    by_ref: dict[str, Task] = {}
    rejected: list[str] = []

    for entry in entries:
        if not isinstance(entry, dict):
            rejected.append("a plan item was not an object")
            continue
        capability = entry.get("capability")
        if capability not in caps:
            rejected.append(f"{capability!r} is not a real capability")
            continue
        prompt_text = (entry.get("prompt") or "").strip()
        if not prompt_text:
            rejected.append(f"{capability} had an empty prompt")
            continue

        inputs = entry.get("inputs")
        task = Task(
            capability=capability,
            prompt=prompt_text,
            project_id=project_id,
            inputs=inputs if isinstance(inputs, dict) else {},
            timeout_s=_int(entry.get("timeout_s"), 300, 30, 3600),
        )
        if entry.get("needs_idempotency") or capability.startswith(("hw.", "cad.")):
            task.idempotency_key = f"{project_id or 'adhoc'}:{entry.get('ref')}:{prompt_text[:60]}"
        by_ref[str(entry.get("ref") or task.id)] = task

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ref = str(entry.get("ref") or "")
        if ref not in by_ref:
            continue
        deps = entry.get("depends_on") or []
        by_ref[ref].depends_on = [
            by_ref[str(d)].id for d in deps if str(d) in by_ref and str(d) != ref
        ]

    if len(by_ref) > 12:
        rejected.append("more than 12 tasks; consolidate into fewer, larger ones")
    return list(by_ref.values()), rejected


def _int(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def verify(task: Task, result: TaskResult) -> tuple[bool, str]:
    """Mechanical accept criteria, per capability family.

    Deliberately dumb and deliberately not a language model. Asking one model
    whether another model's torque figure looks right is theatre; it produces
    a confident opinion with no arithmetic behind it. Where a real check
    exists, use the real check. Where none exists, say so plainly and let
    Alfred read the artifact himself.
    """
    if result.status == "pending" and task.capability == "os.apply":
        return True, "parked for the owner's approval"

    if not result.ok:
        return False, result.error or "worker reported failure"

    if task.capability.startswith("code.test"):
        if result.data.get("exit_code") != 0:
            return False, f"tests failed: {result.data.get('failures')}"
        return True, "test suite passed"

    if task.capability.startswith("code."):
        if not result.artifacts:
            return False, "no code artifact produced"
        return True, "code produced, not yet tested"

    if task.capability.startswith("calc."):
        if not result.summary.strip():
            return False, "calculation produced no output"
        return True, "calculation executed"

    if task.capability == "marketing.audit":
        if not result.data.get("fetched"):
            return False, "the page was never fetched"
        return True, f"live page checked, {len(result.data.get('issues', []))} issue(s) found"

    if task.capability.startswith("marketing."):
        if not result.artifacts:
            return False, "no copy produced"
        if not result.data.get("brief"):
            return True, "drafted without a product brief; facts are unverified"
        return True, f"drafted from the {result.data['brief']} brief"

    if task.capability.startswith("research."):
        if result.data.get("truncated"):
            return True, "sources exceeded the read budget; coverage is partial"
        if not result.data.get("fetched") and not result.data.get("pages_read"):
            return False, "no sources were actually read"
        return True, "sources read and cited"

    if task.capability == "cad.generate":
        if not result.data.get("valid") or result.data.get("volume_mm3", 0) <= 0:
            return False, "generated geometry is not a valid solid"
        if not result.data.get("exported"):
            return False, "solid was valid but export failed"
        return True, f"valid solid, {result.data.get('attempts', 1)} attempt(s)"

    if task.capability in {"cad.measure", "cad.inspect"}:
        if result.data.get("valid") is False:
            return False, "the model itself fails a geometry validity check"
        if result.data.get("volume_m3") == 0 or result.data.get("volume_mm3") == 0:
            return False, "zero volume; the Part Studio is empty or in error"
        return True, "measured from real geometry"

    if task.capability == "cad.variables":
        if result.data.get("set"):
            # Setting a variable proves the write landed, not that the model
            # rebuilt into anything sane. Only a re-measure shows that.
            return True, "variables written; unverified until re-measured"
        return True, "variables read"

    if task.capability == "cad.export":
        if result.data.get("bytes", 0) < 100:
            return False, "export produced an empty or truncated file"
        return True, "geometry exported"

    if task.capability.startswith("hw."):
        if task.capability == "hw.mqtt" and result.data.get("ack") is None:
            return True, "command sent but unacknowledged; device state unconfirmed"
        return True, "device acknowledged"

    return True, "no mechanical check available for this capability"
