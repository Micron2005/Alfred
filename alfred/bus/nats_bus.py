"""NATS + JetStream bus. Phase 2, when work starts leaving the desktop.

The server runs wherever the brain runs (the desktop hosts it itself, see
`alfred.bus.hosting`), or on an always-on box such as a Pi:

    nats-server -js -sd /var/lib/nats

One 15MB static binary, ARM builds available, no cluster, no config file.
JetStream gives you the durable work queue; core NATS gives you results and
the KV bucket gives you heartbeats with automatic expiry.

Subject layout mirrors the capability namespace exactly, so a worker that
advertises "code.*" subscribes to "alfred.task.code.*" and NATS does the
routing for free.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
import urllib.parse

from alfred.bus.base import Bus
from alfred.contracts import Assignment, NodeProfile, Task, TaskResult, WorkerAdvert

STREAM = "ALFRED_TASKS"
TASK_SUBJECT = "alfred.task"
RESULT_SUBJECT = "alfred.result"
WORKER_BUCKET = "alfred_workers"
NODE_BUCKET = "alfred_nodes"
ASSIGN_BUCKET = "alfred_assignments"
HEARTBEAT_TTL_S = 15
NODE_TTL_S = 120
CONNECT_TIMEOUT_S = 5.0

log = logging.getLogger("alfred.bus")


class BusUnreachable(ConnectionError):
    """Nothing is listening at the bus URL. The message says what to check."""


def host_port(url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(url if "://" in url else f"nats://{url}")
    return parsed.hostname or "127.0.0.1", parsed.port or 4222


def is_local_host(host: str) -> bool:
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0", socket.gethostname()}:
        return True
    try:
        return socket.gethostbyname(host) in {"127.0.0.1"} | set(_own_addresses())
    except OSError:
        return False


def _own_addresses() -> list[str]:
    try:
        return [
            info[4][0]
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        ]
    except OSError:
        return []


async def reachable(url: str, timeout: float = CONNECT_TIMEOUT_S) -> str | None:
    """None if a TCP connection to the bus opens; otherwise the reason."""
    host, port = host_port(url)
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout
        )
    except asyncio.TimeoutError:
        return f"no answer from {host}:{port} within {timeout:.0f}s (firewall, or wrong address?)"
    except socket.gaierror:
        return f"cannot resolve host {host!r}"
    except OSError as exc:
        return f"{host}:{port} refused the connection ({exc.strerror or exc})"
    writer.close()
    return None


def unreachable_hint(url: str) -> str:
    host, port = host_port(url)
    if is_local_host(host):
        return (
            f"the bus at {url} is not running on this machine. Alfred's core starts "
            "nats-server itself when the binary is on PATH; install it "
            "(install.sh does) or start it by hand: nats-server -js -sd ~/.alfred/nats"
        )
    return (
        f"cannot reach the bus at {url}. On the machine running Alfred's core: "
        f"check that it is up and that port {port} is open to the LAN "
        f"(e.g. sudo ufw allow {port}/tcp). On this machine: check {host} is the "
        "core's current address, or use --bus auto to find it by broadcast."
    )


def _durable_name(capability: str) -> str:
    """NATS durable names allow no dots, spaces or wildcards.

    Keyed on the CAPABILITY, deliberately not on the worker. A work-queue
    stream permits exactly one consumer per subject, so every worker offering
    `code.write` must share one durable consumer and let NATS hand out
    messages between them. Putting the worker id in this name makes the
    second worker's subscription fail with a filter-subject overlap error —
    which is to say, it works perfectly until the moment you add the machine
    the whole design exists for.
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"cap_{capability}")


