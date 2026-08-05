"""FreeCAD capabilities. Local, private, and able to author geometry.

The division of labour with the Onshape handler is not arbitrary:

    Onshape (cad.measure, cad.variables, cad.export)
        drives a model YOU built. Reads real mass properties back. Cannot
        create geometry in any way worth attempting.

    FreeCAD (cad.generate, cad.inspect)
        creates geometry from nothing. Mounting plates, spacers, brackets,
        shaft adapters — the parts of an arm build that are tedious rather
        than interesting.

Runs on the Zenbook and the desktop, not the Pi: the geometry kernel is
CPU-bound and a Pi will crawl.

Why this works where "ask a model to write CAD" usually does not: the model
never writes a whole script. It writes one function that returns a shape,
inside a harness that owns the imports, the export, and the validation. Then
the result is CHECKED — script runs, shape is valid, volume is non-zero,
at least one solid — and any failure goes back to the model as the actual
traceback. A 7B coder model succeeds at that framing and fails badly at
"write me a FreeCAD script".
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path

from alfred import llm
from alfred.contracts import Task, TaskResult
from alfred.worker.handlers import fail, handler, ok

log = logging.getLogger("alfred.freecad")

# The binary is named differently across distributions and versions.
CANDIDATES = ["freecadcmd", "FreeCADCmd", "freecad-daily-cmd"]


def _binary() -> str | None:
    return next((b for b in CANDIDATES if shutil.which(b)), None)


# The model fills in build(). Everything else is ours, so it cannot get the
# boilerplate wrong — and the report is machine-readable, so verification
# does not depend on parsing prose.
HARNESS = '''
import json, sys, traceback
import FreeCAD, Part

{body}

try:
    shape = build()
except Exception:
    print("ALFRED_ERROR:" + json.dumps({{"trace": traceback.format_exc()[-1500:]}}))
    sys.exit(1)

if not isinstance(shape, Part.Shape):
    if hasattr(shape, "Shape"):
        shape = shape.Shape
    else:
        print("ALFRED_ERROR:" + json.dumps({{"trace": "build() did not return a Part.Shape"}}))
        sys.exit(1)

bb = shape.BoundBox
report = {{
    "valid": bool(shape.isValid()),
    "volume_mm3": float(shape.Volume),
    "area_mm2": float(shape.Area),
    "solids": len(shape.Solids),
    "bbox_mm": [round(bb.XLength, 2), round(bb.YLength, 2), round(bb.ZLength, 2)],
}}
try:
    shape.exportStep(r"{step}")
    shape.exportStl(r"{stl}")
    report["exported"] = True
except Exception as exc:
    report["exported"] = False
    report["export_error"] = str(exc)

print("ALFRED_REPORT:" + json.dumps(report))
'''

GEOMETRY_PROMPT = """Write a single Python function for FreeCAD:

    def build():
        # ... construct and return one Part.Shape

Rules:
- `FreeCAD` and `Part` are already imported. Import nothing else except `math`.
- Return exactly one Part.Shape representing one solid.
- All units are millimetres.
- Build with Part primitives and booleans: Part.makeBox, makeCylinder,
  makeSphere, makeTorus, and .fuse(), .cut(), .common(). Use .translate()
  and .rotate() to position. Do not use the Sketcher or PartDesign APIs.
- Fillets via shape.makeFillet(radius, edges) only if you are confident which
  edges; otherwise leave sharp.

Part to build: {prompt}

Given parameters: {inputs}

