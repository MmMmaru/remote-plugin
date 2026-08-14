"""T4 sync 方法 B：指定文件/路径经 `tar | ssh` 二进制流覆盖到远端 worktree。

- 本地 ``tar -C <local_root> -cf - -- <rel paths>`` 打包（保留相对结构），经
  ``ssh_pipe`` 二进制流送入远端 ``mkdir -p <worktree> && tar -x -C <worktree>``；
  tar 是二进制流，行尾不被改写（CRLF 验收项）。
- 传输后 sha256 抽检：本地对每个常规文件算 sha256，远端用 ``sha256sum`` 比对，
  全部一致才 ``status: ready``。
- 错误语义：输入校验（空列表 / 越界 / 不存在 / worktree 非法）抛
  ``SyncPathsError``；传输或远端执行失败抛 ``ssh.SSHError``（fail closed）；
  仅当传输成功但抽检不一致时返回 ``status: failed``（不抛异常）。

纯标准库 + 系统 ssh（tar / ssh / sha256sum / xargs）。不做 git 语义、不进 mirror。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from . import config, ssh
from .localtools import gnu_tar, tar_path

__all__ = ["SyncResult", "SyncPathsError", "sync_paths"]

_CHUNK = 1024 * 1024


class SyncPathsError(config.RemotePluginError):
    """sync 方法 B 的输入校验错误（空列表 / 越界 / 不存在 / worktree 非法）。"""


@dataclass
class SyncResult:
    """sync 方法 B 结果，对应 spec 输出 ``{status, files, bytes, sha256_ok}``。"""

    status: str = "failed"  # "ready" | "failed"
    files: int = 0          # 传输的常规文件数（目录递归展开，symlink 不计）
    bytes: int = 0          # 上述文件字节数之和
    sha256_ok: bool = False  # 抽检全部一致

    def to_dict(self) -> dict[str, object]:
        """转 JSON 可序列化 dict（供 CLI handler 直接 emit）。"""
        return {
            "status": self.status,
            "files": self.files,
            "bytes": self.bytes,
            "sha256_ok": self.sha256_ok,
        }


def _shq(s: str) -> str:
    """POSIX shell 单引号转义（远端命令路径安全引用）。"""
    return "'" + s.replace("'", "'\\''") + "'"


def _validate_worktree(worktree: str) -> str:
    """worktree 必须是简单目录名，防止远端路径穿越（`<workspace_root>/<worktree>`）。"""
    if not isinstance(worktree, str) or worktree == "":
        raise SyncPathsError("worktree 不能为空")
    if "/" in worktree or "\\" in worktree or worktree in (".", ".."):
        raise SyncPathsError(
            f"worktree 非法（必须是简单目录名，如 main / t4-test）: {worktree!r}"
        )
    return worktree


def _resolve_paths(local_root: Path, paths: list[Path]) -> list[Path]:
    """校验并归一化 ``paths`` → 相对 ``local_root`` 的路径列表。

    - 空列表 / 空字符串 → ``SyncPathsError``；
    - 每个路径 resolve 后必须位于 ``local_root`` 内（``../``、绝对路径越界均拒绝）；
    - 路径必须存在（``lexists``，容忍 broken symlink 本身存在）；
    - 返回去重后相对路径（如 ``docs/../config.py`` 归一化为 ``config.py``）。
    """
    if not paths:
        raise SyncPathsError("paths 不能为空（至少指定一个文件或目录）")
    root = local_root.resolve()
    if not root.is_dir():
        raise SyncPathsError(f"local_root 不存在或不是目录: {local_root}")
    rels: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        if os.fspath(p) == "":
            raise SyncPathsError("路径不能为空字符串")
        resolved = (local_root / p).resolve()
        if not resolved.is_relative_to(root):
            raise SyncPathsError(f"paths 越界（必须位于 local_root 内）: {p!r}")
        if not os.path.lexists(resolved):
            raise SyncPathsError(f"路径不存在: {p!r}")
        rel = resolved.relative_to(root)
        key = os.fspath(rel)
        if key in seen:
            continue
        seen.add(key)
        rels.append(rel)
    return rels


def _collect_files(root: Path, rel_paths: list[Path]) -> dict[str, int]:
    """把 ``rel_paths`` 展开为 ``{相对路径: 字节数}``，仅统计常规文件。

    目录递归展开（含嵌套子目录）；symlink 一律不跟随、不统计（tar 仍会原样归档，
    但 sha256 抽检只覆盖常规文件，避免链接目标在远端不存在的假失败）。
    """
    result: dict[str, int] = {}
    for rel in rel_paths:
        abs_path = root / rel
        if abs_path.is_symlink():
            continue
        if abs_path.is_file():
            # 键与远端 sha256sum 输出比对：必须 POSIX 分隔符（Windows 客户端亦为 /）
            result[rel.as_posix()] = abs_path.stat().st_size
        elif abs_path.is_dir():
            for dirpath, dirnames, filenames in os.walk(abs_path, followlinks=False):
                # 不进入 symlink 指向的目录，避免循环/越界展开
                dirnames[:] = [
                    d for d in dirnames if not (Path(dirpath) / d).is_symlink()
                ]
                for fn in filenames:
                    fp = Path(dirpath) / fn
                    if fp.is_symlink() or not fp.is_file():
                        continue
                    result[fp.relative_to(root).as_posix()] = fp.stat().st_size
    return result


def _local_sha256(path: Path) -> str:
    """分块计算本地文件 sha256（hex 小写）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_sha256_output(text: str) -> dict[str, str]:
    """解析远端 ``sha256sum`` 输出 → ``{路径: hex}``。

    每行形如 ``<64位hex>  <路径>``（sha256sum 用两个空格分隔；路径可含空格）。
    """
    result: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and len(parts[0]) == 64:
            # 二进制模式标记：GNU/Windows coreutils 输出 `<hex> *<路径>`，剥一个前导 *
            result[parts[1].removeprefix("*")] = parts[0]
    return result


