"""Handler registry.

A handler is one async function bound to one capability. Every machine ships
every handler; a machine simply never claims work for capabilities it did not
declare. That keeps deployment to a single `git pull` rather than five
divergent codebases you have to remember to update in lockstep.

Handler modules that need an optional dependency (paho-mqtt on the Pi,
pypdf on the MacBook) are imported defensively — a missing library removes
one capability instead of taking down the worker.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Awaitable, Callable

from alfred.contracts import Task, TaskResult

log = logging.getLogger("alfred.handlers")

Handler = Callable[[Task, dict], Awaitable[TaskResult]]
_REGISTRY: dict[str, Handler] = {}

HANDLER_MODULES = [
    "alfred.worker.handlers.code",
    "alfred.worker.handlers.research",
    "alfred.worker.handlers.docs",
    "alfred.worker.handlers.hardware",
    "alfred.worker.handlers.cad_onshape",
    "alfred.worker.handlers.cad_freecad",
]


def handler(capability: str) -> Callable[[Handler], Handler]:
    def decorate(fn: Handler) -> Handler:
        _REGISTRY[capability] = fn
        return fn
    return decorate


def _load_all() -> None:
    for module in HANDLER_MODULES:
        try:
            importlib.import_module(module)
        except ImportError as exc:
            log.info("skipping %s (%s)", module, exc)


def get_handler(capability: str) -> Handler | None:
    if not _REGISTRY:
        _load_all()
    return _REGISTRY.get(capability)


def registered_capabilities() -> set[str]:
    if not _REGISTRY:
        _load_all()
    return set(_REGISTRY)


def ok(task: Task, summary: str, **kw: Any) -> TaskResult:
    """Success. `summary` is the compressed thing Alfred actually reads —
    keep it under a few hundred words and put bulk output in artifacts."""
    return TaskResult(task_id=task.id, worker_id="", summary=summary, **kw)


def fail(task: Task, error: str) -> TaskResult:
    return TaskResult(task_id=task.id, worker_id="", status="error", error=error)
