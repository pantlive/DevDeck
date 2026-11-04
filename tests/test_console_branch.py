"""验证控制台 feat/dev/main 切换、过滤 agents 分支，以及任务 worktree 拒绝切换。"""

from __future__ import annotations

import json
import subprocess
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
        (self.repo / "devdeck").mkdir()
        commit_file(
            self.repo,
            "devdeck/__main__.py",
            "print('console')",
            "console",
            "2020-01-02T00:00:00",
        )
        git(self.repo, "checkout", "-b", "dev")
        commit_file(self.repo, "dev.txt", "dev", "dev", "2021-01-01T00:00:00")
        git(self.repo, "checkout", "feat")
        git(self.repo, "branch", "main")
        git(self.repo, "branch", "agents/task-60")
        self.manager = Manager(self.root / "data", console_dir=self.repo)

    def tearDown(self) -> None:
        self.manager.close()
        self.temporary.cleanup()

    def test_default_preferred_branch_is_dev(self) -> None:
        snapshot = self.manager.console_snapshot()
        self.assertEqual(snapshot["current"], "feat")
        self.assertEqual(snapshot["default"], "dev")
        self.assertEqual(snapshot["preferred"], "dev")
        self.assertEqual(snapshot["saved"], "")
        self.assertEqual(
            [item["local_name"] for item in snapshot["branches"]],
            ["feat", "dev", "main"],
        )

    def test_startup_checks_out_default_dev_without_saving(self) -> None:
        self.manager.prepare_console_branch()
        self.assertEqual(inspect_repo(self.repo)["current"], "dev")
        self.assertEqual(self.manager.saved_console_branch(), "")
        self.assertEqual(self.manager.console_snapshot()["preferred"], "dev")
        # 再次启动停在 dev 上时不做多余切换。
        self.manager.prepare_console_branch()
        self.assertEqual(inspect_repo(self.repo)["current"], "dev")

    def test_startup_keeps_saved_branch_over_default(self) -> None:
        self.manager.save_console_branch("main")
        self.manager.prepare_console_branch()
        self.assertEqual(inspect_repo(self.repo)["current"], "main")
        self.assertEqual(self.manager.saved_console_branch(), "main")

    def test_startup_reports_dirty_worktree_instead_of_switching(self) -> None:
        (self.repo / "README").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "未提交改动"):
            self.manager.prepare_console_branch()
        self.assertEqual(inspect_repo(self.repo)["current"], "feat")
        snapshot = self.manager.console_snapshot()
        self.assertIn("未提交改动", snapshot["skipped"])
        self.assertTrue(snapshot["switchable"])

    def test_detached_head_skips_startup_switch(self) -> None:
        git(self.repo, "checkout", "--detach")
        with self.assertRaisesRegex(ValueError, "游离 HEAD"):
            self.manager.prepare_console_branch()
        self.assertTrue(inspect_repo(self.repo)["detached"])
        snapshot = self.manager.console_snapshot()
        self.assertIn("游离 HEAD", snapshot["skipped"])
        self.assertTrue(snapshot["switchable"])

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
        with self.assertRaisesRegex(ValueError, "任务 worktree"):
            self.manager.prepare_console_branch()
        self.assertEqual(inspect_repo(self.repo)["current"], "agents/task-52")
        self.assertEqual(self.manager.saved_console_branch(), "dev")
        snapshot = self.manager.console_snapshot()
        self.assertFalse(snapshot["switchable"])
        self.assertIn("任务 worktree", snapshot["skipped"])

    def test_other_branches_are_rejected(self) -> None:
        git(self.repo, "checkout", "-b", "topic")
        git(self.repo, "checkout", "feat")
        with self.assertRaisesRegex(ValueError, "feat、dev 或 main"):
            self.manager.set_console_branch("topic")

    def test_switch_to_main_persists_and_checks_out(self) -> None:
        actual = self.manager.set_console_branch("main")
        self.assertEqual(actual, "main")
        self.assertEqual(inspect_repo(self.repo)["current"], "main")
        self.assertEqual(self.manager.saved_console_branch(), "main")
        self.assertEqual(self.manager.console_snapshot()["preferred"], "main")

    def test_console_branches_exclude_agent_worktree(self) -> None:
        snapshot = self.manager.console_snapshot()
        names = [item["local_name"] for item in snapshot["branches"]]
        self.assertEqual(names, ["feat", "dev", "main"])
        with self.assertRaisesRegex(ValueError, "feat、dev 或 main"):
            self.manager.set_console_branch("agents/task-60")
        self.assertEqual(inspect_repo(self.repo)["current"], "feat")

    def test_console_repo_project_only_lists_mainline_branches(self) -> None:
        self.manager.add(
            {
                "name": "DevDeck",
                "backend": {
                    "directory": str(self.repo),
                    "command": "sleep 60",
                    "branch": "feat",
                },
            }
        )
        project = self.manager.projects[0]
        service = self.manager.snapshot()[0]["services"][0]
        self.assertEqual(
            {item["local_name"] for item in service["git"]["branches"]},
            {"feat", "dev", "main"},
        )
        self.assertEqual(service["git"]["latest"], "dev")
        with self.assertRaisesRegex(ValueError, "feat、dev 或 main"):
            self.manager.checkout_project_branch(
                {"project_id": project["id"], "branch": "agents/task-60"}
            )
        self.assertEqual(inspect_repo(self.repo)["current"], "feat")

    def test_console_repo_project_refuses_start_on_agent_branch(self) -> None:
        self.manager.add(
            {
                "name": "DevDeck",
                "backend": {
                    "directory": str(self.repo),
                    "command": "sleep 60",
                    "branch": "feat",
                },
            }
        )
        self.manager.projects[0]["services"][0]["branch"] = "agents/task-60"
        with self.assertRaisesRegex(ValueError, "feat、dev 或 main"):
            self.manager.action(self.manager.projects[0]["id"], "start", None)
        self.assertFalse(self.manager.processes)
        self.assertEqual(inspect_repo(self.repo)["current"], "feat")

    def test_console_repo_rejects_agent_branch_when_adding(self) -> None:
        with self.assertRaisesRegex(ValueError, "feat、dev 或 main"):
            self.manager.add(
                {
                    "name": "DevDeck",
                    "backend": {
                        "directory": str(self.repo),
                        "command": "sleep 60",
                        "branch": "agents/task-60",
                    },
                }
            )
        self.assertEqual(self.manager.projects, [])

    def test_http_status_and_console_branch_api(self) -> None:
        server = DevDeckServer(("127.0.0.1", 0), self.manager)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("GET", "/")
            page = connection.getresponse().read().decode()
            self.assertIn('id="console-branch"', page)
            self.assertIn("控制台启动默认基于 dev", page)
            self.assertIn("可在右上角或项目卡片切换 feat、dev 或 main", page)
            self.assertIn("branch-skip", page)
            connection.request("GET", "/api/status")
            body = json.loads(connection.getresponse().read())
            self.assertEqual(body["console"]["preferred"], "dev")
            self.assertEqual(body["console"]["current"], "feat")
            self.assertEqual(body["console"]["default"], "dev")
            self.assertEqual(body["console"]["skipped"], "")
            self.assertTrue(body["console"]["switchable"])
            self.assertEqual(
                [item["local_name"] for item in body["console"]["branches"]],
                ["feat", "dev", "main"],
            )
            connection.request(
                "POST",
                "/api/console/branch",
                json.dumps({"branch": "agents/task-60"}),
                {"Content-Type": "application/json"},
            )
            rejected = connection.getresponse()
            self.assertEqual(rejected.status, 400)
            self.assertIn("feat、dev 或 main", json.loads(rejected.read())["error"])
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


