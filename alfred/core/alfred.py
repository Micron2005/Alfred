"""Alfred core. This file runs on the desktop and nowhere else.

Two loops sharing one state store:

    converse()   — runs when you speak. Plans, dispatches, synthesises.
    supervise()  — runs on a clock. Reclaims dead leases, notices finished
                   work, spots stale projects. Never speaks to you directly;
                   it leaves notices the conversation loop delivers.

Without the second loop, "anticipate needs" cannot be implemented at all:
Alfred would only ever discover a finished job because you happened to ask.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from pathlib import Path

from alfred import llm
from alfred.bus.base import Bus
from alfred.contracts import Assignment, NodeProfile, Task, TaskResult, _new_id
from alfred.core import enrollment, planner, scheduler
from alfred.worker.handlers import registered_capabilities
from alfred.core.state import State

log = logging.getLogger("alfred.core")

PERSONA_PATH = Path(__file__).with_name("persona.md")
STALE_PROJECT_DAYS = 7


class Alfred:
    def __init__(self, bus: Bus, cfg: dict) -> None:
        self.bus = bus
        self.cfg = cfg
        self.state = State(cfg["core"]["state_db"])
        self.persona = PERSONA_PATH.read_text() if PERSONA_PATH.exists() else ""
        self.local_worker_id: str = cfg["worker"]["id"]
        self.history: list[tuple[str, str]] = []

    # ---- dispatch --------------------------------------------------------

    async def _dispatch(self, task: Task) -> TaskResult:
        """Submit, wait, retry, and let the local worker take over if nobody
        else does. The fallback is not a special path — the desktop's own
        worker claims from the same queue as everyone else, just later."""
        while True:
            self.state.enqueue(task)
            # The lease is what lets the supervisor notice an abandoned task.
            # Without it, `expired_leases()` returns nothing forever and a
            # worker that sleeps mid-job takes the work silently with it.
            self.state.lease(task.id, "queued", task.timeout_s)
            await self.bus.submit(task)

            waited = 0.0
            result: TaskResult | None = None
            while waited < task.timeout_s:
                result = await self.bus.await_result(task.id, timeout=5.0)
                if result is not None:
                    break
                waited += 5.0
                workers = await self.bus.workers()
                if scheduler.should_run_locally(
                    task, workers, self.local_worker_id,
                    waited, self.cfg["core"]["fallback_after_s"],
                ):
                    log.info("no remote worker for %s; the desktop will take it",
                             task.capability)

            if result is not None:
                self.state.finish(task.id, result.status, result.summary, result.error or "")
                for uri in result.artifacts:
                    self.state.add_artifact(task.project_id or "", task.id, uri)
                if result.ok or task.attempt >= task.max_retries:
                    return result
                log.warning("task %s failed (%s), retry %d/%d",
                            task.id, result.error, task.attempt + 1, task.max_retries)
            elif task.attempt >= task.max_retries:
                self.state.finish(task.id, "error", error="no worker took it in time")
                return TaskResult(
                    task_id=task.id, worker_id="none", status="error",
                    error=f"no worker took {task.capability} within {task.timeout_s}s",
                )

            # Retry as a NEW task id, so the old ledger row and the old result
            # future stay intact and the attempt history is readable.
            task = replace(task, id=_new_id("task"), attempt=task.attempt + 1)

    async def _run_graph(self, tasks: list[Task]) -> dict[str, TaskResult]:
        """Execute the DAG, running everything whose dependencies are met
        concurrently. A flat list would serialise work with no reason to be
        sequential."""
        results: dict[str, TaskResult] = {}
        remaining = {t.id: t for t in tasks}

        while remaining:
            ready = [
                t for t in remaining.values()
                if all(dep in results for dep in t.depends_on)
            ]
            if not ready:
                for t in remaining.values():
                    results[t.id] = TaskResult(
                        task_id=t.id, worker_id="none", status="error",
                        error="dependency cycle or unmet dependency",
                    )
                break

            for task in ready:
                # Upstream summaries flow downstream. Summaries only — never
                # the bulk artifacts, or the context saving is undone.
                upstream = [results[d].summary for d in task.depends_on if d in results]
                if upstream:
                    task.inputs["upstream"] = upstream

            done = await asyncio.gather(*(self._dispatch(t) for t in ready))
            for task, result in zip(ready, done):
                results[task.id] = result
                remaining.pop(task.id, None)
        return results

    # ---- conversation loop ----------------------------------------------

    async def _network_capabilities(self) -> list[str]:
        """What the network can do right now, from live heartbeats.

        A sleeping Zenbook removes code.write from the menu, so Alfred plans
        around its absence instead of queueing work nobody will claim.
        """
        implemented = registered_capabilities()
        available: set[str] = set()
        for worker in await self.bus.workers():
            for cap in worker.capabilities:
                if cap.endswith(".*"):
                    available |= {c for c in implemented if c.startswith(cap[:-1])}
                else:
                    available.add(cap)
        return sorted(available)

    async def converse(self, message: str, project_id: str | None = None) -> str:
        briefing = self.state.briefing(project_id) if project_id else ""

        # Anything the supervisor noticed while you were away.
        pending = self.state.undelivered()
        if pending:
            briefing += "\n\nSince we last spoke:\n" + "\n".join(
                f"  - {n['body']}" for n in pending)

        tasks = await planner.plan(
            message, briefing, self.cfg, project_id,
            available=await self._network_capabilities(),
        )

        if not tasks:
            # Nothing worth delegating. Answer directly.
            reply = await self._speak(message, briefing, [])
            self._remember(message, reply, pending)
            return reply

        log.info("plan: %d tasks across %s", len(tasks),
                 ", ".join(sorted({t.capability for t in tasks})))
        results = await self._run_graph(tasks)

        # Verify before synthesising, so the report reflects checked work.
        checked: list[str] = []
        for task in tasks:
            result = results[task.id]
            passed, note = planner.verify(task, result)
            checked.append(
                f"[{task.capability}] {'OK' if passed else 'FAILED'} ({note})\n"
                f"{result.summary or result.error}"
            )

        reply = await self._speak(message, briefing, checked)
        self._remember(message, reply, pending)
        return reply

    async def _speak(self, message: str, briefing: str, findings: list[str]) -> str:
        """The only place in the entire system that produces user-facing text.

        Workers return artifacts. Alfred does all the narrating. That is the
        whole of what "there is only one Alfred" means in code.
        """
        recent = "\n".join(f"{who}: {what}" for who, what in self.history[-6:])
        body = "\n\n".join(findings) if findings else "(handled without delegation)"
        prompt = (
            f"{briefing}\n\nRecent conversation:\n{recent}\n\n"
            f"User: {message}\n\nWorker results:\n{body}\n\n"
            "Reply to the user. Lead with the answer. State plainly anything "
            "that failed or is unverified — do not paper over it. If a "
            "decision was made, say what it was and why."
        )
        return await llm.complete(prompt, self.cfg, system=self.persona, timeout=600)

    def _remember(self, message: str, reply: str, delivered: list[dict]) -> None:
        self.history.append(("User", message))
        self.history.append(("Alfred", reply))
        self.state.mark_delivered([n["id"] for n in delivered])

    # ---- enrollment ------------------------------------------------------

    async def unassigned_nodes(self) -> list[NodeProfile]:
        """Machines that have announced themselves but have no job yet."""
        out = []
        for profile in await self.bus.seen_nodes():
            if await self.bus.get_assignment(profile.node_id) is None:
                out.append(profile)
        return out

    async def propose_for(self, node_id: str, hint: str = "") -> Assignment | None:
        profile = next(
            (p for p in await self.bus.seen_nodes() if p.node_id == node_id), None
        )
        if profile is None:
            return None
        return await enrollment.propose(
            profile, hint, self.cfg, taken=self.state.coverage()
        )

    async def enroll(self, assignment: Assignment) -> str:
        """Commit an assignment. The node adopts it within one heartbeat."""
        profile = next(
            (p for p in await self.bus.seen_nodes() if p.node_id == assignment.node_id), None
        )
        if profile is None:
            return f"no machine with id {assignment.node_id} is announcing right now"
        good, bad = enrollment.validate(profile, assignment.capabilities)
        if not good:
            return f"none of those will work here: {'; '.join(bad)}"
        assignment.capabilities = good
        await self.bus.assign(assignment)
        self.state.record_node(assignment, profile)
        self.state.notice("enrolled",
                          f"{assignment.name} joined offering {', '.join(good)}")
        message = f"enrolled {assignment.name}: {', '.join(good)}"
        if bad:
            message += f" (declined: {'; '.join(bad)})"
        return message

    # ---- supervisor loop -------------------------------------------------

    async def supervise(self) -> None:
        """Runs forever, independent of conversation. This is the difference
        between a butler and a chatbot."""
        tick = self.cfg["core"]["supervisor_tick_s"]
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("supervisor tick failed")
            await asyncio.sleep(tick)

    async def _tick(self) -> None:
        # A closed laptop lid is indistinguishable from a crash, and both
        # want the same response.
        for row in self.state.expired_leases():
            if row["attempt"] < 2:
                log.warning("lease expired on %s, requeueing", row["id"])
                self.state.finish(row["id"], "queued")
                self.state.notice(
                    "requeue",
                    f"{row['capability']} was reclaimed from {row['assigned_to']} and requeued",
                    row["project_id"],
                )
            else:
                self.state.finish(row["id"], "error", error="abandoned after 2 attempts")
                self.state.notice(
                    "failed",
                    f"{row['capability']} failed after repeated worker loss",
                    row["project_id"],
                )

        # A machine that shows up should not have to wait for you to think
        # to ask. Noticed here, mentioned on your next turn.
        for profile in await self.unassigned_nodes():
            if not self.state.known_node(profile.node_id):
                self.state.notice(
                    "new_node",
                    f"A new machine is on the network and has no job: "
                    f"{profile.describe()} [id {profile.node_id}]",
                )
                self.state.record_node(
                    Assignment(node_id=profile.node_id, name="", capabilities=[],
                               note="seen, not yet assigned"),
                    profile,
                )

        cutoff = time.time() - STALE_PROJECT_DAYS * 86400
        for project in self.state.active_projects():
            if project["updated_at"] < cutoff:
                blocking = [q for q in self.state.open_questions(project["id"]) if q["blocking"]]
                if blocking:
                    self.state.notice(
                        "stalled",
                        f"{project['name']} has been idle a week, blocked on: "
                        f"{blocking[0]['question']}",
                        project["id"],
                    )
