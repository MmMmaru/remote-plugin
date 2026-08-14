"""bootstrap 模块单元测试（fake ssh 打桩，纯本地无网络）。

覆盖：local_pubkey、push_pubkey（BatchMode / 密码引导 / 失败）、askpass 临时脚本、
_password_ssh（env 与用完即删）、ensure_container（全新建/复用/漂移/无 docker/镜像
已在本地/pull 失败/nvidia 设备）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remote_plugin import bootstrap
from remote_plugin.config import ContainerCfg, Endpoint, RemotePluginError
from remote_plugin.ssh import SSHError
from tests.fake_ssh import IMAGE, PUBKEY, FakeSSH, cp, docker_inspect_healthy


def make_vm():
    return Endpoint(host="192.168.9.166", port=22, user="root", workspace_root="/vm-root")


def make_container():
    return ContainerCfg(image=IMAGE, name="xrs_vllm_main", ssh_port=46000, workspace_root="/home/x/ws")


class TestLocalPubkey(unittest.TestCase):
    def test_reads_first_existing_key(self):
        with tempfile.TemporaryDirectory() as td:
            ssh_dir = Path(td) / ".ssh"
            ssh_dir.mkdir()
            (ssh_dir / "id_rsa.pub").write_text("ssh-rsa AAAAB rsa-key\n", encoding="utf-8")
            with mock.patch.object(bootstrap.Path, "home", return_value=Path(td)):
                self.assertEqual(bootstrap.local_pubkey(), "ssh-rsa AAAAB rsa-key")

    def test_prefers_ed25519(self):
        with tempfile.TemporaryDirectory() as td:
            ssh_dir = Path(td) / ".ssh"
            ssh_dir.mkdir()
            (ssh_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAA ed\n", encoding="utf-8")
            (ssh_dir / "id_rsa.pub").write_text("ssh-rsa AAA rsa\n", encoding="utf-8")
            with mock.patch.object(bootstrap.Path, "home", return_value=Path(td)):
                self.assertEqual(bootstrap.local_pubkey(), "ssh-ed25519 AAA ed")

    def test_missing_key_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(bootstrap.Path, "home", return_value=Path(td)):
                with self.assertRaises(RemotePluginError):
                    bootstrap.local_pubkey()


class TestPushPubkey(unittest.TestCase):
    def test_batch_mode_writes_key_via_stdin(self):
        fake = FakeSSH().on("authorized_keys", cp())
        with mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            bootstrap.push_pubkey(make_vm(), PUBKEY, None)
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertIn("authorized_keys", call["script"])
        self.assertEqual(call["input_bytes"], (PUBKEY + "\n").encode("utf-8"))
        self.assertIn("chmod 600", call["script"])

    def test_batch_mode_failure_raises_ssherror(self):
        fake = FakeSSH().on("authorized_keys", cp(returncode=255, stderr=b"Permission denied"))
        with mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            with self.assertRaises(SSHError):
                bootstrap.push_pubkey(make_vm(), PUBKEY, None)

    def test_password_path_uses_askpass_without_leaking(self):
        calls = []

        def fake_password_ssh(endpoint, remote_cmd, password, input_bytes=None, timeout_sec=120):
            calls.append({"endpoint": endpoint, "cmd": remote_cmd, "password": password, "input": input_bytes})
            return cp()

        with mock.patch("remote_plugin.bootstrap._password_ssh", side_effect=fake_password_ssh):
            bootstrap.push_pubkey(make_vm(), PUBKEY, "S3cret!")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["endpoint"].host, "192.168.9.166")
        self.assertEqual(calls[0]["password"], "S3cret!")
        # 密码绝不进入远端命令/脚本
        self.assertNotIn("S3cret!", calls[0]["cmd"])
        self.assertEqual(calls[0]["input"], (PUBKEY + "\n").encode("utf-8"))


class TestAskpassScript(unittest.TestCase):
    def test_script_echoes_password_mode_700_and_cleanup(self):
        pw = "p@ss'\"word\n第二行"
        path = bootstrap._make_askpass_script(pw)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        self.assertTrue(os.path.exists(path))
        if os.name != "nt":  # Windows 的 os.chmod 不设置 unix 权限位
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)
        out = subprocess.run([sys.executable, path], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout, pw)


class TestPasswordSSH(unittest.TestCase):
    def test_env_force_askpass_and_script_deleted_after(self):
        captured = {}

        def fake_run_ssh(cmd, env, input_bytes, timeout_sec):
            captured["cmd"] = cmd
            captured["env"] = dict(env)
            captured["input"] = input_bytes
            captured["askpass_path"] = env["SSH_ASKPASS"]
            captured["askpass_existed"] = os.path.exists(env["SSH_ASKPASS"])
            return cp()

        with mock.patch("remote_plugin.bootstrap._run_ssh", side_effect=fake_run_ssh), \
             mock.patch("remote_plugin.bootstrap.shutil.which", return_value="/usr/bin/setsid"):
            r = bootstrap._password_ssh(make_vm(), "echo hi", "pw", input_bytes=b"x", timeout_sec=9)
        self.assertEqual(r.returncode, 0)
        env = captured["env"]
        self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "force")
        self.assertTrue(captured["askpass_existed"])
        self.assertIn(captured["askpass_path"], env["SSH_ASKPASS"])
        self.assertIn("BatchMode=no", captured["cmd"])
        self.assertIn("root@192.168.9.166", captured["cmd"])
        self.assertIn("-p", captured["cmd"])
        self.assertEqual(captured["input"], b"x")
        self.assertEqual(captured["cmd"][0], "setsid")  # setsid -w 脱离控制终端
        # 用完即删
        self.assertFalse(os.path.exists(captured["askpass_path"]))

    def test_timeout_raises_and_cleans_script(self):
        holder = {}

        def fake_run_ssh(cmd, env, input_bytes, timeout_sec):
            holder["path"] = env["SSH_ASKPASS"]
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout_sec)

        with mock.patch("remote_plugin.bootstrap._run_ssh", side_effect=fake_run_ssh):
            with self.assertRaises(SSHError):
                bootstrap._password_ssh(make_vm(), "echo hi", "pw")
        self.assertFalse(os.path.exists(holder["path"]))


class TestEnsureContainer(unittest.TestCase):
    def _index(self, calls, needle, exact=False):
        for i, c in enumerate(calls):
            s = c["script"].strip()
            if (s == needle) if exact else (needle in c["script"]):
                return i
        return -1

    def _call(self, calls, needle, exact=False):
        i = self._index(calls, needle, exact=exact)
        self.assertGreaterEqual(i, 0, f"未找到脚本含 {needle!r} 的调用")
        return calls[i]

    def test_full_create_flow_order(self):
        events = []
        fake = (
            FakeSSH()
            .on("docker version --format", cp())
            .on("docker image inspect", cp(returncode=1))
            .on("docker pull", cp())
            .on("for d in /dev/davinci*", cp(stdout=b"/dev/davinci0\n/dev/davinci_manager\n"))
            .on("docker inspect ", cp())  # 容器不存在（stdout 空）
            .on("docker run -d", cp())
            .on("docker exec ", cp())  # docker exec 校验
        )
        with mock.patch("remote_plugin.output.progress", side_effect=events.append), \
             mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            bootstrap.ensure_container(make_vm(), make_container(), {"chip": "ascend-a2"})
        calls = fake.calls
        # 顺序：docker 可用 → 拉镜像 → 创建 → docker exec 校验
        i_docker = self._index(calls, "docker version --format")
        i_pull = self._index(calls, "docker pull")
        i_run = self._index(calls, "docker run -d")
        i_exec = self._index(calls, "docker exec ")
        self.assertTrue(i_docker < i_pull < i_run < i_exec)
        # 创建参数：无端口映射、无 sshd；含 ascend 设备 + 镜像 + 保活命令
        run_script = self._call(calls, "docker run -d")["script"]
        self.assertNotIn("-p", run_script)
        self.assertNotIn("sshd", run_script)
        self.assertIn("--device /dev/davinci0", run_script)
        self.assertIn("--device /dev/davinci_manager", run_script)
        self.assertIn(IMAGE, run_script)
        self.assertIn("tail -f /dev/null", run_script)
        # docker exec 校验打到容器名
        exec_script = self._call(calls, "docker exec ")["script"]
        self.assertIn("xrs_vllm_main", exec_script)
        statuses = [e.get("status") for e in events]
        self.assertIn("docker_ok", statuses)
        self.assertIn("pulling", statuses)
        self.assertIn("created", statuses)
        self.assertIn("exec_ok", statuses)
        self.assertNotIn("already_ready", statuses)

    def test_healthy_reuse_reports_already_ready_without_recreate(self):
        events = []
        fake = (
            FakeSSH()
            .on("docker version --format", cp())
            .on("docker image inspect", cp())
            .on("for d in /dev/davinci*", cp(stdout=b"/dev/davinci0\n/dev/davinci_manager\n"))
            .on("docker inspect ", cp(stdout=docker_inspect_healthy()))
            .on("docker exec ", cp())
        )
        with mock.patch("remote_plugin.output.progress", side_effect=events.append), \
             mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            bootstrap.ensure_container(make_vm(), make_container(), {"chip": "ascend-a2"})
        statuses = [e.get("status") for e in events]
        self.assertIn("already_ready", statuses)
        self.assertFalse(any("docker run" in c["script"] for c in fake.calls))
        self.assertFalse(any("docker pull" in c["script"] for c in fake.calls))
        # 复用仍校验 docker exec
        self.assertTrue(any("docker exec" in c["script"] for c in fake.calls))

    def test_image_drift_warns_but_reuses(self):
        # 镜像版本漂移：仅告警 + 复用（运行中且可 exec），不抛 needs_repair
        drifted = json.loads(docker_inspect_healthy().decode("utf-8"))
        drifted[0]["Config"]["Image"] = "other/image:tag"
        events = []
        fake = (
            FakeSSH()
            .on("docker version --format", cp())
            .on("docker image inspect", cp())
            .on("for d in /dev/davinci*", cp(stdout=b"/dev/davinci0\n"))
            .on("docker inspect ", cp(stdout=json.dumps(drifted).encode("utf-8")))
            .on("docker exec ", cp())
        )
        with mock.patch("remote_plugin.output.progress", side_effect=events.append), \
             mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            bootstrap.ensure_container(make_vm(), make_container(), {"chip": "ascend-a2"})
        # 未重建，且镜像漂移有告警事件
        scripts = [c["script"] for c in fake.calls]
        self.assertFalse(any("docker run" in s for s in scripts))
        self.assertTrue(any(e.get("status") == "image_drift" for e in events))
        self.assertTrue(any("docker exec" in s for s in scripts))

    def test_device_drift_warns_but_reuses(self):
        drifted = json.loads(docker_inspect_healthy().decode("utf-8"))
        drifted[0]["HostConfig"]["Devices"] = []
        events = []
        fake = (
            FakeSSH()
            .on("docker version --format", cp())
            .on("docker image inspect", cp())
            .on("for d in /dev/davinci*", cp(stdout=b"/dev/davinci0\n"))
            .on("docker inspect ", cp(stdout=json.dumps(drifted).encode("utf-8")))
            .on("docker exec ", cp())
        )
        with mock.patch("remote_plugin.output.progress", side_effect=events.append), \
             mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            bootstrap.ensure_container(make_vm(), make_container(), {"chip": "ascend-a2"})
        self.assertTrue(any(e.get("status") == "device_drift" for e in events))
        self.assertTrue(any("docker exec" in c["script"] for c in fake.calls))

    def test_not_running_is_drift(self):
        drifted = json.loads(docker_inspect_healthy().decode("utf-8"))
        drifted[0]["State"]["Running"] = False
        fake = (
            FakeSSH()
            .on("docker version --format", cp())
            .on("docker image inspect", cp())
            .on("for d in /dev/davinci*", cp(stdout=b"/dev/davinci0\n"))
            .on("docker inspect ", cp(stdout=json.dumps(drifted).encode("utf-8")))
        )
        with mock.patch("remote_plugin.output.progress"), \
             mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            with self.assertRaises(bootstrap.NeedsRepairError) as ctx:
                bootstrap.ensure_container(make_vm(), make_container(), {"chip": "ascend-a2"})
        self.assertIn("容器未运行", str(ctx.exception))

    def test_docker_unavailable_raises(self):
        fake = FakeSSH().on("docker version --format", cp(returncode=1, stderr=b"cannot connect"))
        with mock.patch("remote_plugin.output.progress"), \
             mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            with self.assertRaises(RemotePluginError) as ctx:
                bootstrap.ensure_container(make_vm(), make_container(), {"chip": "ascend-a2"})
        self.assertIn("docker 不可用", str(ctx.exception))
        self.assertEqual(len(fake.calls), 1)

    def test_image_present_skips_pull(self):
        fake = (
            FakeSSH()
            .on("docker version --format", cp())
            .on("docker image inspect", cp())
            .on("for d in /dev/davinci*", cp(stdout=b"/dev/davinci0\n"))
            .on("docker inspect ", cp(stdout=docker_inspect_healthy()))
            .on("docker exec ", cp())
        )
        with mock.patch("remote_plugin.output.progress"), \
             mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            bootstrap.ensure_container(make_vm(), make_container(), {"chip": "ascend-a2"})
        self.assertFalse(any("docker pull" in c["script"] for c in fake.calls))

    def test_pull_failure_raises(self):
        fake = (
            FakeSSH()
            .on("docker version --format", cp())
            .on("docker image inspect", cp(returncode=1))
            .on("docker pull", cp(returncode=1, stderr=b"manifest unknown"))
        )
        with mock.patch("remote_plugin.output.progress"), \
             mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            with self.assertRaises(RemotePluginError) as ctx:
                bootstrap.ensure_container(make_vm(), make_container(), {"chip": "ascend-a2"})
        msg = str(ctx.exception)
        self.assertIn("docker pull 失败", msg)
        self.assertNotIn("网络因素", msg)  # 非网络错误不加网络提示

    def test_pull_network_error_hints_mirror_or_proxy_and_asks_user(self):
        # 网络因素 + 宿主机无 proxy env → 提示换源/proxy，并提示向用户询问
        fake = (
            FakeSSH()
            .on("docker version --format", cp())
            .on("docker image inspect", cp(returncode=1))
            .on("docker pull", cp(returncode=1, stderr=b"dial tcp: i/o timeout"))
            .on("_proxy=", cp())  # env | grep 无输出 → 无 proxy
        )
        with mock.patch("remote_plugin.output.progress"), \
             mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            with self.assertRaises(RemotePluginError) as ctx:
                bootstrap.ensure_container(make_vm(), make_container(), {"chip": "ascend-a2"})
        msg = str(ctx.exception)
        self.assertIn("网络因素", msg)
        self.assertIn("镜像源", msg)
        self.assertIn("proxy", msg)
        self.assertIn("向用户询问", msg)
        self.assertNotIn("\n", msg)  # 单行错误契约

    def test_pull_network_error_with_proxy_skips_user_hint(self):
        # 网络因素 + 宿主机已有 proxy env → 只提示换源/proxy，不追加"问用户"
        fake = (
            FakeSSH()
            .on("docker version --format", cp())
            .on("docker image inspect", cp(returncode=1))
            .on("docker pull", cp(returncode=1, stderr=b"TLS handshake timeout"))
            .on("_proxy=", cp(stdout=b"https_proxy=http://proxy:8080\n"))
        )
        with mock.patch("remote_plugin.output.progress"), \
             mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            with self.assertRaises(RemotePluginError) as ctx:
                bootstrap.ensure_container(make_vm(), make_container(), {"chip": "ascend-a2"})
        msg = str(ctx.exception)
        self.assertIn("网络因素", msg)
        self.assertNotIn("向用户询问", msg)

    def test_is_network_pull_error_patterns(self):
        for net in ("dial tcp: i/o timeout", "connection refused",
                    "Temporary failure in name resolution", "x509: certificate",
                    "TLS handshake timeout", "proxyconnect tcp: EOF"):
            self.assertTrue(bootstrap._is_network_pull_error(net), net)
        for other in ("manifest unknown", "unauthorized: authentication required",
                      "no space left on device"):
            self.assertFalse(bootstrap._is_network_pull_error(other), other)

    def test_nvidia_chip_uses_gpus_all(self):
        fake = (
            FakeSSH()
            .on("docker version --format", cp())
            .on("docker image inspect", cp(returncode=1))
            .on("docker pull", cp())
            .on("docker inspect ", cp())
            .on("docker run -d", cp())
            .on("docker exec ", cp())
        )
        with mock.patch("remote_plugin.output.progress"), \
             mock.patch("remote_plugin.ssh.ssh_run", side_effect=fake.ssh_run):
            bootstrap.ensure_container(make_vm(), make_container(), {"chip": "nvidia-h100"})
        run_script = self._call(fake.calls, "docker run -d")["script"]
        self.assertIn("--gpus all", run_script)
        self.assertNotIn("--device", run_script)
        # 非 ascend 不做设备枚举（无对应 ssh 调用）
        self.assertFalse(any("/dev/davinci" in c["script"] for c in fake.calls))


if __name__ == "__main__":
    unittest.main()