class LegacyMainTests(unittest.TestCase):
    """origin/main 只有 README 时，不应出现在控制台可切换分支中。"""

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        origin = init_repo(self.root / "origin", "main")
        commit_file(
            origin, "README.md", "legacy", "init", "2020-01-01T00:00:00"
        )
        git(origin, "checkout", "-b", "feat")
        (origin / "devdeck").mkdir()
        commit_file(
            origin,
            "devdeck/__main__.py",
            "print('console')",
            "console",
            "2021-01-01T00:00:00",
        )
        git(origin, "checkout", "-b", "dev")
        commit_file(
            origin, "dev.txt", "dev", "dev", "2022-01-01T00:00:00"
        )
        git(origin, "checkout", "feat")
        self.repo = self.root / "console"
        subprocess.run(
            ["git", "clone", str(origin), str(self.repo)],
            check=True,
            capture_output=True,
            text=True,
        )
        git(self.repo, "config", "user.name", "DevDeck Test")
        git(self.repo, "config", "user.email", "devdeck@example.com")
        git(self.repo, "checkout", "feat")
        self.manager = Manager(self.root / "data", console_dir=self.repo)

    def tearDown(self) -> None:
        self.manager.close()
        self.temporary.cleanup()

    def test_legacy_main_is_hidden_and_rejected(self) -> None:
        snapshot = self.manager.console_snapshot()
        self.assertEqual(
            [item["local_name"] for item in snapshot["branches"]],
            ["feat", "dev"],
        )
        self.assertEqual(snapshot["preferred"], "dev")
        with self.assertRaisesRegex(ValueError, "不包含 DevDeck 代码"):
            self.manager.set_console_branch("main")
        self.assertEqual(inspect_repo(self.repo)["current"], "feat")

    def test_saved_legacy_main_falls_back_to_default_dev_on_startup(self) -> None:
        self.manager.save_console_branch("main")
        self.manager.prepare_console_branch()
        self.assertEqual(inspect_repo(self.repo)["current"], "dev")
        self.assertEqual(self.manager.console_snapshot()["preferred"], "dev")
        self.assertEqual(self.manager.saved_console_branch(), "")
        self.assertFalse(self.manager.console_config.exists())

    def test_legacy_main_not_offered_for_project(self) -> None:
        self.manager.add(
            {
                "name": "DevDeck",
                "backend": {
                    "directory": str(self.repo),
                    "command": "sleep 60",
                    "branch": "feat",
                },
            }
        )
        project = self.manager.projects[0]
        service = self.manager.snapshot()[0]["services"][0]
        self.assertEqual(
            {item["local_name"] for item in service["git"]["branches"]},
            {"feat", "dev"},
        )
        with self.assertRaisesRegex(ValueError, "不包含 DevDeck 代码"):
            self.manager.checkout_project_branch(
                {"project_id": project["id"], "branch": "main"}
            )
        self.assertEqual(inspect_repo(self.repo)["current"], "feat")