Return only the function. No prose, no fences, no example usage."""


def _extract(output: str, marker: str) -> dict | None:
    match = re.search(rf"{marker}:(\{{.*\}})", output)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


@handler("cad.generate")
async def cad_generate(task: Task, cfg: dict) -> TaskResult:
    """Generate a part, run it, validate it, and retry against real errors."""
    binary = _binary()
    if binary is None:
        return fail(task, f"no FreeCAD binary found; looked for {', '.join(CANDIDATES)}")

    directory = Path(cfg["core"]["artifact_dir"]) / task.id
    directory.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_-]", "_", task.inputs.get("name", "part"))
    step, stl = directory / f"{name}.step", directory / f"{name}.stl"

    prompt = GEOMETRY_PROMPT.format(
        prompt=task.prompt,
        inputs=json.dumps(task.inputs.get("parameters", task.inputs)),
    )
    # Attempts are cheap — a local subprocess and a few seconds — and a small
    # coder model burns several of them on trivia like omitting build()
    # entirely. Being stingy here just means failing at a task that one more
    # round would have solved.
    budget = int(task.inputs.get("max_attempts", cfg["worker"].get("cad_attempts", 5)))
    attempts: list[str] = []

    for attempt in range(budget):
        body = await llm.complete(prompt, cfg, model=cfg["worker"].get("code_model"))
        body = re.sub(r"^```[a-z]*\n?|```$", "", body.strip(), flags=re.MULTILINE)
        if "def build(" not in body:
            attempts.append("no build() function in the output")
            prompt += "\n\nYour last answer did not define `def build():`. It must."
            continue

        script = directory / f"build_{attempt}.py"
        script.write_text(HARNESS.format(body=body, step=step, stl=stl))

        proc = await asyncio.create_subprocess_exec(
            binary, str(script),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        raw, _ = await proc.communicate()
        output = raw.decode(errors="replace")

        crash = _extract(output, "ALFRED_ERROR")
        report = _extract(output, "ALFRED_REPORT")

        if crash:
            problem = crash["trace"].strip().splitlines()[-1]
            attempts.append(problem)
            # The real traceback, not a paraphrase. Small models correct well
            # from the actual exception and not at all from "try again".
            prompt += f"\n\nYour last attempt raised:\n{crash['trace'][-800:]}\nFix it."
            continue

        if report is None:
            attempts.append(f"no report; FreeCAD said: {output.strip()[-300:]}")
            continue

        # Mechanical acceptance. No model is asked whether this looks right.
        problems = []
        if not report["valid"]:
            problems.append("shape failed FreeCAD's validity check")
        if report["volume_mm3"] <= 0:
            problems.append("shape has zero volume")
        if report["solids"] != 1:
            problems.append(f"expected 1 solid, produced {report['solids']}")

        if problems:
            attempts.append("; ".join(problems))
            prompt += (
                f"\n\nYour last attempt produced an invalid part: "
                f"{'; '.join(problems)}. Bounding box was {report['bbox_mm']} mm."
            )
            continue

        bx, by, bz = report["bbox_mm"]
        return ok(
            task,
            summary=(
                f"Built {name}: {bx} x {by} x {bz} mm, "
                f"{report['volume_mm3'] / 1000:.1f} cm^3, valid solid"
                + ("" if attempt == 0 else f" (took {attempt + 1} attempts)")
            ),
            artifacts=[step.as_uri(), stl.as_uri()],
            data={**report, "attempts": attempt + 1, "script": script.as_uri()},
        )

    return fail(task, f"no valid solid in {budget} attempts: " + " | ".join(attempts))


@handler("cad.inspect")
async def cad_inspect(task: Task, cfg: dict) -> TaskResult:
    """Measure an existing STEP, IGES, BREP or FCStd file locally.

    The private counterpart to cad.measure: same numbers, no cloud round trip,
    and it works on files a worker produced itself.
    """
    binary = _binary()
    if binary is None:
        return fail(task, f"no FreeCAD binary found; looked for {', '.join(CANDIDATES)}")

    path = task.inputs.get("path")
    if not path or not Path(path).exists():
        return fail(task, f"file not found: {path!r}")

    density = float(task.inputs.get("density_g_cm3", 0))
    script = Path(cfg["core"]["artifact_dir"]) / task.id / "inspect.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f'''
import json, Part
shape = Part.Shape()
shape.read(r"{path}")
bb = shape.BoundBox
c = shape.CenterOfMass
print("ALFRED_REPORT:" + json.dumps({{
    "valid": bool(shape.isValid()),
    "volume_mm3": float(shape.Volume),
    "area_mm2": float(shape.Area),
    "solids": len(shape.Solids),
    "bbox_mm": [round(bb.XLength, 2), round(bb.YLength, 2), round(bb.ZLength, 2)],
    "centroid_mm": [round(c.x, 2), round(c.y, 2), round(c.z, 2)],
}}))
''')

    proc = await asyncio.create_subprocess_exec(
        binary, str(script),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    raw, _ = await proc.communicate()
    report = _extract(raw.decode(errors="replace"), "ALFRED_REPORT")
    if report is None:
        return fail(task, f"FreeCAD could not read it: {raw.decode(errors='replace')[-300:]}")

    bx, by, bz = report["bbox_mm"]
    summary = f"{bx} x {by} x {bz} mm, {report['volume_mm3'] / 1000:.1f} cm^3"
    if density:
        mass_g = report["volume_mm3"] / 1000 * density
        report["mass_g"] = round(mass_g, 1)
        summary += f", {mass_g:.1f} g at {density} g/cm^3"
    if not report["valid"]:
        summary += " — WARNING: geometry fails validity check"

    return ok(task, summary=summary, data=report)
