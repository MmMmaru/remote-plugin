"""T5 方法 A：synthetic snapshot 构建（纯本地，可单测）。

对每个仓库（叶子 submodule → 父 → 根，postorder）构造一个确定性
synthetic commit：临时 index 全量 add（committed + staged + unstaged +
untracked 非 ignored），剔除子模块路径，gitlink 替换为子 snapshot id；
真实 HEAD 记为 source_head。可选地复用上次 snapshot 或以其为 parent，
用于后续生成 delta bundle。不写任何 ref、不碰工作树。
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import RemotePluginError

# 固定身份与时间戳：保证 snapshot commit sha 只由内容决定（确定性）。
_AUTHOR_NAME = "remote-plugin snapshot"
_AUTHOR_EMAIL = "snapshot@remote-plugin.invalid"
_COMMIT_DATE = "1970-01-01T00:00:00Z"

_GIT_TIMEOUT_SEC = 300

# Git SHA-1 与 SHA-256 仓库对象 ID 的长度范围。
_OBJECT_ID_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")

# 插件自有目录：不入 snapshot（否则 state 文件写入会使多次构建非确定）
_PLUGIN_DIRS = (".remote",)


class SnapshotError(RemotePluginError):
    """snapshot 构建失败（fail closed），message 已含定位信息。"""


@dataclass
class RepoSnapshot:
    """单个仓库的 synthetic snapshot 产物。"""

    relpath: str  # '.' = 根仓库；否则相对 local_root 的 POSIX 路径
    repo: Path  # 本地仓库绝对路径
    source_head: str | None  # 真实 HEAD；无提交（unborn）为 None
    commit: str  # synthetic snapshot commit sha
    tree: str  # 对应 tree sha
    changed_paths: list[str]  # source_head..commit 的相对路径（剔 transport-only）
    submodules: list[dict[str, str]] = field(default_factory=list)
    # [{name, path, commit}]，path 相对本仓库
    parent_commit: str | None = None  # 本次新建 snapshot 的上一个 snapshot


@dataclass
class SnapshotSet:
    """一次 build_snapshots 的完整产物。"""

    repos: list[RepoSnapshot]  # postorder：叶子 submodule → 父 → 根
    root: RepoSnapshot  # 根仓库（relpath == '.'）
    by_relpath: dict[str, RepoSnapshot]

    def snapshot_commits(self) -> dict[str, str]:
        """{relpath: snapshot sha}，供 no-change 快路径与远端校验。"""
        return {r.relpath: r.commit for r in self.repos}

    def source_heads(self) -> dict[str, str | None]:
        """{relpath: source_head}，仅调试用途。"""
        return {r.relpath: r.source_head for r in self.repos}

    def aggregate_changed_paths(self) -> list[str]:
        """合并所有仓库 changed_paths，子模块路径前缀其 relpath；去重保序。"""
        out: list[str] = []
        seen: set[str] = set()
        for r in self.repos:
            prefix = "" if r.relpath in ("", ".") else f"{r.relpath}/"
            for p in r.changed_paths:
                full = f"{prefix}{p}"
                if full not in seen:
                    seen.add(full)
                    out.append(full)
        return out


def _git(
    repo: Path,
    args: list[str],
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """在 repo 内执行 git（可注入 GIT_INDEX_FILE 等 env）。"""
    cmd = ["git", "-C", str(repo), *args]
    try:
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=_GIT_TIMEOUT_SEC
        )
    except subprocess.TimeoutExpired as e:
        raise SnapshotError(f"git 超时（>{_GIT_TIMEOUT_SEC}s）: {' '.join(cmd)}") from e
    except OSError as e:
        raise SnapshotError(f"git 启动失败: {e}") from e
    if check and result.returncode != 0:
        raise SnapshotError(
            f"git 失败（{result.returncode}）: {' '.join(cmd)}\n{result.stderr.strip()}"
        )
    return result


def _identity_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = _AUTHOR_NAME
    env["GIT_AUTHOR_EMAIL"] = _AUTHOR_EMAIL
    env["GIT_AUTHOR_DATE"] = _COMMIT_DATE
    env["GIT_COMMITTER_NAME"] = _AUTHOR_NAME
    env["GIT_COMMITTER_EMAIL"] = _AUTHOR_EMAIL
    env["GIT_COMMITTER_DATE"] = _COMMIT_DATE
    return env


def _git_head(repo: Path) -> str | None:
    result = _git(repo, ["rev-parse", "--verify", "HEAD"], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _valid_object_id(value: str | None) -> bool:
    """判断外部状态中的 Git object id 是否具备安全的十六进制格式。"""
    return value is not None and _OBJECT_ID_RE.fullmatch(value) is not None


def _commit_tree(repo: Path, commit: str) -> str | None:
    """读取 commit 对应的 tree；commit 不存在或格式非法时返回 None。"""
    if not _valid_object_id(commit):
        return None
    result = _git(repo, ["rev-parse", f"{commit}^{{tree}}"], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _list_submodules(repo: Path) -> list[tuple[str, str]]:
    """读 .gitmodules 返回 [(name, path)]；无 .gitmodules 返回空。"""
    gitmodules = repo / ".gitmodules"
    if not gitmodules.is_file():
        return []
    result = _git(
        repo,
        ["config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    entries: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        key, _, path = line.partition(" ")
        name = key[len("submodule.") : -len(".path")]
        entries.append((name, path.strip()))
    return entries


@dataclass
class _RepoNode:
    relpath: str
    repo: Path
    children: list["_RepoNode"] = field(default_factory=list)
    submodule_names: dict[str, str] = field(default_factory=dict)  # path -> name


def _discover_tree(
    repo: Path,
    relpath: str = ".",
    extra_repositories: list[tuple[Path, str]] | None = None,
) -> _RepoNode:
    """递归发现 submodule 与 workspace 中的独立仓库/worktree。"""
    if not (repo / ".git").exists():
        raise SnapshotError(f"目录不是 git 工作树: {repo}")
    node = _RepoNode(relpath=relpath, repo=repo)
    child_paths: set[Path] = set()
    for name, path in _list_submodules(repo):
        child_repo = repo / path
        child_relpath = path if relpath in ("", ".") else f"{relpath}/{path}"
        if not (child_repo / ".git").exists():
            raise SnapshotError(
                f"子模块未初始化或不是 git 工作树: {child_repo}"
                "（请先 git submodule update --init --recursive）"
            )
        node.submodule_names[path] = name
        child_paths.add(child_repo.resolve())
        node.children.append(
            _discover_tree(child_repo, child_relpath, extra_repositories)
        )

    # 额外仓库按最近祖先挂载：root/vllm-seu/.worktrees/x 由 vllm-seu
    # 节点展开，不会被重复挂到 root 节点。
    candidates: list[tuple[Path, str]] = []
    for child_repo, child_relpath in extra_repositories or []:
        child_repo = Path(child_repo).resolve()
        if child_repo == repo.resolve() or child_repo in child_paths:
            continue
        try:
            child_repo.relative_to(repo.resolve())
        except ValueError:
            continue
        candidates.append((child_repo, child_relpath))
    for child_repo, child_relpath in sorted(
        candidates, key=lambda item: (len(item[0].parts), str(item[0]))
    ):
        if any(
            child_repo != parent and child_repo.is_relative_to(parent)
            for parent in child_paths
        ):
            continue
        relative = child_repo.relative_to(repo.resolve()).as_posix()
        if relative in node.submodule_names:
            continue
        if not (child_repo / ".git").exists():
            raise SnapshotError(f"发现的 Git worktree 不可用: {child_repo}")
        child_paths.add(child_repo)
        node.submodule_names[relative] = relative
        node.children.append(
            _discover_tree(child_repo, child_relpath, extra_repositories)
        )
    return node


def _iter_postorder(node: _RepoNode):
    for child in node.children:
        yield from _iter_postorder(child)
    yield node


def _gitlink_for_path(repo: Path, commit: str | None, path: str) -> str | None:
    """commit（可为 None）中 path 处的 gitlink sha；非 gitlink 返回 None。"""
    if not commit:
        return None
    result = _git(repo, ["ls-tree", commit, "--", path], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    fields = result.stdout.splitlines()[0].split(maxsplit=3)
    if len(fields) >= 3 and fields[0] == "160000":
        return fields[2]
    return None


def _changed_paths(
    repo: Path,
    source_head: str | None,
    commit: str,
    node: _RepoNode,
    child_commits: dict[str, RepoSnapshot],
) -> list[str]:
    """source_head..commit 的变更文件；剔除 transport-only 子模块路径。"""
    if source_head:
        result = _git(repo, ["diff", "--name-only", source_head, commit], check=False)
    else:
        result = _git(repo, ["show", "--pretty=", "--name-only", commit], check=False)
    paths = [p.strip() for p in result.stdout.splitlines() if p.strip()]

    transport_only: set[str] = set()
    for child in node.children:
        rel = child.repo.relative_to(repo).as_posix()
        child_rec = child_commits[child.relpath]
        src_gitlink = _gitlink_for_path(repo, source_head, rel)
        # 子模块无实质改动时，其 gitlink 在 snapshot 里的替换不算父级变化
        if src_gitlink and src_gitlink == child_rec.source_head and not child_rec.changed_paths:
            transport_only.add(rel)
    return [p for p in paths if p not in transport_only]


def _build_snapshot(
    node: _RepoNode,
    child_commits: dict[str, RepoSnapshot],
    parent_commit: str | None = None,
) -> RepoSnapshot:
    """为单个仓库构造 snapshot，必要时把上次 snapshot 作为 parent。"""
    repo = node.repo
    source_head = _git_head(repo)

    index_file = tempfile.NamedTemporaryFile(prefix="snapshot-index-", delete=False)
    index_file.close()
    index_path = Path(index_file.name)
    env = _identity_env()
    env["GIT_INDEX_FILE"] = str(index_path)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        if source_head:
            _git(repo, ["read-tree", source_head], env=env)
        else:
            _git(repo, ["read-tree", "--empty"], env=env)
        # 全量 add：committed + staged + unstaged + untracked 非 ignored
        _git(repo, ["add", "-A"], env=env)
        # 剔除子模块路径与插件自有目录（gitlink 稍后用子 snapshot id 重建）
        for child in node.children:
            rel = child.repo.relative_to(repo).as_posix()
            _git(repo, ["reset", "-q", "--", rel], env=env, check=False)
        for name in _PLUGIN_DIRS:
            _git(repo, ["reset", "-q", "--", name], env=env, check=False)
        # gitlink 替换为子 snapshot id
        submodules: list[dict[str, str]] = []
        for child in node.children:
            rel = child.repo.relative_to(repo).as_posix()
            child_rec = child_commits[child.relpath]
            _git(
                repo,
                ["update-index", "--add", "--cacheinfo", f"160000,{child_rec.commit},{rel}"],
                env=env,
            )
            submodules.append(
                {
                    "name": node.submodule_names.get(rel, rel),
                    "path": rel,
                    "commit": child_rec.commit,
                }
            )

        tree = _git(repo, ["write-tree"], env=env).stdout.strip()
        message = f"remote-plugin snapshot {node.relpath}"
        parent_tree = _commit_tree(repo, parent_commit or "")
        parent = parent_commit if parent_tree is not None else None
        if parent is not None and parent_tree == tree:
            # 工作树内容没有变化时复用旧 snapshot，保持 no-change 快路径的 SHA 不变。
            commit = parent
            parent_for_result = None
        else:
            commit_args = ["commit-tree", tree]
            if parent is not None:
                commit_args.extend(["-p", parent])
            commit_args.extend(["-m", message])
            commit = _git(repo, commit_args, env=env).stdout.strip()
            parent_for_result = parent
        changed = _changed_paths(repo, source_head, commit, node, child_commits)
        return RepoSnapshot(
            relpath=node.relpath,
            repo=repo,
            source_head=source_head,
            commit=commit,
            tree=tree,
            changed_paths=changed,
            submodules=submodules,
            parent_commit=parent_for_result,
        )
    finally:
        try:
            os.unlink(index_path)
        except OSError:
            pass


def build_snapshots(
    local_root: Path,
    extra_repositories: list[tuple[Path, str]] | None = None,
    parent_commits: dict[str, str] | None = None,
) -> SnapshotSet:
    """postorder 递归为 local_root 整树构造 synthetic snapshots。

    - 叶子 submodule → 父 submodule → 根仓库；
    - 每 repo：临时 index 全量 add、剔除 ignored 与子模块路径、
      gitlink 替换为子 snapshot id；有可用旧 snapshot 且内容变化时以其为 parent，
      内容不变时复用旧 snapshot；否则写 parentless commit；
    - 记录 source_head，输出每 repo 的 snapshot sha 与 changed_paths。
    """
    local_root = Path(local_root).resolve()
    if not (local_root / ".git").exists():
        raise SnapshotError(f"不是 git 工作树: {local_root}")
    tree = _discover_tree(local_root, extra_repositories=extra_repositories)
    child_commits: dict[str, RepoSnapshot] = {}
    repos: list[RepoSnapshot] = []
    parents = parent_commits or {}
    for node in _iter_postorder(tree):
        record = _build_snapshot(node, child_commits, parents.get(node.relpath))
        child_commits[node.relpath] = record
        repos.append(record)
    root = child_commits["."]
    return SnapshotSet(repos=repos, root=root, by_relpath=child_commits)
