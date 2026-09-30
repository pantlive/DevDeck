"""验证控制台自重启、请求隔离和真实子进程清理。"""

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from devdeck.server import DevDeckServer, Manager


def request(port, path, payload=None, headers=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request(
            "GET" if payload is None else "POST",
            path,
            None if payload is None else json.dumps(payload),
            {"Content-Type": "application/json", **(headers or {})},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


class RestartHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.manager = Manager(Path(self.temporary.name))
        self.server = DevDeckServer(("127.0.0.1", 0), self.manager)
        self.worker = threading.Thread(target=self.server.serve_forever)
        self.worker.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()
        self.manager.close()
        self.temporary.cleanup()

    def test_restart_requires_trusted_json_post(self):
        status, before = request(self.port, "/api/status")
        self.assertEqual(status, 200)
        self.assertFalse(before["restarting"])
        self.assertEqual(request(self.port, "/api/restart")[0], 404)
        for headers in (
            {"Origin": "https://untrusted.example"},
            {"Host": "untrusted.example"},
            {"Content-Type": "text/plain"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(
                    request(self.port, "/api/restart", {}, headers)[0], 403
                )
        self.assertEqual(request(self.port, "/api/restart", [])[0], 400)
        self.assertEqual(request(self.port, "/api/status")[1], before)

    def test_restart_blocks_duplicate_requests_and_mutations(self):
        shutdown_requested = threading.Event()
        with patch.object(
            self.server, "shutdown", side_effect=shutdown_requested.set
        ) as shutdown:
            status, result = request(self.port, "/api/restart", {})
            self.assertEqual(status, 202)
            self.assertTrue(result["ok"])
            self.assertTrue(shutdown_requested.wait(1))
            self.assertEqual(request(self.port, "/api/status")[1], {
                "instance_id": result["instance_id"],
                "restarting": True,
            })
            for path in (
                "/api/restart", "/api/projects", "/api/action", "/api/ports"
            ):
                with self.subTest(path=path):
                    self.assertEqual(request(self.port, path, {})[0], 503)
            shutdown.assert_called_once()
            self.assertEqual(self.manager.projects, [])


@unittest.skipUnless(sys.platform.startswith("linux"), "需要 Linux 进程组")
class RestartProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.port = self.free_port()
        self.output = (self.root / "console.log").open("wb")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        environment["DEVDECK_RESTART_TEST"] = "preserved"
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "devdeck", "--port", str(self.port),
             "--data-dir", "relative data"],
            cwd=self.root,
            env=environment,
            stdout=self.output,
            stderr=subprocess.STDOUT,
        )
        self.child_groups = set()
        self.addCleanup(self.cleanup)
        self.wait_for(lambda: request(self.port, "/api/status")[0] == 200)

    def cleanup(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for group in self.child_groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.output.close()
        self.temporary.cleanup()

    def free_port(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def wait_for(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail((self.root / "console.log").read_text())
            try:
                if predicate():
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.03)
        self.fail("等待控制台状态超时：" + (self.root / "console.log").read_text())

    def test_real_restart_preserves_configuration_and_cleans_children(self):
        service_port = self.free_port()
        # shell 保持存活，让 HTTP 服务成为其子进程，验证整个进程组被清理。
        payload = {
            "name": "重启测试",
            "backend": {
                "directory": str(self.root),
                "port": service_port,
                "command": (
                    'echo "environment=$DEVDECK_RESTART_TEST"; '
                    f"{shlex.quote(sys.executable)} -u -m http.server "
                    "{port} --bind 127.0.0.1 & wait"
                ),
            },
        }
        self.assertEqual(request(self.port, "/api/projects", payload)[0], 200)
        project = request(self.port, "/api/projects")[1][0]
        service = project["services"][0]
        log = self.root / "relative data" / f"{service['id']}.log"

        for _ in range(2):
            before = request(self.port, "/api/status")[1]
            self.assertEqual(request(self.port, "/api/action", {
                "project_id": project["id"], "action": "start"
            })[0], 200)
            running = request(self.port, "/api/projects")[1][0]["services"][0]
            self.child_groups.add(running["pid"])

            def listening():
                with socket.create_connection(("127.0.0.1", service_port), 0.1):
                    return True

            self.wait_for(listening)
            self.assertIn("environment=preserved", log.read_text())
            contents = log.read_bytes()
            status, accepted = request(self.port, "/api/restart", {})
            self.assertEqual(status, 202)
            self.assertEqual(accepted["instance_id"], before["instance_id"])
            self.wait_for(lambda: (
                request(self.port, "/api/status")[1]["instance_id"]
                != before["instance_id"]
            ))
            # exec 保留 PID，Popen 监管的原进程仍然存活。
            self.assertIsNone(self.process.poll())
            self.assertFalse(request(self.port, "/api/status")[1]["restarting"])
            restored = request(self.port, "/api/projects")[1][0]
            self.assertEqual(restored["id"], project["id"])
            self.assertEqual(restored["services"][0]["status"], "stopped")
            self.assertEqual(restored["services"][0]["command"], service["command"])
            self.assertTrue(log.read_bytes().startswith(contents))
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", service_port), 0.1)
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=5), 0)

    def test_sigterm_does_not_restart(self):
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=5), 0)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.port), 0.1)

    def test_idle_browser_connection_does_not_block_restart(self):
        # 模拟浏览器提前建立 TCP 连接却未发送请求；它必须有界退出。
        with socket.create_connection(("127.0.0.1", self.port), 0.1):
            before = request(self.port, "/api/status")[1]
            self.assertEqual(request(self.port, "/api/restart", {})[0], 202)
            self.wait_for(lambda: (
                request(self.port, "/api/status")[1]["instance_id"]
                != before["instance_id"]
            ))
            self.assertIsNone(self.process.poll())


if __name__ == "__main__":
    unittest.main()
