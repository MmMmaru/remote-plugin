"""sync_paths（T4 sync 方法 B）单元测试。纯本地，无网络。

覆盖 spec T4 e2e 步骤 1（[本地]）：
- paths 越界（`../x`）、不存在、空列表 → 明确报错；
- tar 打包清单含目录递归；
外加 FakeRemote（本地模拟远端 tar -x 与 sha256sum）驱动的整链路测试：
ready / sha256 不一致 failed / 远端缺文件 failed / 空目录 / 传输失败抛错 /
二进制流不改行尾（CRLF 验收项）。
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remote_plugin import config, ssh
from remote_plugin.sync_paths import (
    SyncPathsError,
    SyncResult,
    _collect_files,
    _resolve_paths,
    sync_paths,
)
from tests.fake_ssh import BASH, TAR, local_path, msys_path


class FakeRemote:
    """本地模拟「远端」：执行 tar 管道落盘 + 执行 sha256sum 脚本，返回真实结果。"""

    def __init__(self) -> None:
        self.local_cmds: list[list[str]] = []
        self.remote_cmds: list[str] = []
        self.scripts: list[str] = []
        self.last_workspace: Path | None = None
        self.tamper: str | None = None   # 抽检前改写远端文件 → 模拟 sha256 不一致
        self.remove: str | None = None   # 抽检前删除远端文件 → 模拟缺文件

    @staticmethod
    def _workspace_from(cmd: str) -> str:
        m = re.search(r"'([^']*)'", cmd)
        if not m:
            raise AssertionError(f"无法从命令解析 workspace 目录: {cmd!r}")
        # 命令内是 MSYS 形式（/c/...），可直接喂给 Git Bash 的 tar/bash；
        # Python 侧落盘操作需经 local_path 转回本地路径
        return m.group(1)

    def ssh_pipe(self, endpoint, local_cmd: list[str], remote_cmd: str) -> int:
        self.local_cmds.append(local_cmd)
        self.remote_cmds.append(remote_cmd)
        workspace = self._workspace_from(remote_cmd)
        self.last_workspace = local_path(workspace)
        self.last_workspace.mkdir(parents=True, exist_ok=True)
        tar_proc = subprocess.run(local_cmd, capture_output=True)
        if tar_proc.returncode != 0:
            raise ssh.SSHError(
                f"本地命令失败（{tar_proc.returncode}）: {' '.join(local_cmd)}"
            )
        extract = subprocess.run(
            [TAR, "-x", "-C", workspace], input=tar_proc.stdout, capture_output=True
        )
        if extract.returncode != 0:
            raise ssh.SSHError(f"远端 tar -x 失败（{extract.returncode}）")
        return 0

    def ssh_run(self, endpoint, script: str, timeout_sec: int = 300, input_bytes=None):
        self.scripts.append(script)
        workspace = local_path(self._workspace_from(script))
        if self.tamper is not None:
            (workspace / self.tamper).write_bytes(b"TAMPERED-BYTES")
        if self.remove is not None:
            (workspace / self.remove).unlink()
        return subprocess.run([BASH, "-c", script], input=input_bytes, capture_output=True)


class _TempBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # resolve() 展开 Windows 8.3 短名（X50063~1 → x50063850），与内核 resolve 对齐
        self.tmp_root = Path(self._tmp.name).resolve()
        self.local_root = self.tmp_root / "local"
        self.local_root.mkdir()
        self.machine = config.Machine(
            alias="t4test",
            mode="ssh",
            host="127.0.0.1",
            port=22,
            user="u",
            # 会嵌进远端脚本并由本地 bash 执行：Windows 路径须转 MSYS 形式
            workspace_root=msys_path(self.tmp_root / "ws"),
        )
        self.state = str(self.tmp_root / "state")

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestResolvePaths(_TempBase):
    """spec T4 e2e 步骤 1：越界 / 不存在 / 空列表 → 明确报错。"""

    def setUp(self) -> None:
        super().setUp()
        (self.local_root / "f.txt").write_bytes(b"hello\n")
        (self.local_root / "docs").mkdir()
        (self.local_root / "docs" / "sub").mkdir()
        (self.local_root / "docs" / "sub" / "a.py").write_bytes(b"x = 1\n")
        (self.local_root / "docs" / "README.md").write_bytes(b"# r\n")

    def test_empty_list_raises(self):
        with self.assertRaises(SyncPathsError) as ctx:
            _resolve_paths(self.local_root, [])
        self.assertIn("不能为空", str(ctx.exception))

    def test_parent_traversal_raises(self):
        for bad in (Path("../x"), Path("docs/../../x"), Path("..")):
            with self.assertRaises(SyncPathsError) as ctx:
                _resolve_paths(self.local_root, [bad])
            self.assertIn("越界", str(ctx.exception))
            self.assertIn(bad.as_posix(), str(ctx.exception))

    def test_nonexistent_raises(self):
        with self.assertRaises(SyncPathsError) as ctx:
            _resolve_paths(self.local_root, [Path("nope.txt")])
        self.assertIn("不存在", str(ctx.exception))

    def test_absolute_outside_raises(self):
        with self.assertRaises(SyncPathsError) as ctx:
            _resolve_paths(self.local_root, [Path("/etc/passwd")])
        self.assertIn("越界", str(ctx.exception))

    def test_absolute_inside_ok(self):
        rels = _resolve_paths(self.local_root, [self.local_root / "f.txt"])
        self.assertEqual(rels, [Path("f.txt")])

    def test_normalizes_dotdot_inside(self):
        rels = _resolve_paths(self.local_root, [Path("docs/../f.txt")])
        self.assertEqual(rels, [Path("f.txt")])

    def test_dedupe_keeps_first(self):
        rels = _resolve_paths(
            self.local_root,
            [Path("f.txt"), Path("docs"), Path("docs/sub/a.py"), Path("f.txt")],
        )
        self.assertEqual(rels, [Path("f.txt"), Path("docs"), Path("docs/sub/a.py")])

    def test_empty_string_path_raises(self):
        # 注意：Path("") 在 pathlib 中等价于 Path(".")，只有原始空字符串才触发该防御
        with self.assertRaises(SyncPathsError) as ctx:
            _resolve_paths(self.local_root, [""])
        self.assertIn("空字符串", str(ctx.exception))

    def test_dot_means_whole_root(self):
        rels = _resolve_paths(self.local_root, [Path(".")])
        self.assertEqual(rels, [Path(".")])

    def test_local_root_missing_raises(self):
        with self.assertRaises(SyncPathsError) as ctx:
            _resolve_paths(self.tmp_root / "no-such-root", [Path("f.txt")])
        self.assertIn("local_root", str(ctx.exception))

    def test_sync_paths_empty_paths_raises_before_any_ssh(self):
        with mock.patch("remote_plugin.ssh.ssh_pipe") as pm, \
             mock.patch("remote_plugin.ssh.ssh_run") as rm, \
             mock.patch("remote_plugin.config.state_dir") as sd:
            with self.assertRaises(SyncPathsError) as ctx:
                sync_paths(self.machine, [], self.local_root)
        self.assertIn("不能为空", str(ctx.exception))
        pm.assert_not_called()
        rm.assert_not_called()
        sd.assert_not_called()

    def test_sync_paths_out_of_root_raises_before_any_ssh(self):
        with mock.patch("remote_plugin.ssh.ssh_pipe") as pm, \
             mock.patch("remote_plugin.config.state_dir") as sd:
            with self.assertRaises(SyncPathsError) as ctx:
                sync_paths(self.machine, [Path("../x")], self.local_root)
        self.assertIn("越界", str(ctx.exception))
        pm.assert_not_called()
        sd.assert_not_called()


class TestCollectFiles(_TempBase):
    def setUp(self) -> None:
        super().setUp()
        (self.local_root / "f.txt").write_bytes(b"0123456789")
        (self.local_root / "docs").mkdir()
        (self.local_root / "docs" / "sub").mkdir()
        (self.local_root / "docs" / "sub" / "a.py").write_bytes(b"x = 1\n")
        (self.local_root / "docs" / "README.md").write_bytes(b"# r\n")

    def test_single_file(self):
        got = _collect_files(self.local_root, [Path("f.txt")])
        self.assertEqual(got, {"f.txt": 10})

    def test_directory_recursion_counts_nested_files(self):
        got = _collect_files(self.local_root, [Path("docs")])
        self.assertEqual(
            got,
            {
                "docs/README.md": 4,
                "docs/sub/a.py": 6,
            },
        )

    def test_symlinks_not_followed_not_counted(self):
        try:
            (self.local_root / "link.sh").symlink_to("f.txt")
            (self.local_root / "docs" / "dirlink").symlink_to(self.local_root / "docs" / "sub")
        except OSError as e:
            self.skipTest(f"本机无 symlink 权限（如未启用的 Windows 开发者模式）: {e}")
        got = _collect_files(self.local_root, [Path("link.sh"), Path("docs")])
        self.assertNotIn("link.sh", got)
        # dirlink 不展开 → docs/sub/a.py 仍在（经真实目录），但 dirlink/a.py 不出现
        self.assertEqual(
            got,
            {
                "docs/README.md": 4,
                "docs/sub/a.py": 6,
            },
        )


class TestTarManifest(_TempBase):
    """spec T4 e2e 步骤 1：tar 打包清单含目录递归（纯本地）。"""

    def setUp(self) -> None:
        super().setUp()
        (self.local_root / "f.txt").write_bytes(b"hi\n")
        (self.local_root / "docs").mkdir()
        (self.local_root / "docs" / "sub").mkdir()
        (self.local_root / "docs" / "sub" / "a.py").write_bytes(b"x = 1\n")

    def _members(self, rel_args: list[str]) -> list[str]:
        cmd = [TAR, "-C", str(self.local_root), "-cf", "-", "--", *rel_args]
        tar_out = subprocess.run(cmd, capture_output=True)
        self.assertEqual(tar_out.returncode, 0, tar_out.stderr)
        listing = subprocess.run(
            [TAR, "-tf", "-"], input=tar_out.stdout, capture_output=True
        )
        self.assertEqual(listing.returncode, 0, listing.stderr)
        return listing.stdout.decode("utf-8", "replace").splitlines()

    def test_directory_manifest_includes_recursion(self):
        members = self._members(["docs"])
        for want in ("docs/", "docs/sub/", "docs/sub/a.py"):
            self.assertIn(want, members)

    def test_single_file_manifest(self):
        self.assertEqual(self._members(["f.txt"]), ["f.txt"])


class TestSyncPathsPipeline(_TempBase):
    """整链路（FakeRemote 本地模拟远端）：传输 + sha256 抽检 + 输出契约。"""

    def setUp(self) -> None:
        super().setUp()
        self.lf_script = b"#!/bin/bash\nset -e\necho hi\n"           # LF 行尾
        self.crlf_text = b"line1\r\nline2\r\n"                       # CRLF 行尾
        self.nested = b"x = 1\n"
        (self.local_root / "a.sh").write_bytes(self.lf_script)
        (self.local_root / "crlf.txt").write_bytes(self.crlf_text)
        (self.local_root / "docs").mkdir()
        (self.local_root / "docs" / "sub").mkdir()
        (self.local_root / "docs" / "sub" / "nested.py").write_bytes(self.nested)
        self.paths = [Path("a.sh"), Path("crlf.txt"), Path("docs")]
        self.expected_bytes = sum(
            p.stat().st_size
            for p in (
                self.local_root / "a.sh",
                self.local_root / "crlf.txt",
                self.local_root / "docs" / "sub" / "nested.py",
            )
        )

    def _run(self, fake: FakeRemote, paths=None):
        patcher_pipe = mock.patch("remote_plugin.ssh.ssh_pipe", side_effect=fake.ssh_pipe)
        patcher_run = mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run)
        patcher_state = mock.patch(
            "remote_plugin.config.state_dir", return_value=self.state
        )
        with patcher_pipe as pm, patcher_run as rm, patcher_state:
            result = sync_paths(
                self.machine,
                paths if paths is not None else self.paths,
                self.local_root,
            )
        return result, pm, rm

    def test_ready_full_pipeline_binary_fidelity(self):
        fake = FakeRemote()
        result, pm, rm = self._run(fake)

        self.assertEqual(result.status, "ready")
        self.assertTrue(result.sha256_ok)
        self.assertEqual(result.files, 3)
        self.assertEqual(result.bytes, self.expected_bytes)
        self.assertEqual(result.to_dict()["status"], "ready")

        # 本地 tar 命令：GNU tar（localtools 解析）、MSYS 路径、相对结构、目录原样传入（`--` 防路径以 `-` 开头被当选项）
        from remote_plugin.localtools import gnu_tar, tar_path
        lc = pm.call_args.args[1]
        self.assertEqual(lc[0], gnu_tar())
        self.assertEqual(lc[1:6], ["-C", tar_path(self.local_root), "-cf", "-", "--"])
        self.assertEqual(set(lc[6:]), {"a.sh", "crlf.txt", "docs"})

        # 远端命令：先 mkdir -p workspace，再 tar -x 覆盖（命令内为 MSYS 形式路径）
        wt = msys_path(self.tmp_root / "ws")
        rc = pm.call_args.args[2]
        self.assertIn(f"mkdir -p '{wt}'", rc)
        self.assertIn(f"tar -x -C '{wt}'", rc)

        # 抽检脚本：cd workspace 后 xargs -0 sha256sum（NUL 分隔，天然防引号问题）
        script = rm.call_args.args[1]
        self.assertIn(f"cd '{wt}' || exit 1", script)
        self.assertIn("xargs -0 sha256sum --", script)

        # 二进制流不改行尾：远端落盘字节与本地逐字节一致（LF 与 CRLF 原样保留）
        remote_wt = self.tmp_root / "ws"
        self.assertEqual((remote_wt / "a.sh").read_bytes(), self.lf_script)
        self.assertEqual((remote_wt / "crlf.txt").read_bytes(), self.crlf_text)
        self.assertEqual((remote_wt / "docs" / "sub" / "nested.py").read_bytes(), self.nested)

    def test_hash_mismatch_returns_failed(self):
        fake = FakeRemote()
        fake.tamper = "a.sh"
        result, _pm, _rm = self._run(fake)
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.sha256_ok)
        self.assertEqual(result.files, 3)
        self.assertEqual(result.bytes, self.expected_bytes)

    def test_remote_missing_file_returns_failed(self):
        fake = FakeRemote()
        fake.remove = "crlf.txt"
        result, _pm, _rm = self._run(fake)
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.sha256_ok)

    def test_only_empty_dirs_no_regular_files(self):
        (self.local_root / "emptydir").mkdir()
        fake = FakeRemote()
        result, pm, rm = self._run(fake, paths=[Path("emptydir")])
        self.assertEqual(result.status, "ready")
        self.assertTrue(result.sha256_ok)
        self.assertEqual(result.files, 0)
        self.assertEqual(result.bytes, 0)
        self.assertTrue((Path(self.tmp_root / "ws" / "emptydir")).is_dir())
        rm.assert_not_called()  # 无常规文件时不发空的假 sha256sum 请求

    def test_transfer_failure_raises_ssherror(self):
        with mock.patch(
            "remote_plugin.ssh.ssh_pipe", side_effect=ssh.SSHError("pipe boom")
        ), mock.patch("remote_plugin.config.state_dir", return_value=self.state):
            with self.assertRaises(ssh.SSHError):
                sync_paths(self.machine, [Path("a.sh")], self.local_root)

    def test_result_is_synresult(self):
        fake = FakeRemote()
        result, _pm, _rm = self._run(fake)
        self.assertIsInstance(result, SyncResult)


if __name__ == "__main__":
    unittest.main()
