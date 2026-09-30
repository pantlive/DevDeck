"""验证真实进程生命周期、配置持久化和 HTTP 来源限制。"""

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path

from devdeck.server import DevDeckServer, Manager


class ManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = Manager(self.root / "data")

    def tearDown(self) -> None:
        self.manager.close()
        self.temporary.cleanup()

    def test_lifecycle(self) -> None:
        # 真实 HTTP 服务验证启动、重复启动、端口释放和重启。
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.manager.add(
            {
                "name": "测试",
                "backend": {
                    "directory": str(self.root),
                    "command": f"'{sys.executable}' -u -m http.server {port} --bind 127.0.0.1",
                },
            }
        )
        project = self.manager.projects[0]
        service = project["services"][0]
        for _ in range(2):
            self.manager.action(project["id"], "start", None)
            process = self.manager.processes[service["id"]]
            self.manager.action(project["id"], "start", None)
            self.assertIs(process, self.manager.processes[service["id"]])
            for attempt in range(100):
                try:
                    with socket.create_connection(
                        ("127.0.0.1", port), timeout=0.1
                    ):
                        break
                except OSError:
                    time.sleep(0.03)
            else:
                self.fail("HTTP 服务未启动")
            self.assertIn("Serving HTTP", self.manager.logs(service["id"]))
            self.manager.action(project["id"], "stop", None)
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=0.1)
        self.assertEqual(
            Manager(self.root / "data").projects, self.manager.projects
        )
        self.manager.action(project["id"], "delete", None)
        self.assertEqual(self.manager.snapshot(), [])
        self.assertTrue(self.root.is_dir())

    def test_child_cleanup(self) -> None:
        # 心跳文件提供子进程确实停止的证据。
        script = self.root / "child.py"
        marker = self.root / "heartbeat"
        script.write_text(
            "import time\nfrom pathlib import Path\n"
            f"p = Path({str(marker)!r})\n"
            "while True:\n    p.write_text(str(time.time()))\n    time.sleep(.02)\n"
        )
        self.manager.add(
            {
                "name": "子进程",
                "backend": {
                    "directory": str(self.root),
                    "command": f"'{sys.executable}' '{script}' & wait",
                },
            }
        )
        project = self.manager.projects[0]
        self.manager.action(project["id"], "start", None)
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.02)
        self.assertTrue(marker.exists())
        self.manager.action(project["id"], "stop", None)
        time.sleep(0.05)
        previous = marker.read_text()
        time.sleep(0.1)
        self.assertEqual(previous, marker.read_text())

    def test_http(self) -> None:
        server = DevDeckServer(("127.0.0.1", 0), self.manager)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", "/")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b"DevDeck", response.read())
            connection.request(
                "POST",
                "/api/projects",
                json.dumps({"name": "bad"}),
                {
                    "Content-Type": "application/json",
                    "Origin": "https://untrusted.example",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.request(
                "GET", "/api/projects", headers={"Host": "untrusted.example"}
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
