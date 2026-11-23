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
import json
import logging
import os
import re
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from alfred import llm
from alfred.bus.base import Bus
from alfred.contracts import (
    Assignment, NodeProfile, Task, TaskResult, WorkerAdvert, _new_id,
)
from alfred.core import enrollment, planner, scheduler
from alfred.core.hands import Hands
from alfred.core.sight import Sight
from alfred.core.state import State
from alfred.probe import node_id as local_node_id
from alfred.worker.handlers import registered_capabilities

log = logging.getLogger("alfred.core")

PERSONA_PATH = Path(__file__).with_name("persona.md")
STALE_PROJECT_DAYS = 7

# Capabilities that change the owner's machine or touch his desktop: parked
# for approval, never dispatched on Alfred's say-so.
GATED = frozenset({"os.apply", "ui.act"})


def hands_outcome(result: TaskResult) -> str:
    """The short past-tense phrase for the Hands card, next to the description
    it already shows."""
    if not result.ok:
        return f"failed: {result.error}"
    return str(result.data.get("did") or result.summary or "done")


def describe_gated(task: Task) -> str:
    """The sentence on the approval card."""
    action = str(task.inputs.get("action", "?"))
    args = task.inputs.get("args") or {}
    if task.capability == "ui.act":
        from alfred.worker.handlers.hands import describe_action
    else:
        from alfred.worker.handlers.oscontrol import describe_action
    return describe_action(action, args)


