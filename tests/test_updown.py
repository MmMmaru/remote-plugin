"""updown 模块单元测试（fake ssh 打桩）：编排顺序、幂等、漂移、密码路径、CLI handler。

重点验证 spec T2 [本地] 步骤 1 的三条：
- 编排顺序 = 免密 → docker → docker exec 校验 → 工作区；
- 已免密时跳过密码路径（无 askpass、无公钥写入）；
- 漂移返回 needs_repair（NeedsRepairError，不自动重建）。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remote_plugin import cli, updown
from remote_plugin.bootstrap import NeedsRepairError
from remote_plugin.config import ConfigError, ContainerCfg, Endpoint, Machine, RemotePluginError
from tests.fake_ssh import IMAGE, PUBKEY, WS, FakeSSH, cp, docker_inspect_healthy


def make_machine(**kw):
    base = dict(
        alias="a2",
        mode="container",
        host="192.168.9.166",
        port=22,
        user="root",
        container=ContainerCfg(image=IMAGE, name="xrs_vllm_main", ssh_port=46000, workspace_root=WS),
        workspace_root="/vm-root",
        tags={"chip": "ascend-a2", "cards": 8, "os": "linux"},
    )
    base.update(kw)
    return Machine(**base)


class UpTestBase(unittest.TestCase):
    """公共夹具：state_dir 指向临时目录、收集 progress 事件、打桩本地公钥。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "state"
        self.state.mkdir(parents=True)
        self.events: list[dict] = []
        for target, kwargs in (
            ("remote_plugin.config.state_dir", {"return_value": self.state}),
            ("remote_plugin.output.progress", {"side_effect": self.events.append}),
            ("remote_plugin.bootstrap.local_pubkey", {"return_value": PUBKEY}),
            ("remote_plugin.updown.local_pubkey", {"return_value": PUBKEY}),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- 断言辅助 ----------------------------------------------------------
    def _index(self, calls, pred):
        for i, c in enumerate(calls):
            if pred(c):
                return i
        return -1

    def _event_steps(self):
        return [e.get("step") for e in self.events]

    def _event_statuses(self):
        return [e.get("status") for e in self.events]


class TestMachineUp(UpTestBase):
    def test_fresh_up_order_passwordless_docker_sshd_workspace(self):
        fake = FakeSSH()
        fake.on("true", [cp(returncode=255), cp()], exact=True)  # 探测失败 → 引导 → 验证成功
        fake.on("docker version --format", cp())
        fake.on("docker image inspect", cp(returncode=1))
        fake.on("docker pull", cp())
        fake.on("for d in /dev/davinci*", cp(stdout=b"/dev/davinci0\n/dev/davinci_manager\n"))
        fake.on("docker inspect ", cp())  # 容器不存在
        fake.on("docker run -d", cp())
        fake.on("docker exec ", cp())
        fake.on("mkdir -p", cp())
        password_calls = []

        def fake_password_ssh(endpoint, remote_cmd, password, input_bytes=None, timeout_sec=120):
            password_calls.append({"endpoint": endpoint, "cmd": remote_cmd, "password": password, "input": input_bytes})
            return cp()

        with mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run), \
             mock.patch("remote_plugin.bootstrap._password_ssh", side_effect=fake_password_ssh):
            ep = updown.machine_up(make_machine(), "S3cret!")

        self.assertEqual((ep.host, ep.port, ep.user, ep.container), ("192.168.9.166", 22, "root", "xrs_vllm_main"))
        self.assertEqual(ep.workspace_root, WS)
        calls = fake.calls

        # ① 免密：探测失败 → SSH_ASKPASS 引导（1 次）→ 探测成功
        self.assertEqual(calls[0]["script"].strip(), "true")
        self.assertEqual(calls[0]["endpoint"].port, 22)
        self.assertEqual(len(password_calls), 1)
        self.assertEqual(password_calls[0]["password"], "S3cret!")
        self.assertNotIn("S3cret!", password_calls[0]["cmd"])  # 密码不进远端命令
        self.assertEqual(password_calls[0]["input"], (PUBKEY + "\n").encode("utf-8"))
        self.assertEqual(calls[1]["script"].strip(), "true")  # 引导后验证
        self.assertEqual(calls[1]["endpoint"].port, 22)

        # ② docker 阶段紧跟其后
        self.assertIn("docker version --format", calls[2]["script"])

        # 顺序：免密 → docker → docker exec 校验 → 工作区（按脚本索引）
        i_docker = self._index(calls, lambda c: "docker version --format" in c["script"])
        i_pull = self._index(calls, lambda c: "docker pull" in c["script"])
        i_run = self._index(calls, lambda c: "docker run -d" in c["script"])
        i_exec = self._index(calls, lambda c: c["script"].strip().startswith("docker exec "))
        i_ws = self._index(calls, lambda c: ".remote-mirrors" in c["script"])
        self.assertTrue(0 < 1 < i_docker < i_pull < i_run < i_exec < i_ws)

        # docker exec 校验打宿主机（port 22），工作区初始化经容器端点（container 名）
        exec_call = calls[i_exec]
        self.assertEqual(exec_call["endpoint"].port, 22)
        self.assertIn("docker exec xrs_vllm_main", exec_call["script"])
        ws_call = calls[i_ws]
        self.assertEqual(ws_call["endpoint"].container, "xrs_vllm_main")
        self.assertIn(f"{WS}/main", ws_call["script"])
        self.assertIn(".remote-mirrors", ws_call["script"])
        self.assertIn("core.autocrlf false", ws_call["script"])
        self.assertIn("core.eol lf", ws_call["script"])

        # 事件级顺序：ssh → container → exec 校验 → endpoint → workspace → done
        steps = self._event_steps()
        self.assertLess(steps.index("ssh"), steps.index("container"))
        self.assertLess(steps.index("container"), steps.index("endpoint"))
        self.assertLess(steps.index("endpoint"), steps.index("workspace"))
        self.assertEqual(steps[-1], "up")  # 最后是 up done 事件
        self.assertNotIn("already_passwordless", self._event_statuses())  # 首次 up 走引导路径

        # endpoint 状态文件：记录宿主机 + 容器名（不再有 ssh_port）
        ep_file = self.state / "endpoints" / "a2.json"
        self.assertTrue(ep_file.is_file())
        data = json.loads(ep_file.read_text(encoding="utf-8"))
        self.assertEqual(data["port"], 22)
        self.assertEqual(data["container"], "xrs_vllm_main")
        self.assertEqual(data["workspace_root"], WS)

        # 密码不落盘 state/、不进 progress
        for p in self.state.rglob("*"):
            if p.is_file():
                self.assertNotIn("S3cret!", p.read_text(encoding="utf-8", errors="replace"))
        self.assertFalse(any("S3cret!" in json.dumps(e, ensure_ascii=False) for e in self.events))

    def test_already_passwordless_skips_password_path(self):
        fake = FakeSSH()
        fake.on("true", cp(), exact=True)
        fake.on("docker version --format", cp())
        fake.on("docker image inspect", cp())
        fake.on("for d in /dev/davinci*", cp(stdout=b"/dev/davinci0\n"))
        fake.on("docker inspect ", cp(stdout=docker_inspect_healthy()))
        fake.on("docker exec ", cp())
        fake.on("mkdir -p", cp())
        pushed, password_calls = [], []
        with mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run), \
             mock.patch("remote_plugin.updown.push_pubkey",
                        side_effect=lambda ep, key, pw: pushed.append((ep, key, pw))), \
             mock.patch("remote_plugin.bootstrap._password_ssh",
                        side_effect=lambda *a, **k: password_calls.append(a) or cp()):
            ep = updown.machine_up(make_machine(), None)
        self.assertEqual(pushed, [])
        self.assertEqual(password_calls, [])
        self.assertEqual(ep.port, 22)
        self.assertEqual(ep.container, "xrs_vllm_main")
        # 首次调用即免密探测成功，无 VM 公钥写入步骤
        self.assertEqual(fake.calls[0]["script"].strip(), "true")
        self.assertIn("already_passwordless", self._event_statuses())
        self.assertIn("already_ready", self._event_statuses())

    def test_drift_raises_needs_repair_no_rebuild(self):
        drifted = json.loads(docker_inspect_healthy().decode("utf-8"))
        drifted[0]["State"]["Running"] = False  # 容器未运行（硬失败）
        fake = FakeSSH()
        fake.on("true", cp(), exact=True)
        fake.on("docker version --format", cp())
        fake.on("docker image inspect", cp())
        fake.on("for d in /dev/davinci*", cp(stdout=b"/dev/davinci0\n"))
        fake.on("docker inspect ", cp(stdout=json.dumps(drifted).encode("utf-8")))
        with mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            with self.assertRaises(NeedsRepairError) as ctx:
                updown.machine_up(make_machine(), None)
        self.assertIn("未运行", str(ctx.exception))
        scripts = [c["script"] for c in fake.calls]
        self.assertFalse(any("docker run" in s for s in scripts))
        self.assertFalse(any("docker exec" in s for s in scripts))
        self.assertFalse(any("mkdir -p" in s for s in scripts))
        self.assertFalse((self.state / "endpoints" / "a2.json").exists())

    def test_healthy_reuse_is_idempotent(self):
        fake = FakeSSH()
        fake.on("true", cp(), exact=True)
        fake.on("docker version --format", cp())
        fake.on("docker image inspect", cp())
        fake.on("for d in /dev/davinci*", cp(stdout=b"/dev/davinci0\n/dev/davinci_manager\n"))
        fake.on("docker inspect ", cp(stdout=docker_inspect_healthy()))
        fake.on("docker exec ", cp())
        fake.on("mkdir -p", cp())
        with mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            ep = updown.machine_up(make_machine(), None)
        self.assertEqual((ep.port, ep.container), (22, "xrs_vllm_main"))
        scripts = [c["script"] for c in fake.calls]
        self.assertFalse(any("docker run" in s for s in scripts))
        self.assertFalse(any("docker pull" in s for s in scripts))
        self.assertIn("already_ready", self._event_statuses())
        self.assertTrue((self.state / "endpoints" / "a2.json").is_file())
        self.assertTrue(any("mkdir -p" in s for s in scripts))  # 工作区仍幂等初始化

    def test_mode_ssh_only_passwordless_check_and_workspace(self):
        m = make_machine(mode="ssh", container=None, workspace_root="/direct-ws")
        fake = FakeSSH()
        fake.on("true", cp(), exact=True)
        fake.on("mkdir -p", cp())
        with mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            ep = updown.machine_up(m, None)
        self.assertEqual((ep.port, ep.workspace_root), (22, "/direct-ws"))
        self.assertEqual(len(fake.calls), 2)  # 只有免密校验 + 工作区
        self.assertFalse(any("docker" in c["script"] for c in fake.calls))
        self.assertIn("/direct-ws/main", fake.calls[1]["script"])
        self.assertFalse((self.state / "endpoints" / "a2.json").exists())

    def test_mode_ssh_not_passwordless_with_password_bootstraps(self):
        m = make_machine(mode="ssh", container=None, workspace_root="/direct-ws")
        fake = FakeSSH()
        fake.on("true", [cp(returncode=255), cp()], exact=True)
        fake.on("mkdir -p", cp())
        password_calls = []
        with mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run), \
             mock.patch("remote_plugin.bootstrap._password_ssh",
                        side_effect=lambda *a, **k: password_calls.append(a) or cp()):
            updown.machine_up(m, "pw123")
        self.assertEqual(len(password_calls), 1)
        self.assertEqual(fake.calls[0]["script"].strip(), "true")
        self.assertIn("mkdir -p", fake.calls[2]["script"])

    def test_no_password_and_not_passwordless_raises(self):
        fake = FakeSSH()
        fake.on("true", cp(returncode=255), exact=True)
        with mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            with self.assertRaises(RemotePluginError) as ctx:
                updown.machine_up(make_machine(), None)
        self.assertIn("无法免密连接", str(ctx.exception))


