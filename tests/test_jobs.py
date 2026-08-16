"""jobs 模块单元测试（纯本地，无网络；ssh 打桩）。"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from remote_plugin import config, jobs, ssh


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

    def _make_job(self, job_id="j-20260813-140530-01", status="running", pid=555,
                  machine="m1", **kw):
        j = jobs.Job(job_id=job_id, machine=machine, status=status,
                     remote_pid=pid, **kw)
        jobs.save_job(j)
        return j


class TestNewJobId(unittest.TestCase):
    def setUp(self):
        jobs._last_ts = ""
        jobs._last_seq = 0

    def test_format_and_same_second_sequence(self):
        t = datetime(2026, 8, 13, 14, 5, 30)
        with mock.patch.object(jobs, "_clock", side_effect=[t, t, t]):
            ids = [jobs.new_job_id() for _ in range(3)]
        self.assertEqual(ids, [
            "j-20260813-140530-01",
            "j-20260813-140530-02",
            "j-20260813-140530-03",
        ])
        for jid in ids:
            self.assertRegex(jid, r"j-\d{8}-\d{6}-\d{2}")

    def test_reset_sequence_on_new_second(self):
        t1 = datetime(2026, 8, 13, 14, 6, 0)
        t2 = datetime(2026, 8, 13, 14, 6, 1)
        with mock.patch.object(jobs, "_clock", side_effect=[t1, t1, t2]):
            a = jobs.new_job_id()
            b = jobs.new_job_id()
            c = jobs.new_job_id()
        self.assertEqual(a, "j-20260813-140600-01")
        self.assertEqual(b, "j-20260813-140600-02")
        self.assertEqual(c, "j-20260813-140601-01")


class TestDefaultOwner(unittest.TestCase):
    def test_session_env_wins(self):
        with mock.patch.dict("os.environ", {"CLAUDE_SESSION_ID": "abc123"}, clear=True):
            self.assertEqual(jobs.default_owner(), "agent-abc123")

    def test_codex_session_env(self):
        with mock.patch.dict("os.environ", {"CODEX_SESSION_ID": "s-9"}, clear=True):
            self.assertEqual(jobs.default_owner(), "agent-s-9")

    def test_fallback_to_local_user(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(jobs.getpass, "getuser", return_value="maru"):
            self.assertEqual(jobs.default_owner(), "maru")


class TestLoadAndSave(_Base):
    def test_save_then_load_roundtrip(self):
        j = self._make_job(status="done", pid=None, exit_code=0)
        loaded = jobs.load_job(j.job_id)
        self.assertEqual(loaded.as_dict(), j.as_dict())

    def test_load_missing_raises(self):
        with self.assertRaises(config.RemotePluginError):
            jobs.load_job("j-20990101-000000-99")

    def test_load_corrupt_raises(self):
        d = self.state / "jobs" / "j-20260813-140530-01"
        d.mkdir(parents=True)
        (d / "meta.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(config.RemotePluginError):
            jobs.load_job("j-20260813-140530-01")


class TestReconcileStale(_Base):
    """spec T3 e2e 步骤2：stale reconcile（fake ssh：远端无进程 → stale）。"""

    def test_process_gone_marks_stale(self):
        self._make_job(status="running", pid=555)
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=1, stdout=b"dead 555\n")), \
                mock.patch.object(config, "load_machines", return_value={"m1": self.machine}):
            items = jobs.jobs("m1")
        self.assertEqual(items[0].status, "stale")
        meta = json.loads((self.state / "jobs" / "j-20260813-140530-01" / "meta.json").read_text())
        self.assertEqual(meta["status"], "stale")
        self.assertIsNotNone(meta["finished_at"])

    def test_alive_stays_running(self):
        self._make_job(status="running", pid=555)
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(rc=0, stdout=b"alive 555\n")), \
                mock.patch.object(config, "load_machines", return_value={"m1": self.machine}):
            items = jobs.jobs("m1")
        self.assertEqual(items[0].status, "running")

    def test_unreachable_marks_stale(self):
        self._make_job(status="running", pid=555)
        with mock.patch.object(ssh, "ssh_run", self._fake_ssh(error=ssh.SSHError("机器不可达"))), \
                mock.patch.object(config, "load_machines", return_value={"m1": self.machine}):
            items = jobs.jobs("m1")
        self.assertEqual(items[0].status, "stale")

    def test_machine_missing_from_config_marks_stale(self):
        self._make_job(status="running", pid=555)
        with mock.patch.object(config, "load_machines", return_value={}):
            items = jobs.jobs("m1")
        self.assertEqual(items[0].status, "stale")

    def test_no_running_jobs_no_ssh(self):
        self._make_job(status="done", pid=None)
        with mock.patch.object(ssh, "ssh_run", side_effect=AssertionError("不应 SSH")), \
                mock.patch.object(config, "load_machines", return_value={"m1": self.machine}):
            items = jobs.jobs("m1")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].status, "done")

    def test_filter_by_machine(self):
        self._make_job(job_id="j-20260813-140530-01", status="done", pid=None, machine="m1")
        self._make_job(job_id="j-20260813-140530-02", status="done", pid=None, machine="m2")
        with mock.patch.object(config, "load_machines", return_value={}):
            items = jobs.jobs("m2")
        self.assertEqual([j.job_id for j in items], ["j-20260813-140530-02"])


class TestRunningJobs(_Base):
    def test_only_running_for_machine(self):
        self._make_job(job_id="j-20260813-140530-01", status="running", pid=1, machine="m1")
        self._make_job(job_id="j-20260813-140530-02", status="running", pid=2, machine="m2")
        self._make_job(job_id="j-20260813-140530-03", status="done", pid=None, machine="m1")
        items = jobs.running_jobs("m1")
        self.assertEqual([j.job_id for j in items], ["j-20260813-140530-01"])


class TestJobTail(_Base):
    def test_last_lines(self):
        self._make_job(status="done", pid=None,
                       logs="full", log="state/jobs/j-20260813-140530-01/full.log")
        d = self.state / "jobs" / "j-20260813-140530-01"
        (d / "full.log").write_text("a\nb\nc\nd\n", encoding="utf-8")
        self.assertEqual(jobs.job_tail("j-20260813-140530-01", 2, "stdout"), "c\nd")
        self.assertEqual(jobs.job_tail("j-20260813-140530-01", 0, "stdout"), "a\nb\nc\nd\n")

    def test_missing_log_file_returns_empty(self):
        self._make_job(status="running", pid=555,
                       logs="full", log="state/jobs/j-20260813-140530-01/full.log")
        self.assertEqual(jobs.job_tail("j-20260813-140530-01", 5, "stderr"), "")

    def test_stream_ignored_merged_log(self):
        """合并日志：stream 参数被忽略，任何取值返回同一份内容。"""
        self._make_job(status="done", pid=None,
                       logs="tail", log="state/jobs/j-20260813-140530-01/tail.log")
        d = self.state / "jobs" / "j-20260813-140530-01"
        (d / "tail.log").write_text("x\ny\n", encoding="utf-8")
        self.assertEqual(jobs.job_tail("j-20260813-140530-01", 1, "stderr"), "y")
        self.assertEqual(jobs.job_tail("j-20260813-140530-01", 5, "bogus"), "x\ny")

    def test_legacy_job_without_log_returns_empty(self):
        """旧格式记录（stdout.log/stderr.log 分离）不再读取。"""
        self._make_job(status="done", pid=None)
        d = self.state / "jobs" / "j-20260813-140530-01"
        (d / "stdout.log").write_text("old\n", encoding="utf-8")
        self.assertEqual(jobs.job_tail("j-20260813-140530-01", 5, "stdout"), "")


class TestJobStop(_Base):
    def test_stop_running_job(self):
        self._make_job(status="running", pid=555)
        with mock.patch.object(ssh, "ssh_run", side_effect=self._fake_ssh(rc=0)) as fake, \
                mock.patch.object(config, "load_machines", return_value={"m1": self.machine}):
            j = jobs.job_stop("j-20260813-140530-01")
        self.assertEqual(j.status, "stopped")
        self.assertIsNotNone(j.finished_at)
        script = fake.call_args.args[1]
        self.assertIn("kill -TERM -- -\"$PID\"", script)
        meta = json.loads((self.state / "jobs" / "j-20260813-140530-01" / "meta.json").read_text())
        self.assertEqual(meta["status"], "stopped")

    def test_stop_terminal_raises(self):
        self._make_job(status="done", pid=None)
        with self.assertRaises(config.RemotePluginError):
            jobs.job_stop("j-20260813-140530-01")

    def test_stop_idempotent(self):
        self._make_job(status="stopped", pid=None)
        with mock.patch.object(ssh, "ssh_run", side_effect=AssertionError("不应 SSH")):
            j = jobs.job_stop("j-20260813-140530-01")
        self.assertEqual(j.status, "stopped")


if __name__ == "__main__":
    unittest.main()
