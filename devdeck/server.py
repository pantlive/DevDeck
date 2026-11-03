"""管理本地进程并提供仅限本机访问的 HTTP 控制台。"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from devdeck.git import (
    CONSOLE_BRANCH_ERROR,
    CONSOLE_BRANCHES,
    DEFAULT_CONSOLE_BRANCH,
    checkout_branch,
    console_branch_options,
    console_branch_rejection,
    console_branch_target,
    fetch_remotes,
    inspect_repo,
    restrict_console_branches,
    validate_branch,
)
from devdeck.ports import ensure_available, scan_ports, validate_port


def default_console_dir() -> Path:
    """控制台代码所在仓库；测试可用 DEVDECK_CONSOLE_DIR 指到临时仓库。"""
    override = os.environ.get("DEVDECK_CONSOLE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent


class Manager:
    """保存项目配置，管理当前控制台启动的进程组。"""

    def __init__(self, data_dir: Path, console_dir: Path | None = None) -> None:
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config = self.data_dir / "projects.json"
        self.console_config = self.data_dir / "console.json"
        self.console_dir = (console_dir or default_console_dir())
        try:
            self.console_dir = self.console_dir.expanduser().resolve()
        except OSError:
            self.console_dir = self.console_dir.expanduser()
        self.projects = (
            json.loads(self.config.read_text()) if self.config.exists() else []
        )
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.lock = threading.RLock()

    def save(self) -> None:
        """原子替换配置，避免写入中断破坏已有配置。"""
        temporary = self.config.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.projects, ensure_ascii=False, indent=2)
        )
        temporary.replace(self.config)

    def project(self, project_id: str) -> dict[str, Any]:
        for project in self.projects:
            if project["id"] == project_id:
                return project
        raise ValueError("项目不存在")

    def log_path(self, service_id: str) -> Path:
        return self.data_dir / f"{service_id}.log"

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            ports = self.ports()
            git_cache: dict[str, dict[str, Any] | None] = {}
            console_root = self.console_root()
            result = json.loads(json.dumps(self.projects))
            for project in result:
                for service in project["services"]:
                    process = self.processes.get(service["id"])
                    code = process.poll() if process else None
                    service.setdefault("branch", "")
                    service.update(
                        status="running"
                        if process and code is None
                        else "stopped",
                        pid=process.pid if process and code is None else None,
                        exit_code=code,
                        port_info=self.port_info(service, ports),
                        git=self.git_info(
                            service["directory"], git_cache, console_root
                        ),
                    )
            return result

    def console_root(self) -> str:
        """返回控制台自身工作区根目录，用于识别同仓库的项目服务。"""
        info = inspect_repo(self.console_dir)
        return str(info["root"]) if info and info.get("root") else ""

    def git_info(
        self,
        directory: str,
        cache: dict[str, dict[str, Any] | None],
        console_root: str = "",
    ) -> dict[str, Any] | None:
        """同一仓库只探测一次，避免一个项目的前后端重复跑 Git。"""
        resolved = str(Path(directory))
        if resolved not in cache:
            info = inspect_repo(Path(directory))
            if info and info.get("root"):
                if console_root and info["root"] == console_root:
                    info = restrict_console_branches(info)
                cache[info["root"]] = info
            cache[resolved] = info
        return cache[resolved]

    def ports(self) -> dict[str, Any]:
        """将监听进程的进程组关联到托管服务，不根据端口号猜测归属。"""
        with self.lock:
            result = scan_ports()
            for listener in result["listeners"]:
                managed = []
                for project in self.projects:
                    for service in project["services"]:
                        process = self.processes.get(service["id"])
                        if process and any(
                            owner["group"] == process.pid
                            for owner in listener["owners"]
                        ):
                            managed.append(
                                {
                                    "project_id": project["id"],
                                    "service_id": service["id"],
                                    "name": project["name"],
                                    "kind": service["kind"],
                                }
                            )
                listener["managed"] = managed
            return result

    def port_info(
        self, service: dict[str, Any], ports: dict[str, Any]
    ) -> dict[str, Any]:
        """区分当前服务监听、其他进程占用、未监听和检测失败。"""
        port = service["port"]
        listeners = [
            item for item in ports["listeners"] if item["port"] == port
        ]
        duplicates = [
            project["name"]
            for project in self.projects
            for other in project["services"]
            if port and other["port"] == port and other["id"] != service["id"]
        ]
        if not port:
            state = "unset"
        elif listeners:
            owned = [
                any(
                    item["service_id"] == service["id"]
                    for item in listener["managed"]
                )
                for listener in listeners
            ]
            state = "listening" if all(owned) else "occupied"
        else:
            state = "unknown" if ports["warning"] else "idle"
        return {
            "state": state,
            "listeners": listeners,
            "duplicates": duplicates,
        }

    def update_port(self, payload: dict[str, Any]) -> None:
        """停止服务后更新端口和命令；已有命令中的数字不自动改写。"""
        port = validate_port(payload.get("port"))
        command = str(payload.get("command", "")).strip()
        if not command or ("{port}" in command and not port):
            raise ValueError("请填写启动命令；使用 {port} 时必须配置端口")
        with self.lock:
            project = self.project(payload["project_id"])
            service = next(
                (
                    s
                    for s in project["services"]
                    if s["id"] == payload["service_id"]
                ),
                None,
            )
            if service is None:
                raise ValueError("服务不存在")
            process = self.processes.get(service["id"])
            if process and process.poll() is None:
                raise ValueError("请先停止服务，再修改端口和命令")
            service.update(port=port, command=command)
            self.save()

    def check_ports(self, services: list[dict[str, Any]]) -> None:
        """批量启动前检查冲突，包括尚未监听的托管服务预留端口。"""
        used: set[int] = set()
        listeners = scan_ports()["listeners"]
        for service in services:
            port = service["port"]
            process = self.processes.get(service["id"])
            if not port or (process and process.poll() is None):
                continue
            if port in used:
                raise ValueError(f"所选服务重复配置端口 {port}，请先修改端口")
            used.add(port)
            for project in self.projects:
                for other in project["services"]:
                    running = self.processes.get(other["id"])
                    if (
                        other["port"] == port
                        and running
                        and running.poll() is None
                    ):
                        raise ValueError(
                            f"端口 {port} 已由运行中的项目「{project['name']}」预留"
                        )
            matches = [item for item in listeners if item["port"] == port]
            if matches:
                owners = sorted(
                    {
                        f"{owner['name']} (PID {owner['pid']})"
                        for item in matches
                        for owner in item["owners"]
                    }
                )
                detail = "、".join(owners) or "进程信息不可见"
                raise ValueError(f"端口 {port} 已被占用：{detail}")
            ensure_available(port)

    def add(self, payload: dict[str, Any]) -> None:
        name = str(payload.get("name", "")).strip()
        if not name or len(name) > 100:
            raise ValueError("项目名称不能为空，且不得超过 100 字")
        services = []
        for kind in ("frontend", "backend"):
            item = payload.get(kind, {})
            command = str(item.get("command", "")).strip()
            if not command:
                continue
            directory = Path(str(item.get("directory", ""))).expanduser()
            if not directory.is_absolute() or not directory.is_dir():
                raise ValueError("服务目录必须是存在的绝对路径")
            port = validate_port(item.get("port"))
            if "{port}" in command and not port:
                raise ValueError("启动命令包含 {port} 时必须配置端口")
            branch = str(item.get("branch", "")).strip()
            if branch:
                validate_branch(branch)
                self.validate_console_branch_target(directory, branch)
            services.append(
                dict(
                    id=uuid.uuid4().hex,
                    kind=kind,
                    directory=str(directory.resolve()),
                    command=command,
                    port=port,
                    branch=branch,
                )
            )
        if not services:
            raise ValueError("请至少配置一个服务的启动命令")
        with self.lock:
            self.projects.append(
                dict(id=uuid.uuid4().hex, name=name, services=services)
            )
            self.save()

    def git_root(self, directory: str) -> str:
        info = inspect_repo(Path(directory))
        return str(info["root"]) if info and info.get("root") else ""

    def ensure_repo_idle(self, root: str) -> None:
        """同一仓库只要有托管进程在跑，就不切换分支，避免改正在运行的代码。"""
        if not root:
            return
        for project in self.projects:
            for service in project["services"]:
                process = self.processes.get(service["id"])
                if process is None or process.poll() is not None:
                    continue
                if self.git_root(service["directory"]) == root:
                    raise ValueError(
                        f"请先停止项目「{project['name']}」的服务，再切换分支"
                    )

    def ensure_console_switch_allowed(self, directory: Path, branch: str) -> None:
        """控制台自身仓库只允许包含控制台代码的 feat / dev / main，且不能在任务 worktree 中切换。"""
        info = inspect_repo(directory)
        console = inspect_repo(self.console_dir)
        if (
            not info
            or not info.get("root")
            or not console
            or info["root"] != console.get("root")
        ):
            return
        if (info.get("current") or "").startswith("agents/"):
            raise ValueError(
                "当前 DevDeck 运行在任务 worktree 中，不能切换 feat、dev 或 main"
            )
        if console_branch_target(info, branch) is None:
            raise ValueError(console_branch_rejection(info, branch))

    def validate_console_branch_target(
        self, directory: Path, branch: str
    ) -> None:
        """保存配置时也拒绝把控制台仓库指向 agents 等非主线分支。"""
        info = inspect_repo(directory)
        console = inspect_repo(self.console_dir)
        if (
            not info
            or not info.get("root")
            or not console
            or info["root"] != console.get("root")
        ):
            return
        if console_branch_target(info, branch) is None:
            raise ValueError(console_branch_rejection(info, branch))

    def prepare_branches(self, services: list[dict[str, Any]]) -> None:
        """启动前按配置检出分支；同一仓库只切一次，冲突的选择直接失败。"""
        planned: dict[str, str] = {}
        owners: dict[str, dict[str, Any]] = {}
        for service in services:
            branch = str(service.get("branch") or "").strip()
            if not branch:
                continue
            root = self.git_root(service["directory"])
            if not root:
                raise ValueError("服务目录不是 Git 仓库，无法按分支启动")
            previous = planned.get(root)
            if previous and previous != branch:
                raise ValueError("同一仓库的服务选择了不同分支")
            self.ensure_console_switch_allowed(Path(service["directory"]), branch)
            planned[root] = branch
            owners[root] = service
        for root, branch in planned.items():
            self.ensure_repo_idle(root)
            actual = checkout_branch(Path(owners[root]["directory"]), branch)
            for project in self.projects:
                for service in project["services"]:
                    if self.git_root(service["directory"]) == root:
                        service["branch"] = actual
            self.save()

    def checkout_project_branch(self, payload: dict[str, Any]) -> None:
        """停止服务后切换分支，并把该仓库下的服务都记到同一分支。"""
        branch = validate_branch(payload.get("branch"))
        with self.lock:
            project = self.project(payload["project_id"])
            service_id = payload.get("service_id")
            selected = [
                service
                for service in project["services"]
                if not service_id or service["id"] == service_id
            ]
            if not selected:
                raise ValueError("服务不存在")
            roots: dict[str, dict[str, Any]] = {}
            for service in selected:
                root = self.git_root(service["directory"])
                if not root:
                    raise ValueError("服务目录不是 Git 仓库")
                roots[root] = service
            if not service_id and len(roots) > 1:
                raise ValueError("项目包含多个 Git 仓库，请选择要切换的服务")
            for root, service in roots.items():
                self.ensure_repo_idle(root)
                self.ensure_console_switch_allowed(
                    Path(service["directory"]), branch
                )
                actual = checkout_branch(Path(service["directory"]), branch)
                for item in project["services"]:
                    if self.git_root(item["directory"]) == root:
                        item["branch"] = actual
            self.save()

    def saved_console_branch(self) -> str:
        if not self.console_config.exists():
            return ""
        try:
            data = json.loads(self.console_config.read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            return ""
        if not isinstance(data, dict):
            return ""
        return str(data.get("branch") or "").strip()

    def save_console_branch(self, branch: str) -> None:
        temporary = self.console_config.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"branch": branch}, ensure_ascii=False, indent=2)
        )
        temporary.replace(self.console_config)

    def console_snapshot(self) -> dict[str, Any]:
        """控制台自身的 feat / dev / main 切换状态；只列包含控制台代码的分支。"""
        saved = self.saved_console_branch()
        info = inspect_repo(self.console_dir)
        options = console_branch_options(info)
        names = [item["local_name"] for item in options]
        if saved in names:
            preferred = saved
        elif DEFAULT_CONSOLE_BRANCH in names:
            preferred = DEFAULT_CONSOLE_BRANCH
        elif info and info.get("current") in names:
            preferred = info["current"]
        else:
            preferred = names[0] if names else ""
        return {
            "root": (info or {}).get("root") or "",
            "current": (info or {}).get("current") or "",
            "detached": bool((info or {}).get("detached")),
            "dirty": bool((info or {}).get("dirty")),
            "preferred": preferred,
            "saved": saved,
            "default": DEFAULT_CONSOLE_BRANCH,
            "warning": (info or {}).get("warning") or "",
            "branches": options,
        }

    def set_console_branch(self, branch: Any) -> str:
        """把控制台仓库切到 feat、dev 或 main，并记下下次启用时使用的分支。"""
        name = validate_branch(branch)
        if name not in CONSOLE_BRANCHES:
            raise ValueError(CONSOLE_BRANCH_ERROR)
        with self.lock:
            info = inspect_repo(self.console_dir)
            options = console_branch_options(info)
            if not options:
                raise ValueError(
                    (info or {}).get("warning")
                    or "控制台目录没有包含 DevDeck 代码的可切换分支"
                )
            target = next(
                (item for item in options if item["local_name"] == name),
                None,
            )
            if target is None:
                raise ValueError(console_branch_rejection(info, name))
            current = (info or {}).get("current") or ""
            if current.startswith("agents/"):
                raise ValueError(
                    "当前 DevDeck 运行在任务 worktree 中，不能切换 feat、dev 或 main"
                )
            if info and info.get("root"):
                self.ensure_repo_idle(str(info["root"]))
            actual = checkout_branch(self.console_dir, name)
            self.save_console_branch(actual)
            return actual

    def prepare_console_branch(self) -> None:
        """启用控制台时检出已保存的分支；尚未选择时保持当前检出，页面默认推荐 feat。"""
        saved = self.saved_console_branch()
        if not saved or saved not in CONSOLE_BRANCHES:
            return
        info = inspect_repo(self.console_dir)
        current = (info or {}).get("current") or ""
        if not info or not info.get("root") or current == saved:
            return
        if current.startswith("agents/"):
            return
        if console_branch_target(info, saved) is None:
            return
        self.set_console_branch(saved)

    def fetch_project(self, payload: dict[str, Any]) -> None:
        """只更新远程跟踪引用，用于发现最新分支，不改变当前检出。"""
        with self.lock:
            project = self.project(payload["project_id"])
            service_id = payload.get("service_id")
            selected = [
                service
                for service in project["services"]
                if not service_id or service["id"] == service_id
            ]
            if not selected:
                raise ValueError("服务不存在")
            fetched: set[str] = set()
            for service in selected:
                root = self.git_root(service["directory"])
                if not root or root in fetched:
                    continue
                fetch_remotes(Path(service["directory"]))
                fetched.add(root)
            if not fetched:
                raise ValueError("服务目录不是 Git 仓库")

    def start(self, service: dict[str, Any], *, prepared: bool = False) -> None:
        service_id = service["id"]
        previous = self.processes.get(service_id)
        if previous and previous.poll() is None:
            return
        # 清理已退出父进程遗留的子进程，防止重复启动。
        if previous:
            self.stop(service_id)
        if not prepared:
            self.prepare_branches([service])
        self.check_ports([service])
        # 显式占位符与 PORT 环境变量使端口配置可以真正传给应用。
        command = service["command"]
        environment = os.environ.copy()
        if service["port"]:
            command = command.replace("{port}", str(service["port"]))
            environment["PORT"] = str(service["port"])
        git = inspect_repo(Path(service["directory"]))
        if git and git.get("current"):
            environment["DEVDECK_BRANCH"] = git["current"]
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        branch_note = f" · 分支 {git['current']}" if git and git.get("current") else ""
        with self.log_path(service_id).open("ab", buffering=0) as output:
            output.write(f"\n--- 启动 {started}{branch_note} ---\n".encode())
            # 独立进程组确保停止时同时终止 npm、shell 等派生的子进程。
            self.processes[service_id] = subprocess.Popen(
                ["/bin/bash", "-lc", command],
                cwd=service["directory"],
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )

    def stop(self, service_id: str) -> None:
        process = self.processes.get(service_id)
        if process is None:
            return
        # 先通知整个进程组退出，再强制回收可能忽略信号的子进程。
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

    def action(
        self, project_id: str, action: str, service_id: str | None
    ) -> None:
        with self.lock:
            project = self.project(project_id)
            services = [
                s
                for s in project["services"]
                if not service_id or s["id"] == service_id
            ]
            if not services:
                raise ValueError("服务不存在")
            if action not in {"start", "stop", "restart", "delete"}:
                raise ValueError("不支持的操作")
            if action == "delete" and service_id:
                raise ValueError("只能删除整个项目")
            # 批量启动先检查全部端口，避免发现配置冲突时只启动了一半。
            if action in {"stop", "restart", "delete"}:
                for service in services:
                    self.stop(service["id"])
            if action in {"start", "restart"}:
                self.prepare_branches(services)
                self.check_ports(services)
            for service in services:
                if action in {"start", "restart"}:
                    self.start(service, prepared=True)
            if action == "delete":
                self.projects.remove(project)
                self.save()

    def logs(self, service_id: str) -> str:
        with self.lock:
            if not any(
                s["id"] == service_id
                for p in self.projects
                for s in p["services"]
            ):
                raise ValueError("服务不存在")
            path = self.log_path(service_id)
            if not path.exists():
                return "暂无日志，启动服务后将在这里显示。"
            # 只读取日志尾部，避免大文件阻塞控制台。
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 64000))
                return stream.read().decode("utf-8", errors="replace")

    def close(self) -> None:
        with self.lock:
            for service_id in self.processes:
                self.stop(service_id)


class DevDeckServer(ThreadingHTTPServer):
    """在原进程中重启，兼容终端和 systemd 的进程监管。"""

    daemon_threads = False

    def __init__(self, address: tuple[str, int], manager: Manager) -> None:
        self.instance_id = uuid.uuid4().hex
        self.restarting = False
        super().__init__(address, handler_for(manager))


def handler_for(manager: Manager) -> type[BaseHTTPRequestHandler]:
    """构造共享进程管理器的请求处理器。"""

    class Handler(BaseHTTPRequestHandler):
        # 浏览器的空闲预连接不能让 server_close 一直等待请求线程。
        timeout = 5

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def reply(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def trusted(self) -> bool:
            # 校验 Host 和来源，防止其他网站借浏览器调用本地命令接口。
            expected = f"127.0.0.1:{self.server.server_port}"
            alternate = f"localhost:{self.server.server_port}"
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            return host in {expected, alternate} and (
                origin is None
                or origin in {f"http://{expected}", f"http://{alternate}"}
            )

        def do_GET(self) -> None:
            if not self.trusted():
                self.reply(403, {"error": "仅允许本机同源访问"})
                return
            path = urlparse(self.path).path
            try:
                if path == "/api/status":
                    self.reply(200, {
                        "instance_id": self.server.instance_id,
                        "restarting": self.server.restarting,
                        "console": manager.console_snapshot(),
                    })
                elif path == "/api/projects":
                    self.reply(200, manager.snapshot())
                elif path == "/api/ports":
                    self.reply(200, manager.ports())
                elif path.startswith("/api/logs/"):
                    self.reply(
                        200, {"text": manager.logs(path.rsplit("/", 1)[-1])}
                    )
                elif path == "/":
                    body = Path(__file__).with_name("index.html").read_bytes()
                    self.send_response(200)
                    self.send_header(
                        "Content-Type", "text/html; charset=utf-8"
                    )
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.reply(404, {"error": "页面不存在"})
            except ValueError as error:
                self.reply(400, {"error": str(error)})

        def do_POST(self) -> None:
            if (
                not self.trusted()
                or self.headers.get("Content-Type") != "application/json"
            ):
                self.reply(403, {"error": "仅接受本机同源 JSON 请求"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("请求体大小不正确")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("请求必须为 JSON 对象")
                # 与服务操作共用锁，确保重启开始后不再启动进程或写入配置。
                with manager.lock:
                    if self.server.restarting:
                        self.reply(503, {"error": "DevDeck 正在重启，请稍候"})
                        return
                    if self.path == "/api/restart":
                        self.server.restarting = True
                        try:
                            self.reply(202, {
                                "ok": True,
                                "instance_id": self.server.instance_id,
                            })
                            self.wfile.flush()
                        finally:
                            # 先返回响应，再从其他线程结束 serve_forever。
                            threading.Thread(
                                target=self.server.shutdown, daemon=True
                            ).start()
                        return
                    if self.path == "/api/projects":
                        manager.add(payload)
                    elif self.path == "/api/action":
                        manager.action(
                            payload["project_id"],
                            payload["action"],
                            payload.get("service_id"),
                        )
                    elif self.path == "/api/ports":
                        manager.update_port(payload)
                    elif self.path == "/api/branch":
                        if payload.get("fetch"):
                            manager.fetch_project(payload)
                        else:
                            manager.checkout_project_branch(payload)
                    elif self.path == "/api/console/branch":
                        manager.set_console_branch(payload.get("branch"))
                    else:
                        self.reply(404, {"error": "接口不存在"})
                        return
                    self.reply(200, {"ok": True})
            except (
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                OSError,
            ) as error:
                self.reply(400, {"error": str(error)})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="DevDeck 本地开发项目中枢")
    parser.add_argument("--port", type=int, default=7310)
    parser.add_argument("--data-dir", type=Path, default=Path(".devdeck"))
    args = parser.parse_args()
    manager = Manager(args.data_dir)
    try:
        manager.prepare_console_branch()
    except ValueError as error:
        print(f"控制台分支未切换：{error}", flush=True)
    server = DevDeckServer(("127.0.0.1", args.port), manager)
    stopping = False

    def shutdown(signum: int, frame: Any) -> None:
        nonlocal stopping
        stopping = True
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"DevDeck 已启动：http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        manager.close()
    if server.restarting and not stopping:
        print("DevDeck 正在重启…", flush=True)
        # 保留解释器选项、启动参数、环境和工作目录；PID 不变，systemd 无需重配。
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
