"""T5 方法 A：git 递归同步（bundle → ssh 二进制流 → mirror → materialize → verify）。

流程（PRD 4.1）：
1. snapshot.build_snapshots 构造 synthetic snapshots；
2. 每 repo 的 snapshot commit 打成 bundle，经 ssh 二进制流送入容器内
   mirror（`<workspace_root>/.remote-mirrors/`），fetch 后更新 parity ref；
3. materialize：worktree 目录内 fetch + 强制对齐到 snapshot ref，子模块
   URL 改写为容器内 mirror，递归显式展开；
4. 校验容器内各 repo commit id 与 snapshot 完全一致，不符 fail closed；
   另按 PRD 4.3 对变更文件做 sha256 抽检。

绝不触发编译/install；未 `up` 过返回 `blocked: need up`。
"""
from __future__ import annotations

import hashlib
import importlib
import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import config, output, snapshot, ssh, workspace
from .config import Machine, RemotePluginError

_SHA256_SAMPLE_LIMIT = 5  # sha256 抽检文件数上限
_SSH_SCRIPT_TIMEOUT_SEC = 900


@dataclass
class SyncResult:
    """sync_git 的返回契约（status: ready|no_change|blocked|failed）。"""

    status: str
    snapshots: dict[str, str]  # {relpath: snapshot sha}
    remote_heads: dict[str, str]  # {relpath: 远端实际 HEAD}
    changed_paths: list[str]
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "status": self.status,
            "snapshots": self.snapshots,
            "remote_heads": self.remote_heads,
            "changed_paths": self.changed_paths,
        }
        if self.reason:
            data["reason"] = self.reason
        return data


def _repo_root(start: Path) -> Path:
    """从 start 向上找第一个含 .git 的目录（仓库根）；无则返回 start。"""
    cur = Path(start).resolve()
    for d in [cur, *cur.parents]:
        if (d / ".git").exists():
            return d
    return cur


def _sanitize_ref_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._/-]+", "-", value).strip("/.-")
    return cleaned or "repo"


def _repo_id(relpath: str) -> str:
    return "root" if relpath in ("", ".") else relpath


def _repo_dir(ws: str, relpath: str) -> PurePosixPath:
    """返回 workspace 中某个 repo/worktree 的远端目录。"""
    return PurePosixPath(ws) if relpath in ("", ".") else PurePosixPath(ws) / relpath


def _mirror_path(ws: str, relpath: str) -> str:
    """容器内 mirror 路径：`<workspace_root>/.remote-mirrors/<id>.git`。"""
    return str(PurePosixPath(ws) / ".remote-mirrors" / f"{_repo_id(relpath)}.git")


def _bundle_tmp(ws: str, relpath: str, commit: str) -> str:
    """远端 bundle 临时文件（流式写入后即删）。"""
    rid = _repo_id(relpath).replace("/", "_")
    return str(PurePosixPath(ws) / ".remote-mirrors" / ".bundles" / f"{rid}-{commit}.bundle")


def _parity_ref(relpath: str) -> str:
    """workspace mirror 内 parity ref。"""
    rid = _sanitize_ref_part(_repo_id(relpath))
    return f"refs/parity/workspace/{rid}"


def _last_state_path(state_dir: Path, alias: str) -> Path:
    return Path(state_dir) / "sync" / alias / "workspace.json"


def _load_last_state(state_dir: Path, alias: str) -> dict[str, Any] | None:
    path = _last_state_path(state_dir, alias)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None  # 状态损坏当作无上次记录，走完整同步
    return data if isinstance(data, dict) else None


def _save_last_state(
    state_dir: Path, alias: str, commits: dict[str, str]
) -> None:
    path = _last_state_path(state_dir, alias)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"snapshot_commits": commits}
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def _ensure_up(machine: Machine, endpoint: config.Endpoint, state_dir: Path) -> str | None:
    """校验是否已 `up`。返回 None 表示已 up；否则返回 blocked 原因。"""
    if machine.mode == "container":
        marker = Path(state_dir) / "endpoints" / f"{machine.alias}.json"
        if not marker.is_file():
            return "need up"
        return None
    # 模式 B：无本地 marker，单次轻量 SSH 探测 workspace_root 是否存在
    result = ssh.ssh_run(
        endpoint,
        "test -d " + shlex.quote(endpoint.workspace_root) + " && echo up-ok",
        timeout_sec=60,
    )
    if result.returncode == 0:
        return None
    if result.returncode == 255:
        raise ssh.SSHError(
            f"SSH 连接失败: {endpoint.user}@{endpoint.host}:{endpoint.port}"
        )
    return "need up"


