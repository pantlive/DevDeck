"""生成并写入 DevDeck 的 systemd 用户单元。"""

from __future__ import annotations

import argparse
from pathlib import Path


def quote_unit_value(value: str) -> str:
    """按 systemd 单元文件规则给含空白的路径加引号。"""
    if value == "":
        return '""'
    if any(character.isspace() for character in value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def render_user_unit(
    python: Path,
    project_dir: Path,
    port: int,
    data_dir: Path,
    log_path: Path,
) -> str:
    """生成可 enable 的用户单元，随 default.target 启动，崩溃后自动拉起。

    不使用 systemd-run 的 transient 单元：Windows 登录任务里的 wsl.exe 退出后，
    用户 systemd 会进入 exit.target，transient 服务会被一起停掉。
    """
    exec_start = " ".join(
        [
            quote_unit_value(str(python)),
            "-m",
            "devdeck",
            "--port",
            str(port),
            "--data-dir",
            quote_unit_value(str(data_dir)),
        ]
    )
    return "\n".join(
        [
            "[Unit]",
            "Description=DevDeck console",
            "After=network.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={quote_unit_value(str(project_dir))}",
            f"ExecStart={exec_start}",
            "Restart=on-failure",
            "RestartSec=2",
            "Environment=PYTHONUNBUFFERED=1",
            f"StandardOutput=append:{log_path}",
            f"StandardError=append:{log_path}",
            "KillMode=control-group",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def write_user_unit(
    path: Path,
    python: Path,
    project_dir: Path,
    port: int,
    data_dir: Path,
    log_path: Path,
) -> Path:
    """把用户单元写到指定路径，目录不存在时创建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_user_unit(
            python=python,
            project_dir=project_dir,
            port=port,
            data_dir=data_dir,
            log_path=log_path,
        ),
        encoding="utf-8",
    )
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="写入 DevDeck systemd 用户单元")
    parser.add_argument("--unit-path", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=7310)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--log-path", type=Path, required=True)
    args = parser.parse_args()
    write_user_unit(
        path=args.unit_path,
        python=args.python,
        project_dir=args.project_dir,
        port=args.port,
        data_dir=args.data_dir,
        log_path=args.log_path,
    )


if __name__ == "__main__":
    main()
