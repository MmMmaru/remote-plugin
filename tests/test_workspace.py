"""workspace 根定位与 registered Git worktree 发现测试。"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from remote_plugin import workspace


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """运行 Git 测试命令，失败时直接让测试给出 stderr。"""
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败: {result.stderr.strip()}")
    return result


def _make_repo(path: Path) -> None:
    """创建带一个初始提交的测试仓库。"""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "workspace-test")
    _git(path, "config", "user.email", "workspace-test@example.invalid")
    (path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "initial")


class TestWorkspaceDiscovery(unittest.TestCase):
    """只把 workspace 内仓库与注册 worktree 纳入同步树。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve() / "workspace"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_registered_worktree_bypasses_parent_ignore(self):
        """父仓库忽略目录时，Git 已注册 worktree 仍被发现。"""
        _make_repo(self.root)
        (self.root / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
        _git(self.root, "add", ".gitignore")
        _git(self.root, "commit", "-qm", "ignore worktrees")

        owner = self.root / "vllm-seu"
        _make_repo(owner)
        worktree = self.root / ".worktrees" / "vllm-seu-feature"
        _git(owner, "worktree", "add", "-b", "feature", str(worktree))

        contexts = workspace.discover_repositories(self.root)
        by_relpath = {item.relpath: item for item in contexts}
        self.assertIn(".", by_relpath)
        self.assertIn("vllm-seu", by_relpath)
        self.assertIn(".worktrees/vllm-seu-feature", by_relpath)
        self.assertTrue(by_relpath[".worktrees/vllm-seu-feature"].is_worktree)

    def test_find_workspace_root_uses_remote_marker(self):
        """从 workspace 子目录执行 CLI 时，根目录由 .remote 标记确定。"""
        (self.root / ".remote").mkdir()
        nested = self.root / "vllm-seu" / "src"
        nested.mkdir(parents=True)
        self.assertEqual(workspace.find_workspace_root(nested), self.root)

    def test_worktree_is_found_when_owner_is_ignored(self):
        """owner 被父仓库忽略时，显式注册的 worktree 仍可作为同步节点。"""
        _make_repo(self.root)
        (self.root / ".gitignore").write_text("vllm-seu/\n.worktrees/\n", encoding="utf-8")
        _git(self.root, "add", ".gitignore")
        _git(self.root, "commit", "-qm", "ignore nested owner")

        owner = self.root / "vllm-seu"
        _make_repo(owner)
        worktree = self.root / ".worktrees" / "vllm-seu-feature"
        _git(owner, "worktree", "add", "-b", "feature", str(worktree))

        contexts = workspace.discover_repositories(self.root)
        relpaths = {item.relpath for item in contexts}
        self.assertNotIn("vllm-seu", relpaths)
        self.assertIn(".worktrees/vllm-seu-feature", relpaths)


if __name__ == "__main__":
    unittest.main()