def _push_bundles(endpoint: config.Endpoint, snapshots: snapshot.SnapshotSet) -> None:
    """每 repo 的 snapshot commit 打 bundle，经 ssh 二进制流送入容器 mirror。"""
    ws = endpoint.workspace_root
    for rec in snapshots.repos:
        mirror = _mirror_path(ws, rec.relpath)
        bundle = _bundle_tmp(ws, rec.relpath, rec.commit)
        ref = f"refs/remote-plugin/sync/workspace/{_sanitize_ref_part(_repo_id(rec.relpath))}"
        # bundle 需要 ref 作为 tip（裸 sha 会被 git 拒绝）
        subprocess.run(
            ["git", "-C", str(rec.repo), "update-ref", ref, rec.commit],
            check=True,
            capture_output=True,
        )
        try:
            remote_cmd = (
                "set -e; "
                f"mkdir -p {shlex.quote(str(PurePosixPath(mirror).parent))} "
                f"{shlex.quote(str(PurePosixPath(bundle).parent))}; "
                f"if [ -e {shlex.quote(mirror)} ] && [ ! -d {shlex.quote(str(PurePosixPath(mirror) / 'objects'))} ]; then "
                f"rm -rf {shlex.quote(mirror)}; fi; "
                f"if [ ! -d {shlex.quote(mirror)} ]; then git init --bare {shlex.quote(mirror)} >/dev/null; fi; "
                f"cat > {shlex.quote(bundle)}; "
                f"git -C {shlex.quote(mirror)} fetch --force {shlex.quote(bundle)} "
                f"{shlex.quote(rec.commit + ':' + _parity_ref(rec.relpath))} >/dev/null; "
                f"rm -f {shlex.quote(bundle)}"
            )
            local_cmd = ["git", "-C", str(rec.repo), "bundle", "create", "-", ref]
            ssh.ssh_pipe(endpoint, local_cmd, remote_cmd)
        finally:
            subprocess.run(
                ["git", "-C", str(rec.repo), "update-ref", "-d", ref],
                check=False,
                capture_output=True,
            )


