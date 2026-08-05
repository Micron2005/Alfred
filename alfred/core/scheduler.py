"""Worker selection. Desktop only.

Routing is by capability, then by live load. No rule anywhere in this file
mentions a machine by name, which is what makes "workers are not permanently
tied to one computer" a property of the system rather than a wish.
"""

from __future__ import annotations

import logging

from alfred.contracts import Task, WorkerAdvert

log = logging.getLogger("alfred.scheduler")

# Cost in arbitrary units. Tune by watching the dashboard, not by theory.
W_QUEUE = 30.0        # each task already in flight on that worker
W_CPU = 0.4           # per percent of CPU in use
W_BATTERY = 60.0      # flat penalty for running on battery at all
W_LOW_BATTERY = 200.0 # below the threshold, effectively disqualifying
LOW_BATTERY_PCT = 30.0


def eligible(task: Task, workers: list[WorkerAdvert]) -> list[WorkerAdvert]:
    """Hard filter. Constraints are not negotiable and are not scored."""
    return [w for w in workers if w.can(task.capability) and w.meets(task.requires)]


def score(worker: WorkerAdvert, task: Task) -> float:
    """Lower is better."""
    cost = W_QUEUE * worker.queue_depth + W_CPU * worker.cpu_percent
    if not worker.on_ac_power:
        cost += W_BATTERY
        if (worker.battery_percent or 100) < LOW_BATTERY_PCT:
            # Sending a 40-minute CAD job to an unplugged laptop at 14% is
            # how you lose both the work and the afternoon.
            cost += W_LOW_BATTERY
    if task.requires.get("min_vram_gb"):
        cost -= min(worker.vram_free_gb, 24) * 2  # reward the GPU that has room
    return cost


def choose(task: Task, workers: list[WorkerAdvert]) -> WorkerAdvert | None:
    candidates = eligible(task, workers)
    if not candidates:
        return None
    best = min(candidates, key=lambda w: score(w, task))
    log.debug("task %s -> %s (from %d candidates)", task.id, best.worker_id, len(candidates))
    return best


def should_run_locally(
    task: Task, workers: list[WorkerAdvert], local_id: str, waited_s: float, fallback_after_s: float
) -> bool:
    """The rule behind 'if the ASUS is unavailable, Alfred does it himself'.

    Not a special case in the request path — a scheduling outcome. If nobody
    qualified shows up before the deadline and the desktop's own worker can
    do the job, the desktop does the job.
    """
    remote = [w for w in eligible(task, workers) if w.worker_id != local_id]
    if remote:
        return False
    local = next((w for w in workers if w.worker_id == local_id), None)
    if local is None or not local.can(task.capability):
        return False
    return waited_s >= fallback_after_s
