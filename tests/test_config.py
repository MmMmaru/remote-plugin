"""config 模块单元测试（纯本地，无网络）。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remote_plugin import config


class _TempDir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestLoadMachines(_TempDir):
    def _write_project(self, payload, name="machines.json") -> Path:
        remote_dir = self.root / ".remote"
        remote_dir.mkdir(parents=True, exist_ok=True)
        path = remote_dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_parses_container_machine(self):
        payload = [
            {
                "alias": "a2",
                "mode": "container",
                "machine_type": "A3",
                "host": "192.168.9.166",
                "port": 22,
                "user": "root",
                "container": {
                    "image": "quay.io/ascend/vllm-ascend:nightly-main",
                    "name": "xrs_vllm_main",
                    "ssh_port": 46000,
                    "workspace_root": "/home/x/vllm-workspace",
                },
                "tags": {"chip": "ascend-a2", "cards": 8, "os": "linux"},
            }
        ]
        self._write_project(payload)
        machines = config.load_machines(self.root)
        self.assertIn("a2", machines)
        m = machines["a2"]
        self.assertEqual(m.machine_type, "A3")
        self.assertEqual(m.tags["chip"], "ascend-a2")
        self.assertEqual(m.tags["cards"], 8)
        self.assertEqual(m.effective_workspace_root(), "/home/x/vllm-workspace")

    def test_missing_host_reports_index_and_field(self):
        self._write_project([{"alias": "x"}])
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_machines(self.root)
        msg = str(ctx.exception)
        self.assertIn("第 0 个元素", msg)
        self.assertIn("host", msg)

    def test_invalid_json_reports_line(self):
        remote_dir = self.root / ".remote"
        remote_dir.mkdir(parents=True, exist_ok=True)
        (remote_dir / "machines.json").write_text('[{"alias": "x"', encoding="utf-8")
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_machines(self.root)
        self.assertIn("JSON 非法", str(ctx.exception))

    def test_nearest_project_wins_with_warning(self):
        # 远层项目级 + 近层项目级同 alias，近层应覆盖
        outer = self.root / ".remote"
        outer.mkdir(parents=True)
        (outer / "machines.json").write_text(
            json.dumps([{"alias": "dup", "mode": "ssh", "host": "1.1.1.1"}]),
            encoding="utf-8",
        )
        inner = self.root / "sub" / ".remote"
        inner.mkdir(parents=True)
        (inner / "machines.json").write_text(
            json.dumps([{"alias": "dup", "mode": "ssh", "host": "2.2.2.2"}]),
            encoding="utf-8",
        )
        machines = config.load_machines(self.root / "sub")
        self.assertEqual(machines["dup"].host, "2.2.2.2")

    def test_mode_ssh_uses_top_level_workspace_root(self):
        self._write_project(
            [{"alias": "b", "mode": "ssh", "host": "h", "workspace_root": "/ws"}]
        )
        machines = config.load_machines(self.root)
        self.assertEqual(machines["b"].workspace_root, "/ws")


class TestResolveEndpoint(_TempDir):
    def test_ssh_mode_direct(self):
        m = config.Machine(alias="b", mode="ssh", host="h", port=2222, user="u", workspace_root="/ws")
        ep = config.resolve_endpoint(m, self.root)
        self.assertEqual((ep.host, ep.port, ep.user, ep.workspace_root), ("h", 2222, "u", "/ws"))

    def test_container_fallback_to_host(self):
        m = config.Machine(
            alias="a",
            mode="container",
            host="vm",
            port=22,
            container=config.ContainerCfg(ssh_port=46000, workspace_root="/ws"),
        )
        ep = config.resolve_endpoint(m, self.root)
        self.assertEqual((ep.host, ep.port), ("vm", 22))

    def test_container_mode_returns_vm_endpoint_with_container(self):
        m = config.Machine(
            alias="a",
            mode="container",
            host="vm",
            port=22,
            container=config.ContainerCfg(name="c1", ssh_port=46000, workspace_root="/ws"),
        )
        ep = config.resolve_endpoint(m, self.root)
        # 模式 A：SSH 到宿主机（port 22）+ docker exec 进容器（container 名）
        self.assertEqual(ep.port, 22)
        self.assertEqual(ep.container, "c1")
        self.assertEqual(ep.workspace_root, "/ws")


class TestStateDir(_TempDir):
    def test_fallback_to_repo_remote_when_no_project_remote(self):
        """向上找不到任何 `.remote` 时，默认落到 remote-plugin 仓库自身 `.remote/state`。"""
        with mock.patch.object(config, "find_remote_dir", return_value=None), \
             mock.patch.object(config, "_PACKAGE_ROOT", self.root):
            sd = config.state_dir(self.root / "sub" / "dir")
        self.assertEqual(sd, self.root / ".remote" / "state")
        self.assertTrue(sd.is_dir())

    def test_project_remote_wins_over_fallback(self):
        """向上找到 `.remote` 时用它，不落到仓库默认位置。"""
        project_remote = self.root / "proj" / ".remote"
        project_remote.mkdir(parents=True)
        with mock.patch.object(config, "find_remote_dir", return_value=project_remote), \
             mock.patch.object(config, "_PACKAGE_ROOT", self.root / "pkg"):
            sd = config.state_dir(self.root / "proj")
        self.assertEqual(sd, project_remote / "state")
        self.assertTrue(sd.is_dir())
        self.assertFalse((self.root / "pkg" / ".remote").exists())


if __name__ == "__main__":
    unittest.main()
