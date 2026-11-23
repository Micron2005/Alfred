import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen
from unittest.mock import patch

import run_server
from alfred.core.state import State


class PanelHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state = State(str(Path(self.temp_dir.name) / "state.db"))

    def tearDown(self):
        self.state.db.close()
        self.temp_dir.cleanup()

    def add_turns(self):
        for role, body, project_id, at in (
            ("User", "A first", "A", 1.0),
            ("User", "B first", "B", 2.0),
            ("User", "Unscoped", None, 3.0),
            ("Alfred", "A second", "A", 4.0),
            ("Alfred", "B second", "B", 5.0),
            ("User", "A third", "A", 6.0),
        ):
            self.state.log_turn(role, body, project_id, at=at)

    def test_recent_turns_at_scopes_orders_and_limits(self):
        self.add_turns()

        self.assertEqual(
            self.state.recent_turns_at("A"),
            [
                ("User", "A first", 1.0),
                ("Alfred", "A second", 4.0),
                ("User", "A third", 6.0),
            ],
        )
        self.assertEqual(
            self.state.recent_turns_at("A", limit=2),
            [("Alfred", "A second", 4.0), ("User", "A third", 6.0)],
        )
        self.assertEqual(
            self.state.recent_turns_at("B"),
            [("User", "B first", 2.0), ("Alfred", "B second", 5.0)],
        )
        self.assertEqual(
            self.state.recent_turns_at(None),
            [("User", "Unscoped", 3.0)],
        )

    def test_history_endpoint_uses_the_default_project(self):
        self.add_turns()
        bridge = SimpleNamespace(
            alfred=SimpleNamespace(state=self.state),
            default_project="A",
        )
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), run_server.make_handler(bridge))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/api/history"
        no_lock = Path(self.temp_dir.name) / "no-lock.json"

        def get_turns():
            with urlopen(url, timeout=5) as response:
                return json.loads(response.read())["turns"]

        try:
            with patch.object(run_server.shell_lock, "LOCK_FILE", no_lock):
                self.assertEqual(
                    get_turns(),
                    [
                        ["User", "A first", 1.0],
                        ["Alfred", "A second", 4.0],
                        ["User", "A third", 6.0],
                    ],
                )
                bridge.default_project = "B"
                self.assertEqual(
                    get_turns(),
                    [
                        ["User", "B first", 2.0],
                        ["Alfred", "B second", 5.0],
                    ],
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