class Alfred:
    def __init__(self, bus: Bus, cfg: dict) -> None:
        self.bus = bus
        self.cfg = cfg
        self.state = State(cfg["core"]["state_db"])
        current_project = self.state.setting("current_project")
        current = self.state.project(current_project) if current_project else None
        self.active_project: str | None = (
            current_project if current and current["status"] == "active" else None
        )
        self.persona = PERSONA_PATH.read_text() if PERSONA_PATH.exists() else ""
        self.local_worker_id: str = cfg["worker"]["id"]
        self.local_node_id: str = local_node_id()
        self._bg_tasks: set = set()   # background learners, kept referenced
        self._online_nodes: set[str] = set()   # heartbeating machines, last tick
        self._offline_nodes: set[str] = set()  # assigned machines absent, last tick
        self._assigned_seen: set[str] | None = None  # assignments known last tick
        self._talking = False   # a reply is being composed; the eyes hold still
        self._asked_at = 0.0    # when the message being answered arrived
        self.sight = Sight(cfg, self.state, busy=lambda: self._talking)
        self.hands = Hands()

    # ---- dispatch --------------------------------------------------------

    async def _dispatch(self, task: Task) -> TaskResult:
        """Submit, wait, retry, and let the local worker take over if nobody
        else does. The fallback is not a special path — the desktop's own
        worker claims from the same queue as everyone else, just later."""
        # OS changes and touches on the desktop never run on Alfred's
        # judgment. Park them; the owner's approval (shell button or
        # /approve) is what dispatches them. The one exception is a standing
        # "drive for N minutes" grant on the hands, and STOP ends that.
        granted = False
        if task.capability in GATED and not task.inputs.get("_approved"):
            description = describe_gated(task)
            if task.capability == "ui.act" and self.hands.stopped_since(self._asked_at):
                return TaskResult(
                    task_id=task.id, worker_id="owner-stop", status="rejected",
                    error=f"'{description}' not done: the owner pressed STOP",
                )
            if task.capability == "ui.act" and self.hands.driving:
                task.inputs["_approved"] = True
                task.inputs["_granted"] = True
                granted = True
            else:
                action_id = self.state.park_action(
                    task.project_id, description, task.to_json())
                return TaskResult(
                    task_id=task.id, worker_id="owner-approval", status="pending",
                    summary=f"'{description}' is queued as pending change #{action_id}, "
                            "awaiting your approval.",
                )
        if granted:
            result = await self._dispatch_now(task)
            self.hands.record(description, hands_outcome(result), result.ok)
            outcome = result.summary if result.ok else f"failed: {result.error}"
            self.state.notice("hands", f"Hands (driving): {outcome}", task.project_id)
            return result
        return await self._dispatch_now(task)

    async def _dispatch_now(self, task: Task) -> TaskResult:
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

    async def _run_graph(self, tasks: list[Task],
                         offline: dict[str, str] | None = None) -> dict[str, TaskResult]:
        """Execute the DAG, running everything whose dependencies are met
        concurrently. A flat list would serialise work with no reason to be
        sequential.

        Steps that need a capability only an offline machine offers are
        refused here, not queued: nothing would claim them, and the owner is
        better served by "the zenbook is off" now than a timeout later."""
        results: dict[str, TaskResult] = {}
        remaining = {t.id: t for t in tasks}
        for task in tasks:
            machine = (offline or {}).get(task.capability)
            if machine:
                results[task.id] = TaskResult(
                    task_id=task.id, worker_id="none", status="rejected",
                    error=f"needs {task.capability}, which only {machine} offers, "
                          f"and {machine} is offline; nothing was done",
                )
                remaining.pop(task.id)

        while remaining:
            ready = [
                t for t in remaining.values()
                if all(dep in results for dep in t.depends_on)
            ]
            for task in ready[:]:
                blocked = [d for d in task.depends_on
                           if results[d].status == "rejected" and results[d].worker_id == "none"]
                if blocked:
                    results[task.id] = TaskResult(
                        task_id=task.id, worker_id="none", status="rejected",
                        error="skipped: depends on a step that could not run",
                    )
                    remaining.pop(task.id)
                    ready.remove(task)
            if not ready and not remaining:
                break
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

    async def _offline_hands(self, available: list[str]) -> dict[str, str]:
        """Capabilities the household has on paper but not on the bus right
        now, mapped to the machine that offers them: a sleeping Zenbook takes
        code.write with it. The planner still plans for them so the refusal
        can name the step and the machine, instead of quietly planning around
        a hole."""
        live = await self.live_workers_by_node()
        out: dict[str, str] = {}
        for row in self.state.all_nodes():
            if row["node_id"] in live:
                continue
            for cap in json.loads(row.get("capabilities") or "[]"):
                if cap not in available:
                    out.setdefault(cap, row.get("name") or row["node_id"])
        return out

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

    _CHAT_ONLY = frozenset({
        "", "hi", "hello", "hey", "good morning", "good afternoon",
        "good evening", "good night", "how are you", "thanks", "thank you",
        "got it",
    })

    def _might_need_work(self, message: str) -> bool:
        return message.casefold().strip().rstrip(".!?") not in self._CHAT_ONLY

    def _recent_conversation(self, project_id: str | None) -> str:
        return "\n".join(
            f"{role}: {text if len(text) <= 400 else text[:250] + ' … ' + text[-147:]}"
            for role, text in self.state.recent_turns(project_id, limit=6)
        )

    def _conversation_briefing(self, project_id: str | None,
                               base: str | None = None) -> tuple[str, list[dict]]:
        briefing = base if base is not None else (
            self.state.briefing(project_id) if project_id else "")
        for part in (self._owner_briefing(), self.sight.briefing(), self.hands.briefing()):
            if part:
                briefing = (briefing + "\n\n" + part) if briefing else part
        pending = self.state.undelivered()
        if pending:
            briefing += (
                "\n\nHappened since your last reply (these are facts and supersede "
                "anything said earlier in the conversation; mention briefly, and "
                "only if relevant to what the user is saying):\n"
                + "\n".join(f"  - {notice['body']}" for notice in pending)
            )
        return briefing, pending

    async def converse(self, message: str, project_id: str | None = None,
                       attachments: list[str] | None = None) -> str:
        self._asked_at = time.time()
        switched = self.sight.command(message) or self.hands.command(message)
        if switched:
            self._remember(message, switched, [], project_id)
            return switched
        self._talking = True
        try:
            pending = self.state.undelivered()
            command = await self._project_command(message, project_id)
            if command is not None:
                reply, project_id_used = command
                self._remember(message, reply, pending, project_id_used)
                return reply
            return await self._converse(message, project_id, attachments)
        finally:
            self._talking = False

    async def _converse(self, message: str, project_id: str | None,
                        attachments: list[str] | None) -> str:
        recent = self._recent_conversation(project_id)
        briefing, pending = self._conversation_briefing(project_id)

        offline: dict[str, str] = {}
        if attachments:
            available = await self._network_capabilities()
            tasks = self._attachment_tasks(attachments, message, available=set(available))
        elif self._asks_about_own_screen(message):
            # A fresh look beats a twenty-second-old glance, and the owner's
            # own screen never goes through the planner or a worker.
            reply = await self._speak(message, briefing, recent, [await self._fresh_look(message)])
            self._remember(message, reply, pending, project_id)
            return reply
        elif self._might_need_work(message):
            available = await self._network_capabilities()
            offline = await self._offline_hands(available)
            tasks = await planner.plan(
                message, briefing, self.cfg, project_id,
                available=sorted(set(available) | set(offline)),
                recent_conversation=recent,
            )
        else:
            tasks = []

        if not tasks:
            # Nothing worth delegating. Answer directly.
            reply = await self._speak(message, briefing, recent, [])
            self._remember(message, reply, pending, project_id)
            self._learn_in_background(message)
            return reply

        log.info("plan: %d tasks across %s", len(tasks),
                 ", ".join(sorted({t.capability for t in tasks})))
        reply, _ = await self._carry_out(message, tasks, offline, briefing, recent)
        self._remember(message, reply, pending, project_id)
        self._learn_in_background(message)
        return reply

    async def _carry_out(
        self, message: str, tasks: list[Task], offline: dict[str, str],
        briefing: str, recent: str,
    ) -> tuple[str, list[tuple[Task, TaskResult, str]]]:
        results = await self._run_graph(tasks, offline)
        checked: list[str] = []
        failed: list[str] = []
        records: list[tuple[Task, TaskResult, str]] = []
        for task in tasks:
            result = results[task.id]
            label = planner.outcome(task, result)
            _, note = planner.verify(task, result)
            records.append((task, result, label))
            checked.append(
                f"[{task.capability}] {label} ({note})\n"
                f"{result.summary or result.error}"
            )
            if label == "failed":
                failed.append(f"{task.capability}: {note}")
        if failed:
            checked.insert(0, (
                f"{len(failed)} of {len(tasks)} steps FAILED and the user must be told "
                "which, and why, in plain words:\n  - " + "\n  - ".join(failed)
            ))

        prefix = "Next step for this project: "
        if message.startswith(prefix) and tasks:
            step_body = message[len(prefix):]
            outcomes = ", ".join(
                f"{task.capability} {label}" for task, _, label in records)
            successful = all(
                label in {"verified", "done, unverified"}
                for _, _, label in records)
            open_steps = self.state.steps(tasks[0].project_id, "next")
            if successful:
                removed = False
                remaining = []
                for step in open_steps:
                    if not removed and step["body"].casefold() == step_body.casefold():
                        removed = True
                        continue
                    remaining.append(step)
                open_steps = remaining
            state_line = f"Project state: {step_body} — {outcomes}."
            if open_steps:
                state_line += f" Next open step: {open_steps[0]['body']}."
            else:
                state_line += " No open next step remains."
            checked.append(state_line)

        reply = await self._speak(message, briefing, recent, checked)
        reply = self._attach_deliverables(reply, tasks, results, self.local_worker_id)
        return reply, records

    def _current_project_id(self, project_id: str | None) -> str | None:
        for candidate in (project_id, self.active_project):
            project = self.state.project(candidate) if candidate else None
            if project and project["status"] == "active":
                return candidate
        return None

    def _active_project_listing(self) -> str:
        projects = self.state.active_projects()
        if not projects:
            return "There are no active projects yet."
        return "Active projects: " + ", ".join(
            f"{project['name']} ({project['id']})" for project in projects)

    def _project_candidates(self, query: str) -> list[dict]:
        folded = query.casefold()
        return [
            project for project in self.state.active_projects()
            if folded in project["name"].casefold()
        ]

    async def _project_command(
        self, message: str, project_id: str | None,
    ) -> tuple[str, str | None] | None:
        command = message.strip()
        command = re.sub(r"^alfred(?:,\s*|\s+)", "", command, flags=re.IGNORECASE)
        plain = command.strip().rstrip(".!?").strip()
        current_pid = self._current_project_id(project_id)

        resume = re.fullmatch(
            r"(?:continue|resume|keep going|carry on|pick up)(?:\s+working)?"
            r"(?:\s+(?:(?:on|with)\s+)?(?:project\s+)?(.+))?",
            plain, re.IGNORECASE)
        if resume:
            return await self._resume((resume.group(1) or "").strip(), current_pid)
        resume = re.fullmatch(r"let(?:'|’)s continue(?:\s+(.+))?", plain, re.IGNORECASE)
        if resume:
            return await self._resume((resume.group(1) or "").strip(), current_pid)

        status = re.fullmatch(
            r"(?:where did we leave off|what(?:'|’)s the status)"
            r"(?:\s+(?:of|on|for)\s+(?:project\s+)?(.+))?",
            plain, re.IGNORECASE)
        if status:
            return await self._project_status(
                (status.group(1) or "").strip(), current_pid)

        next_step = re.fullmatch(r"(?:add\s+)?next step\s*:\s*(.+)", command,
                                 re.IGNORECASE | re.DOTALL)
        if next_step:
            body = next_step.group(1).strip()
            if not current_pid:
                return self._ask_for_project(), None
            self.state.log_step(current_pid, "next", body)
            steps = self.state.steps(current_pid, "next", limit=5)
            listing = "\n".join(
                f"{index}. {step['body']}" for index, step in enumerate(steps, 1))
            return f"Added next step. Open next steps:\n{listing}", current_pid

        problem = re.fullmatch(r"problem\s*:\s*(.+)", command,
                               re.IGNORECASE | re.DOTALL)
        if problem:
            if not current_pid:
                return self._ask_for_project(), None
            body = problem.group(1).strip()
            self.state.log_step(current_pid, "problem", body)
            return f"Recorded the problem: {body}", current_pid

        decision = re.fullmatch(
            r"decision\s*:\s*(.+?)\s+because\s+(.+)", command,
            re.IGNORECASE | re.DOTALL)
        if decision:
            if not current_pid:
                return self._ask_for_project(), None
            choice, rationale = decision.group(1).strip(), decision.group(2).strip()
            self.state.record_decision(current_pid, choice, rationale)
            return f"Recorded the decision: {choice} ({rationale}).", current_pid

        done = re.fullmatch(r"done\s*:\s*(.+)", command,
                            re.IGNORECASE | re.DOTALL)
        if done:
            if not current_pid:
                return self._ask_for_project(), None
            body = done.group(1).strip()
            self.state.log_step(current_pid, "done", body)
            matching = next(
                (step for step in self.state.steps(current_pid, "next")
                 if step["body"].casefold() == body.casefold()),
                None,
            )
            if matching:
                self.state.close_step(matching["id"], closed_by_kind="done")
            return f"Recorded as done: {body}", current_pid

        new_project = re.fullmatch(r"new project\s+(.+)", plain, re.IGNORECASE)
        if new_project:
            specification = new_project.group(1).strip()
            root = None
            root_matches = list(re.finditer(r"\s+in\s+(.+)$", specification,
                                            re.IGNORECASE))
            if root_matches:
                root_match = root_matches[-1]
                root = root_match.group(1).strip()
                specification = specification[:root_match.start()].strip()
            if ":" in specification:
                name, goal = (part.strip() for part in specification.split(":", 1))
            else:
                name, goal = specification, ""
            if not name:
                return "Please provide a name for the new project.", current_pid
            if root:
                root_path = Path(root).expanduser()
                if not root_path.is_dir():
                    return (f"I didn't create {name}: {root} isn't an existing "
                            "directory."), current_pid
                root = str(root_path.resolve())
            pid = self.state.create_project(name, goal, root=root)
            self.active_project = pid
            self.state.set_setting("current_project", pid)
            suffix = f" Root: {root}." if root else ""
            return f"Created and switched to project {name} ({pid}).{suffix}", pid

        return None

    def _ask_for_project(self) -> str:
        return "Which project should I use? " + self._active_project_listing()

    async def _project_status(
        self, name: str, fallback_pid: str | None,
    ) -> tuple[str, str | None]:
        query = name or fallback_pid
        project = self.state.find_project(query) if query else None
        if project and project["status"] != "active":
            project = None
        if not project:
            if name:
                candidates = self._project_candidates(name)
                if len(candidates) > 1:
                    listing = ", ".join(
                        f"{item['name']} ({item['id']})" for item in candidates)
                    return f"That name is ambiguous: {listing}. Which project?", fallback_pid
                return f"I couldn't find an active project named {name}. " + (
                    self._active_project_listing()), fallback_pid
            return self._ask_for_project(), fallback_pid

        brief = self.state.resume_brief(project["id"])
        briefing, _ = self._conversation_briefing(project["id"], base=brief)
        recent = self._recent_conversation(project["id"])
        reply = await self._speak(
            f"Summarize the current status of {project['name']} from the project brief. "
            "Take no action.",
            briefing, recent, [],
        )
        return reply, project["id"]

    async def _resume(
        self, name: str, fallback_pid: str | None,
    ) -> tuple[str, str | None]:
        query = name or fallback_pid
        project = self.state.find_project(query) if query else None
        if project and project["status"] != "active":
            project = None
        if not project:
            if name:
                candidates = self._project_candidates(name)
                if len(candidates) > 1:
                    listing = ", ".join(
                        f"{item['name']} ({item['id']})" for item in candidates)
                    return f"That project name is ambiguous: {listing}. Which one?", fallback_pid
                return f"I couldn't find an active project named {name}. " + (
                    self._active_project_listing()), fallback_pid
            return self._ask_for_project(), fallback_pid

        pid = project["id"]
        self.active_project = pid
        self.state.set_setting("current_project", pid)
        brief = self.state.resume_brief(pid)
        next_steps = self.state.steps(pid, "next")
        briefing, _ = self._conversation_briefing(pid, base=brief)
        recent = self._recent_conversation(pid)
        if not next_steps:
            reply = await self._speak(
                "Summarize this project briefly and ask what the next step should be.",
                briefing, recent, [],
            )
            return reply, pid

        step_body = next_steps[0]["body"]
        request = f"Next step for this project: {step_body}"
        available = await self._network_capabilities()
        offline = await self._offline_hands(available)
        tasks = await planner.plan(
            request, briefing, self.cfg, pid,
            available=sorted(set(available) | set(offline)),
            recent_conversation=recent,
        )
        if not tasks:
            self.state.log_step(pid, "note", f"could not plan step: {step_body}")
            reply = await self._speak(
                f"The next step is {step_body!r}, but I couldn't plan it. Explain that "
                "and ask for a more specific step.",
                briefing, recent, [],
            )
            return reply, pid

        reply, records = await self._carry_out(
            request, tasks, offline, briefing, recent)
        labels = ", ".join(label for _, _, label in records)
        task_ids = [task.id for task, _, _ in records]
        if records and all(label in {"verified", "done, unverified"}
                           for _, _, label in records):
            self.state.close_step(next_steps[0]["id"])
            summaries = "; ".join(
                f"{task.capability}: {(result.summary or result.error or '')[:200]}"
                for task, result, _ in records
            )
            self.state.log_step(
                pid, "done", f"{step_body} — {labels}: {summaries}", task_ids)
        elif any(label == "failed" for _, _, label in records):
            notes = "; ".join(
                f"{task.capability}: {(result.error or result.summary or 'failed')[:200]}"
                for task, result, label in records if label == "failed"
            )
            self.state.log_step(
                pid, "problem", f"{step_body} failed: {notes}", task_ids)
        elif any(label == "awaiting approval" for _, _, label in records):
            for task, result, label in records:
                if label != "awaiting approval":
                    continue
                pending = re.search(r"pending change #(\d+)", result.summary)
                if pending:
                    change = pending.group(1)
                else:
                    actions = [
                        action for action in self.state.pending_actions()
                        if action["project_id"] == pid
                    ]
                    change = str(actions[-1]["id"]) if actions else "unknown"
                self.state.log_step(
                    pid, "note",
                    f"awaiting approval: {step_body} (pending change #{change})",
                    task_ids,
                )
        return reply, pid

    def _asks_about_own_screen(self, message: str) -> bool:
        """"What's on my screen?" -- but "what's on the zenbook's screen" is a
        request about another machine and goes to the planner as before."""
        if not self.sight.watching or not self.sight.asks_about_screen(message):
            return False
        m = message.lower()
        others = (self._online_nodes | self._offline_nodes) - {self.local_node_id}
        return not any(n.lower() in m for n in others)

    async def _fresh_look(self, question: str) -> str:
        try:
            seen = await self.sight.glance(question)
        except Exception as exc:
            return ("[eyes] FAILED (could not look at the screen just now)\n"
                    f"{exc}")
        return ("[eyes] OK (a fresh look at the owner's screen, taken just now; "
                f"answer from this, not from older glances)\n{seen}")

    @staticmethod
    def _attach_deliverables(reply: str, tasks: list[Task],
                             results: dict[str, TaskResult], local_worker_id: str) -> str:
        """Copy the owner asked for is handed over in full, however the
        narration treated it, and every file produced is named so he can
        find it — including which machine it is on, when that is not this
        one. Small models like to describe a draft instead of pasting it."""
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
        files = []
        for task in tasks:
            result = results[task.id]
            if not result.ok:
                continue
            where = "" if result.worker_id in {local_worker_id, ""} else f"  (on {result.worker_id})"
            paths = [url2pathname(urlparse(u).path) for u in result.artifacts if u.startswith("file:")]
            files += [p + where for p in paths if p not in reply]  # the model may have named it already
        if files:
            extra.append("Files:\n" + "\n".join(f"  {p}" for p in files))
        return reply if not extra else reply.rstrip() + "\n\n" + "\n\n".join(extra)

    async def _speak(self, message: str, briefing: str, recent: str,
                     findings: list[str]) -> str:
        """The only place in the entire system that produces user-facing text.

        Workers return artifacts. Alfred does all the narrating. That is the
        whole of what "there is only one Alfred" means in code.
        """
        if findings:
            body = "\n\n".join(findings)
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
        else:
            # Plain conversation. No worker ran, and the reply must neither
            # mention that machinery nor pretend anything was done.
            prompt = (
                f"{briefing}\n\nRecent conversation:\n{recent}\n\n"
                f"User: {message}\n\n"
                "Reply to the user directly and naturally, as in conversation; "
                "never mention tasks, workers or requests. Nothing was done, made "
                "or looked up for this message — if the user asked for that, say "
                "so plainly and say what you would need, or that it is beyond you; "
                "never imply it happened. Use earlier turns to resolve references. "
                "If the intended action or item is unclear, ask one specific question "
                "rather than guessing."
            )
        return await llm.complete(prompt, self.cfg, system=self.persona, timeout=600)

    def _remember(self, message: str, reply: str, delivered: list[dict],
                  project_id: str | None) -> None:
        self.state.log_turn("User", message, project_id, at=self._asked_at)
        self.state.log_turn("Alfred", reply, project_id)
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

    async def live_workers_by_node(self) -> dict[str, WorkerAdvert]:
        """Heartbeating workers keyed by the machine they run on."""
        return {
            w.node_id or w.worker_id: w
            for w in await self.bus.workers()
            if w.capabilities
        }

    async def unassigned_nodes(self) -> list[NodeProfile]:
        """Machines that have announced themselves but have no job yet.

        A machine whose worker already heartbeats with capabilities — one
        started from its own config file rather than enrolled — has a job,
        and is not nagged about."""
        working = await self.live_workers_by_node()
        out = []
        for profile in await self.bus.seen_nodes():
            if profile.node_id in working:
                continue
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
        task.inputs["_approved"] = True           # the mark this path and a grant set
        # The owner's approval is taken atomically once, which is the very
        # guarantee the key exists to provide.
        task.idempotency_key = task.idempotency_key or f"approved-action:{action_id}"
        result = await self._dispatch(task)
        self.state.settle_action(action_id, result.ok, result.summary or result.error or "")
        outcome = result.summary if result.ok else f"failed: {result.error}"
        if task.capability == "ui.act":
            self.hands.record(row["description"], hands_outcome(result), result.ok)
            self.state.notice("hands", f"Hands #{action_id}: {outcome}", row["project_id"])
        else:
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
        try:
            now = time.time()
            workers = await self.live_workers_by_node()
            seen = {p.node_id: p for p in await self.bus.seen_nodes()}
            nodes = []
            for row in self.state.all_nodes():
                nid = row["node_id"]
                last = seen[nid].seen_at if nid in seen else (row.get("last_seen") or 0)
                online = nid in seen or nid in workers
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
            path = Path.home() / ".alfred" / "status.json"
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

        # A worker that arrived with its own config file is as much a member
        # of the household as an enrolled one: record it so /nodes, the
        # dashboard and the enrollment proposals know what it covers.
        seen = {p.node_id: p for p in await self.bus.seen_nodes()}
        live = await self.live_workers_by_node()
        for node_id, worker in live.items():
            known = self.state.known_node(node_id)
            if known and json.loads(known.get("capabilities") or "[]") == worker.capabilities:
                continue
            profile = seen.get(node_id)
            if profile is None:
                continue
            if not known and node_id != self.local_node_id:
                self.state.notice(
                    "new_node",
                    f"{worker.worker_id} ({profile.hostname}) joined offering "
                    f"{', '.join(worker.capabilities)}",
                )
            self.state.record_node(
                Assignment(node_id=node_id, name=worker.worker_id,
                           capabilities=list(worker.capabilities),
                           note="configured on the machine itself"),
                profile,
            )

        # Comings and goings, once per transition: the owner hears that the
        # zenbook dropped off before he asks for the work only it can do.
        online = set(live)
        assigned = {
            r["node_id"]: r for r in self.state.all_nodes()
            if r.get("capabilities") not in (None, "", "[]")
        }
        for node_id in (self._online_nodes - online) & set(assigned):
            self.state.notice("node_offline",
                              f"{assigned[node_id].get('name') or node_id} went offline")
        for node_id in online & self._offline_nodes:
            self.state.notice("node_online", f"{live[node_id].worker_id} is back online")
        # A machine enrolled this tick has not "gone" anywhere: its first
        # heartbeat is a join (already announced), not a return. Likewise the
        # first tick after boot, before anyone has had time to heartbeat.
        fresh = set(assigned) if self._assigned_seen is None else set(assigned) - self._assigned_seen
        self._online_nodes = online
        self._offline_nodes = set(assigned) - online - fresh
        self._assigned_seen = set(assigned)

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
