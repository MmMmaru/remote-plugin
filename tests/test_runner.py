"""runner 模块单元测试（纯本地，无网络；fake ssh 打桩 + 本地 bash 跑真实 launcher）。"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from remote_plugin import config, jobs, runner, ssh
from tests.fake_ssh import BASH, msys_path


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = self.root / "state"
        self.state.mkdir(parents=True)
        p = mock.patch.object(config, "state_dir", return_value=self.state)
        p.start()
        self.addCleanup(p.stop)
        self.machine = config.Machine(
            alias="m1", mode="ssh", host="h", port=22, user="u", workspace_root="/ws"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_ssh(self, rc=0, stdout=b"", stderr=b"", error=None):
        def fake(endpoint, script, timeout_sec=300, input_bytes=None):
            if error is not None:
                raise error
            return subprocess.CompletedProcess([], rc, stdout, stderr)
        return fake


class TestPreviewText(unittest.TestCase):
    def test_long_head_tail_truncated(self):
        text = "A" * 4000 + "B" * 1000
        p = runner.preview_text(text, limit=4000)
        self.assertTrue(p["truncated"])
        self.assertEqual(p["head"], "A" * 4000)
        self.assertEqual(p["tail"], "A" * 3000 + "B" * 1000)  # 最后 4000 字符

    def test_short_no_truncation(self):
        p = runner.preview_text("hello", limit=4000)
        self.assertFalse(p["truncated"])
        self.assertEqual(p["head"], "hello")
        self.assertEqual(p["tail"], "")

    def test_exact_limit_no_truncation(self):
        text = "x" * 4000
        p = runner.preview_text(text, limit=4000)
        self.assertFalse(p["truncated"])
        self.assertEqual(p["head"], text)


class TestWorktreeDir(unittest.TestCase):
    def test_main_and_named(self):
        self.assertEqual(runner.worktree_dir("/vllm-workspace", "main"), "/vllm-workspace/main/")
        self.assertEqual(runner.worktree_dir("/vllm-workspace", "t1"), "/vllm-workspace/t1/")

    def test_invalid_worktree_raises(self):
        for bad in ("", ".", "..", "a/b", "a\\b"):
            with self.assertRaises(config.RemotePluginError):
                runner.worktree_dir("/ws", bad)


class TestRunForeground(_Base):
    """spec T3 e2e 步骤2：fake ssh 前台截断预览（>4000 字符 head/tail）。"""

    def test_preview_and_logs(self):
        out = b"__RP_PID__=4242\n" + b"x" * 5000
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=0, stdout=out,
                                                              stderr=b"y" * 123)):
            job = runner.run_remote(self.machine, "main", "echo hi", None, {}, None,
                                    None, 600, False)
        self.assertEqual(job.status, "done")
        self.assertEqual(job.exit_code, 0)
        self.assertEqual(job.remote_pid, 4242)
        self.assertEqual(job.cwd, "/ws/main/")
        self.assertEqual(job.stdout_log, f"state/jobs/{job.job_id}/stdout.log")
        d = self.state / "jobs" / job.job_id
        self.assertEqual((d / "stdout.log").read_bytes(), b"x" * 5000)  # 标记行已剥离
        self.assertEqual((d / "stderr.log").read_bytes(), b"y" * 123)
        self.assertTrue(job.stdout_preview["truncated"])
        self.assertEqual(job.stdout_preview["head"], "x" * 4000)
        self.assertEqual(job.stdout_preview["tail"], "x" * 4000)
        self.assertFalse(job.stderr_preview["truncated"])
        self.assertEqual(job.stderr_preview["head"], "y" * 123)
        meta = json.loads((self.state / "jobs" / job.job_id / "meta.json").read_text())
        self.assertEqual(meta["status"], "done")
        self.assertEqual(meta["exit_code"], 0)

    def test_nonzero_exit_failed(self):
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=3, stdout=b"__RP_PID__=9\nboom\n")):
            job = runner.run_remote(self.machine, "main", "false", None, {}, None, None, 600, False)
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.exit_code, 3)
        d = self.state / "jobs" / job.job_id
        self.assertEqual((d / "stdout.log").read_bytes(), b"boom\n")

    def test_timeout_status_and_cleanup(self):
        err = ssh.SSHError("ssh 执行超时（>600s）")

        def fake(endpoint, script, timeout_sec=300, input_bytes=None):
            if "cmd.sh" in script:
                raise err
            return subprocess.CompletedProcess([], 0, b"", b"")  # cleanup 调用

        with mock.patch.object(ssh, "ssh_run", side_effect=fake) as f:
            job = runner.run_remote(self.machine, "main", "sleep 60", None, {}, None,
                                    None, 600, False)
        self.assertEqual(job.status, "timeout")
        self.assertEqual(job.exit_code, 124)
        self.assertIsNotNone(job.finished_at)
        # launcher 一次 + cleanup 一次
        self.assertEqual(f.call_count, 2)
        self.assertIn("kill -TERM", f.call_args_list[1].args[1])

    def test_ssh_error_failed(self):
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(error=ssh.SSHError("连接失败"))):
            job = runner.run_remote(self.machine, "main", "x", None, {}, None, None, 600, False)
        self.assertEqual(job.status, "failed")
        self.assertIsNone(job.exit_code)

    def test_explicit_cwd_and_env_cards(self):
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=0, stdout=b"__RP_PID__=1\n")):
            job = runner.run_remote(self.machine, "main", "pwd", "/custom/dir",
                                    {"ASCEND_RT_VISIBLE_DEVICES": "0,1"}, None, None, 600, False)
        self.assertEqual(job.cwd, "/custom/dir")
        self.assertEqual(job.cards, [0, 1])


class TestRunBackground(_Base):
    def test_returns_immediately_with_pid(self):
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=0, stdout=b"__RP_PID__=777\n")), \
                mock.patch.object(runner, "_spawn_streamer", return_value=None) as spawn:
            job = runner.run_remote(self.machine, "t1", "sleep 600", None, {}, None,
                                    "占坑", 600, True)
        self.assertEqual(job.status, "running")
        self.assertEqual(job.remote_pid, 777)
        self.assertEqual(job.cwd, "/ws/t1/")
        self.assertEqual(job.remote_log_dir, f"/ws/.remote-logs/{job.job_id}")
        self.assertEqual(spawn.call_count, 2)
        meta = json.loads((self.state / "jobs" / job.job_id / "meta.json").read_text())
        self.assertEqual(meta["status"], "running")
        self.assertEqual(meta["remote_pid"], 777)
        # advisory 占用提示包含自己
        self.assertEqual([r["job_id"] for r in job.running], [job.job_id])

    def test_launch_failure_marks_failed(self):
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=1, stdout=b"")), \
                mock.patch.object(runner, "_spawn_streamer", return_value=None):
            job = runner.run_remote(self.machine, "main", "x", None, {}, None, None, 600, True)
        self.assertEqual(job.status, "failed")
        self.assertIsNotNone(job.finished_at)

    def test_unreachable_marks_failed(self):
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(error=ssh.SSHError("不可达"))), \
                mock.patch.object(runner, "_spawn_streamer", return_value=None):
            job = runner.run_remote(self.machine, "main", "x", None, {}, None, None, 600, True)
        self.assertEqual(job.status, "failed")


class TestLauncherLocal(unittest.TestCase):
    """用本地 bash 真实执行 launcher 脚本（无网络），验证 PID/输出/退出码/超时强杀。"""

    def _bash(self, script):
        return subprocess.run([BASH, "-s"], input=script.encode("utf-8"),
                              capture_output=True, timeout=30)

    def test_foreground_script_output_and_rc(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "log"
            # 脚本交给本地 bash 执行：Windows 路径须转 MSYS 形式，否则会创建杂散目录
            script = runner._launcher("printf 'hello-from-cmd\\n'", "/tmp", {},
                                      msys_path(log), 600, False)
            cp = self._bash(script)
            self.assertEqual(cp.returncode, 0)
            self.assertTrue(cp.stdout.startswith(b"__RP_PID__="))
            self.assertIn(b"hello-from-cmd", cp.stdout)
            self.assertTrue((log / "cmd.sh").is_file())

    def test_background_timeout_kills_process_group(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "log"
            script = runner._launcher("sleep 30", "/tmp", {}, msys_path(log), 2, True)
            cp = self._bash(script)
            self.assertEqual(cp.returncode, 0)
            pid = int(cp.stdout.decode().strip().split("=", 1)[1])
            deadline = time.time() + 8
            while time.time() < deadline and not (log / "done").exists():
                time.sleep(0.2)
            self.assertTrue((log / "done").exists())
            self.assertEqual((log / "status").read_text().strip(), "timeout")
            rc = subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode
            self.assertNotEqual(rc, 0)  # 进程组已死


class TestCliRun(_Base):
    def test_unknown_alias_raises(self):
        args = mock.Mock(alias="nope", worktree="main", cmd="x", cwd=None, env={},
                         cards=None, task=None, timeout=600, background=False)
        with mock.patch.object(config, "load_machines", return_value={"m1": self.machine}):
            with self.assertRaises(config.ConfigError):
                runner.cli_run(args)

    def test_cli_run_payload(self):
        args = mock.Mock(alias="m1", worktree="main", cmd="echo hi", cwd=None, env={},
                         cards=None, task="t", timeout=600, background=False)
        with mock.patch.object(config, "load_machines", return_value={"m1": self.machine}), \
                mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=0, stdout=b"__RP_PID__=5\nhi\n")):
            payload = runner.cli_run(args)
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["exit_code"], 0)
        self.assertIn("stdout_preview", payload)
        self.assertIn("running", payload)
        self.assertEqual(payload["stdout_preview"]["head"], "hi\n")


if __name__ == "__main__":
    unittest.main()