def _remote_worktree_dir(endpoint: config.Endpoint, worktree: str) -> str:
    """远端 worktree 目录 = ``workspace_root/<worktree>``（禁止写死绝对路径）。"""
    return f"{endpoint.workspace_root.rstrip('/')}/{worktree}"


def sync_paths(
    machine: config.Machine,
    worktree: str,
    paths: list[Path],
    local_root: Path,
) -> SyncResult:
    """方法 B：把 ``paths``（相对 ``local_root``）经 ``tar | ssh`` 覆盖到远端 worktree。

    - 输入校验失败 → ``SyncPathsError``；
    - 传输/远端执行失败 → ``ssh.SSHError``（fail closed）；
    - 传输成功但 sha256 抽检不一致 → 返回 ``status: failed``。
    """
    _validate_worktree(worktree)
    rel_paths = _resolve_paths(local_root, paths)
    root = local_root.resolve()

    files_map = _collect_files(root, rel_paths)
    total_bytes = sum(files_map.values())

    endpoint = config.resolve_endpoint(machine, config.state_dir())
    wt_dir = _remote_worktree_dir(endpoint, worktree)

    # 1) 本地 tar 打包（保留相对结构、二进制流）→ ssh | tar -x 覆盖到 worktree
    # tar 经 localtools 解析 GNU 版本（Windows 上裸 "tar" 会命中 System32 bsdtar），
    # 路径转 MSYS 形式（Git tar 不识别反斜杠 Windows 路径）
    local_cmd = [
        gnu_tar(), "-C", tar_path(root), "-cf", "-", "--",
        *[r.as_posix() for r in rel_paths],
    ]
    remote_cmd = f"mkdir -p {_shq(wt_dir)} && tar -x -C {_shq(wt_dir)}"
    ssh.ssh_pipe(endpoint, local_cmd, remote_cmd)

    # 2) sha256 抽检：本地逐文件算，远端 sha256sum 比对（全部一致才算 ok）
    if files_map:
        local_hashes = {rel: _local_sha256(root / rel) for rel in files_map}
        script = (
            "set -e\n"
            f"cd {_shq(wt_dir)} || exit 1\n"
            "xargs -0 sha256sum --\n"
        )
        input_bytes = "\0".join(files_map).encode("utf-8") + b"\0"
        proc = ssh.ssh_run(endpoint, script, input_bytes=input_bytes)
        if proc.returncode != 0:
            return SyncResult(
                status="failed", files=len(files_map), bytes=total_bytes, sha256_ok=False
            )
        remote_hashes = _parse_sha256_output(proc.stdout.decode("utf-8", "replace"))
        sha256_ok = remote_hashes == local_hashes
    else:
        # 只有空目录（无常规文件）：tar 传输成功即视为通过，不做空 stdin 假哈希
        sha256_ok = True

    if not sha256_ok:
        return SyncResult(
            status="failed", files=len(files_map), bytes=total_bytes, sha256_ok=False
        )
    return SyncResult(
        status="ready", files=len(files_map), bytes=total_bytes, sha256_ok=True
    )