class RemoteMainHasCodeTests(unittest.TestCase):
    """本地 main 只有 README、远程 main 已有入口文件时，应列出并可切换。"""

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        origin = init_repo(self.root / "origin", "main")
        commit_file(
            origin, "README.md", "legacy", "init", "2020-01-01T00:00:00"
        )
        git(origin, "checkout", "-b", "feat")
        (origin / "devdeck").mkdir()
        commit_file(
            origin,
            "devdeck/__main__.py",
            "print('console')",
            "console",
            "2021-01-01T00:00:00",
        )
        git(origin, "checkout", "-b", "dev")
        commit_file(
            origin, "dev.txt", "dev", "dev", "2022-01-01T00:00:00"
        )
        git(origin, "checkout", "feat")
        self.repo = self.root / "console"
        subprocess.run(
            ["git", "clone", str(origin), str(self.repo)],
            check=True,
            capture_output=True,
            text=True,
        )
        git(self.repo, "config", "user.name", "DevDeck Test")
        git(self.repo, "config", "user.email", "devdeck@example.com")
        git(self.repo, "checkout", "feat")
        git(self.repo, "branch", "main", "origin/main")
        git(origin, "checkout", "main")
        (origin / "devdeck").mkdir(exist_ok=True)
        commit_file(
            origin,
            "devdeck/__main__.py",
            "print('main console')",
            "main console",
            "2023-01-01T00:00:00",
        )
        git(self.repo, "fetch", "origin")
        self.manager = Manager(self.root / "data", console_dir=self.repo)

    def tearDown(self) -> None:
        self.manager.close()
        self.temporary.cleanup()

    def test_remote_main_with_code_is_listed(self) -> None:
        snapshot = self.manager.console_snapshot()
        self.assertEqual(
            [item["local_name"] for item in snapshot["branches"]],
            ["feat", "dev", "main"],
        )
        main = next(
            item for item in snapshot["branches"] if item["local_name"] == "main"
        )
        self.assertTrue(main["remote"])
        self.assertEqual(inspect_repo(self.repo)["current"], "feat")

    def test_switch_fast_forwards_local_main_from_remote(self) -> None:
        actual = self.manager.set_console_branch("main")
        self.assertEqual(actual, "main")
        self.assertEqual(inspect_repo(self.repo)["current"], "main")
        self.assertTrue((self.repo / "devdeck" / "__main__.py").exists())
        self.assertEqual(self.manager.saved_console_branch(), "main")

    def test_console_project_card_switch_writes_console_json(self) -> None:
        self.manager.add(
            {
                "name": "DevDeck",
                "backend": {
                    "directory": str(self.repo),
                    "command": "sleep 60",
                    "branch": "feat",
                },
            }
        )
        project = self.manager.projects[0]
        restart = self.manager.checkout_project_branch(
            {"project_id": project["id"], "branch": "main"}
        )
        self.assertTrue(restart)
        self.assertEqual(inspect_repo(self.repo)["current"], "main")
        self.assertEqual(self.manager.saved_console_branch(), "main")
        self.assertEqual(project["services"][0]["branch"], "main")


