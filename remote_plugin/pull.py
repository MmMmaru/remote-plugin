"""remote pull：把远端（容器内）文件/目录经 `tar | ssh` 二进制流拉回本地。

``sync_paths`` 的反向：远端 ``tar -C <base> -cf - -- <rels>`` 的 stdout 经 ssh
二进制流回本地，本地 GNU tar 解包到 ``--dest``；远端先出 sha256 清单，本地
解包后重算比对，不一致 fail closed（``PullError`` → 单行 JSON、exit=1）。

- 相对路径按 worktree 目录（``<workspace_root>/<worktree>``）解析；绝对路径
  视为容器内路径原样使用。多路径打进同一个 tar（base 取公共前缀）。
- 本地 tar 经 ``localtools.gnu_tar()`` 解析（Windows 上规避 System32 bsdtar）。
"""
from __future__ import annotations

import posixpath
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config, output, ssh
from .localtools import gnu_tar, tar_path
from .sync_paths import _local_sha256, _parse_sha256_output, _shq, _validate_worktree

__all__ = ["PullResult", "PullError", "pull_paths"]

PULL_TIMEOUT_SEC = 3600
_MANIFEST_TIMEOUT_SEC = 300


class PullError(config.RemotePluginError):
    """pull 的输入校验 / 远端路径缺失 / 传输失败 / 校验不一致错误。"""


@dataclass
class PullResult:
    """``pull_paths`` 结果，对应输出 ``{status, files, bytes, dest}``。"""

    status: str = "ready"  # 成功固定 "ready"；失败一律抛 PullError（fail closed）
    files: int = 0
    bytes: int = 0
    dest: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "files": self.files,
            "bytes": self.bytes,
            "dest": self.dest,
        }


def _resolve_remote_paths(
    workspace_root: str, worktree: str, remote_paths: list[str]
) -> tuple[str, list[str]]:
    """校验并归一化远端路径 → ``(tar 基准目录 base, 相对 base 的 rel 列表)``。

    - 相对路径按 worktree 目录解析，normpath 后逃出 worktree（如 ``../x``）→ 越界；
    - 绝对路径（容器内）原样使用；
    - 多路径打进同一个 tar：base 取全部解析结果的公共前缀；单一路径取其
      dirname（拉回内容保留最后一级目录名）。
    """
    if not remote_paths:
        raise PullError("remote_path 不能为空（至少指定一个文件或目录）")
    wt = f"{workspace_root.rstrip('/')}/{worktree}"
    resolved: list[str] = []
    seen: set[str] = set()
    for raw in remote_paths:
        p = str(raw)
        if p == "":
            raise PullError("remote_path 不能为空字符串")
        if "\\" in p or "\x00" in p:
            raise PullError(f"remote_path 必须是 POSIX 路径（禁止反斜杠/NUL）: {p!r}")
        if p.startswith("/"):
            res = posixpath.normpath(p)
        else:
            res = posixpath.normpath(f"{wt}/{p}")
            if res != wt and not res.startswith(wt + "/"):
                raise PullError(f"remote_path 越界（必须位于 worktree 内）: {p!r}")
        if res in seen:
            continue
        seen.add(res)
        resolved.append(res)
    if len(resolved) == 1:
        base = posixpath.dirname(resolved[0]) or "/"
        return base, [posixpath.basename(resolved[0])]
    base = posixpath.commonpath(resolved)
    return base, [posixpath.relpath(r, base) for r in resolved]


def _manifest_script(base: str, rels: list[str]) -> str:
    """远端清单脚本：存在性校验 + 常规文件（不跟随 symlink）sha256 清单。"""
    quoted = " ".join(_shq(r) for r in rels)
    return (
        "set -e\n"
        f"cd {_shq(base)} || exit 95\n"
        f"for p in {quoted}; do\n"
        '  [ -e "$p" ] || [ -L "$p" ] || { echo "MISSING $p"; exit 96; }\n'
        "done\n"
        f"find {quoted} -type f -print0 | sort -z | xargs -0 -r sha256sum --\n"
    )


