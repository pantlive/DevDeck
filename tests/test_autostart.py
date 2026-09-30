"""验证 systemd 用户单元会随 default.target 持久拉起，而不是 transient。"""

import tempfile
import unittest
from pathlib import Path

from devdeck.autostart import render_user_unit, write_user_unit


class AutostartTests(unittest.TestCase):
    def test_render_persistent_user_unit(self) -> None:
        # 单元必须能 enable，并在用户 systemd 重启后由 default.target 再次拉起。
        text = render_user_unit(
            python=Path("/home/lsl/miniconda3/bin/python"),
            project_dir=Path("/home/lsl/project/github/DevDeck"),
            port=7310,
            data_dir=Path("/home/lsl/project/github/DevDeck/.devdeck"),
            log_path=Path(
                "/home/lsl/project/github/DevDeck/.devdeck/devdeck-startup.log"
            ),
        )
        self.assertIn("WantedBy=default.target", text)
        self.assertIn("Restart=on-failure", text)
        self.assertIn("-m devdeck --port 7310 --data-dir", text)
        self.assertNotIn("CollectMode", text)
        self.assertNotIn("systemd-run", text)

    def test_write_creates_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "systemd" / "user" / "devdeck.service"
            write_user_unit(
                path=path,
                python=Path("/opt/python"),
                project_dir=Path("/tmp/DevDeck"),
                port=7311,
                data_dir=Path("/tmp/data"),
                log_path=Path("/tmp/data/devdeck-startup.log"),
            )
            content = path.read_text(encoding="utf-8")
            self.assertTrue(path.is_file())
            self.assertIn("--port 7311", content)
            self.assertIn("WorkingDirectory=/tmp/DevDeck", content)


if __name__ == "__main__":
    unittest.main()
