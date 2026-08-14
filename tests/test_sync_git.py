"""sync_git 模块单元测试。

fake ssh 层把"远端脚本"当作本地 bash 执行，workspace_root 即本地真实
目录，从而在纯本地跑通 bundle → mirror → materialize → verify 全链路。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from remote_plugin import config, output, ssh, snapshot, sync_git, sync_paths
from remote_plugin.config import ContainerCfg, Machine
from remote_plugin.sync_git import SyncResult
from tests.fake_ssh import BASH, msys_path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败: {result.stderr.strip()}")
    return result


class FakeSSH:
    """把远端脚本当作本地 bash 执行；可选 sabotage / probe 失败模拟。"""

    def __init__(self, workspace_root: Path, sabotage: bool = False, probe_rc: int = 0):
        self.ws = Path(workspace_root)
        self.sabotage = sabotage
        self.probe_rc = probe_rc
        self.run_calls: list[str] = []
        self.pipe_calls: list[str] = []
        self._sabotaged = False

    def ssh_run(self, endpoint, script, timeout_sec=300, input_bytes=None):
        self.run_calls.append(script)
        if "test -d" in script:  # up 探测
            if self.probe_rc == 255:
                return subprocess.CompletedProcess([], 255, b"", b"ssh: connect failed")
            if self.probe_rc != 0:
                return subprocess.CompletedProcess([], self.probe_rc, b"", b"")
        if input_bytes is None:
            proc = subprocess.run([BASH, "-s"], input=script.encode(), capture_output=True)
        else:
            proc = subprocess.run([BASH, "-c", script], input=input_bytes, capture_output=True)
        if self.sabotage and not self._sabotaged and "reset --hard" in script:
            # 模拟远端对齐后又被外部改动（HEAD 漂移）
            subprocess.run(
                [
                    "git", "-C", str(self.ws / "main"),
                    "-c", "user.name=saboteur", "-c", "user.email=s@t",
                    "commit", "--allow-empty", "-qm", "sabotage",
                ],
                capture_output=True,
            )
            self._sabotaged = True
        return proc

    def ssh_pipe(self, endpoint, local_cmd, remote_cmd):
        self.pipe_calls.append(remote_cmd)
        local = subprocess.run(local_cmd, capture_output=True)
        if local.returncode != 0:
            raise ssh.SSHError(f"本地命令失败: {local.stderr.decode('utf-8', 'replace')}")
        proc = subprocess.run(
            [BASH, "-c", remote_cmd], input=local.stdout, capture_output=True
        )
        if proc.returncode != 0:
            raise ssh.SSHError(
                f"远端命令失败（{proc.returncode}）: {proc.stderr.decode('utf-8', 'replace')[:500]}"
            )
        return proc.returncode


class SyncGitBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # resolve() 展开 Windows 8.3 短名（X50063~1 → x50063850），与 git 输出对齐
        self.tmp = Path(self._tmp.name).resolve()
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.local_root = self.tmp / "repo"
        self.local_root.mkdir()
        (self.local_root / ".remote").mkdir()  # 让 state_dir 落在 fixture 内
        self._make_fixture()
        self.fake = FakeSSH(self.ws)
        self._patches = [
            mock.patch.object(ssh, "ssh_run", self.fake.ssh_run),
            mock.patch.object(ssh, "ssh_pipe", self.fake.ssh_pipe),
            mock.patch.object(output, "progress", lambda obj: None),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_fixture(self) -> None:
        """主 repo + 一层 submodule，dirty 树（改跟踪 + 未跟踪 + ignored）。"""
        _git(self.local_root, "init", "-q")
        _git(self.local_root, "config", "user.email", "t@t")
        _git(self.local_root, "config", "user.name", "t")
        sub = self.local_root / "sub"
        sub.mkdir()
        _git(sub, "init", "-q")
        _git(sub, "config", "user.email", "t@t")
        _git(sub, "config", "user.name", "t")
        (sub / "x.txt").write_text("x1", encoding="utf-8")
        _git(sub, "add", "-A")
        _git(sub, "commit", "-qm", "sub c1")
        sub_head = _git(sub, "rev-parse", "HEAD").stdout.strip()
        (self.local_root / ".gitmodules").write_text(
            '[submodule "sub"]\n\tpath = sub\n\turl = file:///none\n', encoding="utf-8"
        )
        _git(self.local_root, "add", ".gitmodules")
        _git(self.local_root, "update-index", "--add", "--cacheinfo", f"160000,{sub_head},sub")
        _git(self.local_root, "commit", "-qm", "root c1")
        (self.local_root / "a.txt").write_text("v1", encoding="utf-8")
        _git(self.local_root, "add", "a.txt")
        _git(self.local_root, "commit", "-qm", "c2")
        # dirty
        (self.local_root / "a.txt").write_text("v2", encoding="utf-8")
        (self.local_root / "b.txt").write_text("new", encoding="utf-8")
        (self.local_root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        (self.local_root / "ignored.txt").write_text("secret", encoding="utf-8")
        (sub / "x.txt").write_text("x2", encoding="utf-8")

    def _ssh_machine(self, alias: str = "t5box") -> Machine:
        # workspace_root 会嵌进远端脚本并由本地 bash 执行：Windows 路径须转 MSYS 形式
        return Machine(
            alias=alias, mode="ssh", host="127.0.0.1", port=22, user="root",
            workspace_root=msys_path(self.ws),
        )

    def _container_machine(self, alias: str = "t5box") -> Machine:
        machine = Machine(
            alias=alias, mode="container", host="127.0.0.1", port=22, user="root",
            container=ContainerCfg(image="img", name="c", ssh_port=22, workspace_root=msys_path(self.ws)),
            workspace_root="/fallback",
        )
        ep_dir = self.local_root / ".remote" / "state" / "endpoints"
        ep_dir.mkdir(parents=True, exist_ok=True)
        (ep_dir / f"{alias}.json").write_text(
            json.dumps(
                {"host": "127.0.0.1", "port": 22, "user": "root", "workspace_root": msys_path(self.ws)}
            ),
            encoding="utf-8",
        )
        return machine


class TestSyncGit(SyncGitBase):
    def test_ready_end_to_end(self):
        machine = self._ssh_machine()
        result = sync_git.sync_git(machine, "main", self.local_root)
        self.assertEqual(result.status, "ready")
        self.assertEqual(set(result.snapshots), {"sub", "."})
        self.assertEqual(result.remote_heads, result.snapshots)
        # 容器内 mirror 已建立
        self.assertTrue((self.ws / ".remote-mirrors" / "root.git" / "objects").is_dir())
        self.assertTrue((self.ws / ".remote-mirrors" / "sub.git" / "objects").is_dir())
        # worktree 各 repo HEAD == snapshot
        self.assertEqual(
            _git(self.ws / "main", "rev-parse", "HEAD").stdout.strip(),
            result.snapshots["."],
        )
        self.assertEqual(
            _git(self.ws / "main" / "sub", "rev-parse", "HEAD").stdout.strip(),
            result.snapshots["sub"],
        )
        # 内容对齐：dirty/untracked 远端可见，ignored 不可见
        self.assertEqual((self.ws / "main" / "a.txt").read_text(encoding="utf-8"), "v2")
        self.assertEqual((self.ws / "main" / "b.txt").read_text(encoding="utf-8"), "new")
        self.assertFalse((self.ws / "main" / "ignored.txt").exists())
        self.assertEqual((self.ws / "main" / "sub" / "x.txt").read_text(encoding="utf-8"), "x2")
        # 子模块 URL 改写为容器内 mirror
        cfg = (self.ws / "main" / ".git" / "config").read_text(encoding="utf-8")
        # git-for-windows 会把 URL 归一化为原生形式（C:/...），Linux 上即 str() 本身
        want_url = str(self.ws / ".remote-mirrors" / "sub.git").replace("\\", "/")
        self.assertIn(want_url, cfg)
        # 本地状态已记录（供快路径）
        state_file = self.local_root / ".remote" / "state" / "sync" / "t5box" / "main.json"
        self.assertTrue(state_file.is_file())
        self.assertEqual(
            json.loads(state_file.read_text(encoding="utf-8"))["snapshot_commits"],
            result.snapshots,
        )
        # changed_paths：dirty/untracked/子模块内部变更可见，ignored 不可见
        cp = result.changed_paths
        self.assertIn("a.txt", cp)
        self.assertIn("b.txt", cp)
        self.assertIn("sub", cp)      # gitlink 移动（sub dirty → 非 transport-only）
        self.assertIn("sub/x.txt", cp)
        self.assertNotIn("ignored.txt", cp)

    def test_no_change_fast_path_single_ssh(self):
        machine = self._container_machine()
        r1 = sync_git.sync_git(machine, "main", self.local_root)
        self.assertEqual(r1.status, "ready")
        pipes_after_first = len(self.fake.pipe_calls)
        self.assertGreater(pipes_after_first, 0)  # 首次同步按 repo 数推 bundle
        calls_after_first = len(self.fake.run_calls)
        r2 = sync_git.sync_git(machine, "main", self.local_root)
        self.assertEqual(r2.status, "no_change")
        self.assertEqual(r2.snapshots, r1.snapshots)
        self.assertEqual(r2.changed_paths, [])
        # 快路径：仅一次 SSH 校验、零新增 bundle
        self.assertEqual(len(self.fake.run_calls) - calls_after_first, 1)
        self.assertEqual(len(self.fake.pipe_calls), pipes_after_first)

    def test_blocked_container_need_up(self):
        machine = Machine(
            alias="noup", mode="container", host="h", port=22, user="root",
            container=ContainerCfg(image="i", name="n", ssh_port=22, workspace_root="/x"),
        )
        result = sync_git.sync_git(machine, "main", self.local_root)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "need up")
        self.assertEqual(self.fake.run_calls, [])  # 零 SSH

    def test_blocked_ssh_workspace_missing(self):
        machine = Machine(
            alias="t", mode="ssh", host="h", port=22, user="root",
            workspace_root=msys_path(self.tmp / "nope"),
        )
        result = sync_git.sync_git(machine, "main", self.local_root)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "need up")

    def test_failed_when_remote_head_mismatch(self):
        machine = self._ssh_machine()
        self.fake.sabotage = True
        result = sync_git.sync_git(machine, "main", self.local_root)
        self.assertEqual(result.status, "failed")
        self.assertNotEqual(result.remote_heads, result.snapshots)
        self.assertIn("fail closed", result.reason or "")

    def test_ssh_unreachable_raises(self):
        machine = self._ssh_machine()
        self.fake.probe_rc = 255
        with self.assertRaises(ssh.SSHError):
            sync_git.sync_git(machine, "main", self.local_root)

    def test_not_git_local_root_raises(self):
        plain = self.local_root / "plain"  # 在 fixture 内，避免 state_dir 落到 home
        plain.mkdir()
        machine = self._ssh_machine()
        with self.assertRaises(snapshot.SnapshotError):
            sync_git.sync_git(machine, "main", plain)


class TestCliSync(SyncGitBase):
    def _machine(self) -> Machine:
        return Machine(alias="a", mode="ssh", host="h", port=22, user="root")

    def test_dispatches_to_sync_paths(self):
        machine = self._machine()
        args = SimpleNamespace(alias="a", worktree="main", paths=["x.py", "tests/"])
        with mock.patch.object(config, "load_machines", return_value={"a": machine}), \
             mock.patch.object(
                 sync_paths, "sync_paths",
                 return_value={"status": "ready", "files": 1, "bytes": 3, "sha256_ok": True},
             ) as m:
            result = sync_git.cli_sync(args)
        self.assertEqual(result["status"], "ready")
        m.assert_called_once()
        call = m.call_args
        self.assertIs(call.args[0], machine)
        self.assertEqual(call.args[1], "main")
        self.assertEqual([str(p) for p in call.args[2]], ["x.py", "tests"])
        self.assertIsInstance(call.args[3], Path)

    def test_dispatches_to_sync_git(self):
        machine = self._machine()
        args = SimpleNamespace(alias="a", worktree="main", paths=None)
        with mock.patch.object(config, "load_machines", return_value={"a": machine}), \
             mock.patch.object(
                 sync_git, "sync_git",
                 return_value=SyncResult("ready", {".": "x"}, {".": "x"}, ["f"]),
             ) as m:
            result = sync_git.cli_sync(args)
        m.assert_called_once()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["changed_paths"], ["f"])

    def test_unknown_alias_raises(self):
        args = SimpleNamespace(alias="zzz", worktree="main", paths=None)
        with mock.patch.object(config, "load_machines", return_value={}):
            with self.assertRaises(config.ConfigError):
                sync_git.cli_sync(args)

    def test_sync_paths_module_missing_graceful(self):
        machine = self._machine()
        args = SimpleNamespace(alias="a", worktree="main", paths=["x"])
        with mock.patch.object(config, "load_machines", return_value={"a": machine}), \
             mock.patch.dict(sys.modules, {"remote_plugin.sync_paths": None}):
            with self.assertRaises(config.RemotePluginError) as ctx:
                sync_git.cli_sync(args)
        self.assertIn("尚未实现", str(ctx.exception))

    def test_repo_root_resolution(self):
        self.assertEqual(sync_git._repo_root(self.local_root), self.local_root)


if __name__ == "__main__":
    unittest.main()