class NatsBus(Bus):
    def __init__(self, url: str = "nats://127.0.0.1:4222") -> None:
        self.url = url
        self._nc = None
        self._js = None
        self._kv = None
        self._nodes_kv = None
        self._assign_kv = None
        self._results: dict[str, asyncio.Future[TaskResult]] = {}
        self._subs: dict[str, object] = {}

    async def connect(self) -> None:
        if self._nc is not None:
            return
        try:
            import nats
        except ImportError as exc:
            raise BusUnreachable(
                "the NATS client is not installed here: pip install nats-py"
            ) from exc

        reason = await reachable(self.url)
        if reason:
            raise BusUnreachable(f"{reason}; {unreachable_hint(self.url)}")

        async def _quiet(exc: Exception) -> None:
            log.warning("bus: %s", exc)

        async def _reconnected() -> None:
            log.info("bus: reconnected to %s", self.url)

        nc = await nats.connect(
            self.url,
            connect_timeout=CONNECT_TIMEOUT_S,
            drain_timeout=5,  # shutdown should not hang on idle pull subscriptions
            reconnect_time_wait=2,
            max_reconnect_attempts=-1,  # a sleeping laptop is normal, not an error
            error_cb=_quiet,
            reconnected_cb=_reconnected,
        )
        self._nc = nc
        self._js = self._nc.jetstream()

        try:
            await self._js.add_stream(
                name=STREAM,
                subjects=[f"{TASK_SUBJECT}.>"],
                retention="workqueue",  # a claimed task is removed, not fanned out
                max_age=24 * 3600,
            )
        except Exception:
            # Already exists from a previous run. Do not attempt to update it:
            # changing retention on a live stream discards queued work.
            await self._js.stream_info(STREAM)
        async def _bucket(name: str, ttl: int | None = None):
            try:
                return await self._js.create_key_value(bucket=name, ttl=ttl) if ttl \
                    else await self._js.create_key_value(bucket=name)
            except Exception:
                return await self._js.key_value(name)

        self._kv = await _bucket(WORKER_BUCKET, HEARTBEAT_TTL_S)
        # Node profiles expire; a machine that goes away stops being listed.
        self._nodes_kv = await _bucket(NODE_BUCKET, NODE_TTL_S)
        # Assignments do NOT expire. A node that reboots after a month must
        # come back as itself, not as an unknown device needing re-enrollment.
        self._assign_kv = await _bucket(ASSIGN_BUCKET)

        async def _on_result(msg) -> None:
            result = TaskResult.from_json(msg.data)
            loop = asyncio.get_running_loop()
            fut = self._results.setdefault(result.task_id, loop.create_future())
            if not fut.done():
                fut.set_result(result)

        await self._nc.subscribe(f"{RESULT_SUBJECT}.*", cb=_on_result)

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None

    # ---- Alfred side -----------------------------------------------------

    async def submit(self, task: Task) -> None:
        await self.connect()
        loop = asyncio.get_running_loop()
        self._results.setdefault(task.id, loop.create_future())
        await self._js.publish(
            f"{TASK_SUBJECT}.{task.capability}",
            task.to_json().encode(),
            headers={"Nats-Msg-Id": task.idempotency_key or task.id},
        )

    async def await_result(self, task_id: str, timeout: float) -> TaskResult | None:
        loop = asyncio.get_running_loop()
        fut = self._results.setdefault(task_id, loop.create_future())
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            return None

    async def workers(self) -> list[WorkerAdvert]:
        await self.connect()
        out: list[WorkerAdvert] = []
        try:
            keys = await self._kv.keys()
        except Exception:
            return out  # empty bucket raises rather than returning []
        for key in keys:
            try:
                entry = await self._kv.get(key)
                out.append(WorkerAdvert.from_json(entry.value))
            except Exception:
                continue  # expired between listing and reading
        return out

    # ---- Worker side -----------------------------------------------------

    async def claim(
        self, capabilities: list[str], worker_id: str, wait_s: float = 5.0
    ) -> Task | None:
        await self.connect()
        for cap in capabilities:
            if cap not in self._subs:
                self._subs[cap] = await self._js.pull_subscribe(
                    f"{TASK_SUBJECT}.{cap}",
                    durable=_durable_name(cap),
                    stream=STREAM,
                )

        # Round-robin so one busy capability cannot starve the others.
        per_cap = max(wait_s / max(len(capabilities), 1), 0.5)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            for cap in capabilities:
                try:
                    msgs = await self._subs[cap].fetch(1, timeout=per_cap)
                except Exception:
                    continue  # normal: nothing waiting on this subject
                if msgs:
                    msg = msgs[0]
                    # Ack now. The lease/requeue path lives in the supervisor
                    # loop, which has the project context to decide on a retry.
                    await msg.ack()
                    return Task.from_json(msg.data)
        return None

    async def complete(self, result: TaskResult) -> None:
        await self.connect()
        await self._nc.publish(
            f"{RESULT_SUBJECT}.{result.task_id}", result.to_json().encode()
        )

    async def heartbeat(self, advert: WorkerAdvert) -> None:
        await self.connect()
        await self._kv.put(advert.worker_id, advert.to_json().encode())

    # ---- enrollment ------------------------------------------------------

    async def announce(self, profile: NodeProfile) -> None:
        await self.connect()
        await self._nodes_kv.put(profile.node_id, profile.to_json().encode())

    async def seen_nodes(self) -> list[NodeProfile]:
        await self.connect()
        out: list[NodeProfile] = []
        try:
            keys = await self._nodes_kv.keys()
        except Exception:
            return out
        for key in keys:
            try:
                entry = await self._nodes_kv.get(key)
                out.append(NodeProfile.from_json(entry.value))
            except Exception:
                continue
        return out

    async def assign(self, assignment: Assignment) -> None:
        await self.connect()
        await self._assign_kv.put(assignment.node_id, assignment.to_json().encode())

    async def get_assignment(self, node_id: str) -> Assignment | None:
        await self.connect()
        try:
            entry = await self._assign_kv.get(node_id)
            return Assignment.from_json(entry.value)
        except Exception:
            return None
