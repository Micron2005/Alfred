"""Onshape capabilities.

Onshape is the one CAD option that changes the architecture rather than just
the implementation. FreeCAD and OpenSCAD need the software installed on the
worker; Fusion can only be scripted from inside its own running GUI. Onshape
is a cloud REST API, so this "CAD worker" is an HTTPS client with no local
compute and no GPU. It can run on the Pi.

What it can honestly do:

    cad.measure    read real mass, volume, centroid, inertia from a Part Studio
    cad.variables  read and set driving dimensions in a Variable Studio
    cad.export     pull STEP or STL for downstream use
    cad.evaluate   run FeatureScript to measure anything the other three miss

What it cannot do: author a robotic arm from scratch. Onshape's REST API can
add features programmatically, but building real geometry that way is
punishing and fragile. Do not aim for it.

The loop that is actually worth having: you model the arm in Onshape with
its driving dimensions in a Variable Studio. Alfred sets a variable, reads
the resulting mass properties back, feeds those real numbers to
calc.engineering, and iterates. That beats a language model guessing at a
mass figure, which is what you get from every CAD integration that only
generates geometry and never measures it.

Auth: personal API keys, HTTP Basic (access key as user, secret as password).
Set ONSHAPE_ACCESS_KEY and ONSHAPE_SECRET_KEY in the worker's environment.
Never in the config file — configs go in git.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from alfred.contracts import Task, TaskResult
from alfred.worker.handlers import fail, handler, ok

BASE = "https://cad.onshape.com/api/v9"


def _auth_header() -> str:
    access = os.environ.get("ONSHAPE_ACCESS_KEY", "")
    secret = os.environ.get("ONSHAPE_SECRET_KEY", "")
    if not access or not secret:
        raise RuntimeError(
            "set ONSHAPE_ACCESS_KEY and ONSHAPE_SECRET_KEY in the worker's environment"
        )
    return "Basic " + base64.b64encode(f"{access}:{secret}".encode()).decode()


def _call(method: str, path: str, body: dict | None = None, raw: bool = False):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body else None,
        headers={
            "Authorization": _auth_header(),
            "Accept": "application/json;charset=UTF-8; qs=0.09",
            "Content-Type": "application/json;charset=UTF-8; qs=0.09",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = resp.read()
            return payload if raw else json.loads(payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode(errors="replace")
        raise RuntimeError(f"Onshape {exc.code} on {path}: {detail}") from exc


def _target(task: Task, cfg: dict) -> tuple[str, str, str, str] | None:
    """Resolve document/workspace/element, falling back to config defaults so
    Alfred need not repeat the ids on every task."""
    onshape = cfg.get("onshape", {})
    did = task.inputs.get("did") or onshape.get("document_id")
    wid = task.inputs.get("wid") or onshape.get("workspace_id")
    eid = task.inputs.get("eid") or onshape.get("part_studio_id")
    wvm = task.inputs.get("wvm", "w")
    return (did, wvm, wid, eid) if did and wid and eid else None


@handler("cad.measure")
async def cad_measure(task: Task, cfg: dict) -> TaskResult:
    """Real mass properties from the live model. Read-only, so retries are free."""
    target = _target(task, cfg)
    if not target:
        return fail(task, "need did/wid/eid in inputs or [onshape] defaults in config")
    did, wvm, wid, eid = target

    query = ""
    if task.inputs.get("configuration"):
        query = "?configuration=" + urllib.request.quote(task.inputs["configuration"])

    data = await asyncio.to_thread(
        _call, "GET", f"/partstudios/d/{did}/{wvm}/{wid}/e/{eid}/massproperties{query}"
    )
    bodies = data.get("bodies", {})
    combined = bodies.get("-all-") or next(iter(bodies.values()), None)
    if not combined:
        return fail(task, "Onshape returned no bodies; is the Part Studio empty or in error?")

    # Onshape returns [value, low, high] triples and SI units throughout.
    mass_kg = combined.get("mass", [0])[0]
    volume_m3 = combined.get("volume", [0])[0]
    centroid = combined.get("centroid", [0, 0, 0])[:3]
    inertia = combined.get("inertia", [])[:9]

    return ok(
        task,
        summary=(
            f"mass {mass_kg * 1000:.1f} g, volume {volume_m3 * 1e9:.0f} mm^3, "
            f"centroid ({', '.join(f'{c * 1000:.1f}' for c in centroid)}) mm"
        ),
        data={
            "mass_kg": mass_kg,
            "volume_m3": volume_m3,
            "centroid_m": centroid,
            "inertia": inertia,
            "has_mass_override": combined.get("hasMass", False),
        },
    )


@handler("cad.variables")
async def cad_variables(task: Task, cfg: dict) -> TaskResult:
    """Read or set driving dimensions.

    Writing only works against a Variable Studio. Variables defined by a
    feature inside a Part Studio must be edited through the features endpoint
    instead, which is far more fragile — so put anything Alfred should drive
    into a Variable Studio and point `variable_studio_id` at it.
    """
    onshape = cfg.get("onshape", {})
    did = task.inputs.get("did") or onshape.get("document_id")
    wid = task.inputs.get("wid") or onshape.get("workspace_id")
    eid = task.inputs.get("eid") or onshape.get("variable_studio_id")
    if not (did and wid and eid):
        return fail(task, "need did/wid and a variable_studio_id")

    updates = task.inputs.get("set") or {}
    if not updates:
        current = await asyncio.to_thread(
            _call, "GET", f"/variables/d/{did}/w/{wid}/e/{eid}/variables"
        )
        table = {}
        for studio in current if isinstance(current, list) else [current]:
            for var in studio.get("variables", []):
                table[var.get("name")] = var.get("expression")
        return ok(
            task,
            summary=", ".join(f"{k}={v}" for k, v in table.items()) or "no variables defined",
            data={"variables": table},
        )

    if not task.idempotency_key:
        return fail(task, "refusing to modify a model without an idempotency_key")

    body = [
        {"name": name, "type": "LENGTH", "expression": str(expr), "description": "set by Alfred"}
        for name, expr in updates.items()
    ]
    await asyncio.to_thread(
        _call, "POST", f"/variables/d/{did}/w/{wid}/e/{eid}/variables", body
    )
    return ok(
        task,
        summary=f"set {', '.join(f'{k}={v}' for k, v in updates.items())}; "
                "re-measure to see the effect",
        data={"set": updates},
    )


@handler("cad.export")
async def cad_export(task: Task, cfg: dict) -> TaskResult:
    """Pull geometry out as STL or STEP. Lands in the shared artifact dir."""
    target = _target(task, cfg)
    if not target:
        return fail(task, "need did/wid/eid in inputs or [onshape] defaults in config")
    did, wvm, wid, eid = target

    fmt = task.inputs.get("format", "stl").lower()
    if fmt not in {"stl", "step"}:
        return fail(task, f"format must be stl or step, got {fmt!r}")
    query = "?mode=binary&units=millimeter" if fmt == "stl" else ""

    payload = await asyncio.to_thread(
        _call, "GET", f"/partstudios/d/{did}/{wvm}/{wid}/e/{eid}/{fmt}{query}", None, True
    )
    directory = Path(cfg["core"]["artifact_dir"]) / task.id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task.inputs.get('name', 'part')}.{fmt}"
    path.write_bytes(payload)

    return ok(
        task,
        summary=f"exported {path.name}, {len(payload) / 1024:.0f} KB",
        artifacts=[path.as_uri()],
        data={"format": fmt, "bytes": len(payload)},
    )


@handler("cad.evaluate")
async def cad_evaluate(task: Task, cfg: dict) -> TaskResult:
    """Run FeatureScript against the model.

    The escape hatch for anything the measure endpoint will not give you —
    face areas, edge lengths, bounding boxes, feature queries. Read-only
    expressions only; this is not a route for authoring geometry.
    """
    target = _target(task, cfg)
    if not target:
        return fail(task, "need did/wid/eid in inputs or [onshape] defaults in config")
    did, wvm, wid, eid = target

    script = task.inputs.get("script")
    if not script:
        return fail(task, "cad.evaluate needs inputs.script (a FeatureScript function)")

    data = await asyncio.to_thread(
        _call, "POST",
        f"/partstudios/d/{did}/{wvm}/{wid}/e/{eid}/featurescript",
        {"script": script, "queries": task.inputs.get("queries", [])},
    )
    if data.get("notices"):
        errors = [n for n in data["notices"] if n.get("level") == "ERROR"]
        if errors:
            return fail(task, f"FeatureScript error: {errors[0].get('message')}")

    result = data.get("result")
    return ok(
        task,
        summary=json.dumps(result)[:1200] if result is not None else "script returned nothing",
        data={"result": result},
    )