class TestMachineDown(UpTestBase):
    def test_removes_managed_container_and_endpoint_state(self):
        ep_file = self.state / "endpoints" / "a2.json"
        ep_file.parent.mkdir(parents=True)
        ep_file.write_text("{}", encoding="utf-8")
        fake = FakeSSH()
        fake.on("docker version --format", cp())
        fake.on("docker inspect ", cp())
        fake.on("docker rm -f", cp())
        with mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            updown.machine_down(make_machine())
        rm_calls = [c for c in fake.calls if "docker rm -f" in c["script"]]
        self.assertEqual(len(rm_calls), 1)
        self.assertIn("xrs_vllm_main", rm_calls[0]["script"])
        self.assertEqual(len([c for c in fake.calls if "docker rm" in c["script"]]), 1)  # 只动受管容器
        self.assertFalse(ep_file.exists())
        statuses = self._event_statuses()
        self.assertIn("removed", statuses)
        self.assertIn("endpoint_removed", statuses)

    def test_container_missing_skips_rm_but_cleans_endpoint(self):
        ep_file = self.state / "endpoints" / "a2.json"
        ep_file.parent.mkdir(parents=True)
        ep_file.write_text("{}", encoding="utf-8")
        fake = FakeSSH()
        fake.on("docker version --format", cp())
        fake.on("docker inspect ", cp(returncode=1))
        with mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            updown.machine_down(make_machine())
        self.assertFalse(any("docker rm" in c["script"] for c in fake.calls))
        self.assertFalse(ep_file.exists())
        self.assertIn("not_found", self._event_statuses())

    def test_mode_ssh_noop_without_ssh(self):
        with mock.patch("remote_plugin.ssh.ssh_run", side_effect=AssertionError("不应有 ssh 调用")):
            updown.machine_down(make_machine(mode="ssh", container=None))
        self.assertIn("noop", self._event_statuses())


