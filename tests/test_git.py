"""验证分支列表、最近更新检测、脏工作区拒绝切换，以及按分支启动。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path

from devdeck.git import checkout_branch, fetch_remotes, inspect_repo, validate_branch
from devdeck.server import DevDeckServer, Manager


def git(directory: Path, *arguments: str, env: dict[str, str] | None = None) -> None:
    environment = os.environ.copy()
    if env:
        environment.update(env)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *arguments],
        cwd=directory,
        check=True,
        env=environment,
        capture_output=True,
        text=True,
    )


def init_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", branch)
    git(path, "config", "user.name", "DevDeck Test")
    git(path, "config", "user.email", "devdeck@example.com")
    return path


def commit_file(
    path: Path,
    name: str,
    content: str,
    message: str,
    when: str,
) -> None:
    (path / name).write_text(content, encoding="utf-8")
    git(path, "add", name)
    git(
        path,
        "commit",
        "-m",
        message,
        env={
            "GIT_AUTHOR_DATE": when,
            "GIT_COMMITTER_DATE": when,
        },
    )


class GitInspectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_non_git_directory_is_empty(self) -> None:
        directory = self.root / "plain"
        directory.mkdir()
        self.assertIsNone(inspect_repo(directory))

    def test_latest_branch_uses_committer_date(self) -> None:
        repo = init_repo(self.root / "repo")
        commit_file(repo, "README", "old", "init", "2020-01-01T00:00:00")
        git(repo, "checkout", "-b", "feat")
        commit_file(repo, "app.txt", "new", "later", "2024-06-01T00:00:00")
        git(repo, "checkout", "main")
        info = inspect_repo(repo)
        self.assertEqual(info["current"], "main")
        self.assertEqual(info["latest"], "feat")
        self.assertFalse(info["dirty"])
        names = [item["name"] for item in info["branches"]]
        self.assertEqual(names[0], "feat")
        self.assertTrue(info["branches"][0]["latest"])
        self.assertFalse(next(item["current"] for item in info["branches"] if item["name"] == "feat"))
        self.assertTrue(next(item["current"] for item in info["branches"] if item["name"] == "main"))

    def test_remote_only_branch_can_be_latest(self) -> None:
        origin = init_repo(self.root / "origin", "main")
        commit_file(origin, "README", "base", "init", "2020-01-01T00:00:00")
        git(origin, "checkout", "-b", "hotfix")
        commit_file(origin, "fix.txt", "hot", "hotfix", "2025-01-01T00:00:00")
        git(origin, "checkout", "main")
        local = self.root / "local"
        subprocess.run(
            ["git", "clone", str(origin), str(local)],
            check=True,
            capture_output=True,
            text=True,
        )
        info = inspect_repo(local)
        self.assertEqual(info["current"], "main")
        self.assertEqual(info["latest"], "origin/hotfix")
        remote = next(item for item in info["branches"] if item["name"] == "origin/hotfix")
        self.assertTrue(remote["remote"])
        self.assertEqual(remote["local_name"], "hotfix")
        actual = checkout_branch(local, "hotfix")
        self.assertEqual(actual, "hotfix")
        self.assertEqual(inspect_repo(local)["current"], "hotfix")

    def test_dirty_tracked_file_blocks_checkout(self) -> None:
        repo = init_repo(self.root / "dirty")
        commit_file(repo, "README", "one", "init", "2020-01-01T00:00:00")
        git(repo, "checkout", "-b", "feat")
        commit_file(repo, "app.txt", "two", "feat", "2021-01-01T00:00:00")
        git(repo, "checkout", "main")
        (repo / "README").write_text("changed", encoding="utf-8")
        self.assertTrue(inspect_repo(repo)["dirty"])
        with self.assertRaisesRegex(ValueError, "未提交改动"):
            checkout_branch(repo, "feat")
        self.assertEqual(inspect_repo(repo)["current"], "main")

    def test_untracked_file_does_not_block_checkout(self) -> None:
        repo = init_repo(self.root / "untracked")
        commit_file(repo, "README", "one", "init", "2020-01-01T00:00:00")
        git(repo, "checkout", "-b", "feat")
        commit_file(repo, "app.txt", "two", "feat", "2021-01-01T00:00:00")
        git(repo, "checkout", "main")
        (repo / "notes.local").write_text("scratch", encoding="utf-8")
        self.assertFalse(inspect_repo(repo)["dirty"])
        self.assertEqual(checkout_branch(repo, "feat"), "feat")
        self.assertEqual(inspect_repo(repo)["current"], "feat")
        self.assertTrue((repo / "notes.local").exists())

    def test_invalid_branch_names(self) -> None:
        for name in ("", "HEAD", "-bad", "feat..main", "a b", "feat~1"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_branch(name)

    def test_fetch_updates_latest_from_local_remote(self) -> None:
        origin = init_repo(self.root / "origin")
        commit_file(origin, "README", "base", "init", "2020-01-01T00:00:00")
        local = self.root / "clone"
        subprocess.run(
            ["git", "clone", str(origin), str(local)],
            check=True,
            capture_output=True,
            text=True,
        )
        git(local, "config", "user.name", "DevDeck Test")
        git(local, "config", "user.email", "devdeck@example.com")
        git(origin, "checkout", "-b", "feat")
        commit_file(origin, "later.txt", "new", "feat", "2024-02-02T00:00:00")
        self.assertEqual(inspect_repo(local)["latest"], inspect_repo(local)["current"])
        fetch_remotes(local)
        self.assertEqual(inspect_repo(local)["latest"], "origin/feat")


class GitManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = Manager(self.root / "data")
        self.repo = init_repo(self.root / "app")
        commit_file(self.repo, "README", "base", "init", "2020-01-01T00:00:00")
        git(self.repo, "checkout", "-b", "feat")
        commit_file(self.repo, "app.txt", "feat", "feat", "2024-01-01T00:00:00")
        git(self.repo, "checkout", "main")

    def tearDown(self) -> None:
        self.manager.close()
        self.temporary.cleanup()

    def add_project(self, branch: str = "", directory: Path | None = None) -> dict:
        payload = {
            "name": "分支项目",
            "backend": {
                "directory": str(directory or self.repo),
                "command": (
                    f"echo branch=$DEVDECK_BRANCH; '{sys.executable}' "
                    "-u -m http.server {port} --bind 127.0.0.1"
                ),
                "port": self.free_port(),
                "branch": branch,
            },
        }
        self.manager.add(payload)
        return self.manager.projects[-1]

    def free_port(self) -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def wait_listening(self, port: int) -> None:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.03)
        self.fail("服务未监听")

    def test_snapshot_marks_latest_branch(self) -> None:
        self.add_project()
        snapshot = self.manager.snapshot()[0]["services"][0]
        self.assertEqual(snapshot["git"]["current"], "main")
        self.assertEqual(snapshot["git"]["latest"], "feat")
        self.assertEqual(snapshot["branch"], "")

    def test_start_checks_out_configured_branch(self) -> None:
        project = self.add_project("feat")
        service = project["services"][0]
        self.manager.action(project["id"], "start", None)
        self.wait_listening(service["port"])
        self.assertEqual(inspect_repo(self.repo)["current"], "feat")
        logs = self.manager.logs(service["id"])
        self.assertIn("branch=feat", logs)
        self.assertIn("分支 feat", logs)
        self.manager.action(project["id"], "stop", None)

    def test_running_service_blocks_checkout(self) -> None:
        project = self.add_project()
        self.manager.action(project["id"], "start", None)
        with self.assertRaisesRegex(ValueError, "先停止"):
            self.manager.checkout_project_branch(
                {
                    "project_id": project["id"],
                    "branch": "feat",
                }
            )
        self.assertEqual(inspect_repo(self.repo)["current"], "main")
        self.manager.action(project["id"], "stop", None)
        self.manager.checkout_project_branch(
            {"project_id": project["id"], "branch": "feat"}
        )
        self.assertEqual(inspect_repo(self.repo)["current"], "feat")
        self.assertEqual(self.manager.projects[0]["services"][0]["branch"], "feat")

    def test_conflicting_branches_in_same_repo_fail_before_start(self) -> None:
        frontend_dir = self.repo / "web"
        frontend_dir.mkdir()
        self.manager.add(
            {
                "name": "冲突",
                "frontend": {
                    "directory": str(frontend_dir),
                    "command": "sleep 60",
                    "branch": "feat",
                },
                "backend": {
                    "directory": str(self.repo),
                    "command": "sleep 60",
                    "branch": "main",
                },
            }
        )
        project = self.manager.projects[0]
        with self.assertRaisesRegex(ValueError, "不同分支"):
            self.manager.action(project["id"], "start", None)
        self.assertFalse(self.manager.processes)
        self.assertEqual(inspect_repo(self.repo)["current"], "main")

    def test_http_branch_api_rejects_untrusted_and_switches(self) -> None:
        project = self.add_project()
        server = DevDeckServer(("127.0.0.1", 0), self.manager)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request(
                "POST",
                "/api/branch",
                json.dumps({"project_id": project["id"], "branch": "feat"}),
                {
                    "Content-Type": "application/json",
                    "Origin": "https://untrusted.example",
                },
            )
            self.assertEqual(connection.getresponse().status, 403)
            connection.request(
                "POST",
                "/api/branch",
                json.dumps({"project_id": project["id"], "branch": "feat"}),
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"ok": True})
            connection.request("GET", "/api/projects")
            body = json.loads(connection.getresponse().read())
            git = body[0]["services"][0]["git"]
            self.assertEqual(git["current"], "feat")
            self.assertEqual(git["latest"], "feat")
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