def _tar_script(base: str, rels: list[str]) -> str:
    quoted = " ".join(_shq(r) for r in rels)
    return f"cd {_shq(base)} || exit 95\ntar -cf - -- {quoted}\n"


def pull_paths(
    machine: config.Machine,
    worktree: str,
    remote_paths: list[str],
    dest: Path | str,
) -> PullResult:
    """把远端 ``remote_paths`` 拉回本地 ``dest``。

    - 校验失败 / 远端缺失 / 传输或解包失败 / sha256 不一致 → ``PullError``
      （CLI 层转单行 JSON、exit=1，fail closed）；
    - 成功返回 ``PullResult(status="ready", files, bytes, dest)``。
    """
    _validate_worktree(worktree)
    endpoint = config.resolve_endpoint(machine, config.state_dir())
    base, rels = _resolve_remote_paths(
        endpoint.workspace_root, worktree, list(remote_paths)
    )
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    # 1) 远端存在性校验 + sha256 清单（键为相对 base 的 POSIX 路径）
    output.progress({"step": "pull", "status": "manifest", "base": base, "paths": len(rels)})
    proc = ssh.ssh_run(
        endpoint, _manifest_script(base, rels), timeout_sec=_MANIFEST_TIMEOUT_SEC
    )
    if proc.returncode == 96:
        missing = (proc.stdout or b"").decode("utf-8", "replace").strip()
        raise PullError(f"远端路径不存在: {missing}")
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise PullError(f"远端 sha256 清单失败（rc={proc.returncode}）: {err[-300:]}")
    remote_hashes = _parse_sha256_output(proc.stdout.decode("utf-8", "replace"))

    # 2) 远端 tar → ssh stdout 二进制流拉回
    output.progress({"step": "pull", "status": "transferring", "paths": len(rels)})
    tar = ssh.ssh_run(endpoint, _tar_script(base, rels), timeout_sec=PULL_TIMEOUT_SEC)
    if tar.returncode != 0:
        err = (tar.stderr or b"").decode("utf-8", "replace").strip()
        raise PullError(f"远端 tar 失败（rc={tar.returncode}）: {err[-300:]}")

    # 3) 本地 GNU tar 解包到 dest（Windows 路径转 MSYS 形式）
    extract = subprocess.run(
        [gnu_tar(), "-x", "-C", tar_path(dest)], input=tar.stdout, capture_output=True
    )
    if extract.returncode != 0:
        err = extract.stderr.decode("utf-8", "replace").strip() if extract.stderr else ""
        raise PullError(f"本地解包失败（rc={extract.returncode}）: {err[-300:]}")

    # 4) 本地重算 sha256 逐文件比对（fail closed）
    local_hashes: dict[str, str] = {}
    for rel in remote_hashes:
        fp = dest.joinpath(*rel.split("/"))
        if not fp.is_file():
            raise PullError(f"本地解包缺文件: {rel}")
        local_hashes[rel] = _local_sha256(fp)
    if local_hashes != remote_hashes:
        diff = [r for r in remote_hashes if local_hashes.get(r) != remote_hashes[r]]
        raise PullError(f"sha256 校验不一致（fail closed）: {', '.join(diff[:5])}")

    total = sum(
        dest.joinpath(*rel.split("/")).stat().st_size for rel in local_hashes
    )
    output.progress({"step": "pull", "status": "ready", "files": len(local_hashes)})
    return PullResult("ready", len(local_hashes), total, str(dest))


def cli_pull(args) -> dict:
    machines = config.load_machines()
    machine = machines.get(args.alias)
    if machine is None:
        raise config.ConfigError(
            f"机器 '{args.alias}' 未注册（machines.json 中不存在该机器）"
        )
    result = pull_paths(machine, args.worktree, args.remote_paths, args.dest)
    return result.to_dict()
