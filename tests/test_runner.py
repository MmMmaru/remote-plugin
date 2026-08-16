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


class TestRunForeground(_Base):
    """前台 run：默认 none（不落盘、不记录 job），显式 tail/full 才保留。"""

    def test_foreground_default_none_no_job_record(self):
        """默认前台 logs=none：不写日志文件、不写 meta.json，仅返回合并预览。"""
        out = b"__RP_PID__=4242\n" + b"x" * 5000
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=0, stdout=out,
                                                              stderr=b"y" * 123)):
            job = runner.run_remote(self.machine, "echo hi", None, {}, None,
                                    None, 600, False)
        self.assertEqual(job.status, "done")
        self.assertEqual(job.exit_code, 0)
        self.assertEqual(job.remote_pid, 4242)
        self.assertEqual(job.cwd, "/ws")
        self.assertEqual(job.logs, "none")
        self.assertEqual(job.log, "")
        self.assertFalse((self.state / "jobs" / job.job_id).exists())
        # 合并预览：stdout 段在前，stderr 段在后
        self.assertTrue(job.preview["truncated"])
        self.assertEqual(job.preview["head"], "x" * 4000)
        self.assertTrue(job.preview["tail"].endswith("y" * 123))

    def test_foreground_full_keeps_merged_log(self):
        out = b"__RP_PID__=4242\n" + b"x" * 5000
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=0, stdout=out,
                                                              stderr=b"y" * 123)):
            job = runner.run_remote(self.machine, "echo hi", None, {}, None,
                                    None, 600, False, logs="full")
        self.assertEqual(job.status, "done")
        self.assertEqual(job.logs, "full")
        self.assertEqual(job.log, f"state/jobs/{job.job_id}/full.log")
        d = self.state / "jobs" / job.job_id
        self.assertEqual((d / "full.log").read_bytes(),
                         b"x" * 5000 + b"\n" + b"y" * 123)  # stdout 段 + 换行 + stderr 段
        meta = json.loads((d / "meta.json").read_text())
        self.assertEqual(meta["status"], "done")
        self.assertEqual(meta["exit_code"], 0)
        self.assertEqual(meta["logs"], "full")

    def test_foreground_tail_keeps_last_lines(self):
        out = b"__RP_PID__=1\n" + b"".join(f"o{i}\n".encode() for i in range(250))
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=0, stdout=out,
                                                              stderr=b"e\n")):
            job = runner.run_remote(self.machine, "echo hi", None, {}, None,
                                    None, 600, False, logs="tail")
        self.assertEqual(job.log, f"state/jobs/{job.job_id}/tail.log")
        d = self.state / "jobs" / job.job_id
        lines = (d / "tail.log").read_bytes().splitlines()
        self.assertEqual(len(lines), runner.TAIL_LOG_LINES)  # 只留最后 200 行
        self.assertEqual(lines[0], b"o51")
        self.assertEqual(lines[-1], b"e")

    def test_nonzero_exit_failed(self):
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=3, stdout=b"__RP_PID__=9\nboom\n")):
            job = runner.run_remote(self.machine, "false", None, {}, None, None, 600, False,
                                    logs="full")
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.exit_code, 3)
        d = self.state / "jobs" / job.job_id
        self.assertEqual((d / "full.log").read_bytes(), b"boom\n")

    def test_timeout_status_and_cleanup(self):
        err = ssh.SSHError("ssh 执行超时（>600s）")

        def fake(endpoint, script, timeout_sec=300, input_bytes=None):
            if "cmd.sh" in script:
                raise err
            return subprocess.CompletedProcess([], 0, b"", b"")  # cleanup 调用

        with mock.patch.object(ssh, "ssh_run", side_effect=fake) as f:
            job = runner.run_remote(self.machine, "sleep 60", None, {}, None,
                                    None, 600, False)
        self.assertEqual(job.status, "timeout")
        self.assertEqual(job.exit_code, 124)
        self.assertIsNotNone(job.finished_at)
        # launcher 一次 + cleanup 一次
        self.assertEqual(f.call_count, 2)
        self.assertIn("kill -TERM", f.call_args_list[1].args[1])

    def test_ssh_error_failed(self):
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(error=ssh.SSHError("连接失败"))):
            job = runner.run_remote(self.machine, "x", None, {}, None, None, 600, False)
        self.assertEqual(job.status, "failed")
        self.assertIsNone(job.exit_code)

    def test_explicit_cwd_and_env_cards(self):
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=0, stdout=b"__RP_PID__=1\n")):
            job = runner.run_remote(self.machine, "pwd", "/custom/dir",
                                    {"ASCEND_RT_VISIBLE_DEVICES": "0,1"}, None, None, 600, False)
        self.assertEqual(job.cwd, "/custom/dir")
        self.assertEqual(job.cards, [0, 1])


