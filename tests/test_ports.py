"""通过真实监听 socket 验证端口检测、冲突拦截和配置生效。"""

import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from devdeck.ports import scan_ports, validate_port
from devdeck.server import Manager


class PortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = Manager(self.root / "data")

    def tearDown(self) -> None:
        self.manager.close()
        self.temporary.cleanup()

    def free_port(self) -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def add_service(self, port: int, command: str = "") -> dict[str, Any]:
        self.manager.add(
            {
                "name": "端口测试",
                "backend": {
                    "directory": str(self.root),
                    "port": port,
                    "command": command
                    or (
                        f"'{sys.executable}' -u -m http.server "
                        "{port} --bind 127.0.0.1"
                    ),
                },
            }
        )
        return self.manager.projects[-1]

    def wait_listening(self, port: int) -> None:
        for _ in range(100):
            try:
                with socket.create_connection(
                    ("127.0.0.1", port), timeout=0.1
                ):
                    return
            except OSError:
                time.sleep(0.03)
        self.fail(f"端口 {port} 未开始监听")

    def test_external_listener_blocks_start(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            project = self.add_service(port)
            snapshot = self.manager.snapshot()[0]["services"][0]
            self.assertEqual(snapshot["port_info"]["state"], "occupied")
            owners = snapshot["port_info"]["listeners"][0]["owners"]
            self.assertIn(os.getpid(), [owner["pid"] for owner in owners])
            with self.assertRaisesRegex(ValueError, f"端口 {port} 已被占用"):
                self.manager.action(project["id"], "start", None)
            self.assertFalse(self.manager.processes)
            # 冲突检测不会停止外部监听进程。
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                pass

    def test_bound_socket_blocks_start(self) -> None:
        # 尚未 listen 的绑定不会出现在监听列表中，也必须阻止启动。
        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            project = self.add_service(bound.getsockname()[1])
            with self.assertRaisesRegex(ValueError, "已被占用"):
                self.manager.action(project["id"], "start", None)

    def test_ipv6_listener(self) -> None:
        try:
            listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            listener.bind(("::1", 0))
        except OSError:
            self.skipTest("当前环境不支持 IPv6 回环监听")
        with listener:
            listener.listen()
            port = listener.getsockname()[1]
            matches = [
                item
                for item in scan_ports()["listeners"]
                if item["port"] == port and item["family"] == "IPv6"
            ]
            self.assertEqual(matches[0]["address"], "::1")
            project = self.add_service(port)
            with self.assertRaisesRegex(ValueError, "已被占用"):
                self.manager.action(project["id"], "start", None)

    def test_edit_template_environment_and_ownership(self) -> None:
        project = self.add_service(self.free_port())
        service = project["services"][0]
        port = self.free_port()
        # 先打印 PORT，再让 shell 派生的 Python 子进程监听占位符端口。
        command = f"echo PORT=$PORT; {service['command']}"
        payload = {
            "project_id": project["id"],
            "service_id": service["id"],
            "port": port,
            "command": command,
        }
        self.manager.update_port(payload)
        restored = Manager(self.root / "data")
        self.assertEqual(restored.projects[0]["services"][0]["port"], port)
        self.manager.action(project["id"], "start", None)
        self.wait_listening(port)
        info = self.manager.snapshot()[0]["services"][0]["port_info"]
        self.assertEqual(info["state"], "listening")
        managed = info["listeners"][0]["managed"]
        self.assertEqual(managed[0]["service_id"], service["id"])
        self.assertIn(f"PORT={port}", self.manager.logs(service["id"]))
        with self.assertRaisesRegex(ValueError, "先停止服务"):
            self.manager.update_port(payload)
        self.manager.action(project["id"], "restart", None)
        self.wait_listening(port)
        self.manager.action(project["id"], "stop", None)
        self.assertEqual(
            self.manager.snapshot()[0]["services"][0]["port_info"]["state"],
            "idle",
        )

    def test_duplicate_batch_does_not_partially_start(self) -> None:
        port = self.free_port()
        service = {
            "directory": str(self.root),
            "command": "sleep 60",
            "port": port,
        }
        self.manager.add(
            {"name": "重复端口", "frontend": service, "backend": service}
        )
        project = self.manager.projects[0]
        with self.assertRaisesRegex(ValueError, "重复配置端口"):
            self.manager.action(project["id"], "start", None)
        self.assertFalse(self.manager.processes)
        self.assertTrue(
            self.manager.snapshot()[0]["services"][0]["port_info"][
                "duplicates"
            ]
        )

    def test_starting_service_reserves_port(self) -> None:
        port = self.free_port()
        first = self.add_service(port, "sleep 60")
        second = self.add_service(port)
        self.manager.action(first["id"], "start", None)
        with self.assertRaisesRegex(ValueError, "预留"):
            self.manager.action(second["id"], "start", None)
        self.assertEqual(len(self.manager.processes), 1)

    def test_invalid_ports_and_missing_template_value(self) -> None:
        for value in (True, -1, 65536, 1.5, "3.5", "1; echo bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_port(value)
        self.assertEqual(validate_port("5173"), 5173)
        self.assertEqual(validate_port(""), 0)
        with self.assertRaisesRegex(ValueError, "必须配置端口"):
            self.add_service(0)


if __name__ == "__main__":
    unittest.main()
