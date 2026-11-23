import asyncio
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from alfred.bus.local import LocalBus
from alfred.contracts import Task, TaskResult
from alfred.core import planner
from alfred.core.alfred import Alfred
from alfred.core.state import State
from alfred.worker.handlers.code import code_test
from run_server import Bridge


class ProjectContinuityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.db_path = str(self.base / "state.db")
        self.project_root = self.base / "project"
        self.project_root.mkdir()
        self.states = []
        self.state = self.make_state()

    def tearDown(self):
        for state in self.states:
            state.db.close()
        self.temp_dir.cleanup()

    def make_state(self):
        state = State(self.db_path)
        self.states.append(state)
        return state

    def make_core(self):
        cfg = {
            "core": {
                "state_db": self.db_path,
                "artifact_dir": str(self.base / "artifacts"),
                "ollama_url": "http://127.0.0.1:9",
            },
            "worker": {"id": "project-continuity-test"},
            "sight": {"enabled": False},
        }
        core = Alfred(LocalBus(), cfg)
        self.states.append(core.state)
        return core

    def test_old_projects_table_gets_root_once(self):
        old_db = str(self.base / "old.db")
        with sqlite3.connect(old_db) as db:
            db.execute(
                "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                "goal TEXT, status TEXT DEFAULT 'active', created_at REAL, "
                "updated_at REAL)"
            )
        first = State(old_db)
        columns = [row["name"] for row in first.db.execute(
            "PRAGMA table_info(projects)")]
        self.assertEqual(columns.count("root"), 1)
        first.db.close()

        second = State(old_db)
        columns = [row["name"] for row in second.db.execute(
            "PRAGMA table_info(projects)")]
        self.assertEqual(columns.count("root"), 1)
        second.db.close()

    def test_find_project_matches_id_names_and_unique_substrings(self):
        alpha = self.state.create_project("Alpha Workspace")
        self.state.create_project("Beta Workspace")
        self.state.create_project("Gamma")

        self.assertEqual(self.state.find_project(alpha)["id"], alpha)
        self.assertEqual(self.state.find_project("alpha workspace")["id"], alpha)
        self.assertEqual(self.state.find_project("Alpha")["id"], alpha)
        self.assertEqual(self.state.find_project("Gamma")["name"], "Gamma")
        self.assertIsNone(self.state.find_project("workspace"))

    def test_project_files_excludes_junk_directories_and_sorts_by_mtime(self):
        root = self.project_root
        old_file = root / "old.txt"
        new_file = root / "new.txt"
        old_file.write_text("old")
        new_file.write_text("new")
        nested = root / "src"
        nested.mkdir()
        nested_file = nested / "main.py"
        nested_file.write_text("code")
        junk_names = (
            ".hidden", ".git", "node_modules", "__pycache__", ".venv", "venv",
        )
        for name in junk_names:
            directory = root / name
            directory.mkdir()
            (directory / "ignored.txt").write_text(name)

        os.utime(old_file, (10, 10))
        os.utime(nested_file, (20, 20))
        os.utime(new_file, (30, 30))
        pid = self.state.create_project("Files", root=str(root))

        self.assertEqual(
            self.state.project_files(pid),
            ["new.txt", "src/main.py", "old.txt"],
        )
        self.assertEqual(self.state.project_files("missing"), [])

    def test_resume_brief_contains_project_work_and_numbered_next_steps(self):
        pid = self.state.create_project(
            "Continuity", "Ship the prototype", root=str(self.project_root))
        (self.project_root / "main.py").write_text("print('hello')")
        self.state.record_decision(pid, "Use Python", "The prototype is small")
        self.state.log_step(pid, "done", "Create the project skeleton")
        self.state.log_step(pid, "problem", "The service URL is unknown")
        self.state.log_step(pid, "next", "Add a health check")
        self.state.log_step(pid, "next", "Write an integration test")

        brief = self.state.resume_brief(pid)

        for expected in (
            "Continuity", "Ship the prototype", str(self.project_root),
            "main.py", "Use Python", "Create the project skeleton",
            "The service URL is unknown", "1. Add a health check",
            "2. Write an integration test",
        ):
            self.assertIn(expected, brief)
        self.assertLessEqual(len(brief), 3000)

    async def _resume_with_code_test(self, failing=False, core=None, pid=None):
        if core is None:
            core = self.make_core()
        if pid is None:
            project_name = "Failing Project" if failing else "Passing Project"
            pid = core.state.create_project(
                project_name, "Run its unit tests", root=str(self.project_root))
            step_id = core.state.log_step(pid, "next", "Run the test suite")
        else:
            project_name = core.state.project(pid)["name"]
            step_id = core.state.steps(pid, "next")[0]["id"]
        if failing:
            test_source = (
                "import unittest\n"
                "class FailingTest(unittest.TestCase):\n"
                "    def test_failure(self):\n"
                "        self.assertEqual(1, 2)\n"
            )
        else:
            test_source = (
                "import unittest\n"
                "class PassingTest(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertEqual(1, 1)\n"
            )
        (self.project_root / "test_ok.py").write_text(test_source)
        task = Task(
            capability="code.test",
            prompt="Run the project unit tests",
            project_id=pid,
            inputs={
                "workdir": str(self.project_root),
                "command": "python -m unittest -q",
            },
        )
        network = patch.object(
            core, "_network_capabilities", new_callable=AsyncMock,
            return_value=["code.test"],
        )
        offline = patch.object(
            core, "_offline_hands", new_callable=AsyncMock, return_value={})
        plan = patch.object(
            planner, "plan", new_callable=AsyncMock, return_value=[task])
        network.start()
        offline.start()
        plan.start()

        async def run_real_handler(planned_task):
            return await code_test(planned_task, core.cfg)

        dispatch = patch.object(
            core, "_dispatch_now", new_callable=AsyncMock,
            side_effect=run_real_handler,
        )
        dispatch.start()
        spoken = []

        async def speak(message, briefing, recent, findings):
            spoken.append(findings)
            return "\n".join(findings) or "No worker findings."

        speak_patch = patch.object(
            core, "_speak", new_callable=AsyncMock, side_effect=speak)
        speak_patch.start()
        try:
            reply = await core.converse(
                f"continue working on {project_name}")
        finally:
            speak_patch.stop()
            dispatch.stop()
            plan.stop()
            offline.stop()
            network.stop()
        steps = core.state.steps(pid, "next", open_only=False)
        return core, pid, step_id, reply, spoken, steps

    async def test_resume_runs_real_code_test_and_logs_verified_completion(self):
        core, pid, step_id, reply, spoken, steps = await self._resume_with_code_test()

        self.assertEqual(core.active_project, pid)
        step = next(item for item in steps if item["id"] == step_id)
        self.assertEqual(step["status"], "closed")
        done = core.state.steps(pid, "done", open_only=False)[0]
        self.assertIn("verified", done["body"])
        self.assertEqual(len(done["task_ids"]), 1)
        self.assertIn("[code.test] verified", reply)
        self.assertTrue(any("Project state:" in finding for finding in spoken[0]))

    async def test_failing_code_test_logs_problem_and_leaves_step_open(self):
        core, pid, step_id, reply, _, steps = await self._resume_with_code_test(failing=True)

        step = next(item for item in steps if item["id"] == step_id)
        self.assertEqual(step["status"], "open")
        problem = core.state.steps(pid, "problem")[0]
        self.assertIn("Run the test suite failed:", problem["body"])
        self.assertIn("tests failed", problem["body"])
        self.assertIn("[code.test] failed", reply)

    async def test_strict_resume_plan_rejections_keep_step_open(self):
        core = self.make_core()
        pid = core.state.create_project(
            "Strict Resume", "Complete the partial plan",
            root=str(self.project_root))
        step_id = core.state.log_step(pid, "next", "Build the protected feature")
        entries = [
            {"ref": "test", "capability": "code.test",
             "prompt": "Run the project tests"},
            {"ref": "bad", "capability": "not.real",
             "prompt": "Do an unsupported action"},
        ]
        result = TaskResult(
            task_id="unused", worker_id="test", summary="passed",
            data={"exit_code": 0})

        with (
            patch.object(core, "_network_capabilities", new_callable=AsyncMock,
                         return_value=["code.test"]),
            patch.object(core, "_offline_hands", new_callable=AsyncMock,
                         return_value={}),
            patch("alfred.core.planner.llm.complete_json",
                  new_callable=AsyncMock, return_value=entries) as complete,
            patch.object(core, "_dispatch_now", new_callable=AsyncMock,
                         return_value=result) as dispatch,
            patch.object(core, "_speak", new_callable=AsyncMock,
                         return_value="Please make the step more specific."),
        ):
            reply = await core.converse("continue working on Strict Resume")

        self.assertEqual(complete.await_count, 3)
        dispatch.assert_not_awaited()
        self.assertIn("more specific", reply)
        step = next(item for item in core.state.steps(
            pid, "next", open_only=False) if item["id"] == step_id)
        self.assertEqual(step["status"], "open")
        note = core.state.steps(pid, "note")[0]
        self.assertEqual(note["body"],
                         "could not plan step: Build the protected feature")

    async def test_resume_saves_success_before_speak_failure(self):
        core = self.make_core()
        pid = core.state.create_project(
            "Speak Failure", "Save work before replying",
            root=str(self.project_root))
        step_id = core.state.log_step(pid, "next", "Run the project tests")
        task = Task(
            capability="code.test",
            prompt="Run the project tests",
            project_id=pid,
            inputs={"workdir": str(self.project_root),
                    "command": "python -m unittest -q"},
        )
        result = TaskResult(
            task_id=task.id, worker_id="test", summary="test suite passed",
            data={"exit_code": 0})

        with (
            patch.object(core, "_network_capabilities", new_callable=AsyncMock,
                         return_value=["code.test"]),
            patch.object(core, "_offline_hands", new_callable=AsyncMock,
                         return_value={}),
            patch.object(planner, "plan", new_callable=AsyncMock,
                         return_value=[task]),
            patch.object(core, "_dispatch_now", new_callable=AsyncMock,
                         return_value=result),
            patch.object(core, "_speak", new_callable=AsyncMock,
                         side_effect=RuntimeError("synthesis failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthesis failed"):
                await core.converse("continue working on Speak Failure")

        step = next(item for item in core.state.steps(
            pid, "next", open_only=False) if item["id"] == step_id)
        self.assertEqual(step["status"], "closed")
        done = core.state.steps(pid, "done", open_only=False)[0]
        self.assertIn("Run the project tests — verified", done["body"])

    async def test_ordinary_continue_and_status_messages_stay_conversational(self):
        core = self.make_core()
        pid = core.state.create_project("Rover", "Drive around the garden")
        core.active_project = pid
        core.state.set_setting("current_project", pid)
        messages = (
            "continue",
            "keep going",
            "pick up milk",
            "resume the download",
            "what's the status",
            "what's the status of my order",
        )

        with patch.object(
            core, "_converse", new_callable=AsyncMock, return_value="conversation"
        ) as converse:
            for message in messages:
                self.assertEqual(
                    await core.converse(message, project_id=pid), "conversation")

        self.assertEqual(converse.await_count, len(messages))
        self.assertEqual(core.active_project, pid)
        self.assertEqual(core.state.setting("current_project"), pid)

        explicit_status = await core.converse(
            "what's the status of project Nonexistent", project_id=pid)
        self.assertIn("couldn't find an active project named Nonexistent",
                      explicit_status)
        self.assertIn("Rover", explicit_status)

    async def test_implicit_resume_resolves_project_and_continue_working_uses_current(self):
        core = self.make_core()
        rover = core.state.create_project("Rover", "Drive around the garden")
        with (
            patch.object(core, "_speak", new_callable=AsyncMock,
                         return_value="Rover has no open next step."),
            patch.object(core, "_converse", new_callable=AsyncMock) as converse,
        ):
            await core.converse("resume rover")
            self.assertEqual(core.active_project, rover)
            self.assertEqual(core.state.setting("current_project"), rover)
            await core.converse("continue working")

        converse.assert_not_awaited()
        self.assertEqual(core.active_project, rover)

    async def test_successful_retry_closes_prior_step_problem(self):
        core, pid, step_id, _, _, _ = await self._resume_with_code_test(failing=True)
        problem = core.state.steps(pid, "problem")[0]
        self.assertEqual(problem["status"], "open")
        self.assertTrue(problem["body"].startswith("Run the test suite failed:"))

        core, retry_pid, retry_step_id, reply, _, steps = (
            await self._resume_with_code_test(failing=False, core=core, pid=pid))

        self.assertEqual(retry_pid, pid)
        self.assertEqual(retry_step_id, step_id)
        self.assertIn("[code.test] verified", reply)
        closed_problem = core.state.steps(pid, "problem", open_only=False)[0]
        self.assertEqual(closed_problem["status"], "closed")
        self.assertIsNotNone(closed_problem["closed_at"])
        step = next(item for item in steps if item["id"] == step_id)
        self.assertEqual(step["status"], "closed")

    async def test_gated_action_is_parked_without_dispatch_and_step_stays_open(self):
        core = self.make_core()
        pid = core.state.create_project(
            "Approval Project", "Make a protected change", root=str(self.project_root))
        step_id = core.state.log_step(pid, "next", "Edit a protected file")
        task = Task(
            capability="os.apply",
            prompt="Write the requested file",
            project_id=pid,
            inputs={"action": "file_write", "args": {
                "path": str(self.project_root / "protected.txt"),
                "content": "approved only",
            }},
        )
        async def speak(message, briefing, recent, findings):
            return "\n".join(findings)

        with (
            patch.object(core, "_network_capabilities", new_callable=AsyncMock,
                         return_value=["os.apply"]),
            patch.object(core, "_offline_hands", new_callable=AsyncMock,
                         return_value={}),
            patch.object(planner, "plan", new_callable=AsyncMock,
                         return_value=[task]),
            patch.object(core, "_dispatch_now", new_callable=AsyncMock) as dispatch,
            patch.object(core, "_speak", new_callable=AsyncMock, side_effect=speak),
        ):
            await core.converse("continue working on Approval Project")

        self.assertEqual(len(core.state.pending_actions()), 1)
        dispatch.assert_not_awaited()
        note = core.state.steps(pid, "note")[0]
        self.assertIn("awaiting approval", note["body"])
        self.assertIn("pending change #", note["body"])
        step = next(item for item in core.state.steps(
            pid, "next", open_only=False) if item["id"] == step_id)
        self.assertEqual(step["status"], "open")
        self.assertFalse((self.project_root / "protected.txt").exists())

    async def _resume_with_gated_action(self):
        core = self.make_core()
        pid = core.state.create_project(
            "Approval Resume", "Make a protected change",
            root=str(self.project_root))
        step_id = core.state.log_step(pid, "next", "Edit a protected file")
        task = Task(
            capability="os.apply",
            prompt="Write the requested file",
            project_id=pid,
            inputs={"action": "file_write", "args": {
                "path": str(self.project_root / "protected.txt"),
                "content": "approved only",
            }},
        )
        async def speak(message, briefing, recent, findings):
            return "\n".join(findings)

        with (
            patch.object(core, "_network_capabilities", new_callable=AsyncMock,
                         return_value=["os.apply"]),
            patch.object(core, "_offline_hands", new_callable=AsyncMock,
                         return_value={}),
            patch.object(planner, "plan", new_callable=AsyncMock,
                         return_value=[task]),
            patch.object(core, "_speak", new_callable=AsyncMock,
                         side_effect=speak),
        ):
            await core.converse("continue working on Approval Resume")
        action = core.state.pending_actions()[0]
        return core, pid, step_id, task, action

    async def test_approved_project_step_is_closed_and_logged_done(self):
        core, pid, step_id, task, action = await self._resume_with_gated_action()
        setting_key = f"pending_step:{action['id']}"
        self.assertEqual(core.state.setting(setting_key), str(step_id))
        result = TaskResult(
            task_id=task.id, worker_id="test", summary="file written")

        with patch.object(core, "_dispatch_now", new_callable=AsyncMock,
                          return_value=result) as dispatch:
            await core.approve_action(action["id"])

        dispatch.assert_awaited_once()
        step = next(item for item in core.state.steps(
            pid, "next", open_only=False) if item["id"] == step_id)
        self.assertEqual(step["status"], "closed")
        done = core.state.steps(pid, "done", open_only=False)[0]
        self.assertIn("approved, done, unverified", done["body"])
        self.assertIn("file written", done["body"])
        self.assertEqual(core.state.setting(setting_key), "")

    async def test_failed_project_step_approval_logs_problem_and_stays_open(self):
        core, pid, step_id, task, action = await self._resume_with_gated_action()
        setting_key = f"pending_step:{action['id']}"
        result = TaskResult(
            task_id=task.id, worker_id="test", status="error",
            error="permission denied")

        with patch.object(core, "_dispatch_now", new_callable=AsyncMock,
                          return_value=result):
            await core.approve_action(action["id"])

        step = next(item for item in core.state.steps(
            pid, "next", open_only=False) if item["id"] == step_id)
        self.assertEqual(step["status"], "open")
        problem = core.state.steps(pid, "problem")[0]
        self.assertIn("Edit a protected file failed: permission denied",
                      problem["body"])
        self.assertEqual(core.state.setting(setting_key), "")

    async def test_declined_project_step_logs_note_and_stays_open(self):
        core, pid, step_id, _, action = await self._resume_with_gated_action()
        setting_key = f"pending_step:{action['id']}"

        self.assertEqual(core.decline_action(action["id"]),
                         f"declined pending change #{action['id']}")

        step = next(item for item in core.state.steps(
            pid, "next", open_only=False) if item["id"] == step_id)
        self.assertEqual(step["status"], "open")
        note = core.state.steps(pid, "note")[-1]
        self.assertEqual(
            note["body"],
            f"owner declined pending change #{action['id']} for: "
            "Edit a protected file",
        )
        self.assertEqual(core.state.setting(setting_key), "")

    async def test_dependent_task_waits_for_project_approval(self):
        core = self.make_core()
        pid = core.state.create_project(
            "Approval Dependency", "Make a protected change",
            root=str(self.project_root))
        step_id = core.state.log_step(pid, "next", "Edit then test a protected file")
        change = Task(
            capability="os.apply",
            prompt="Write the requested file",
            project_id=pid,
            inputs={"action": "file_write", "args": {
                "path": str(self.project_root / "protected.txt"),
                "content": "approved only",
            }},
        )
        test = Task(
            capability="code.test",
            prompt="Test the protected file change",
            project_id=pid,
            depends_on=[change.id],
            inputs={"workdir": str(self.project_root),
                    "command": "python -m unittest -q"},
        )

        with (
            patch.object(core, "_network_capabilities", new_callable=AsyncMock,
                         return_value=["os.apply", "code.test"]),
            patch.object(core, "_offline_hands", new_callable=AsyncMock,
                         return_value={}),
            patch.object(planner, "plan", new_callable=AsyncMock,
                         return_value=[change, test]),
            patch.object(core, "_dispatch_now", new_callable=AsyncMock) as dispatch,
            patch.object(core, "_speak", new_callable=AsyncMock,
                         return_value="Waiting for approval."),
        ):
            await core.converse("continue working on Approval Dependency")

        dispatch.assert_not_awaited()
        self.assertEqual(len(core.state.pending_actions()), 1)
        note = core.state.steps(pid, "note")[0]
        self.assertIn("awaiting approval", note["body"])
        self.assertEqual(core.state.steps(pid, "problem"), [])
        step = next(item for item in core.state.steps(
            pid, "next", open_only=False) if item["id"] == step_id)
        self.assertEqual(step["status"], "open")

    async def test_restart_restores_project_and_status_does_not_plan(self):
        first = self.make_core()
        pid = first.state.create_project(
            "Restart Project", "Keep the service healthy", root=str(self.project_root))
        first.state.set_setting("current_project", pid)
        first.state.log_step(pid, "done", "Add initial service tests")
        first.state.log_step(pid, "next", "Document the health endpoint")
        first.state.db.close()
        self.states.remove(first.state)

        restarted = self.make_core()
        captured = []

        async def speak(message, briefing, recent, findings):
            captured.append((message, briefing, recent, findings))
            return briefing

        with (
            patch.object(restarted.sight, "briefing", return_value=""),
            patch.object(restarted.hands, "briefing", return_value=""),
            patch.object(restarted, "_speak", new_callable=AsyncMock,
                         side_effect=speak),
            patch.object(planner, "plan", new_callable=AsyncMock) as plan,
        ):
            await restarted.converse("where did we leave off", project_id=pid)

        self.assertEqual(restarted.active_project, pid)
        self.assertIn("Add initial service tests", captured[0][1])
        self.assertIn("1. Document the health endpoint", captured[0][1])
        plan.assert_not_awaited()

    async def test_project_commands_persist_steps_decisions_and_new_root(self):
        core = self.make_core()
        pid = core.state.create_project("Command Project", "Test commands")

        confirmation = await core.converse("Alfred, next step: Build a widget", pid)
        self.assertIn("1. Build a widget", confirmation)
        await core.converse("problem: Waiting on a design", pid)
        await core.converse(
            "decision: Use a blue interface because it matches the brand", pid)
        await core.converse("done: build A WIDGET", pid)

        next_step = core.state.steps(pid, "next", open_only=False)[0]
        self.assertEqual(next_step["status"], "closed")
        self.assertIn("Waiting on a design",
                      core.state.steps(pid, "problem")[0]["body"])
        decision = core.state.standing_decisions(pid)[0]
        self.assertEqual(decision["decision"], "Use a blue interface")
        self.assertEqual(decision["rationale"], "it matches the brand")
        self.assertEqual(
            core.state.steps(pid, "done", open_only=False)[0]["body"].casefold(),
            "build a widget",
        )

        new_root = self.base / "new-root"
        new_root.mkdir()
        created = await core.converse(
            f"new project Notes: Organize the notes in {new_root}", pid)
        new_project = core.state.find_project("Notes")
        self.assertIn("Created and switched", created)
        self.assertEqual(new_project["goal"], "Organize the notes")
        self.assertEqual(new_project["root"], str(new_root))
        self.assertEqual(core.active_project, new_project["id"])
        self.assertEqual(core.state.setting("current_project"), new_project["id"])

        core.active_project = pid
        core.state.set_setting("current_project", pid)
        with patch.object(core, "_speak", new_callable=AsyncMock,
                          return_value="No next step is recorded."):
            await core.converse("resume Notes", pid)
        self.assertEqual(core.active_project, new_project["id"])

        count = len(core.state.active_projects())
        refusal = await core.converse(
            "new project Missing: Invalid root in ~/path/that/does/not/exist",
            new_project["id"],
        )
        self.assertIn("isn't an existing directory", refusal)
        self.assertEqual(len(core.state.active_projects()), count)

    async def test_unknown_resume_lists_projects_without_switching_or_logging_steps(self):
        core = self.make_core()
        core.state.create_project("Existing Project", "Still here")

        reply = await core.converse("continue working on Nonexistent")

        self.assertIn("Existing Project", reply)
        self.assertIsNone(core.active_project)
        self.assertEqual(core.state.setting("current_project"), "")
        self.assertEqual(core.state.db.execute(
            "SELECT COUNT(*) FROM project_log").fetchone()[0], 0)

    async def test_bridge_chat_follows_active_project_switch(self):
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        core = None
        try:
            async def setup():
                nonlocal core
                core = self.make_core()
                return Bridge(core, loop)

            bridge = asyncio.run_coroutine_threadsafe(setup(), loop).result(timeout=5)
            original = core.state.create_project("Original")
            core.active_project = original
            bridge.default_project = original

            bridge.chat("new project Switched", original)

            self.assertEqual(core.active_project, bridge.default_project)
            self.assertEqual(
                core.state.project(bridge.default_project)["name"], "Switched")
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            if core:
                core.state.db.close()
                self.states.remove(core.state)
            loop.close()

    async def test_planner_outcome_labels_pending_and_unverified_results(self):
        gated = Task(capability="os.apply", prompt="change")
        pending = TaskResult(task_id=gated.id, worker_id="owner-approval",
                             status="pending")
        self.assertEqual(planner.outcome(gated, pending), "awaiting approval")
        waiting_task = Task(capability="code.test", prompt="test")
        waiting = TaskResult(
            task_id=waiting_task.id, worker_id="owner-approval",
            status="pending",
            summary="waiting: depends on a change awaiting your approval",
            error="",
        )
        self.assertEqual(planner.outcome(waiting_task, waiting),
                         "awaiting approval")
        passed, note = planner.verify(waiting_task, waiting)
        self.assertTrue(passed)
        self.assertEqual(
            note, "NOT DONE: waiting on a change awaiting the owner's approval")

        test_task = Task(capability="code.test", prompt="test")
        passed = TaskResult(task_id=test_task.id, worker_id="test",
                            summary="passed", data={"exit_code": 0})
        self.assertEqual(planner.outcome(test_task, passed), "verified")
        code_task = Task(capability="code.write", prompt="write")
        code_result = TaskResult(task_id=code_task.id, worker_id="writer",
                                 artifacts=["file:///tmp/output.py"])
        self.assertEqual(planner.outcome(code_task, code_result), "done, unverified")


class ProjectContinuityMigrationTests(unittest.TestCase):
    def test_fresh_projects_schema_includes_root(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(str(Path(directory) / "state.db"))
            try:
                columns = [row["name"] for row in state.db.execute(
                    "PRAGMA table_info(projects)")]
                self.assertIn("root", columns)
            finally:
                state.db.close()
