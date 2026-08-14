"""machines 模块单元测试（mock ssh，无网络、无真实 SSH）。"""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remote_plugin import config, machines, ssh
from remote_plugin.config import Machine


def _machine(alias: str = "a2", **kw) -> Machine:
    base = dict(
        alias=alias, mode="ssh", host="h", port=22, user="root",
        workspace_root="/ws", tags={"chip": "ascend-a2", "cards": 8},
    )
    base.update(kw)
    return Machine(**base)


def _ok_facts(**over):
    facts = {
        "uname": "Linux node 5.15.0-91-generic x86_64 GNU/Linux",
        "kernel": "5.15.0-91-generic",
        "os": "ubuntu 22.04",
        "cpu_model": "Intel(R) Xeon(R)",
        "mem_mb": 131072,
        "workspace_exists": True,
        "workspace_writable": True,
        "disk_free_gb": 512,
        "disk_usage_pct": 42,
        "python_version": "3.10.12",
        "pip_index_url": "https://pypi.org/simple/",
        "pip_index_reachable": True,
        "pip_index_latency_ms": 123,
        "has_proxy": False,
        "proxy_env": "",
        "apt_mirror": "http://archive.ubuntu.com/ubuntu/",
        "dns_ok": True,
        "npu_smi_ok": True,
        "npu_count": 8,
        "npu_model": "910B4",
        "npu_cards": [
            {"index": 0, "model": "910B4", "aicore_pct": 12,
             "hbm_used_mb": 3425, "hbm_total_mb": 65536},
            {"index": 1, "model": "910B4", "aicore_pct": None,
             "hbm_used_mb": 0, "hbm_total_mb": 65536},
        ],
        "cards_match": True,
        "torch_version": "2.1.0",
        "torch_npu_version": "2.1.0.post5",
    }
    facts.update(over)
    return facts


def _proc(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=rc, stdout=stdout.encode("utf-8"), stderr=stderr.encode("utf-8")
    )


