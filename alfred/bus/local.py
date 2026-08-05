"""In-process bus. No server, no network, no install.

Run Alfred and every handler inside one Python process on the desktop. This
is what you should build against first: get planning, verification and
synthesis working before a single packet leaves the machine.

It is also permanently useful. When the Zenbook is asleep, the desktop's own
worker claims the task from this same queue and Alfred never notices the
difference.
"""

from __future__ import annotations

import asyncio
import time

from alfred.bus.base import Bus
from alfred.contracts import Assignment, NodeProfile, Task, TaskResult, WorkerAdvert

HEARTBEAT_TTL_S = 15.0


class LocalBus(Bus):
    def __init__(self) -> None:
        self._pending: list[Task] = []
        self._results: dict[str, asyncio.Future[TaskResult]] = {}
        self._workers: dict[str, WorkerAdvert] = {}
        self._nodes: dict[str, NodeProfile] = {}
        self._assignments: dict[str, Assignment] = {}
        self._lock = asyncio.Lock()
        self._arrived = asyncio.Event()

    async def submit(self, task: Task) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            self._pending.append(task)
            self._results.setdefault(task.id, loop.create_future())
            self._arrived.set()

    async def await_result(self, task_id: str, timeout: float) -> TaskResult | None:
        loop = asyncio.get_running_loop()
        fut = self._results.setdefault(task_id, loop.create_future())
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            return None

    async def workers(self) -> list[WorkerAdvert]:
        cutoff = time.time() - HEARTBEAT_TTL_S
        return [w for w in self._workers.values() if w.sent_at >= cutoff]

    async def claim(
        self, capabilities: list[str], worker_id: str, wait_s: float = 5.0
    ) -> Task | None:
        probe = WorkerAdvert(worker_id=worker_id, host="local", capabilities=capabilities)
        deadline = time.time() + wait_s
        while True:
            async with self._lock:
                for i, task in enumerate(self._pending):
                    if probe.can(task.capability):
                        return self._pending.pop(i)
                self._arrived.clear()
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._arrived.wait(), remaining)
            except asyncio.TimeoutError:
                return None

    async def complete(self, result: TaskResult) -> None:
        loop = asyncio.get_running_loop()
        fut = self._results.setdefault(result.task_id, loop.create_future())
        if not fut.done():
            fut.set_result(result)

    async def heartbeat(self, advert: WorkerAdvert) -> None:
        self._workers[advert.worker_id] = advert

    async def announce(self, profile: NodeProfile) -> None:
        self._nodes[profile.node_id] = profile

    async def seen_nodes(self) -> list[NodeProfile]:
        cutoff = time.time() - HEARTBEAT_TTL_S * 4
        return [n for n in self._nodes.values() if n.seen_at >= cutoff]

    async def assign(self, assignment: Assignment) -> None:
        self._assignments[assignment.node_id] = assignment

    async def get_assignment(self, node_id: str) -> Assignment | None:
        return self._assignments.get(node_id)


# One process, one bus. run_core.py hands this same object to the core and to
# any in-process workers, which is what makes the local path a real path
# rather than a mock.
SHARED = LocalBus()
