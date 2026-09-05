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
from urllib.parse import urlparse
from urllib.request import url2pathname

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
        # Conversation survives restarts: the last turns reload so a
        # service restart or self-update does not wipe the thread.
        self.history: list[tuple[str, str]] = list(self.state.recent_turns(6))
        self._bg_tasks: set = set()   # background learners, kept referenced

    # ---- dispatch --------------------------------------------------------

    async def _dispatch(self, task: Task) -> TaskResult:
        """Submit, wait, retry, and let the local worker take over if nobody
        else does. The fallback is not a special path — the desktop's own
        worker claims from the same queue as everyone else, just later."""
        # OS changes never run on Alfred's judgment. Park them; the owner's
        # approval (shell button or /approve) is what dispatches them.
        if task.capability == "os.apply" and not task.inputs.get("_approved"):
            from alfred.worker.handlers.oscontrol import describe_action
            description = describe_action(
                str(task.inputs.get("action", "?")), task.inputs.get("args") or {})
            action_id = self.state.park_action(
                task.project_id, description, task.to_json())
            return TaskResult(
                task_id=task.id, worker_id="owner-approval", status="pending",
                summary=f"'{description}' is queued as pending change #{action_id}, "
                        "awaiting your approval.",
            )

        while True:
            self.state.enqueue(task)
            # The lease is what lets the supervisor notice an abandoned task.
            # Without it, `expired_leases()` returns nothing forever and a
            # worker that sleeps mid-job takes the work silently with it.
            self.state.lease(task.id, "queued", task.timeout_s)
            await self.bus.submit(task)

            waited = 0.0
            result: TaskResult | None = None
            fallback_announced = False
            while waited < task.timeout_s:
                result = await self.bus.await_result(task.id, timeout=5.0)
                if result is not None:
                    break
                waited += 5.0
                workers = await self.bus.workers()
                if not fallback_announced and scheduler.should_run_locally(
                    task, workers, self.local_worker_id,
                    waited, self.cfg["core"]["fallback_after_s"],
                ):
                    log.info("no remote worker for %s; the local worker will take it",
                             task.capability)
                    fallback_announced = True

            if result is not None:
                self.state.finish(task.id, result.status, result.summary, result.error or "")
                for uri in result.artifacts:
                    self.state.add_artifact(task.project_id or "", task.id, uri)
                if result.ok or result.status == "rejected" or task.attempt >= task.max_retries:
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
                # Upstream summaries flow downstream, plus the URIs of what
                # was produced — never the artifact contents, or the context
                # saving is undone. A tester needs to know where the code is.
                upstream = [results[d].summary for d in task.depends_on if d in results]
                if upstream:
                    task.inputs["upstream"] = upstream
                task.artifacts = [
                    uri for d in task.depends_on if d in results
                    for uri in results[d].artifacts
                ]

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

    ATTACH_ROUTES = (
        ({".png",".jpg",".jpeg",".webp",".gif",".bmp"}, "vision.describe", 240),
        ({".mp4",".mov",".mkv",".webm",".avi",".m4v"}, "media.video", 600),
        ({".pdf",".txt",".md"}, "research.document", 300),
    )

    def _attachment_tasks(self, attachments: list[str], message: str,
                          available: set[str] | None = None) -> list[Task]:
        """Files the user showed him. Routed by type, deterministically — a
        7B planner should never be between an uploaded image and the vision
        model. When the preferred capability is not offered anywhere on the
        network (no vision model yet), the file degrades honestly to
        media.inspect rather than dispatching into a void."""
        tasks = []
        for raw in attachments:
            path = str(raw)
            ext = ("." + path.rsplit(".", 1)[-1].lower()) if "." in path else ""
            for exts, capability, timeout in self.ATTACH_ROUTES:
                if ext in exts and (available is None or capability in available):
                    inputs = ({"paths": [path]} if capability == "research.document"
                              else {"path": path, "question": message})
                    tasks.append(Task(capability=capability, prompt=message,
                                      inputs=inputs, timeout_s=timeout))
                    break
            else:
                tasks.append(Task(capability="media.inspect", prompt=message,
                                  inputs={"path": path}, timeout_s=120))
        return tasks

    def _owner_briefing(self) -> str:
        """What Alfred currently knows about his employer. The fresh-start
        persona promises he learns you over time; this is where that memory
        actually enters his context."""
        facts = self.state.known_facts("owner")
        if not facts:
            return ""
        lines = []
        for f in facts:
            label = f["key"].replace("_", " ")
            tag = " (inferred)" if f["confidence"] == "inferred" else ""
            lines.append(f"  - {label}: {f['value']}{tag}")
        return ("What you know about your employer (learned over time; use "
                "it naturally, do not recite it):\n" + "\n".join(lines))

    # Verbs and nouns that signal real work worth planning for. The gate
    # errs toward planning: a false 'yes' costs one wasted planner call, a
    # false 'no' would drop a real task — so the bar to skip is high.
    _WORK_HINTS = (
        "calculate", "compute", "design", "model", "cad", "measure",
        "write code", "script", "program", "build", "generate",
        "research", "look up", "search", "find out", "analyze", "analyse",
        "install", "update", "upgrade", "restart", "service", "package",
        "observe", "check the", "disk", "memory", "read the", "document",
        "torque", "stress", "load", "bracket", "gear", "motor", "simulate",
        "market", "promote", "seo", "audit", "campaign", "launch", "pitch",
        "tagline", "landing page", "ad copy", "outreach", "email", "draft",
        "write me", "write a", "write two", "write three", "post", "tweet",
        "headline", "newsletter", "blog", "facebook", "linkedin", "reddit",
        "customers", "sell", "competitor",
        "create", "make a", "make me", "file", "save", "write", "delete",
        "remove", "set up", "setup", "configure", "run ", "test", "fix",
        "add ", "check", "show me", "list", "how much", "how many", "what is",
        "open", "look at", "summar", "compare", "price", "pricing",
    )

    def _might_need_work(self, message: str) -> bool:
        m = message.lower().strip()
        if len(m) < 4:
            return False
        return any(h in m for h in self._WORK_HINTS)

    async def converse(self, message: str, project_id: str | None = None,
                       attachments: list[str] | None = None) -> str:
        briefing = self.state.briefing(project_id) if project_id else ""
        owner = self._owner_briefing()
        if owner:
            briefing = (owner + "\n\n" + briefing) if briefing else owner

        # Anything the supervisor noticed while you were away.
        pending = self.state.undelivered()
        if pending:
            briefing += (
                "\n\nHappened since your last reply (these are facts and supersede "
                "anything said earlier in the conversation; mention briefly, and "
                "only if relevant to what the user is saying):\n"
                + "\n".join(f"  - {n['body']}" for n in pending)
            )

        if attachments:
            tasks = self._attachment_tasks(
                attachments, message, available=await self._network_capabilities())
        elif self._might_need_work(message):
            # Only consult the planner when the message plausibly asks for
            # something a worker does. Plain conversation skips it entirely
            # and answers in one LLM call instead of two.
            tasks = await planner.plan(
                message, briefing, self.cfg, project_id,
                available=await self._network_capabilities(),
            )
        else:
            tasks = []

        if not tasks:
            # Nothing worth delegating. Answer directly.
            reply = await self._speak(message, briefing, [])
            self._remember(message, reply, pending)
            self._learn_in_background(message)
            return reply

        log.info("plan: %d tasks across %s", len(tasks),
                 ", ".join(sorted({t.capability for t in tasks})))
        results = await self._run_graph(tasks)

        # Verify before synthesising, so the report reflects checked work.
        checked: list[str] = []
        failed: list[str] = []
        for task in tasks:
            result = results[task.id]
            passed, note = planner.verify(task, result)
            checked.append(
                f"[{task.capability}] {'OK' if passed else 'FAILED'} ({note})\n"
                f"{result.summary or result.error}"
            )
            if not passed:
                failed.append(f"{task.capability}: {note}")
        if failed:
            checked.insert(0, (
                f"{len(failed)} of {len(tasks)} steps FAILED and the user must be told "
                "which, and why, in plain words:\n  - " + "\n  - ".join(failed)
            ))

        reply = await self._speak(message, briefing, checked)
        reply = self._attach_deliverables(reply, tasks, results)
        self._remember(message, reply, pending)
        self._learn_in_background(message)
        return reply

    @staticmethod
    def _attach_deliverables(reply: str, tasks: list[Task],
                             results: dict[str, TaskResult]) -> str:
        """Copy the owner asked for is handed over in full, however the
        narration treated it, and every file produced is named so he can
        find it. Small models like to describe a draft instead of pasting it."""
        extra: list[str] = []
        for task in tasks:
            result = results[task.id]
            if task.capability == "marketing.draft" and result.ok and result.summary:
                draft = result.summary.split("\n---\n")[0].strip()
                probe = " ".join(draft.split())[:60]
                if probe and probe not in " ".join(reply.split()):
                    extra.append(draft)
            if task.capability == "research.web" and result.ok:
                missing = [u for u in result.data.get("fetched", []) if u not in reply]
                if missing:
                    extra.append("Sources read:\n" + "\n".join(f"  {u}" for u in missing))
        files = [url2pathname(urlparse(u).path)
                 for task in tasks if results[task.id].ok
                 for u in results[task.id].artifacts if u.startswith("file:")]
        if files:
            extra.append("Files:\n" + "\n".join(f"  {p}" for p in files))
        return reply if not extra else reply.rstrip() + "\n\n" + "\n\n".join(extra)

    async def _speak(self, message: str, briefing: str, findings: list[str]) -> str:
        """The only place in the entire system that produces user-facing text.

        Workers return artifacts. Alfred does all the narrating. That is the
        whole of what "there is only one Alfred" means in code.
        """
        recent = "\n".join(f"{who}: {what}" for who, what in self.history[-6:])
        body = "\n\n".join(findings) if findings else (
            "(none needed — this is conversation, answer it directly and naturally, "
            "and do not mention tasks or workers. The one exception: if the user "
            "asked for something to be done or made, nothing was, so say so plainly "
            "and say what is needed — never claim it was done.)"
        )
        prompt = (
            f"{briefing}\n\nRecent conversation:\n{recent}\n\n"
            f"User: {message}\n\nWorker results for this message:\n{body}\n\n"
            "Reply to the user about THIS message only. Lead with the answer. "
            "State plainly anything in this message's worker results that "
            "failed or is unverified — do not paper over it; if nothing did, "
            "say nothing about failures at all. Older failures "
            "and earlier topics were already reported; do not repeat them "
            "unless the user asks. If a decision was made, say what it was "
            "and why."
        )
        return await llm.complete(prompt, self.cfg, system=self.persona, timeout=600)

    def _remember(self, message: str, reply: str, delivered: list[dict]) -> None:
        self.history.append(("User", message))
        self.history.append(("Alfred", reply))
        self.history = self.history[-12:]
        self.state.log_turn("User", message)
        self.state.log_turn("Alfred", reply)
        self.state.mark_delivered([n["id"] for n in delivered])

    def _learn_in_background(self, message: str) -> None:
        async def _run():
            try:
                await self._learn_from(message)
            except Exception:
                pass  # a missed fact is fine; never disturb the reply
        task = asyncio.ensure_future(_run())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _learn_from(self, message: str) -> None:
        """Notice durable facts the owner stated about himself and file
        them. Conservative on purpose: clear, lasting facts only — a name,
        a preference, a constraint — never a passing mood. A miss is fine;
        a false memory is not. Explicit forget is honoured directly."""
        text = message.strip()
        low = text.lower()
        if low.startswith(("forget ", "forget that ")):
            what = text.split(" ", 1)[1].strip().rstrip(".").lower()
            for f in self.state.known_facts("owner"):
                if f["key"].replace("_", " ") in what or f["value"].lower() in what:
                    self.state.forget(f["key"])
            return
        schema = (
            "Extract only durable facts the user stated about THEMSELVES that "
            "a butler should remember long-term: name or preferred form of "
            "address, stable preferences, standing constraints, key "
            "relationships, recurring context. Ignore anything transient, "
            "hypothetical, about the world rather than the user, or already "
            "obvious. Return strict JSON "
            "{\"facts\":[{\"key\":\"snake_case\",\"value\":\"short\","
            "\"confidence\":\"stated|inferred\"}]} with an empty list if "
            "nothing qualifies. User message: \"" + text[:600] + "\""
        )
        try:
            data = await llm.complete_json(schema, self.cfg, timeout=60)
        except Exception:
            return
        for fact in (data or {}).get("facts", [])[:3]:
            key = str(fact.get("key", "")).strip()
            value = str(fact.get("value", "")).strip()
            if key and value and len(value) < 200:
                self.state.learn(key, value, source=text[:200],
                                 confidence=fact.get("confidence", "stated"))

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

    # ---- owner approval gate ---------------------------------------------

    async def approve_action(self, action_id: int) -> str:
        row = self.state.take_action(action_id)   # atomic; double-click safe
        if row is None:
            return f"pending change #{action_id} is not awaiting approval"
        task = Task.from_json(row["task_json"])
        task.inputs["_approved"] = True           # the mark only this path sets
        # The owner's approval is taken atomically once, which is the very
        # guarantee the key exists to provide.
        task.idempotency_key = task.idempotency_key or f"approved-action:{action_id}"
        result = await self._dispatch(task)
        self.state.settle_action(action_id, result.ok, result.summary or result.error or "")
        outcome = result.summary if result.ok else f"failed: {result.error}"
        self.state.notice("os_change", f"Change #{action_id}: {outcome}", row["project_id"])
        return outcome

    def decline_action(self, action_id: int) -> str:
        if self.state.decline_action(action_id):
            return f"declined pending change #{action_id}"
        return f"pending change #{action_id} is not awaiting approval"

    # ---- supervisor loop -------------------------------------------------

    async def supervise(self) -> None:
        """Runs forever, independent of conversation. This is the difference
        between a butler and a chatbot."""
        tick = self.cfg["core"]["supervisor_tick_s"]
        while True:
            try:
                await self._tick()
                await self._write_status()
            except Exception:
                log.exception("supervisor tick failed")
            await asyncio.sleep(tick)

    async def _write_status(self) -> None:
        """Heartbeat for the dashboard: current household truth to a file the
        status board reads. Best-effort; never breaks supervision."""
        import json, time, tempfile, os, pathlib
        try:
            now = time.time()
            workers = {w.worker_id: w for w in await self.bus.workers()}
            seen = {p.node_id: p for p in await self.bus.seen_nodes()}
            nodes = []
            for row in self.state.all_nodes():
                nid = row["node_id"]
                last = row.get("last_seen") or 0
                online = (now - last) < 30 if last else (nid in seen)
                try:
                    caps = json.loads(row.get("capabilities") or "[]")
                except Exception:
                    caps = [c for c in (row.get("capabilities") or "").split(",") if c]
                nodes.append({
                    "id": nid, "name": row.get("name") or nid,
                    "capabilities": caps,
                    "online": bool(online),
                    "last_seen_s": round(now - last, 1) if last else None,
                    "working": nid in workers and workers[nid].queue_depth > 0,
                    "queue": workers.get(nid).queue_depth if nid in workers else 0,
                })
            status = {
                "ts": now,
                "alfred": {
                    "model": self.cfg.get("core", {}).get("model", "?"),
                    "bus": self.cfg.get("bus", {}).get("kind", "?"),
                    "projects": len(self.state.active_projects()),
                    "pending_actions": len(self.state.pending_actions()),
                },
                "nodes": nodes,
                "in_flight": self.state.in_flight_tasks(),
            }
            path = pathlib.Path.home() / ".alfred" / "status.json"
            path.parent.mkdir(exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(status))
            os.replace(tmp, path)
        except Exception:
            if not getattr(self, "_status_err_logged", False):
                self._status_err_logged = True
                log.exception("status heartbeat failed (dashboard will be blind)")

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
