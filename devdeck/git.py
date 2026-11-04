"""读取项目 Git 分支，标出最近更新的分支，并在工作区干净时切换。"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

# 切换分支时禁用仓库 hooks，避免 post-checkout 脚本改文件或访问网络。
_GIT_FLAGS = ["-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0"]
_UNSET_GIT = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
)
_HIDDEN_NAMES = {"HEAD", "FETCH_HEAD", "ORIG_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD"}
CONSOLE_BRANCHES = ("feat", "dev", "main")
# 系统启动默认基于 dev：日常集成分支是 dev，feat 和 main 只在需要时手动切换。
DEFAULT_CONSOLE_BRANCH = "dev"
CONSOLE_BRANCH_ERROR = "控制台仓库只支持切换 feat、dev 或 main"
# 用入口文件判断分支是否真的包含控制台代码，避免切到只有 README 的旧 main。
CONSOLE_CODE_PATH = "devdeck/__main__.py"
_REF_FORMAT = (
    "%(objectname:short)%09%(committerdate:unix)%09"
    "%(committerdate:iso-strict)%09%(refname)%09%(HEAD)%09%(upstream:short)"
)


def git_environment() -> dict[str, str]:
    """去掉会把命令指到错误仓库的 Git 变量，并禁止等待凭据输入。"""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in _UNSET_GIT
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_ASKPASS"] = ""
    environment["GCM_INTERACTIVE"] = "never"
    return environment


def public_git_error(result: subprocess.CompletedProcess[str]) -> str:
    """只保留最后一行 Git 错误，去掉 URL 中的用户名和口令。"""
    text = (result.stderr or result.stdout or "").strip()
    text = re.sub(r"://[^/\s]+@", "://", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:200] if lines else "Git 命令失败"


def run_git(
    directory: Path,
    *arguments: str,
    timeout: float = 5,
) -> subprocess.CompletedProcess[str]:
    """在指定目录执行 git；未安装 Git 时抛出 FileNotFoundError。"""
    return subprocess.run(
        ["git", *_GIT_FLAGS, *arguments],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=git_environment(),
        check=False,
    )


def validate_branch(value: Any) -> str:
    """校验可切换的分支名，拒绝 HEAD 等伪引用和 Git 非法字符。"""
    name = str(value or "").strip()
    if (
        not name
        or len(name) > 200
        or name in _HIDDEN_NAMES
        or name.startswith("-")
        or name.startswith("/")
        or name.endswith("/")
        or ".." in name
        or "@{" in name
        or any(character.isspace() for character in name)
        or any(character in name for character in "~^:?*[\\")
    ):
        raise ValueError("分支名称无效")
    command = ["check-ref-format"]
    if "/" not in name:
        command.append("--allow-onelevel")
    try:
        result = run_git(Path("/"), *command, name)
    except FileNotFoundError as error:
        raise ValueError("未安装 Git，无法校验分支名") from error
    except subprocess.TimeoutExpired as error:
        raise ValueError("校验分支名超时") from error
    if result.returncode != 0:
        raise ValueError("分支名称无效")
    return name


def find_repo(directory: Path) -> Path | None:
    """从服务目录向上找到 Git 工作区根目录。"""
    if not directory.is_dir():
        return None
    try:
        result = run_git(directory, "rev-parse", "--show-toplevel")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    root = Path(result.stdout.strip())
    try:
        return root.resolve() if root.is_dir() else None
    except OSError:
        return None


def inspect_repo(directory: Path) -> dict[str, Any] | None:
    """返回当前分支、是否有未提交改动、以及按提交时间排序的分支列表。"""
    try:
        root = find_repo(directory)
    except OSError:
        return None
    if root is None:
        try:
            run_git(Path("/"), "--version")
        except FileNotFoundError:
            return {
                "root": "",
                "current": "",
                "head": "",
                "detached": False,
                "dirty": False,
                "latest": "",
                "has_remotes": False,
                "warning": "未安装 Git，无法读取分支",
                "branches": [],
            }
        except subprocess.TimeoutExpired:
            return None
        return None
    try:
        current = run_git(root, "branch", "--show-current")
        head = run_git(root, "rev-parse", "--short", "HEAD")
        status = run_git(root, "status", "--porcelain", "--untracked-files=no")
        remotes = run_git(root, "remote")
        refs = run_git(
            root,
            "for-each-ref",
            "--sort=-committerdate",
            f"--format={_REF_FORMAT}",
            "refs/heads/",
            "refs/remotes/",
        )
    except FileNotFoundError:
        return {
            "root": str(root),
            "current": "",
            "head": "",
            "detached": False,
            "dirty": False,
            "latest": "",
            "has_remotes": False,
            "warning": "未安装 Git，无法读取分支",
            "branches": [],
        }
    except subprocess.TimeoutExpired:
        return {
            "root": str(root),
            "current": "",
            "head": "",
            "detached": False,
            "dirty": False,
            "latest": "",
            "has_remotes": False,
            "warning": "读取 Git 分支超时",
            "branches": [],
        }
    warning = ""
    if any(
        command.returncode != 0
        for command in (current, status, remotes, refs)
    ):
        warning = public_git_error(
            next(
                command
                for command in (current, status, remotes, refs)
                if command.returncode != 0
            )
        )
    current_name = current.stdout.strip() if current.returncode == 0 else ""
    head_name = head.stdout.strip() if head.returncode == 0 else ""
    remote_names = [
        line.strip()
        for line in remotes.stdout.splitlines()
        if line.strip()
    ]
    local_branches, remote_branches = parse_refs(refs.stdout, remote_names)
    local_names = {item["local_name"] for item in local_branches}
    for item in local_branches:
        upstream = next(
            (
                remote
                for remote in remote_branches
                if remote["name"] == item["upstream"]
            ),
            None,
        )
        item["sort_at"] = item["timestamp"]
        if upstream:
            item["upstream_commit"] = upstream["commit"]
            item["upstream_at"] = upstream["updated_at"]
            if upstream["timestamp"] > item["timestamp"]:
                item["sort_at"] = upstream["timestamp"]
    remote_only = [
        item
        for item in remote_branches
        if item["local_name"] not in local_names
    ]
    branches = local_branches + remote_only
    branches.sort(key=lambda item: (-item["sort_at"], item["name"]))
    latest = ""
    if branches:
        latest = branches[0]["name"]
        branches[0]["latest"] = True
    for item in branches:
        item.pop("timestamp", None)
        item.pop("sort_at", None)
    return {
        "root": str(root),
        "current": current_name,
        "head": head_name,
        "detached": not current_name and bool(head_name),
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else False,
        "latest": latest,
        "has_remotes": bool(remote_names),
        "warning": warning,
        "branches": branches,
    }


def parse_refs(
    output: str, remote_names: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 for-each-ref 输出拆成本地分支和远程跟踪分支。"""
    remotes = sorted(remote_names, key=len, reverse=True)
    local_branches: list[dict[str, Any]] = []
    remote_branches: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        commit, timestamp, updated_at, refname, marker, upstream = parts[:6]
        try:
            unix = int(timestamp)
        except ValueError:
            continue
        if refname.startswith("refs/heads/"):
            name = refname[len("refs/heads/") :]
            if not name or name in _HIDDEN_NAMES:
                continue
            local_branches.append(
                branch_entry(
                    name=name,
                    local_name=name,
                    commit=commit,
                    timestamp=unix,
                    updated_at=updated_at,
                    current=marker.strip() == "*",
                    remote=False,
                    upstream=upstream.strip(),
                )
            )
        elif refname.startswith("refs/remotes/"):
            short = refname[len("refs/remotes/") :]
            if not short or short.endswith("/HEAD"):
                continue
            remote = next(
                (item for item in remotes if short == item or short.startswith(f"{item}/")),
                short.split("/", 1)[0],
            )
            local_name = short[len(remote) + 1 :] if short.startswith(f"{remote}/") else short
            if not local_name or local_name in _HIDDEN_NAMES:
                continue
            remote_branches.append(
                branch_entry(
                    name=short,
                    local_name=local_name,
                    commit=commit,
                    timestamp=unix,
                    updated_at=updated_at,
                    current=False,
                    remote=True,
                    upstream="",
                )
            )
    return local_branches, remote_branches