class _Isolated(unittest.TestCase):
    """把 config.state_dir 指向临时目录，避免触碰真实 state。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.state.mkdir(parents=True)
        patcher = mock.patch.object(config, "state_dir", return_value=self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestVerifyMachine(_Isolated):
    def test_ok_status_and_doc(self):
        m = _machine()
        with mock.patch.object(
            ssh, "ssh_run", return_value=_proc(0, json.dumps(_ok_facts()))
        ) as run:
            result = machines.verify_machine(m)
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.doc.is_file())
        text = result.doc.read_text(encoding="utf-8")
        self.assertIn("- verify_status: ok", text)
        self.assertIn("910B4", text)
        self.assertIn("pip index", text)
        # 每卡占用写入档案文档 + 结构化 sidecar
        self.assertIn("每卡占用", text)
        self.assertIn("3425 / 65536", text)
        sidecar = result.doc.parent / f"{result.doc.stem}.facts.json"
        self.assertTrue(sidecar.is_file())
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(payload["verify_status"], "ok")
        self.assertEqual(payload["npu_cards"][0]["hbm_used_mb"], 3425)
        # 脚本注入了 WS_ROOT / EXPECTED_CARDS
        script = run.call_args[0][1]
        self.assertIn("export WS_ROOT=/ws", script)
        self.assertIn("export EXPECTED_CARDS=8", script)

    def test_container_uses_container_workspace_root(self):
        m = Machine(
            alias="c", mode="container", host="vm", port=22, user="root",
            container=config.ContainerCfg(
                image="img", name="n", ssh_port=46000, workspace_root="/home/x/ws"
            ),
            tags={"chip": "ascend-a2", "cards": 8},
        )
        with mock.patch.object(
            ssh, "ssh_run", return_value=_proc(0, json.dumps(_ok_facts()))
        ) as run:
            result = machines.verify_machine(m)
        self.assertEqual(result.status, "ok")
        self.assertIn("export WS_ROOT=/home/x/ws", run.call_args[0][1])

    def test_unreachable_on_ssherror(self):
        m = _machine(alias="down")
        with mock.patch.object(ssh, "ssh_run", side_effect=ssh.SSHError("connect timeout")):
            result = machines.verify_machine(m)
        self.assertEqual(result.status, "unreachable")
        self.assertIn("error", result.facts)
        self.assertTrue(result.doc.is_file())
        self.assertIn("unreachable", result.doc.read_text(encoding="utf-8"))

    def test_unreachable_on_rc255(self):
        m = _machine()
        with mock.patch.object(
            ssh, "ssh_run", return_value=_proc(255, "", "Could not resolve hostname")
        ):
            result = machines.verify_machine(m)
        self.assertEqual(result.status, "unreachable")
        self.assertIn("Could not resolve", result.facts["error"])

    def test_needs_up_when_workspace_missing(self):
        m = _machine()
        facts = _ok_facts(workspace_exists=False, workspace_writable=False)
        with mock.patch.object(ssh, "ssh_run", return_value=_proc(0, json.dumps(facts))):
            result = machines.verify_machine(m)
        self.assertEqual(result.status, "needs_up")
        self.assertIn("需先执行 up", result.doc.read_text(encoding="utf-8"))

    def test_degraded_on_cards_mismatch(self):
        m = _machine()
        facts = _ok_facts(npu_count=4, cards_match=False)
        with mock.patch.object(ssh, "ssh_run", return_value=_proc(0, json.dumps(facts))):
            result = machines.verify_machine(m)
        self.assertEqual(result.status, "degraded")
        self.assertIn("实测卡数 4 ≠ 配置 8", result.doc.read_text(encoding="utf-8"))

    def test_degraded_on_npu_smi_unavailable(self):
        m = _machine()
        facts = _ok_facts(npu_smi_ok=False, npu_count=0, npu_model="")
        with mock.patch.object(ssh, "ssh_run", return_value=_proc(0, json.dumps(facts))):
            result = machines.verify_machine(m)
        self.assertEqual(result.status, "degraded")

    def test_degraded_on_parse_failure(self):
        m = _machine()
        with mock.patch.object(ssh, "ssh_run", return_value=_proc(0, "not json at all")):
            result = machines.verify_machine(m)
        self.assertEqual(result.status, "degraded")
        self.assertIn("无法解析", result.facts["error"])

    def test_non_ascend_no_npu_judge(self):
        m = _machine(tags={"chip": "nvidia-h100"})
        facts = _ok_facts()
        del facts["npu_smi_ok"], facts["npu_count"], facts["npu_model"], facts["cards_match"]
        with mock.patch.object(ssh, "ssh_run", return_value=_proc(0, json.dumps(facts))):
            result = machines.verify_machine(m)
        self.assertEqual(result.status, "ok")


class TestListMachines(_Isolated):
    def test_no_doc_no_jobs(self):
        reg = {"a": _machine(alias="a"), "b": _machine(alias="b", tags={"chip": "nvidia-h100"})}
        with mock.patch.object(config, "load_machines", return_value=reg):
            views = machines.list_machines()
        self.assertEqual([v.alias for v in views], ["a", "b"])
        self.assertIsNone(views[0].verify_status)
        self.assertFalse(views[0].busy)

    def test_verify_status_from_doc(self):
        doc = self.state / "docs" / "a.md"
        doc.parent.mkdir(parents=True)
        doc.write_text(
            "# 机器档案: a\n- verify_status: ok\n- verified_at: 2026-01-01T00:00:00Z\n",
            encoding="utf-8",
        )
        with mock.patch.object(config, "load_machines", return_value={"a": _machine(alias="a")}):
            views = machines.list_machines()
        self.assertEqual(views[0].verify_status, "ok")
        self.assertEqual(views[0].verified_at, "2026-01-01T00:00:00Z")

    def test_running_job_occupancy(self):
        running = {
            "job_id": "j-20260813-140530-01", "machine": "a", "status": "running",
            "owner": "agent-x", "task": "编译", "cards": [0, 1],
        }
        jd = self.state / "jobs" / running["job_id"]
        jd.mkdir(parents=True)
        (jd / "meta.json").write_text(json.dumps(running), encoding="utf-8")
        done = self.state / "jobs" / "j-20260813-140531-02"
        done.mkdir(parents=True)
        (done / "meta.json").write_text(
            json.dumps({"job_id": done.name, "machine": "a", "status": "done"}), encoding="utf-8"
        )
        with mock.patch.object(config, "load_machines", return_value={"a": _machine(alias="a")}):
            views = machines.list_machines()
        self.assertTrue(views[0].busy)
        self.assertEqual(len(views[0].jobs), 1)
        self.assertEqual(views[0].jobs[0]["owner"], "agent-x")
        self.assertEqual(views[0].jobs[0]["task"], "编译")
        self.assertEqual(views[0].jobs[0]["cards"], [0, 1])

    def test_corrupt_job_meta_ignored(self):
        jd = self.state / "jobs" / "j-bad"
        jd.mkdir(parents=True)
        (jd / "meta.json").write_text("{not json", encoding="utf-8")
        with mock.patch.object(config, "load_machines", return_value={"a": _machine(alias="a")}):
            views = machines.list_machines()
        self.assertFalse(views[0].busy)


class TestMachineStatus(_Isolated):
    def test_missing_alias_raises_clean_error(self):
        with mock.patch.object(config, "load_machines", return_value={}):
            with self.assertRaises(config.RemotePluginError):
                machines.machine_status("nope", probe=False)

    def test_basic_without_probe_no_ssh(self):
        with mock.patch.object(config, "load_machines", return_value={"a": _machine(alias="a")}):
            with mock.patch.object(ssh, "ssh_run") as run:
                st = machines.machine_status("a", probe=False)
        self.assertEqual(st.alias, "a")
        self.assertTrue(st.reachable)
        self.assertIsNone(st.load)
        run.assert_not_called()

    def test_probe_parses_load_and_npu(self):
        out = (
            "LOAD 0.52 0.61 0.55\n"
            "CPUS 128\n"
            "CPU_MODEL Intel Xeon\n"
            "MEM_TOTAL_KB 131072000\n"
            "MEM_AVAIL_KB 65536000\n"
            "NPU_BEGIN\n"
            "CARD 0 910B4 12 3425 65536\n"
            "CARD 1 910B4 3 999 32768\n"
            "NPU_END\n"
        )
        with mock.patch.object(config, "load_machines", return_value={"a": _machine(alias="a")}):
            with mock.patch.object(ssh, "ssh_run", return_value=_proc(0, out)):
                st = machines.machine_status("a", probe=True)
        self.assertTrue(st.reachable)
        self.assertEqual(st.load["1m"], 0.52)
        self.assertEqual(st.cpu["cores"], 128)
        self.assertEqual(st.cpu["model"], "Intel Xeon")
        self.assertEqual(st.mem["total_gb"], 125.0)
        self.assertEqual(st.mem["available_gb"], 62.5)
        self.assertTrue(st.npu_smi)
        self.assertEqual(len(st.npu), 2)
        self.assertEqual(st.npu[0]["model"], "910B4")
        self.assertEqual(st.npu[0]["aicore_pct"], 12)
        self.assertEqual(st.npu[0]["hbm_used_mb"], 3425)
        self.assertEqual(st.npu[0]["hbm_total_mb"], 65536)

    def test_probe_npu_smi_missing(self):
        out = "LOAD 0.1 0.2 0.3\nCPUS 4\nNPU_SMI_MISSING\n"
        with mock.patch.object(config, "load_machines", return_value={"a": _machine(alias="a")}):
            with mock.patch.object(ssh, "ssh_run", return_value=_proc(0, out)):
                st = machines.machine_status("a", probe=True)
        self.assertTrue(st.reachable)
        self.assertFalse(st.npu_smi)
        self.assertEqual(st.npu, [])

    def test_probe_unreachable(self):
        with mock.patch.object(config, "load_machines", return_value={"a": _machine(alias="a")}):
            with mock.patch.object(ssh, "ssh_run", side_effect=ssh.SSHError("timeout")):
                st = machines.machine_status("a", probe=True)
        self.assertFalse(st.reachable)
        self.assertIn("timeout", st.probe_error)


class TestHandlers(_Isolated):

    def _args(self, **kw):
        return argparse.Namespace(**kw)

    def test_cli_verify_unknown_alias_unreachable(self):
        with mock.patch.object(config, "load_machines", return_value={}):
            result = machines.cli_verify(self._args(alias="ghost"))
        self.assertEqual(result["status"], "unreachable")
        self.assertIn("未注册", result["facts"]["error"])
        self.assertTrue(result["doc"].endswith("ghost.md"))

    def test_cli_verify_known_alias(self):
        with mock.patch.object(config, "load_machines", return_value={"a": _machine(alias="a")}):
            with mock.patch.object(ssh, "ssh_run", return_value=_proc(0, json.dumps(_ok_facts()))):
                result = machines.cli_verify(self._args(alias="a"))
        self.assertEqual(result["status"], "ok")
        self.assertIn("facts", result)
        self.assertIn("doc", result)

    def test_cli_machines_shape(self):
        with mock.patch.object(config, "load_machines", return_value={"a": _machine(alias="a")}):
            result = machines.cli_machines(self._args())
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["machines"][0]["alias"], "a")
        self.assertIn("busy", result["machines"][0])
        self.assertIn("jobs", result["machines"][0])
        self.assertIn("npu_cards", result["machines"][0])

    def test_cli_machines_includes_per_card_utilization_from_verify(self):
        """verify 后 machines 输出每卡 HBM/AICore 实测占用（sidecar 驱动）。"""
        with mock.patch.object(config, "load_machines", return_value={"a": _machine(alias="a")}):
            with mock.patch.object(ssh, "ssh_run", return_value=_proc(0, json.dumps(_ok_facts()))):
                machines.cli_verify(self._args(alias="a"))
            result = machines.cli_machines(self._args())
        cards = result["machines"][0]["npu_cards"]
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]["index"], 0)
        self.assertEqual(cards[0]["aicore_pct"], 12)
        self.assertEqual(cards[0]["hbm_used_mb"], 3425)
        self.assertEqual(cards[0]["hbm_total_mb"], 65536)
        self.assertIsNone(cards[1]["aicore_pct"])  # 解析不出时记 null

    def test_verify_status_md_fallback_without_sidecar(self):
        """旧档案（仅 md，无 sidecar）仍能读出 status/verified_at，npu_cards 为 None。"""
        doc = self.state / "docs" / "a.md"
        doc.parent.mkdir(parents=True)
        doc.write_text(
            "# 机器档案: a\n- verify_status: degraded\n- verified_at: 2026-01-01T00:00:00Z\n",
            encoding="utf-8",
        )
        status, verified, cards = machines._read_verify_summary(self.state, "a")
        self.assertEqual(status, "degraded")
        self.assertEqual(verified, "2026-01-01T00:00:00Z")
        self.assertIsNone(cards)

    def test_cli_status_shape(self):
        with mock.patch.object(config, "load_machines", return_value={"a": _machine(alias="a")}):
            result = machines.cli_status(self._args(alias="a", probe=False))
        self.assertEqual(result["alias"], "a")
        self.assertIn("npu", result)
        self.assertIn("reachable", result)


if __name__ == "__main__":
    unittest.main()
