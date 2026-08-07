"""What each capability costs and requires.

This exists so that assigning work to a new machine is a filter, not a guess.
When an unknown device enrolls, Alfred does not ask a 7B model "what should
this run?" and hope — he computes which capabilities the hardware can
actually support, and only then asks the model to choose among the eligible
ones. A model that cannot assign `cad.generate` to a box without FreeCAD
installed cannot make that mistake at all.

`weight` is a rough cost hint used to keep heavy work off weak machines even
when they technically qualify.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    binaries: list[str] = field(default_factory=list)   # must be on PATH
    python_pkgs: list[str] = field(default_factory=list)
    min_ram_gb: float = 1.0
    needs_model: bool = False    # needs an LLM, local or remote
    needs_gpu: bool = False
    weight: str = "light"        # light | medium | heavy


MANIFEST: dict[str, Capability] = {c.name: c for c in [
    Capability(
        "code.write", "Generate source code from a specification",
        min_ram_gb=2, needs_model=True, weight="medium",
    ),
    Capability(
        "code.test", "Run a test suite and report pass/fail",
        binaries=["python3"], min_ram_gb=2, weight="medium",
    ),
    Capability(
        "calc.engineering", "Write and execute a calculation script",
        binaries=["python3"], min_ram_gb=2, needs_model=True, weight="light",
    ),
    Capability(
        "research.web", "Search, fetch and distil web sources",
        python_pkgs=["httpx"], min_ram_gb=2, needs_model=True, weight="light",
    ),
    Capability(
        "research.document", "Read local PDFs and extract findings",
        python_pkgs=["httpx", "pypdf"], min_ram_gb=2, needs_model=True, weight="medium",
    ),
    Capability(
        "docs.write", "Turn task results into documentation",
        min_ram_gb=1, needs_model=True, weight="light",
    ),
    Capability(
        "hw.mqtt", "Publish commands to MQTT devices",
        python_pkgs=["paho"], min_ram_gb=0.5, weight="light",
    ),
    Capability(
        "hw.sensor", "Sample retained MQTT sensor topics",
        python_pkgs=["paho"], min_ram_gb=0.5, weight="light",
    ),
    Capability(
        "cad.measure", "Read mass properties from Onshape",
        min_ram_gb=0.5, weight="light",
    ),
    Capability(
        "cad.variables", "Read and set Onshape driving dimensions",
        min_ram_gb=0.5, weight="light",
    ),
    Capability(
        "cad.export", "Export STEP or STL from Onshape",
        min_ram_gb=0.5, weight="light",
    ),
    Capability(
        "cad.evaluate", "Run FeatureScript against an Onshape model",
        min_ram_gb=0.5, weight="light",
    ),
    Capability(
        "cad.generate", "Author new geometry in FreeCAD",
        binaries=["freecadcmd"], min_ram_gb=4, needs_model=True, weight="heavy",
    ),
    Capability(
        "cad.inspect", "Measure a local STEP/FCStd file",
        binaries=["freecadcmd"], min_ram_gb=4, weight="medium",
    ),
    Capability(
        "vision.describe", "Look at an image and describe or answer about it",
        min_ram_gb=6, needs_model=True, weight="medium",
    ),
    Capability(
        "media.video", "Watch a video via sampled frames plus audio transcript",
        binaries=["ffmpeg"], min_ram_gb=6, needs_model=True, weight="heavy",
    ),
    Capability(
        "media.inspect", "Identify a file and preview it safely without opening it",
        min_ram_gb=0.5, weight="light",
    ),
    Capability(
        "speech.transcribe", "Turn spoken audio into text (the household's ears)",
        python_pkgs=["faster_whisper"], min_ram_gb=1, weight="medium",
    ),
    Capability(
        "speech.synthesize", "Turn text into spoken audio (the household's mouth)",
        binaries=["piper"], min_ram_gb=0.5, weight="light",
    ),
    Capability(
        "os.observe", "Read-only look at this machine: disk, memory, services, "
        "packages, logs. Safe, immediate.",
        binaries=["systemctl"], min_ram_gb=0.5, weight="light",
    ),
    Capability(
        "os.apply", "Change this machine: install/remove packages, control "
        "services, desktop settings, write home files. ALWAYS requires the "
        "owner's approval before it runs.",
        binaries=["systemctl"], min_ram_gb=0.5, weight="light",
    ),
]}

WEIGHT_MIN_RAM = {"light": 0.5, "medium": 4.0, "heavy": 8.0}


def eligible_for(profile) -> list[str]:
    """Which capabilities this machine could actually perform.

    Hard filter only — no scoring, no preferences. A capability that fails
    here would produce a worker advertising something it cannot do, which
    shows up as tasks that queue forever with no error to explain them.
    """
    out: list[str] = []
    for cap in MANIFEST.values():
        if any(b not in profile.binaries for b in cap.binaries):
            continue
        if any(p not in profile.python_pkgs for p in cap.python_pkgs):
            continue
        if profile.ram_gb < max(cap.min_ram_gb, WEIGHT_MIN_RAM[cap.weight]):
            continue
        if cap.needs_gpu and profile.vram_gb <= 0:
            continue
        if cap.needs_model and not (profile.has_local_model or profile.can_reach_model):
            continue
        out.append(cap.name)
    return sorted(out)


def explain(capability: str) -> str:
    cap = MANIFEST.get(capability)
    if not cap:
        return f"{capability} (unknown)"
    needs = cap.binaries + cap.python_pkgs
    detail = f", needs {', '.join(needs)}" if needs else ""
    return f"{cap.name}: {cap.description} [{cap.weight}{detail}]"