class ConsoleProjectSyncTests(unittest.TestCase):
    """右上角和项目卡片共用 console.json，并同步同一仓库的服务分支。"""

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = init_repo(self.root / "console", "feat")
        commit_file(self.repo, "README", "feat", "init", "2020-01-01T00:00:00")
        (self.repo / "devdeck").mkdir()
        commit_file(
            self.repo,
            "devdeck/__main__.py",
            "print('console')",
            "console",
            "2020-01-02T00:00:00",
        )
        git(self.repo, "checkout", "-b", "dev")
        commit_file(self.repo, "dev.txt", "dev", "dev", "2021-01-01T00:00:00")
        git(self.repo, "checkout", "feat")
        git(self.repo, "branch", "main")
        self.manager = Manager(self.root / "data", console_dir=self.repo)
        self.manager.add(
            {
                "name": "DevDeck",
                "backend": {
                    "directory": str(self.repo),
                    "command": "sleep 60",
                    "branch": "feat",
                },
            }
        )

    def tearDown(self) -> None:
        self.manager.close()
        self.temporary.cleanup()

    def test_set_console_branch_updates_project_service_branch(self) -> None:
        self.manager.set_console_branch("dev")
        self.assertEqual(self.manager.saved_console_branch(), "dev")
        self.assertEqual(
            self.manager.projects[0]["services"][0]["branch"], "dev"
        )

    def test_project_card_switch_saves_console_branch_and_asks_restart(self) -> None:
        project = self.manager.projects[0]
        restart = self.manager.checkout_project_branch(
            {"project_id": project["id"], "branch": "dev"}
        )
        self.assertTrue(restart)
        self.assertEqual(inspect_repo(self.repo)["current"], "dev")
        self.assertEqual(self.manager.saved_console_branch(), "dev")
        self.assertEqual(project["services"][0]["branch"], "dev")

    def test_http_project_branch_api_marks_console_restart(self) -> None:
        project = self.manager.projects[0]
        server = DevDeckServer(("127.0.0.1", 0), self.manager)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            connection = HTTPConnection(
                "127.0.0.1", server.server_port, timeout=2
            )
            connection.request(
                "POST",
                "/api/branch",
                json.dumps({"project_id": project["id"], "branch": "dev"}),
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(
                json.loads(response.read()), {"ok": True, "restart": True}
            )
            connection.request("GET", "/api/status")
            after = json.loads(connection.getresponse().read())
            self.assertEqual(after["console"]["current"], "dev")
            self.assertEqual(after["console"]["saved"], "dev")
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
