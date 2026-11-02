"""验证控制台默认 feat、页面可切 dev，以及任务 worktree 拒绝切换。"""

from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_git import commit_file, git, init_repo
from devdeck.git import inspect_repo
from devdeck.server import DevDeckServer, Manager


class ConsoleBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = init_repo(self.root / "console", "feat")
        commit_file(self.repo, "README", "feat", "init", "2020-01-01T00:00:00")
        git(self.repo, "checkout", "-b", "dev")
        commit_file(self.repo, "dev.txt", "dev", "dev", "2021-01-01T00:00:00")
        git(self.repo, "checkout", "feat")
        self.manager = Manager(self.root / "data", console_dir=self.repo)

    def tearDown(self) -> None:
        self.manager.close()
        self.temporary.cleanup()

    def test_default_preferred_branch_is_feat(self) -> None:
        snapshot = self.manager.console_snapshot()
        self.assertEqual(snapshot["current"], "feat")
        self.assertEqual(snapshot["default"], "feat")
        self.assertEqual(snapshot["preferred"], "feat")
        self.assertEqual(snapshot["saved"], "")
        self.assertEqual(
            [item["local_name"] for item in snapshot["branches"]],
            ["feat", "dev"],
        )

    def test_preferred_stays_feat_when_current_is_dev(self) -> None:
        git(self.repo, "checkout", "dev")
        snapshot = self.manager.console_snapshot()
        self.assertEqual(snapshot["current"], "dev")
        self.assertEqual(snapshot["preferred"], "feat")
        self.manager.prepare_console_branch()
        self.assertEqual(inspect_repo(self.repo)["current"], "dev")
        self.assertEqual(self.manager.saved_console_branch(), "")

    def test_switch_to_dev_persists_and_checks_out(self) -> None:
        actual = self.manager.set_console_branch("dev")
        self.assertEqual(actual, "dev")
        self.assertEqual(inspect_repo(self.repo)["current"], "dev")
        self.assertEqual(self.manager.saved_console_branch(), "dev")
        self.assertEqual(self.manager.console_snapshot()["preferred"], "dev")

    def test_startup_checks_out_saved_branch(self) -> None:
        git(self.repo, "checkout", "dev")
        restored = Manager(self.root / "data", console_dir=self.repo)
        try:
            restored.prepare_console_branch()
            self.assertEqual(inspect_repo(self.repo)["current"], "dev")
            git(self.repo, "checkout", "feat")
            restored.save_console_branch("dev")
            restored.prepare_console_branch()
            self.assertEqual(inspect_repo(self.repo)["current"], "dev")
        finally:
            restored.close()

    def test_dirty_worktree_blocks_console_switch(self) -> None:
        (self.repo / "README").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "未提交改动"):
            self.manager.set_console_branch("dev")
        self.assertEqual(inspect_repo(self.repo)["current"], "feat")
        self.assertEqual(self.manager.saved_console_branch(), "")

    def test_agent_worktree_refuses_console_switch(self) -> None:
        git(self.repo, "checkout", "-b", "agents/task-52")
        with self.assertRaisesRegex(ValueError, "任务 worktree"):
            self.manager.set_console_branch("dev")
        self.assertEqual(inspect_repo(self.repo)["current"], "agents/task-52")
        self.manager.save_console_branch("dev")
        self.manager.prepare_console_branch()
        self.assertEqual(inspect_repo(self.repo)["current"], "agents/task-52")

    def test_other_branches_are_rejected(self) -> None:
        git(self.repo, "checkout", "-b", "topic")
        git(self.repo, "checkout", "feat")
        with self.assertRaisesRegex(ValueError, "只支持切换 feat 或 dev"):
            self.manager.set_console_branch("topic")

    def test_http_status_and_console_branch_api(self) -> None:
        server = DevDeckServer(("127.0.0.1", 0), self.manager)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("GET", "/")
            page = connection.getresponse().read().decode()
            self.assertIn('id="console-branch"', page)
            self.assertIn("控制台默认使用 feat", page)
            self.assertIn("可在右上角切换到 dev", page)
            connection.request("GET", "/api/status")
            body = json.loads(connection.getresponse().read())
            self.assertEqual(body["console"]["preferred"], "feat")
            self.assertEqual(body["console"]["current"], "feat")
            self.assertEqual(
                [item["local_name"] for item in body["console"]["branches"]],
                ["feat", "dev"],
            )
            connection.request(
                "POST",
                "/api/console/branch",
                json.dumps({"branch": "dev"}),
                {
                    "Content-Type": "application/json",
                    "Origin": "https://untrusted.example",
                },
            )
            self.assertEqual(connection.getresponse().status, 403)
            connection.request(
                "POST",
                "/api/console/branch",
                json.dumps({"branch": "dev"}),
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"ok": True})
            connection.request("GET", "/api/status")
            after = json.loads(connection.getresponse().read())
            self.assertEqual(after["console"]["current"], "dev")
            self.assertEqual(after["console"]["saved"], "dev")
            self.assertEqual(inspect_repo(self.repo)["current"], "dev")
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
