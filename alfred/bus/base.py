"""The transport seam.

Everything above this interface — Alfred, the scheduler, every handler — is
written once and never changes. Going from "one machine" to "five machines"
is swapping LocalBus for NatsBus in a config file. That is the whole point of
having this abstraction, and it is also how the desktop fallback works:
the local path is not a special case, it is the default path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from alfred.contracts import Assignment, NodeProfile, Task, TaskResult, WorkerAdvert


class Bus(ABC):
    async def connect(self) -> None:
        """Open the connection. Safe to call more than once."""

    async def close(self) -> None:
        """Shut down cleanly."""

    # ---- Alfred side -----------------------------------------------------

    @abstractmethod
    async def submit(self, task: Task) -> None:
        """Enqueue a task. Returns as soon as it is durably queued.

        Note what this does NOT do: pick a machine. Alfred publishes to a
        capability, and whichever qualified worker is free claims it.
        """

    @abstractmethod
    async def await_result(self, task_id: str, timeout: float) -> TaskResult | None:
        """Wait for a result. None on timeout — the caller decides to retry."""

    @abstractmethod
    async def workers(self) -> list[WorkerAdvert]:
        """Currently alive workers. Stale heartbeats are already filtered out."""

    # ---- Worker side -----------------------------------------------------

    @abstractmethod
    async def claim(
        self, capabilities: list[str], worker_id: str, wait_s: float = 5.0
    ) -> Task | None:
        """Pull one task this worker is qualified for. None if nothing waiting.

        Pull, not push. A laptop that sleeps, roams to another network, or
        gets a new DHCP lease needs no inbound reachability — it just dials
        out again when it wakes.
        """

    @abstractmethod
    async def complete(self, result: TaskResult) -> None:
        """Publish a finished result and release the claim."""

    @abstractmethod
    async def heartbeat(self, advert: WorkerAdvert) -> None:
        """Announce liveness and current load. Called every few seconds."""

    # ---- enrollment ------------------------------------------------------

    @abstractmethod
    async def announce(self, profile: NodeProfile) -> None:
        """A node describing itself. Sent whether or not it has a job yet."""

    @abstractmethod
    async def seen_nodes(self) -> list[NodeProfile]:
        """Every machine that has announced recently, assigned or not."""

    @abstractmethod
    async def assign(self, assignment: Assignment) -> None:
        """Give a node its name and its capabilities. Durable: a node that
        reboots re-reads this rather than enrolling again."""

    @abstractmethod
    async def get_assignment(self, node_id: str) -> Assignment | None:
        """What this node has been told to be, if anything yet."""