def _materialize(endpoint: config.Endpoint, snapshots: snapshot.SnapshotSet) -> None:
    """把 workspace 中各 repo 对齐 snapshot；递归展开子模块和 worktree。"""
    ws = endpoint.workspace_root
    by_rel = snapshots.by_relpath
    lines: list[str] = ["set -e", f"mkdir -p {shlex.quote(ws)}"]

    def repo_dir(rec: snapshot.RepoSnapshot) -> PurePosixPath:
        return _repo_dir(ws, rec.relpath)

    def render(rec: snapshot.RepoSnapshot) -> list[str]:
        rd = repo_dir(rec)
        is_root = rec.relpath in ("", ".")
        mirror = _mirror_path(ws, rec.relpath)
        parity = _parity_ref(rec.relpath)
        # 根仓库目录是 worktree 本体，绝不整目录删除；子模块目录可整体重建
        if is_root:
            init_cmd = (
                f"if [ ! -e {shlex.quote(str(rd / '.git'))} ]; then "
                f"git init {shlex.quote(str(rd))} >/dev/null; fi"
            )
        else:
            init_cmd = (
                f"if [ ! -e {shlex.quote(str(rd / '.git'))} ]; then "
                f"rm -rf {shlex.quote(str(rd))} && git init {shlex.quote(str(rd))} >/dev/null; fi"
            )
        out = [
            f"mkdir -p {shlex.quote(str(rd.parent))}",
            init_cmd,
            f"if ! git -C {shlex.quote(str(rd))} remote get-url parity >/dev/null 2>&1; then "
            f"git -C {shlex.quote(str(rd))} remote add parity {shlex.quote(mirror)}; fi",
            f"git -C {shlex.quote(str(rd))} remote set-url parity {shlex.quote(mirror)}",
            f"git -C {shlex.quote(str(rd))} fetch --force --no-recurse-submodules parity "
            f"{shlex.quote(parity + ':refs/remotes/parity/current')} >/dev/null",
            f"git -C {shlex.quote(str(rd))} checkout -f -B parity/current refs/remotes/parity/current >/dev/null",
            f"git -C {shlex.quote(str(rd))} reset --hard refs/remotes/parity/current >/dev/null",
            # mirror 与任务日志位于 workspace 根目录，不能被根仓库 clean 清掉。
            (
                f"git -C {shlex.quote(str(rd))} clean -ffd"
                " -e .remote-mirrors -e .remote-logs >/dev/null"
            ),
        ]
        if is_root:
            # 运行时 mirror/log 目录位于 workspace 根下；加入该仓库本地 exclude，
            # 这样旧的 dirty=0 校验不会把同步基础设施误判为代码改动。
            out.extend(
                [
                    f"git_dir=$(git -C {shlex.quote(str(rd))} rev-parse --absolute-git-dir)",
                    'mkdir -p "$git_dir/info"',
                    'for runtime_dir in .remote-mirrors .remote-logs; do '
                    'grep -Fqx "$runtime_dir/" "$git_dir/info/exclude" 2>/dev/null '
                    '|| printf "%s\\n" "$runtime_dir/" >> "$git_dir/info/exclude"; '
                    'done',
                ]
            )
        # 子模块 URL 改写为容器内 mirror（只写 .git/config，不动 .gitmodules）
        for sub in rec.submodules:
            child = by_rel[_child_relpath(rec.relpath, sub["path"])]
            child_mirror = _mirror_path(ws, child.relpath)
            key = f"submodule.{sub['name']}.url"
            out.append(
                f"git -C {shlex.quote(str(rd))} config {shlex.quote(key)} {shlex.quote(child_mirror)}"
            )
        return out

    def walk(rec: snapshot.RepoSnapshot) -> None:
        lines.extend(render(rec))
        for sub in rec.submodules:
            walk(by_rel[_child_relpath(rec.relpath, sub["path"])])

    walk(snapshots.root)
    result = ssh.ssh_run(
        endpoint, "\n".join(lines), timeout_sec=_SSH_SCRIPT_TIMEOUT_SEC
    )
    if result.returncode != 0:
        err = (result.stderr or b"").decode("utf-8", "replace")[:2000]
        raise ssh.SSHError(f"远端 materialize 失败（{result.returncode}）: {err}")


def _child_relpath(parent_relpath: str, sub_path: str) -> str:
    if parent_relpath in ("", "."):
        return sub_path
    return f"{parent_relpath}/{sub_path}"


