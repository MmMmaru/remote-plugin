"""Workspace 根定位与 Git 仓库/worktree 发现。

文件筛选仍由各仓库的 Git 规则负责；本模块只发现同步树中的仓库节点。
已注册且位于 workspace 内的 worktree 是唯一允许越过父仓库 ignore 的节点。
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config, output


class WorkspaceError(config.RemotePluginError):
    """workspace 定位或仓库发现失败。"""


@dataclass(frozen=True)
class RepositoryContext:
    """workspace 中一个独立 Git 工作树。"""

    path: Path
    relpath: str
    is_worktree: bool = False


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """在 repo 中运行 Git，返回原始字节输出。"""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceError(f"git 执行失败（{repo}）: {exc}") from exc


def find_workspace_root(start: Path | None = None) -> Path:
    """从 start 向上寻找最近的 .remote 所在目录。"""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        if (directory / ".remote").is_dir():
            return directory
    result = _run_git(current, "rev-parse", "--show-toplevel")
    if result.returncode == 0:
        return Path(result.stdout.decode("utf-8", "replace").strip()).resolve()
    return current


def _git_roots(root: Path) -> list[Path]:
    """递归发现 .git 目录或文件；不进入 Git 元数据目录。"""
    found: list[Path] = []
    for directory, dirnames, filenames in os.walk(root):
        current = Path(directory)
        if ".git" in dirnames or ".git" in filenames:
            found.append(current.resolve())
        dirnames[:] = [name for name in dirnames if name != ".git"]
    return sorted(set(found), key=lambda path: (len(path.parts), str(path)))


def _ignored_by_parent(path: Path, root: Path, candidates: set[Path]) -> bool:
    """用最近父仓库的 Git ignore 规则判断 path 是否被忽略。"""
    parents = [
        candidate
        for candidate in candidates
        if candidate != path and path.is_relative_to(candidate)
    ]
    if not parents:
        return False
    parent = max(parents, key=lambda candidate: len(candidate.parts))
    relative = path.relative_to(parent).as_posix()
    result = _run_git(parent, "check-ignore", "-q", "--", relative)
    return result.returncode == 0


def _registered_worktrees(
    repo: Path, workspace_root: Path
) -> tuple[set[Path], list[str]]:
    """读取 repo 的 registered worktrees，筛选 workspace 内路径。"""
    result = _run_git(repo, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        return set(), []
    inside: set[Path] = set()
    warnings: list[str] = []
    primary = True
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[9:]).expanduser().resolve()
        # `git worktree list` 的首项是该仓库的主工作树；它不是需要额外
        # 挂载的 worktree。对从 linked worktree 调用的情况也跳过首项。
        if primary:
            primary = False
            continue
        if path == repo.resolve():
            continue
        if path.is_relative_to(workspace_root):
            inside.add(path)
        else:
            warnings.append(f"worktree 位于 workspace 外，已忽略: {path}")
    return inside, warnings


def _discover_repository_data(
    workspace_root: Path,
) -> tuple[list[RepositoryContext], list[str]]:
    """发现非 ignored 仓库和 workspace 内 registered worktree。"""
    root = Path(workspace_root).resolve()
    candidates = set(_git_roots(root))
    if (root / ".git").exists():
        candidates.add(root)

    # 先对全部 Git 根查询注册表：owner 自身可能被父仓库忽略，但其已注册
    # worktree 仍是显式同步对象。最终普通仓库节点是否进入 contexts 再按 ignore 过滤。
    owners = sorted(candidates, key=lambda path: (len(path.parts), str(path)))
    registered: set[Path] = set()
    warnings: list[str] = []
    for owner in owners:
        paths, messages = _registered_worktrees(owner, root)
        registered.update(paths)
        warnings.extend(messages)
    warnings = list(dict.fromkeys(warnings))
    candidates.update(registered)

    contexts: list[RepositoryContext] = []
    for path in sorted(candidates, key=lambda candidate: (len(candidate.parts), str(candidate))):
        if not (path / ".git").exists():
            continue
        if path != root and path not in registered and _ignored_by_parent(path, root, candidates):
            continue
        if not path.is_relative_to(root):
            continue
        relpath = path.relative_to(root).as_posix()
        contexts.append(
            RepositoryContext(
                path=path,
                relpath="." if relpath == "." else relpath,
                is_worktree=path in registered,
            )
        )
    if (root / ".git").exists() and not any(item.path == root for item in contexts):
        contexts.insert(0, RepositoryContext(root, "."))
    return contexts, warnings


def discover_repositories(workspace_root: Path) -> list[RepositoryContext]:
    """递归发现 workspace 内可同步的仓库和 registered worktree。"""
    contexts, warnings = _discover_repository_data(workspace_root)
    for warning in warnings:
        output.progress({"warning": warning})
    return contexts


def discover_registered_worktrees(
    repository: RepositoryContext, workspace_root: Path
) -> list[Path]:
    """返回 repository 在 workspace 内注册的 worktree 路径。"""
    paths, warnings = _registered_worktrees(repository.path, Path(workspace_root).resolve())
    for warning in warnings:
        output.progress({"warning": warning})
    return sorted(paths)
