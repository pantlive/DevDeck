"""读取 Linux / WSL 的 TCP 监听端口，不依赖额外命令或管理员权限。"""

import errno
import os
import socket
import sys
from pathlib import Path
from typing import Any


def validate_port(value: Any) -> int:
    """空值表示不管理端口，其他值必须是合法整数。"""
    if value is None or value == "":
        return 0
    if isinstance(value, bool) or not str(value).isascii():
        raise ValueError("端口必须是 1–65535 的整数，或留空")
    if not str(value).isdigit() or not 0 <= int(value) <= 65535:
        raise ValueError("端口必须是 1–65535 的整数，或留空")
    return int(value)


def decode_address(encoded: str, family: socket.AddressFamily) -> str:
    """按本机字节序解码 proc 中的 IPv4 / IPv6 地址。"""
    raw = bytes.fromhex(encoded)
    if sys.byteorder == "little":
        raw = b"".join(raw[i : i + 4][::-1] for i in range(0, len(raw), 4))
    return socket.inet_ntop(family, raw)


def scan_ports(proc_root: Path = Path("/proc")) -> dict[str, Any]:
    """返回 TCP 监听信息；无权读取的进程保留未知归属。"""
    listeners: list[dict[str, Any]] = []
    warnings = []
    for filename, family in (
        ("tcp", socket.AF_INET),
        ("tcp6", socket.AF_INET6),
    ):
        try:
            lines = (proc_root / "net" / filename).read_text().splitlines()[1:]
        except FileNotFoundError:
            # 未启用 IPv6 的 Linux 可以没有 tcp6 表。
            if filename == "tcp":
                warnings.append("无法读取 IPv4 监听表")
            continue
        except OSError:
            warnings.append(f"无法读取 {filename} 监听表")
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            address, port = fields[1].split(":")
            listeners.append(
                {
                    "address": decode_address(address, family),
                    "port": int(port, 16),
                    "family": "IPv4" if family == socket.AF_INET else "IPv6",
                    "inode": fields[9],
                    "owners": [],
                }
            )

    # 仅将监听 socket 的 inode 映射到 PID，不读取可能含密钥的命令参数。
    by_inode: dict[str, list[dict[str, Any]]] = {}
    for listener in listeners:
        by_inode.setdefault(listener["inode"], []).append(listener)
    try:
        processes = list(proc_root.iterdir()) if by_inode else []
    except OSError:
        processes = []
        warnings.append("无法读取进程信息")
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            inodes = set()
            for descriptor in (process / "fd").iterdir():
                try:
                    target = str(descriptor.readlink())
                except OSError:
                    continue
                if target.startswith("socket:["):
                    inodes.add(target[8:-1])
            relevant = inodes & by_inode.keys()
            if not relevant:
                continue
            pid = int(process.name)
            name = (process / "comm").read_text().strip()
            group = os.getpgid(pid)
        except OSError:
            # 进程可能刚退出，或属于其他用户；监听记录仍会显示。
            continue
        for inode in relevant:
            for listener in by_inode[inode]:
                listener["owners"].append(
                    {
                        "pid": pid,
                        "name": name,
                        "group": group,
                    }
                )
    for listener in listeners:
        listener.pop("inode")
        listener["owners"].sort(key=lambda owner: owner["pid"])
    listeners.sort(
        key=lambda item: (item["port"], item["family"], item["address"])
    )
    return {"listeners": listeners, "warning": "；".join(warnings)}


def ensure_available(port: int) -> None:
    """用短暂绑定检测已绑定但尚未 listen 的 socket，并立即释放。"""
    for family, address in (
        (socket.AF_INET, "0.0.0.0"),
        (socket.AF_INET6, "::"),
    ):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                # 避免将停止服务后的 TIME_WAIT 误判成监听冲突。
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    probe.setsockopt(
                        socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1
                    )
                probe.bind((address, port))
        except OSError as error:
            if family == socket.AF_INET6 and error.errno in {
                errno.EAFNOSUPPORT,
                errno.EADDRNOTAVAIL,
                errno.ENOPROTOOPT,
            }:
                continue
            if error.errno == errno.EADDRINUSE:
                raise ValueError(
                    f"端口 {port} 已被占用，请更换端口"
                ) from error
            raise ValueError(
                f"无法绑定端口 {port}：{error.strerror}"
            ) from error