def _sha256_hex(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _sample_files(
    snapshots: snapshot.SnapshotSet, local_root: Path
) -> list[tuple[str, Path]]:
    """取变更文件中的普通文件做 sha256 抽检：[(worktree 相对路径, 本地路径)]。"""
    out: list[tuple[str, Path]] = []
    for p in snapshots.aggregate_changed_paths():
        local_path = local_root / p
        if local_path.is_file():
            out.append((p, local_path))
            if len(out) >= _SHA256_SAMPLE_LIMIT:
                break
    return out


def _verify_remote(
    endpoint: config.Endpoint,
    snapshots: snapshot.SnapshotSet,
    sample: list[tuple[str, Path]],
) -> tuple[bool, dict[str, str]]:
    """单次 SSH：校验远端各 repo HEAD == snapshot commit，并 sha256 抽检。"""
    ws = endpoint.workspace_root
    base = PurePosixPath(ws)
    lines: list[str] = ["set -e"]
    for rec in snapshots.repos:
        rd = _repo_dir(ws, rec.relpath)
        lines.append(
            f"printf 'HEAD\\t'; printf '%s\\t' {shlex.quote(rec.relpath)}; "
            f"git -C {shlex.quote(str(rd))} rev-parse HEAD"
        )
        lines.append(
            f"n=$(git -C {shlex.quote(str(rd))} status --porcelain | wc -l); "
            f"printf 'DIRTY\\t'; printf '%s\\t' {shlex.quote(rec.relpath)}; printf '%s\\n' \"$n\""
        )
    for rel_path, _local in sample:
        lines.append(
            f"printf 'SHA256\\t'; printf '%s\\t' {shlex.quote(rel_path)}; "
            f"sha256sum {shlex.quote(str(base / rel_path))} | cut -d' ' -f1"
        )
    result = ssh.ssh_run(
        endpoint, "\n".join(lines), timeout_sec=_SSH_SCRIPT_TIMEOUT_SEC
    )
    if result.returncode != 0:
        return False, {}

    heads: dict[str, str] = {}
    dirty: dict[str, int] = {}
    sha_map: dict[str, str] = {}
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        kind, rel, val = parts
        if kind == "HEAD":
            heads[rel] = val.strip()
        elif kind == "DIRTY":
            try:
                dirty[rel] = int(val.strip())
            except ValueError:
                dirty[rel] = 1
        elif kind == "SHA256":
            sha_map[rel] = val.strip()

    ok = heads == snapshots.snapshot_commits() and all(v == 0 for v in dirty.values())
    if ok and sample:
        for rel_path, local_path in sample:
            if sha_map.get(rel_path) != _sha256_hex(local_path):
                ok = False
                break
    return ok, heads


def sync_git(machine: Machine, local_root: Path) -> SyncResult:
    """方法 A 同步：bundle → ssh 二进制流 → mirror → materialize → 校验。

    返回 ``SyncResult``；transport/SSH 异常抛 ``SSHError``（fail closed），
    校验不符返回 ``status=failed``，未 up 返回 ``status=blocked``。
    """
    local_root = Path(local_root).resolve()
    state_dir = config.state_dir(local_root)
    endpoint = config.resolve_endpoint(machine, state_dir)

    reason = _ensure_up(machine, endpoint, state_dir)
    if reason is not None:
        output.progress({"phase": "blocked", "reason": reason})
        return SyncResult("blocked", {}, {}, [], reason=reason)

    output.progress({"phase": "snapshot-build"})
    contexts = workspace.discover_repositories(local_root)
    extras = [
        (context.path, context.relpath)
        for context in contexts
        if context.path != local_root
    ]
    snapshots = snapshot.build_snapshots(local_root, extras)
    commits = snapshots.snapshot_commits()

    last = _load_last_state(state_dir, machine.alias)
    if last is not None and last.get("snapshot_commits") == commits:
        # no-change 快路径：snapshot 与上次一致 → 单次 SSH 校验
        output.progress({"phase": "no-change-check"})
        ok, heads = _verify_remote(endpoint, snapshots, [])
        if ok:
            output.progress({"phase": "no-change"})
            return SyncResult("no_change", commits, heads, [])
        output.progress({"phase": "full-sync"})

    output.progress({"phase": "bundle-push"})
    _push_bundles(endpoint, snapshots)
    output.progress({"phase": "materialize"})
    _materialize(endpoint, snapshots)
    output.progress({"phase": "verify"})

    sample = _sample_files(snapshots, local_root)
    ok, heads = _verify_remote(endpoint, snapshots, sample)
    if not ok:
        return SyncResult(
            "failed",
            commits,
            heads,
            snapshots.aggregate_changed_paths(),
            reason="远端 commit/dirty/sha256 与 snapshot 不一致，fail closed",
        )

    _save_last_state(state_dir, machine.alias, commits)
    output.progress({"phase": "ready"})
    return SyncResult("ready", commits, heads, snapshots.aggregate_changed_paths())


def cli_sync(args: Any) -> dict[str, Any]:
    """sync 子命令 handler：`--paths` 非空走方法 B，否则方法 A。"""
    local_root = workspace.find_workspace_root(Path.cwd())
    machines = config.load_machines()
    if args.alias not in machines:
        raise config.ConfigError(f"未知机器 alias: {args.alias}")
    machine = machines[args.alias]

    if args.paths:
        # T4 方法 B：惰性 import，T4 未落盘时优雅报错
        try:
            sync_paths_mod = importlib.import_module("remote_plugin.sync_paths")
        except ImportError as e:
            raise RemotePluginError(f"sync_paths（方法 B）尚未实现: {e}") from e
        if sync_paths_mod is None:
            raise RemotePluginError("sync_paths（方法 B）尚未实现")
        paths = [Path(p) for p in args.paths]
        result = sync_paths_mod.sync_paths(machine, paths, local_root)
        if isinstance(result, dict):
            return result
        if hasattr(result, "to_dict"):
            return result.to_dict()
        return {"status": "ready", "result": result}

    result = sync_git(machine, local_root)
    return result.to_dict()