class TestRunBackground(_Base):
    def test_returns_immediately_with_pid(self):
        """后台默认 logs=full：fetcher 等 done 后拉全量合并日志并清理远端。"""
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=0, stdout=b"__RP_PID__=777\n")), \
                mock.patch.object(runner, "_spawn_log_fetcher", return_value=None) as fetch:
            job = runner.run_remote(self.machine, "sleep 600", None, {}, None,
                                    "占坑", 600, True)
        self.assertEqual(job.status, "running")
        self.assertEqual(job.remote_pid, 777)
        self.assertEqual(job.cwd, "/ws")
        self.assertEqual(job.logs, "full")
        self.assertEqual(job.log, f"state/jobs/{job.job_id}/full.log")
        self.assertEqual(job.remote_log_dir, f"/ws/.remote-logs/{job.job_id}")
        self.assertEqual(fetch.call_count, 1)
        args, kwargs = fetch.call_args
        self.assertEqual(args[1], f"/ws/.remote-logs/{job.job_id}/combined.log")
        self.assertEqual(args[3], self.state / "jobs" / job.job_id / "full.log")
        self.assertEqual(args[4], f"/ws/.remote-logs/{job.job_id}")
        self.assertIsNone(args[5])  # full：拉全量
        meta = json.loads((self.state / "jobs" / job.job_id / "meta.json").read_text())
        self.assertEqual(meta["status"], "running")
        self.assertEqual(meta["remote_pid"], 777)
        # advisory 占用提示包含自己
        self.assertEqual([r["job_id"] for r in job.running], [job.job_id])

    def test_background_tail_fetches_last_lines(self):
        """后台 --logs tail：fetcher 只拉最后 TAIL_LOG_LINES 行。"""
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=0, stdout=b"__RP_PID__=9\n")), \
                mock.patch.object(runner, "_spawn_log_fetcher", return_value=None) as fetch:
            job = runner.run_remote(self.machine, "sleep 600", None, {}, None,
                                    "t", 600, True, logs="tail")
        self.assertEqual(job.log, f"state/jobs/{job.job_id}/tail.log")
        self.assertEqual(fetch.call_count, 1)
        args, kwargs = fetch.call_args
        self.assertEqual(args[1], f"/ws/.remote-logs/{job.job_id}/combined.log")
        self.assertEqual(args[3], self.state / "jobs" / job.job_id / "tail.log")
        self.assertEqual(args[4], f"/ws/.remote-logs/{job.job_id}")
        self.assertEqual(args[5], runner.TAIL_LOG_LINES)

    def test_background_none_no_job_record(self):
        """后台 --logs none：不记录 job、不启动任何同步进程。"""
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=0, stdout=b"__RP_PID__=9\n")), \
                mock.patch.object(runner, "_spawn_log_fetcher", return_value=None) as fetch:
            job = runner.run_remote(self.machine, "sleep 600", None, {}, None,
                                    None, 600, True, logs="none")
        self.assertEqual(job.logs, "none")
        self.assertEqual(job.log, "")
        self.assertFalse((self.state / "jobs" / job.job_id).exists())
        self.assertEqual(fetch.call_count, 0)

    def test_launch_failure_marks_failed(self):
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=1, stdout=b"")), \
                mock.patch.object(runner, "_spawn_log_fetcher", return_value=None):
            job = runner.run_remote(self.machine, "x", None, {}, None, None, 600, True)
        self.assertEqual(job.status, "failed")
        self.assertIsNotNone(job.finished_at)

    def test_unreachable_marks_failed(self):
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(error=ssh.SSHError("不可达"))), \
                mock.patch.object(runner, "_spawn_log_fetcher", return_value=None):
            job = runner.run_remote(self.machine, "x", None, {}, None, None, 600, True)
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

    def test_foreground_self_clean_removes_log_dir(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "log"
            script = runner._launcher("printf 'hi\\n'", "/tmp", {}, msys_path(log),
                                      600, False, self_clean=True)
            cp = self._bash(script)
            self.assertEqual(cp.returncode, 0)
            self.assertFalse(log.exists())  # 远端日志目录已自清理

    def test_background_self_clean_removes_log_dir_after_done(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "log"
            script = runner._launcher("sleep 1", "/tmp", {}, msys_path(log),
                                      5, True, self_clean=True)
            cp = self._bash(script)
            self.assertEqual(cp.returncode, 0)
            deadline = time.time() + 8
            while time.time() < deadline and log.exists():
                time.sleep(0.2)
            self.assertFalse(log.exists())  # waiter 写 done 后自清理

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
            self.assertTrue((log / "combined.log").is_file())  # 合并日志
            rc = subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode
            self.assertNotEqual(rc, 0)  # 进程组已死


class TestCliRun(_Base):
    def test_unknown_alias_raises(self):
        args = mock.Mock(alias="nope", cmd="x", cwd=None, env={},
                         cards=None, task=None, timeout=600, background=False,
                         logs=None)
        with mock.patch.object(config, "load_machines", return_value={"m1": self.machine}):
            with self.assertRaises(config.ConfigError):
                runner.cli_run(args)

    def test_cli_run_none_payload(self):
        """默认前台 none：返回 status/exit_code/preview/logs，无 job_id。"""
        args = mock.Mock(alias="m1", cmd="echo hi", cwd=None, env={},
                         cards=None, task="t", timeout=600, background=False,
                         logs=None)
        with mock.patch.object(config, "load_machines", return_value={"m1": self.machine}), \
                mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=0, stdout=b"__RP_PID__=5\nhi\n")):
            payload = runner.cli_run(args)
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["logs"], "none")
        self.assertEqual(payload["preview"]["head"], "hi\n")
        self.assertNotIn("job_id", payload)

    def test_cli_run_full_payload_includes_job_id(self):
        args = mock.Mock(alias="m1", cmd="echo hi", cwd=None, env={},
                         cards=None, task="t", timeout=600, background=False,
                         logs="full")
        with mock.patch.object(config, "load_machines", return_value={"m1": self.machine}), \
                mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=0, stdout=b"__RP_PID__=5\nhi\n")):
            payload = runner.cli_run(args)
        self.assertEqual(payload["logs"], "full")
        self.assertIn("job_id", payload)
        self.assertEqual(payload["preview"]["head"], "hi\n")
        self.assertIn("running", payload)


if __name__ == "__main__":
    unittest.main()
