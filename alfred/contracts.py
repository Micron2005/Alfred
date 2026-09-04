"""Shared vocabulary. Every machine imports this module and nothing disagrees.

If you change anything here, redeploy every node. This file is the protocol.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------
# Capabilities
#
# A capability is what a worker can DO, never which machine it is. Workers
# advertise a list of these; tasks require exactly one. Namespaced with dots
# so a worker can subscribe to a whole family with a prefix match.
# --------------------------------------------------------------------------

CAP_CODE_WRITE = "code.write"
CAP_CODE_TEST = "code.test"
CAP_CAD = "cad.model"
CAP_CALC = "calc.engineering"
CAP_RESEARCH_WEB = "research.web"
CAP_RESEARCH_DOC = "research.document"
CAP_DOCS_WRITE = "docs.write"
CAP_MARKETING_AUDIT = "marketing.audit"
CAP_MARKETING_DRAFT = "marketing.draft"
CAP_HW_MQTT = "hw.mqtt"
CAP_HW_SENSOR = "hw.sensor"


@dataclass
class Task:
    """One unit of delegated work.

    `capability` is the routing key. `requires` holds hard constraints the
    scheduler filters on before scoring. `depends_on` makes the plan a DAG
    rather than a list, so independent work runs in parallel.
    """

    capability: str
    prompt: str
    id: str = field(default_factory=lambda: _new_id("task"))
    project_id: str | None = None
    parent_id: str | None = None
    depends_on: list[str] = field(default_factory=list)

    # Inputs stay small. Big things travel as artifact URIs, never inline.
    inputs: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)

    # Hard constraints. The scheduler drops any worker that fails these.
    requires: dict[str, Any] = field(default_factory=dict)

    timeout_s: int = 300
    max_retries: int = 2
    attempt: int = 0

    # Two runs with the same key must not cause the effect twice. Matters
    # enormously for hw.* tasks, where a retry moves real hardware again.
    idempotency_key: str | None = None

    created_at: float = field(default_factory=_now)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: str | bytes) -> "Task":
        return Task(**json.loads(raw))


@dataclass
class TaskResult:
    """What comes back. `summary` is the only field Alfred is guaranteed to
    read, so a worker's real job is to make it short and true.

    Bulk output belongs in `artifacts` as URIs. If a worker returns 6000
    words in `summary`, it has defeated the point of being a worker.
    """

    task_id: str
    worker_id: str
    status: str = "ok"  # ok | error | rejected
    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0
    finished_at: float = field(default_factory=_now)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: str | bytes) -> "TaskResult":
        return TaskResult(**json.loads(raw))


@dataclass
class WorkerAdvert:
    """Heartbeat payload. The scheduler scores workers from these.

    Sent every few seconds. Three missed heartbeats and the supervisor loop
    reclaims whatever this worker was holding.
    """

    worker_id: str
    host: str
    capabilities: list[str]
    queue_depth: int = 0
    cpu_percent: float = 0.0
    ram_free_gb: float = 0.0
    vram_free_gb: float = 0.0
    battery_percent: float | None = None
    on_ac_power: bool = True
    software: list[str] = field(default_factory=list)
    sent_at: float = field(default_factory=_now)

    def can(self, capability: str) -> bool:
        """Exact match, or prefix match if the worker advertised a family."""
        return any(
            capability == c or (c.endswith(".*") and capability.startswith(c[:-1]))
            for c in self.capabilities
        )

    def meets(self, requires: dict[str, Any]) -> bool:
        if requires.get("min_ram_gb", 0) > self.ram_free_gb:
            return False
        if requires.get("min_vram_gb", 0) > self.vram_free_gb:
            return False
        for pkg in requires.get("software", []):
            if pkg not in self.software:
                return False
        if requires.get("ac_power") and not self.on_ac_power:
            return False
        return True

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: str | bytes) -> "WorkerAdvert":
        return WorkerAdvert(**json.loads(raw))


@dataclass
class NodeProfile:
    """A machine describing itself. Sent before it has any name or any job.

    This is what makes a device droppable: run the node program with nothing
    but a bus address and it announces what it is. Alfred decides the rest.
    """

    node_id: str
    hostname: str
    os: str = ""
    arch: str = ""
    cpu_cores: int = 1
    ram_gb: float = 0.0
    vram_gb: float = 0.0
    gpu_name: str = ""
    binaries: list[str] = field(default_factory=list)
    python_pkgs: list[str] = field(default_factory=list)
    has_local_model: bool = False
    can_reach_model: bool = False
    python_version: str = ""
    seen_at: float = field(default_factory=_now)

    def describe(self) -> str:
        """One line a human can judge. Alfred reads this out when a machine
        appears, so it has to say what actually matters for the decision."""
        gpu = f", {self.gpu_name} {self.vram_gb}GB" if self.gpu_name else ", no GPU"
        notable = [b for b in ("freecadcmd", "ollama", "docker", "mosquitto_pub")
                   if b in self.binaries]
        return (
            f"{self.hostname} ({self.os} {self.arch}): {self.cpu_cores} cores, "
            f"{self.ram_gb}GB RAM{gpu}"
            + (f", has {', '.join(notable)}" if notable else "")
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: str | bytes) -> "NodeProfile":
        return NodeProfile(**json.loads(raw))


@dataclass
class Assignment:
    """Alfred's answer to an enrolling node: a name and a job.

    Persisted on Alfred's side, so a machine that reboots or moves networks
    picks its role back up without being re-enrolled.
    """

    node_id: str
    name: str
    capabilities: list[str] = field(default_factory=list)
    concurrency: int = 1
    claim_delay_s: float = 0.0
    note: str = ""
    assigned_at: float = field(default_factory=_now)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: str | bytes) -> "Assignment":
        return Assignment(**json.loads(raw))
