"""The worker program.

This exact file runs on the Zenbook, the MacBook, the Pi, the Chromebook and
the desktop. Nothing in it is machine-specific. What varies is the config it
was handed, and therefore which handlers it registers and which tasks it is
willing to claim.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import shutil
import time

from alfred.bus.base import Bus
from alfred.contracts import Assignment, Task, TaskResult, WorkerAdvert
from alfred.probe import probe
from alfred.worker.handlers import get_handler, registered_capabilities

log = logging.getLogger("alfred.worker")

try:
    import psutil
except ImportError:  # optional; the Pi and Chromebook can skip it
    psutil = None


def telemetry() -> dict:
    """Live load. Feeds the scheduler's scoring function.

    Battery matters: sending a 40-minute CAD job to an unplugged laptop at
    14% is how you lose the work and the afternoon.
    """
    if psutil is None:
        return {"cpu_percent": 0.0, "ram_free_gb": 4.0, "on_ac_power": True}
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_free_gb": round(psutil.virtual_memory().available / 1e9, 2),
        "battery_percent": battery.percent if battery else None,
        "on_ac_power": battery.power_plugged if battery else True,
    }


def vram_free_gb() -> float:
    if not shutil.which("nvidia-smi"):
        return 0.0
    import subprocess

    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip().splitlines()
        return round(max(int(line) for line in out) / 1024, 2)
    except Exception:
        return 0.0


class WorkerRuntime:
    def __init__(self, bus: Bus, cfg: dict) -> None:
        self.bus = bus
        self.cfg = cfg
        wcfg = cfg["worker"]
        self.worker_id: str = wcfg["id"]
        self.concurrency: int = wcfg["concurrency"]
        self.heartbeat_s: int = wcfg["heartbeat_s"]
        self.software: list[str] = wcfg["software"]

        # Advertise only what this machine declares AND has a handler for.
        # Stops a typo in a config from black-holing tasks.
        declared = set(wcfg["capabilities"])
        available = registered_capabilities()
        self.capabilities = sorted(
            c for c in declared
            if c in available or (c.endswith(".*") and any(a.startswith(c[:-1]) for a in available))
        )
        missing = declared - set(self.capabilities)
        if missing:
            log.warning("declared but unimplemented, not advertising: %s", sorted(missing))

        self._running = 0
        self._done_keys: dict[str, TaskResult] = {}
        self._stop = asyncio.Event()

        # The desktop is a fallback, not a competitor. Without this delay it
        # polls the same queue continuously, wins every race at t=0, and the
        # Zenbook never sees a task — the whole network reduced to one
        # machine that also happens to run four idle workers.
        self.claim_delay_s: float = wcfg.get("claim_delay_s", 0)

        # A node with nothing configured is not broken — it is new. It probes
        # itself, announces what it is, and waits to be told what it is for.
        self.profile = probe(cfg)
        self.enrolling = not declared
        if self.enrolling:
            self.worker_id = self.profile.node_id

    # ---- liveness --------------------------------------------------------

    def advert(self) -> WorkerAdvert:
        return WorkerAdvert(
            worker_id=self.worker_id,
            host=platform.node(),
            capabilities=self.capabilities,
            queue_depth=self._running,
            vram_free_gb=vram_free_gb(),
            software=self.software,
            **telemetry(),
        )

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                # Announce the machine itself as well as the worker. Alfred
                # uses the profile to decide what an unknown box is good for,
                # and to notice when a known one gains FreeCAD or a GPU.
                self.profile.seen_at = time.time()
                await self.bus.announce(self.profile)
                if self.capabilities:
                    await self.bus.heartbeat(self.advert())
            except Exception as exc:
                log.warning("heartbeat failed: %s", exc)
            await asyncio.sleep(self.heartbeat_s)

    async def _await_assignment(self) -> Assignment:
        """Wait to be given a name and a job.

        Deliberately patient rather than fatal: leave the machine plugged in,
        get to Alfred when convenient, and it joins the moment you do.
        """
        log.info("unassigned. announcing as %s and waiting for Alfred", self.profile.node_id)
        log.info("  %s", self.profile.describe())
        announced = False
        while not self._stop.is_set():
            self.profile.seen_at = time.time()
            await self.bus.announce(self.profile)
            assignment = await self.bus.get_assignment(self.profile.node_id)
            if assignment and assignment.capabilities:
                return assignment
            if not announced:
                log.info("  tell Alfred: a new machine has appeared")
                announced = True
            await asyncio.sleep(self.heartbeat_s)
        raise asyncio.CancelledError

    def _adopt(self, assignment: Assignment) -> None:
        implemented = registered_capabilities()
        self.worker_id = assignment.name or assignment.node_id
        self.capabilities = sorted(c for c in assignment.capabilities if c in implemented)
        self.concurrency = assignment.concurrency
        self.claim_delay_s = assignment.claim_delay_s
        refused = set(assignment.capabilities) - set(self.capabilities)
        if refused:
            # Alfred filters against the manifest, but the node has the final
            # say about itself. Better a refused capability than a worker
            # advertising something it cannot actually perform.
            log.warning("assigned but not implemented here: %s", sorted(refused))
        log.info("enrolled as '%s' offering: %s",
                 self.worker_id, ", ".join(self.capabilities))

    # ---- work ------------------------------------------------------------

    async def _execute(self, task: Task) -> TaskResult:
        started = time.time()

        # Idempotency. A retried web search is harmless. A retried
        # "rotate joint 3 by 90 degrees" is not.
        key = task.idempotency_key
        if key and key in self._done_keys:
            log.info("task %s already done under key %s, replaying", task.id, key)
            cached = self._done_keys[key]
            return TaskResult(**{**cached.__dict__, "task_id": task.id})

        handler = get_handler(task.capability)
        if handler is None:
            return TaskResult(
                task_id=task.id, worker_id=self.worker_id, status="rejected",
                error=f"no handler for {task.capability}",
            )

        try:
            result = await asyncio.wait_for(
                handler(task, self.cfg), timeout=task.timeout_s
            )
        except asyncio.TimeoutError:
            result = TaskResult(
                task_id=task.id, worker_id=self.worker_id, status="error",
                error=f"timed out after {task.timeout_s}s",
            )
        except Exception as exc:
            log.exception("handler raised")
            result = TaskResult(
                task_id=task.id, worker_id=self.worker_id, status="error",
                error=f"{type(exc).__name__}: {exc}",
            )

        result.task_id = task.id
        result.worker_id = self.worker_id
        result.duration_s = round(time.time() - started, 2)
        if key and result.ok:
            self._done_keys[key] = result
        return result

    async def _run_one(self, task: Task) -> None:
        self._running += 1
        try:
            log.info("claimed %s (%s)", task.id, task.capability)
            result = await self._execute(task)
            await self.bus.complete(result)
            log.info("finished %s -> %s in %ss", task.id, result.status, result.duration_s)
        finally:
            self._running -= 1

    async def run(self) -> None:
        await self.bus.connect()
        hb_early = None
        if self.enrolling:
            hb_early = asyncio.create_task(self._heartbeat_loop())
            try:
                self._adopt(await self._await_assignment())
            finally:
                hb_early.cancel()
        if not self.capabilities:
            log.error("no capabilities available; check the config or the assignment")
            return

        log.info("worker %s up, offering: %s", self.worker_id, ", ".join(self.capabilities))
        if self.claim_delay_s:
            log.info("deferring %ss before each poll (fallback worker)", self.claim_delay_s)
        hb = asyncio.create_task(self._heartbeat_loop())
        inflight: set[asyncio.Task] = set()
        try:
            while not self._stop.is_set():
                if self._running >= self.concurrency:
                    await asyncio.sleep(0.2)
                    continue
                if self.claim_delay_s:
                    # Approximate but effective: anything still queued after
                    # this nap is work nobody else wanted.
                    await asyncio.sleep(self.claim_delay_s)
                task = await self.bus.claim(self.capabilities, self.worker_id, wait_s=5.0)
                if task is None:
                    continue
                runner = asyncio.create_task(self._run_one(task))
                inflight.add(runner)
                runner.add_done_callback(inflight.discard)
        finally:
            hb.cancel()
            for runner in inflight:
                runner.cancel()
            await self.bus.close()

    def stop(self) -> None:
        self._stop.set()