class TestCliUpDown(unittest.TestCase):
    def setUp(self):
        self.machine = make_machine()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "state"
        self.state.mkdir(parents=True)
        patcher = mock.patch("remote_plugin.config.state_dir", return_value=self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _args(self, **kw):
        ns = argparse.Namespace(alias="a2", password_env=None, password_stdin=False)
        for k, v in kw.items():
            setattr(ns, k, v)
        return ns

    def _machine_up_spy(self, seen, ep=None):
        return mock.patch(
            "remote_plugin.updown.machine_up",
            side_effect=lambda machine, password: (seen.append(password), ep or Endpoint("h", 46000, "root", WS))[1],
        )

    def test_cli_up_ok(self):
        ep = Endpoint(host="192.168.9.166", port=46000, user="root", workspace_root=WS)
        with mock.patch("remote_plugin.config.load_machines", return_value={"a2": self.machine}), \
             mock.patch("remote_plugin.updown.machine_up", return_value=ep):
            result = updown.cli_up(self._args())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["endpoint"], {"host": "192.168.9.166", "port": 46000, "user": "root", "workspace_root": WS})

    def test_cli_up_needs_repair(self):
        with mock.patch("remote_plugin.config.load_machines", return_value={"a2": self.machine}), \
             mock.patch("remote_plugin.updown.machine_up",
                        side_effect=NeedsRepairError("容器 xrs_vllm_main 漂移（镜像漂移），不自动重建")):
            result = updown.cli_up(self._args())
        self.assertEqual(result["status"], "needs_repair")
        self.assertIn("漂移", result["reason"])

    def test_cli_up_unknown_alias_raises(self):
        with mock.patch("remote_plugin.config.load_machines", return_value={}):
            with self.assertRaises(ConfigError):
                updown.cli_up(self._args())

    def test_password_priority_config_over_env(self):
        m = make_machine(password="from-config")
        seen = []
        with mock.patch("remote_plugin.config.load_machines", return_value={"a2": m}), \
             self._machine_up_spy(seen):
            updown.cli_up(self._args(password_env="PW"))
        self.assertEqual(seen, ["from-config"])

    def test_password_from_env(self):
        seen = []
        with mock.patch.dict(os.environ, {"PW": "from-env"}), \
             mock.patch("remote_plugin.config.load_machines", return_value={"a2": self.machine}), \
             self._machine_up_spy(seen):
            updown.cli_up(self._args(password_env="PW"))
        self.assertEqual(seen, ["from-env"])

    def test_password_env_missing_raises(self):
        with mock.patch("remote_plugin.config.load_machines", return_value={"a2": self.machine}):
            with self.assertRaises(ConfigError):
                updown.cli_up(self._args(password_env="NO_SUCH_VAR"))

    def test_password_from_stdin(self):
        seen = []
        with mock.patch("remote_plugin.config.load_machines", return_value={"a2": self.machine}), \
             mock.patch("sys.stdin", io.StringIO("from-stdin\n")), \
             self._machine_up_spy(seen):
            updown.cli_up(self._args(password_stdin=True))
        self.assertEqual(seen, ["from-stdin"])

    def test_cli_down_ok(self):
        with mock.patch("remote_plugin.config.load_machines", return_value={"a2": self.machine}), \
             mock.patch("remote_plugin.updown.machine_down", return_value=None):
            result = updown.cli_down(self._args())
        self.assertEqual(result, {"status": "ok", "machine": "a2", "mode": "container"})

    def test_cli_main_up_full_path_emits_json(self):
        """经 cli.main 全链路：handler → output.emit（stdout 单行 JSON）。"""
        ep = Endpoint(host="192.168.9.166", port=46000, user="root", workspace_root=WS)
        out = io.StringIO()
        with mock.patch("remote_plugin.config.load_machines", return_value={"a2": self.machine}), \
             mock.patch("remote_plugin.updown.machine_up", return_value=ep), \
             mock.patch("remote_plugin.output.progress"), \
             contextlib.redirect_stdout(out):
            rc = cli.main(["up", "a2"])
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue().strip())
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["endpoint"]["port"], 46000)


if __name__ == "__main__":
    unittest.main()
