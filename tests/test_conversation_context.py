import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from alfred.bus.local import LocalBus
from alfred.contracts import TaskResult
from alfred.core.alfred import Alfred
from alfred.core import planner
from alfred.core.state import State


class ConversationContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "state.db")
        self.states = []

    def tearDown(self):
        for state in self.states:
            state.db.close()
        self.temp_dir.cleanup()

    def make_core(self):
        cfg = {
            "core": {
                "state_db": self.db_path,
                "ollama_url": "http://127.0.0.1:9",
            },
            "worker": {"id": "test-worker"},
            "sight": {"enabled": False},
        }
        core = Alfred(LocalBus(), cfg)
        self.states.append(core.state)
        return core

    def close_core(self, core):
        core.state.db.close()
        self.states.remove(core.state)

    def mock_conversation_environment(self, core, capabilities=("os.observe",)):
        network = self.enterContext(
            patch.object(core, "_network_capabilities", new_callable=AsyncMock)
        )
        network.return_value = list(capabilities)
        offline = self.enterContext(
            patch.object(core, "_offline_hands", new_callable=AsyncMock)
        )
        offline.return_value = {}
        self.enterContext(patch.object(core.sight, "briefing", return_value=""))
        self.enterContext(patch.object(core.hands, "briefing", return_value=""))
        self.enterContext(patch.object(core, "_learn_in_background"))
        return network, offline

    def test_recent_turns_are_scoped_and_chronological(self):
        state = State(self.db_path)
        self.states.append(state)
        for role, body, project_id in (
            ("User", "A first", "A"),
            ("User", "B first", "B"),
            ("User", "Unscoped first", None),
            ("Alfred", "A second", "A"),
            ("Alfred", "B second", "B"),
            ("Alfred", "Unscoped second", None),
            ("User", "A third", "A"),
        ):
            state.log_turn(role, body, project_id)

        self.assertEqual(
            state.recent_turns("A"),
            [("User", "A first"), ("Alfred", "A second"), ("User", "A third")],
        )
        self.assertEqual(
            state.recent_turns("B", limit=1),
            [("Alfred", "B second")],
        )
        self.assertEqual(
            state.recent_turns(None),
            [("User", "Unscoped first"), ("Alfred", "Unscoped second")],
        )

    def test_recent_conversation_limits_messages_and_preserves_edges(self):
        core = self.make_core()
        source = [
            f"BEGIN{index}" + ("x" * 800) + f"END{index}"
            for index in range(7)
        ]
        for index, body in enumerate(source):
            core.state.log_turn(f"Turn{index}", body, "project")

        recent = core._recent_conversation("project")
        lines = recent.splitlines()

        self.assertEqual(len(lines), 6)
        self.assertNotIn("BEGIN0", recent)
        for index, line in enumerate(lines, start=1):
            role, body = line.split(": ", 1)
            expected = source[index][:250] + " … " + source[index][-147:]
            self.assertEqual(role, f"Turn{index}")
            self.assertEqual(body, expected)
            self.assertEqual(len(body), 400)
            self.assertTrue(body.startswith(f"BEGIN{index}"))
            self.assertTrue(body.endswith(f"END{index}"))

    def test_restart_retains_history_for_the_current_project(self):
        first = self.make_core()
        first.state.log_turn("User", "Remember this for project A.", "A")
        first.state.log_turn("Alfred", "I will.", "A")
        self.close_core(first)

        restarted = self.make_core()
        self.assertEqual(
            restarted._recent_conversation("A"),
            "User: Remember this for project A.\nAlfred: I will.",
        )
        self.assertEqual(restarted._recent_conversation("B"), "")

    async def test_remember_logs_both_roles_for_sight_and_hands_commands(self):
        core = self.make_core()

        sight_reply = await core.converse("eyes off", project_id="sight-project")
        hands_reply = await core.converse("drive for 1 minute", project_id="hands-project")

        self.assertIn("Looking away", sight_reply)
        self.assertIn("Driving for", hands_reply)
        self.assertEqual(
            core.state.recent_turns("sight-project"),
            [
                ("User", "eyes off"),
                ("Alfred", sight_reply),
            ],
        )
        self.assertEqual(
            core.state.recent_turns("hands-project"),
            [
                ("User", "drive for 1 minute"),
                ("Alfred", hands_reply),
            ],
        )

    async def test_thanks_skips_planner_and_capability_lookup(self):
        core = self.make_core()
        network, offline = self.mock_conversation_environment(core)
        plan = self.enterContext(
            patch.object(planner, "plan", new_callable=AsyncMock)
        )
        speak = self.enterContext(
            patch("alfred.core.alfred.llm.complete", new_callable=AsyncMock)
        )
        speak.return_value = "You're welcome."

        reply = await core.converse("thanks!", project_id="A")

        self.assertEqual(reply, "You're welcome.")
        network.assert_not_awaited()
        offline.assert_not_awaited()
        plan.assert_not_awaited()
        speak.assert_awaited_once()

    async def test_follow_up_phrases_enter_planner_route(self):
        core = self.make_core()
        network, _ = self.mock_conversation_environment(core)
        plan = self.enterContext(
            patch.object(planner, "plan", new_callable=AsyncMock, return_value=[])
        )
        speak = self.enterContext(
            patch("alfred.core.alfred.llm.complete", new_callable=AsyncMock)
        )
        speak.return_value = "Could you clarify?"

        for message in ("yes", "ok", "okay", "do it", "shorter", "again"):
            await core.converse(message, project_id="A")

        self.assertEqual(plan.await_count, 6)
        self.assertEqual(network.await_count, 6)

    async def test_follow_up_uses_prior_alternatives_in_real_planner_and_synthesis(self):
        core = self.make_core()
        self.mock_conversation_environment(core)
        planner_json = self.enterContext(
            patch("alfred.core.planner.llm.complete_json", new_callable=AsyncMock)
        )
        planner_json.side_effect = [
            {"tasks": []},
            {
                "tasks": [
                    {
                        "ref": "observe-memory",
                        "capability": "os.observe",
                        "prompt": "Check memory usage.",
                        "inputs": {"what": "memory"},
                        "depends_on": [],
                    }
                ]
            },
        ]
        speak = self.enterContext(
            patch("alfred.core.alfred.llm.complete", new_callable=AsyncMock)
        )
        speak.side_effect = [
            "1. Check disk. 2. Check memory.",
            "The memory check completed.",
        ]

        async def dispatch(task):
            return TaskResult(
                task_id=task.id,
                worker_id="test-worker",
                summary="Memory observation complete.",
            )

        dispatched = self.enterContext(
            patch.object(core, "_dispatch", new_callable=AsyncMock, side_effect=dispatch)
        )

        first_message = "What are two checks I could run?"
        await core.converse(first_message, project_id="A")
        reply = await core.converse("do the second one", project_id="A")

        self.assertEqual(reply, "The memory check completed.")
        self.assertEqual(planner_json.await_count, 2)
        self.assertEqual(dispatched.await_count, 1)
        task = dispatched.await_args.args[0]
        self.assertEqual(task.capability, "os.observe")
        self.assertEqual(task.inputs["what"], "memory")

        history = f"User: {first_message}\nAlfred: 1. Check disk. 2. Check memory."
        planner_prompt = planner_json.await_args_list[1].args[0]
        self.assertIn(f"Recent conversation (reference only):\n{history}", planner_prompt)
        self.assertIn("Latest user request:\ndo the second one", planner_prompt)
        self.assertIn(history, speak.await_args_list[1].args[0])

    async def test_project_b_ambiguous_follow_up_omits_project_a_and_does_not_dispatch(self):
        core = self.make_core()
        self.mock_conversation_environment(core)
        core.state.log_turn("User", "Project A private context.", "A")
        core.state.log_turn("Alfred", "1. First A choice. 2. Second A choice.", "A")
        planner_json = self.enterContext(
            patch("alfred.core.planner.llm.complete_json", new_callable=AsyncMock)
        )
        planner_json.return_value = {"tasks": []}
        speak = self.enterContext(
            patch("alfred.core.alfred.llm.complete", new_callable=AsyncMock)
        )
        speak.return_value = "Which item do you mean?"
        dispatched = self.enterContext(
            patch.object(core, "_dispatch", new_callable=AsyncMock)
        )

        reply = await core.converse("do the second one", project_id="B")

        self.assertEqual(reply, "Which item do you mean?")
        self.assertNotIn("Project A private context", planner_json.await_args.args[0])
        self.assertNotIn("Second A choice", planner_json.await_args.args[0])
        self.assertIn("(no prior turns)", planner_json.await_args.args[0])
        self.assertNotIn("Project A private context", speak.await_args.args[0])
        self.assertIn(
            "If the intended action or item is unclear, ask one specific question",
            speak.await_args.args[0],
        )
        dispatched.assert_not_awaited()

    async def test_explicit_empty_capabilities_skips_planner_model_call(self):
        core = self.make_core()
        planner_json = self.enterContext(
            patch("alfred.core.planner.llm.complete_json", new_callable=AsyncMock)
        )

        tasks = await planner.plan(
            "Check memory.",
            "",
            core.cfg,
            "A",
            available=[],
        )

        self.assertEqual(tasks, [])
        planner_json.assert_not_awaited()

    async def test_model_approval_flags_are_removed_and_apply_is_parked(self):
        core = self.make_core()
        planner_json = self.enterContext(
            patch("alfred.core.planner.llm.complete_json", new_callable=AsyncMock)
        )
        planner_json.return_value = {
            "tasks": [
                {
                    "ref": "propose-file",
                    "capability": "os.apply",
                    "prompt": "Propose a file change.",
                    "inputs": {
                        "action": "file_write",
                        "args": {"path": "~/never-written", "content": "safe test"},
                        "keep": "normal-input",
                        "_approved": True,
                        "_granted": True,
                    },
                    "depends_on": [],
                }
            ]
        }
        dispatch_now = self.enterContext(
            patch.object(core, "_dispatch_now", new_callable=AsyncMock)
        )

        tasks = await planner.plan(
            "Propose a file change.",
            "",
            core.cfg,
            "A",
            available=["os.apply"],
        )

        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(
            task.inputs,
            {
                "action": "file_write",
                "args": {"path": "~/never-written", "content": "safe test"},
                "keep": "normal-input",
            },
        )
        result = await core._dispatch(task)

        self.assertEqual(result.status, "pending")
        dispatch_now.assert_not_awaited()
        self.assertEqual(core.state.pending_actions()[0]["project_id"], "A")
        parked = core.state.db.execute(
            "SELECT task_json FROM pending_actions"
        ).fetchone()
        self.assertNotIn("_approved", json.loads(parked["task_json"])["inputs"])
        self.assertNotIn("_granted", json.loads(parked["task_json"])["inputs"])


if __name__ == "__main__":
    unittest.main()
