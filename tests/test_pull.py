"""pull 模块单元测试（纯本地，无网络）。

FakeRemote 把「远端脚本」当作本地 bash 执行（workspace_root 为本地真实目录的
MSYS 形式），从而跑通 清单 → tar 流 → 本地解包 → sha256 比对 全链路：
正常拉回 / sha256 不一致 fail closed / 远端缺失 / 路径越界拒绝 / CLI 解析。
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from remote_plugin import cli, config, pull, ssh
from remote_plugin.pull import PullError, pull_paths
from tests.fake_ssh import BASH, local_path, msys_path


class FakeRemote:
    """本地模拟「远端」：bash -s 执行清单脚本，bash -c 执行 tar 脚本（stdout=tar 流）。"""

    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.tamper: str | None = None  # tar 前改写该相对路径 → 模拟 sha256 不一致

    def ssh_run(self, endpoint, script, timeout_sec=300, input_bytes=None):
        self.scripts.append(script)
        if "tar -cf -" in script and self.tamper is not None:
            m = re.search(r"cd '([^']*)'", script)
            (local_path(m.group(1)) / self.tamper).write_bytes(b"TAMPERED-BYTES")
        if input_bytes is None:
            return subprocess.run([BASH, "-s"], input=script.encode(), capture_output=True)
        return subprocess.run([BASH, "-c", script], input=input_bytes, capture_output=True)


class _TempBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # resolve() 展开 Windows 8.3 短名；workspace_root 转 MSYS 供本地 bash 执行
        self.tmp = Path(self._tmp.name).resolve()
        self.ws = self.tmp / "ws"
        (self.ws / "out" / "sub").mkdir(parents=True)
        (self.ws / "out" / "a.json").write_bytes(b'{"acc": 0.91}\n')
        (self.ws / "out" / "sub" / "b.log").write_bytes(b"log-line\n" * 100)
        (self.ws / "abs.bin").write_bytes(b"\x00\x01\x02" * 512)
        self.machine = config.Machine(
            alias="p1", mode="ssh", host="127.0.0.1", port=22, user="u",
            workspace_root=msys_path(self.ws),
        )
        self.dest = self.tmp / "dest"
        patcher = mock.patch(
            "remote_plugin.config.state_dir", return_value=str(self.tmp / "state")
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake = FakeRemote()
        patcher2 = mock.patch(
            "remote_plugin.ssh.ssh_run", side_effect=self.fake.ssh_run
        )
        patcher2.start()
        self.addCleanup(patcher2.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestResolveRemotePaths(unittest.TestCase):
    def _resolve(self, paths):
        return pull._resolve_remote_paths("/vllm-workspace", paths)

    def test_relative_resolves_against_workspace(self):
        base, rels = self._resolve(["out"])
        self.assertEqual(base, "/vllm-workspace")
        self.assertEqual(rels, ["out"])

    def test_multi_paths_single_tar_common_base(self):
        base, rels = self._resolve(["out/a.json", "out/sub"])
        self.assertEqual(base, "/vllm-workspace/out")
        self.assertEqual(rels, ["a.json", "sub"])

    def test_absolute_used_as_is(self):
        base, rels = self._resolve(["/var/log/x.log"])
        self.assertEqual(base, "/var/log")
        self.assertEqual(rels, ["x.log"])

    def test_traversal_rejected(self):
        for bad in ("../x", "a/../../x", ".."):
            with self.assertRaises(PullError) as ctx:
                self._resolve([bad])
            self.assertIn("越界", str(ctx.exception))

    def test_empty_and_backslash_rejected(self):
        with self.assertRaises(PullError):
            self._resolve([""])
        with self.assertRaises(PullError):
            self._resolve(["out\\a.json"])
        with self.assertRaises(PullError):
            self._resolve([])


class TestPullPipeline(_TempBase):
    def test_ready_full_pipeline(self):
        result = pull_paths(self.machine, ["out"], self.dest)
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.files, 2)
        self.assertEqual(result.bytes, 14 + 900)
        self.assertEqual(result.dest, str(self.dest))
        self.assertEqual((self.dest / "out" / "a.json").read_bytes(), b'{"acc": 0.91}\n')
        self.assertEqual((self.dest / "out" / "sub" / "b.log").read_bytes(), b"log-line\n" * 100)
        # 远端先清单后 tar，均经同一 base
        self.assertIn("sha256sum", self.fake.scripts[0])
        self.assertIn("tar -cf -", self.fake.scripts[1])
        self.assertEqual(result.to_dict()["status"], "ready")

    def test_multi_paths_single_tar(self):
        result = pull_paths(self.machine, ["out/a.json", "out/sub"], self.dest)
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.files, 2)
        self.assertTrue((self.dest / "a.json").is_file())
        self.assertTrue((self.dest / "sub" / "b.log").is_file())

    def test_absolute_path(self):
        result = pull_paths(self.machine, [msys_path(self.ws / "abs.bin")], self.dest)
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.files, 1)
        self.assertEqual((self.dest / "abs.bin").read_bytes(), b"\x00\x01\x02" * 512)

    def test_sha256_mismatch_fails_closed(self):
        self.fake.tamper = "out/a.json"
        with self.assertRaises(PullError) as ctx:
            pull_paths(self.machine, ["out"], self.dest)
        self.assertIn("sha256 校验不一致", str(ctx.exception))

    def test_remote_missing_fails(self):
        with self.assertRaises(PullError) as ctx:
            pull_paths(self.machine, ["no-such"], self.dest)
        self.assertIn("远端路径不存在", str(ctx.exception))
        # 缺失时只做清单、不发 tar
        self.assertFalse(any("tar -cf -" in s for s in self.fake.scripts))

    def test_traversal_rejected_before_any_ssh(self):
        with self.assertRaises(PullError):
            pull_paths(self.machine, ["../x"], self.dest)
        self.assertEqual(self.fake.scripts, [])

class TestCliPull(_TempBase):
    def test_parser_shape(self):
        args = cli.build_parser().parse_args(
            ["pull", "p1", "out", "x.log", "--dest", "d"]
        )
        self.assertEqual(args.command, "pull")
        self.assertEqual(args.alias, "p1")
        self.assertEqual(args.remote_paths, ["out", "x.log"])
        self.assertEqual(args.dest, "d")

    def test_registered_in_commands(self):
        self.assertIn("pull", cli.COMMANDS)

    def test_cli_pull_payload(self):
        args = SimpleNamespace(
            alias="p1", remote_paths=["out"], dest=str(self.dest)
        )
        with mock.patch.object(config, "load_machines", return_value={"p1": self.machine}):
            payload = pull.cli_pull(args)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["files"], 2)
        self.assertIn("bytes", payload)
        self.assertIn("dest", payload)

    def test_cli_pull_unknown_alias(self):
        args = SimpleNamespace(
            alias="ghost", remote_paths=["out"], dest=str(self.dest)
        )
        with mock.patch.object(config, "load_machines", return_value={}):
            with self.assertRaises(config.ConfigError):
                pull.cli_pull(args)


if __name__ == "__main__":
    unittest.main()