def branch_entry(
    name: str,
    local_name: str,
    commit: str,
    timestamp: int,
    updated_at: str,
    current: bool,
    remote: bool,
    upstream: str,
) -> dict[str, Any]:
    """统一分支条目字段，供页面列出和选择。"""
    return {
        "name": name,
        "local_name": local_name,
        "commit": commit,
        "timestamp": timestamp,
        "sort_at": timestamp,
        "updated_at": updated_at,
        "current": current,
        "latest": False,
        "remote": remote,
        "upstream": upstream,
        "upstream_commit": "",
        "upstream_at": "",
    }


def branch_has_console_code(repo: Path, reference: str) -> bool:
    """检查分支是否包含控制台入口文件，用于排除只有 README 的旧 main。"""
    try:
        result = run_git(
            repo, "cat-file", "-e", f"{reference}:{CONSOLE_CODE_PATH}"
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _public_branch_option(item: dict[str, Any]) -> dict[str, Any]:
    """去掉内部排序字段，避免控制台选项把 timestamp 传到页面。"""
    option = dict(item)
    option.pop("timestamp", None)
    option.pop("sort_at", None)
    return option


def _remote_console_option(
    root: Path,
    name: str,
    local: dict[str, Any] | None,
    remote: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """本地缺入口文件时，找出已 fetch、且包含控制台代码的远程引用。"""
    refs: list[str] = []
    if remote:
        refs.append(remote["name"])
    if local and local.get("upstream"):
        refs.append(local["upstream"])
    refs.append(f"origin/{name}")
    seen: set[str] = set()
    for ref in refs:
        if not ref or ref in seen:
            continue
        seen.add(ref)
        if not branch_has_console_code(root, ref):
            continue
        if remote and remote["name"] == ref:
            return _public_branch_option(remote)
        commit = (local or {}).get("upstream_commit") or ""
        updated_at = (local or {}).get("upstream_at") or ""
        if not commit:
            try:
                parsed = run_git(root, "rev-parse", "--short", ref)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                parsed = None
            if parsed is not None and parsed.returncode == 0:
                commit = parsed.stdout.strip()
        option = branch_entry(
            name=ref,
            local_name=name,
            commit=commit,
            timestamp=0,
            updated_at=updated_at,
            current=False,
            remote=True,
            upstream="",
        )
        return _public_branch_option(option)
    return None


def console_branch_options(info: dict[str, Any] | None) -> list[dict[str, Any]]:
    """控制台只提供包含控制台代码的 feat / dev / main。

    同名时优先本地；本地缺入口文件、已 fetch 的远程有入口文件时改用远程。
    """
    if not info:
        return []
    root = Path(info["root"]) if info.get("root") else None
    options: list[dict[str, Any]] = []
    for name in CONSOLE_BRANCHES:
        local = next(
            (
                item
                for item in info["branches"]
                if item["local_name"] == name and not item["remote"]
            ),
            None,
        )
        remote = next(
            (
                item
                for item in info["branches"]
                if item["local_name"] == name and item["remote"]
            ),
            None,
        )
        if root is None:
            match = local or remote
            if match:
                options.append(_public_branch_option(match))
            continue
        if local and branch_has_console_code(root, local["name"]):
            options.append(_public_branch_option(local))
            continue
        remote_option = _remote_console_option(root, name, local, remote)
        if remote_option:
            options.append(remote_option)
    return options


def console_branch_target(
    info: dict[str, Any] | None, branch: str
) -> dict[str, Any] | None:
    """只在包含控制台代码的可选分支中查找目标，兼容远程引用写法。"""
    options = console_branch_options(info)
    exact = next(
        (
            item
            for item in options
            if item["name"] == branch or item["local_name"] == branch
        ),
        None,
    )
    if exact is not None:
        return exact
    target = resolve_branch_target(info, branch) if info else None
    if target is None:
        return None
    return next(
        (
            item
            for item in options
            if item["local_name"] == target["local_name"]
        ),
        None,
    )


def _console_local_name(info: dict[str, Any] | None, branch: str) -> str:
    """把 origin/main 这类远程写法折成 feat/dev/main 本地名。"""
    if branch in CONSOLE_BRANCHES:
        return branch
    resolved = resolve_branch_target(info, branch) if info else None
    if resolved and resolved["local_name"] in CONSOLE_BRANCHES:
        return resolved["local_name"]
    for name in CONSOLE_BRANCHES:
        if branch.endswith(f"/{name}"):
            return name
    return ""


def console_branch_rejection(
    info: dict[str, Any] | None, branch: str
) -> str:
    """生成控制台分支被拒绝时的提示，区分分支不存在和旧 main 缺代码。"""
    name = _console_local_name(info, branch)
    if not name:
        return CONSOLE_BRANCH_ERROR
    if any(
        item["local_name"] == name
        for item in (info or {}).get("branches", [])
    ) or any(
        item.get("upstream") == branch or item["name"] == branch
        for item in (info or {}).get("branches", [])
    ):
        return f"分支 {name} 不包含 DevDeck 代码，不能切换"
    return f"找不到分支 {name}"


def restrict_console_branches(info: dict[str, Any] | None) -> dict[str, Any] | None:
    """把控制台仓库的分支视图裁剪到 feat / dev / main，避免项目卡片列出 agents 分支。"""
    if not info:
        return info
    options = [dict(item) for item in console_branch_options(info)]
    order: dict[str, int] = {}
    for index, item in enumerate(info.get("branches", [])):
        order.setdefault(item["local_name"], index)
        order.setdefault(item["name"], index)
    options.sort(
        key=lambda item: (
            order.get(item["local_name"], order.get(item["name"], 999)),
            item["local_name"],
        )
    )
    for item in options:
        item["latest"] = False
    latest = options[0]["name"] if options else ""
    if options:
        options[0]["latest"] = True
    result = dict(info)
    result["branches"] = options
    result["latest"] = latest
    return result


def resolve_branch_target(info: dict[str, Any], branch: str) -> dict[str, Any] | None:
    """先精确匹配引用名，再回退到同名本地分支或远程跟踪分支。"""
    target = next((item for item in info["branches"] if item["name"] == branch), None)
    if target is not None:
        return target
    local = next(
        (
            item
            for item in info["branches"]
            if item["local_name"] == branch and not item["remote"]
        ),
        None,
    )
    remote = next(
        (
            item
            for item in info["branches"]
            if item["local_name"] == branch and item["remote"]
        ),
        None,
    )
    return local or remote


def checkout_console_branch(directory: Path, branch: str) -> str:
    """切到包含控制台代码的 feat / dev / main；本地缺入口文件时按远程快进。"""
    name = validate_branch(branch)
    info = inspect_repo(directory)
    if info is None or not info["root"]:
        raise ValueError(
            info["warning"] if info and info.get("warning") else "服务目录不是 Git 仓库"
        )
    if info["warning"] and not info["branches"]:
        raise ValueError(info["warning"])
    target = console_branch_target(info, name)
    if target is None:
        raise ValueError(console_branch_rejection(info, name))
    local_name = target["local_name"]
    if info["dirty"]:
        raise ValueError("工作区有未提交改动，无法切换分支")
    repo = Path(info["root"])
    local_exists = any(
        item["local_name"] == local_name and not item["remote"]
        for item in info["branches"]
    )
    try:
        if not target["remote"]:
            if info["current"] == local_name:
                return local_name
            result = run_git(repo, "checkout", local_name, timeout=10)
        elif not local_exists:
            result = run_git(
                repo, "checkout", "--track", target["name"], timeout=10
            )
        else:
            if info["current"] != local_name:
                switched = run_git(repo, "checkout", local_name, timeout=10)
                if switched.returncode != 0:
                    raise ValueError(f"切换分支失败：{public_git_error(switched)}")
            if branch_has_console_code(repo, local_name):
                return local_name
            result = run_git(
                repo, "merge", "--ff-only", target["name"], timeout=10
            )
    except FileNotFoundError as error:
        raise ValueError("未安装 Git，无法切换分支") from error
    except subprocess.TimeoutExpired as error:
        raise ValueError("切换分支超时") from error
    if result.returncode != 0:
        if target["remote"] and local_exists:
            raise ValueError(
                f"本地 {local_name} 不包含 DevDeck 代码，且无法快进到远程："
                f"{public_git_error(result)}"
            )
        raise ValueError(f"切换分支失败：{public_git_error(result)}")
    current = inspect_repo(directory)
    return (current or {}).get("current") or local_name


def checkout_branch(directory: Path, branch: str) -> str:
    """在工作区没有已跟踪改动时切换分支；远程跟踪分支会创建本地同名分支。"""
    name = validate_branch(branch)
    info = inspect_repo(directory)
    if info is None or not info["root"]:
        raise ValueError(info["warning"] if info and info.get("warning") else "服务目录不是 Git 仓库")
    if info["warning"] and not info["branches"]:
        raise ValueError(info["warning"])
    if info["current"] == name:
        return name
    target = resolve_branch_target(info, name)
    if target is None:
        raise ValueError(f"找不到分支 {name}")
    if target["local_name"] == info["current"]:
        return info["current"]
    if info["dirty"]:
        raise ValueError("工作区有未提交改动，无法切换分支")
    repo = Path(info["root"])
    try:
        if target["remote"]:
            result = run_git(repo, "checkout", "--track", target["name"], timeout=10)
        else:
            result = run_git(repo, "checkout", target["local_name"], timeout=10)
    except FileNotFoundError as error:
        raise ValueError("未安装 Git，无法切换分支") from error
    except subprocess.TimeoutExpired as error:
        raise ValueError("切换分支超时") from error
    if result.returncode != 0:
        raise ValueError(f"切换分支失败：{public_git_error(result)}")
    current = inspect_repo(directory)
    return (current or {}).get("current") or target["local_name"]


def fetch_remotes(directory: Path) -> None:
    """刷新远程跟踪分支，不修改当前检出；失败时不泄露凭据。"""
    info = inspect_repo(directory)
    if info is None or not info["root"]:
        raise ValueError(info["warning"] if info and info.get("warning") else "服务目录不是 Git 仓库")
    if not info["has_remotes"]:
        raise ValueError("此仓库还没有配置远程，无法刷新")
    repo = Path(info["root"])
    try:
        result = run_git(repo, "fetch", "--all", "--prune", timeout=25)
    except FileNotFoundError as error:
        raise ValueError("未安装 Git，无法刷新远程分支") from error
    except subprocess.TimeoutExpired as error:
        raise ValueError("刷新远程分支超时") from error
    if result.returncode != 0:
        raise ValueError(f"刷新远程分支失败：{public_git_error(result)}")
