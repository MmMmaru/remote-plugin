"""snapshot 模块单元测试（纯本地，真实 git 操作，无网络）。"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from remote_plugin import snapshot
from remote_plugin.snapshot import SnapshotError


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败: {result.stderr.strip()}")
    return result


def _make_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@test")
    _git(path, "config", "user.name", "t")


def _commit_all(path: Path, msg: str) -> str:
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", msg)
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def _register_submodule(parent: Path, path: str, sub_commit: str, name: str | None = None) -> None:
    """手工在 parent 注册子模块：写 .gitmodules + gitlink + 提交。"""
    name = name or path.replace("/", "-")
    gm_path = parent / ".gitmodules"
    text = gm_path.read_text(encoding="utf-8") if gm_path.exists() else ""
    text += f'\n[submodule "{name}"]\n\tpath = {path}\n\turl = file:///nonexistent/{name}\n'
    gm_path.write_text(text, encoding="utf-8")
    _git(parent, "add", ".gitmodules")
    _git(parent, "update-index", "--add", "--cacheinfo", f"160000,{sub_commit},{path}")
    _git(parent, "commit", "-qm", f"add submodule {path}")


class _TempDir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestBuildSnapshots(_TempDir):
    def test_deterministic_dirty_tree_ignored_and_index_untouched(self):
        repo = self.root / "repo"
        _make_repo(repo)
        (repo / "a.txt").write_text("v1", encoding="utf-8")
        _commit_all(repo, "c1")
        source_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

        # dirty：改跟踪文件 + 新未跟踪文件 + ignored 文件
        (repo / "a.txt").write_text("v2", encoding="utf-8")
        (repo / "b.txt").write_text("untracked", encoding="utf-8")
        (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        (repo / "ignored.txt").write_text("secret", encoding="utf-8")

        s1 = snapshot.build_snapshots(repo)
        s2 = snapshot.build_snapshots(repo)

        # 确定性：连跑两次 sha 相同
        self.assertEqual(s1.root.commit, s2.root.commit)
        self.assertEqual(s1.root.tree, s2.root.tree)
        self.assertEqual(s1.root.changed_paths, s2.root.changed_paths)
        # source_head 记录正确
        self.assertEqual(s1.root.source_head, source_head)
        # changed_paths 含 dirty/untracked，不含 ignored
        self.assertEqual(set(s1.root.changed_paths), {"a.txt", "b.txt", ".gitignore"})
        self.assertNotIn("ignored.txt", s1.root.changed_paths)
        # snapshot 树内容 = 工作树
        self.assertEqual(_git(repo, "show", f"{s1.root.commit}:a.txt").stdout, "v2")
        self.assertEqual(_git(repo, "show", f"{s1.root.commit}:b.txt").stdout, "untracked")
        ls = _git(repo, "ls-tree", "--name-only", s1.root.commit).stdout
        self.assertNotIn("ignored.txt", ls)
        # 不碰工作树与真实 index
        status = _git(repo, "status", "--porcelain").stdout
        self.assertIn(" M a.txt", status)
        self.assertIn("?? b.txt", status)

    def test_plugin_state_dir_excluded_and_deterministic(self):
        """插件自有 .remote/ 目录不入 snapshot，state 写入不影响确定性。"""
        repo = self.root / "repo"
        _make_repo(repo)
        (repo / "a.txt").write_text("v1", encoding="utf-8")
        _commit_all(repo, "c1")
        remote_dir = repo / ".remote" / "state"
        remote_dir.mkdir(parents=True)

        s1 = snapshot.build_snapshots(repo)
        # 两次构建之间 state 文件出现 → snapshot 不变
        (remote_dir / "sync.json").write_text('{"k": 1}', encoding="utf-8")
        s2 = snapshot.build_snapshots(repo)
        self.assertEqual(s1.root.commit, s2.root.commit)
        ls = _git(repo, "ls-tree", "--name-only", s2.root.commit).stdout
        self.assertNotIn(".remote", ls)

    def test_submodule_gitlink_replacement_and_transport_only(self):
        root_repo = self.root / "root"
        root_repo.mkdir(parents=True)
        sub_repo = root_repo / "sub"
        _make_repo(sub_repo)
        (sub_repo / "x.txt").write_text("sub-v1", encoding="utf-8")
        sub_head = _commit_all(sub_repo, "sub c1")

        _make_repo(root_repo)
        _register_submodule(root_repo, "sub", sub_head)
        root_head = _git(root_repo, "rev-parse", "HEAD").stdout.strip()

        # sub 干净：postorder 顺序、source_head 正确、transport-only 过滤
        s = snapshot.build_snapshots(root_repo)
        self.assertEqual([r.relpath for r in s.repos], ["sub", "."])
        self.assertEqual(s.by_relpath["sub"].source_head, sub_head)
        self.assertEqual(s.by_relpath["sub"].changed_paths, [])
        self.assertEqual(s.root.source_head, root_head)
        self.assertEqual(s.root.changed_paths, [])  # gitlink 替换不算父级变化
        # 父级 snapshot 中 gitlink == 子 snapshot id
        fields = _git(root_repo, "ls-tree", s.root.commit, "--", "sub").stdout.split()
        self.assertEqual(fields[0], "160000")
        self.assertEqual(fields[2], s.by_relpath["sub"].commit)

        # sub dirty（改 + 新增）→ 子 snapshot 变化，父级 gitlink 指向新子 snapshot
        (sub_repo / "x.txt").write_text("sub-v2", encoding="utf-8")
        (sub_repo / "new.txt").write_text("new", encoding="utf-8")
        s2 = snapshot.build_snapshots(root_repo)
        sub_snap = s2.by_relpath["sub"]
        self.assertEqual(set(sub_snap.changed_paths), {"x.txt", "new.txt"})
        fields2 = _git(root_repo, "ls-tree", s2.root.commit, "--", "sub").stdout.split()
        self.assertEqual(fields2[2], sub_snap.commit)
        self.assertIn("sub", s2.root.changed_paths)  # gitlink 移动非 transport-only
        self.assertEqual(
            set(s2.aggregate_changed_paths()), {"sub", "sub/new.txt", "sub/x.txt"}
        )

    def test_empty_repo_unborn_head(self):
        repo = self.root / "empty"
        _make_repo(repo)
        (repo / "f.txt").write_text("data", encoding="utf-8")
        s = snapshot.build_snapshots(repo)
        self.assertIsNone(s.root.source_head)
        self.assertEqual(set(s.root.changed_paths), {"f.txt"})
        self.assertEqual(_git(repo, "show", f"{s.root.commit}:f.txt").stdout, "data")
        s2 = snapshot.build_snapshots(repo)
        self.assertEqual(s.root.commit, s2.root.commit)  # 确定性

    def test_nested_submodule_recursion(self):
        root_repo = self.root / "root"
        sub_repo = root_repo / "sub"
        leaf_repo = sub_repo / "leaf"
        leaf_repo.mkdir(parents=True)
        _make_repo(leaf_repo)
        (leaf_repo / "l.txt").write_text("l", encoding="utf-8")
        leaf_head = _commit_all(leaf_repo, "leaf c1")

        _make_repo(sub_repo)
        _register_submodule(sub_repo, "leaf", leaf_head)
        sub_head = _git(sub_repo, "rev-parse", "HEAD").stdout.strip()

        _make_repo(root_repo)
        _register_submodule(root_repo, "sub", sub_head)

        s = snapshot.build_snapshots(root_repo)
        self.assertEqual([r.relpath for r in s.repos], ["sub/leaf", "sub", "."])
        root_sub = _git(root_repo, "ls-tree", s.root.commit, "--", "sub").stdout.split()
        self.assertEqual(root_sub[2], s.by_relpath["sub"].commit)
        sub_leaf = _git(sub_repo, "ls-tree", s.by_relpath["sub"].commit, "--", "leaf").stdout.split()
        self.assertEqual(sub_leaf[2], s.by_relpath["sub/leaf"].commit)

    def test_not_git_repo(self):
        plain = self.root / "plain"
        plain.mkdir()
        with self.assertRaises(SnapshotError):
            snapshot.build_snapshots(plain)

    def test_uninitialized_submodule_fails_closed(self):
        root_repo = self.root / "root"
        _make_repo(root_repo)
        (root_repo / ".gitmodules").write_text(
            '[submodule "sub"]\n\tpath = sub\n\turl = x\n', encoding="utf-8"
        )
        _git(root_repo, "add", ".gitmodules")
        _git(root_repo, "commit", "-qm", "c")
        with self.assertRaises(SnapshotError):
            snapshot.build_snapshots(root_repo)


if __name__ == "__main__":
    unittest.main()
