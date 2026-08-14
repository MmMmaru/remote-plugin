"""probes 模块单元测试（纯本地，无网络）。"""
from __future__ import annotations

import subprocess
import unittest

from remote_plugin.probes import NPU_PARSE_AWK, build_probe_script


class TestBuildProbeScript(unittest.TestCase):
    def test_ascend_includes_npu_smi_and_cards_check(self):
        script = build_probe_script({"chip": "ascend-a2", "cards": 8})
        self.assertIn("npu-smi", script)
        self.assertIn("EXPECTED_CARDS", script)
        self.assertIn("cards_match", script)
        self.assertIn("npu_count", script)

    def test_non_ascend_excludes_npu_smi(self):
        script = build_probe_script({"chip": "nvidia-h100", "cards": 8})
        self.assertNotIn("npu-smi", script)
        self.assertNotIn("cards_match", script)

    def test_empty_tags_defaults(self):
        script = build_probe_script({})
        self.assertNotIn("npu-smi", script)
        self.assertIn("uname", script)
        self.assertIn("pip_index", script)
        self.assertIn("workspace_exists", script)
        self.assertIn("EXPECTED_CARDS", script)

    def test_cards_embedded_default(self):
        script = build_probe_script({"chip": "ascend-a2", "cards": 8})
        self.assertIn('EXPECTED_CARDS="${EXPECTED_CARDS:-8}"', script)

    def test_cards_missing_default_empty(self):
        script = build_probe_script({"chip": "ascend-a2"})
        self.assertIn('EXPECTED_CARDS="${EXPECTED_CARDS:-}"', script)

    def test_string_cards_normalized(self):
        script = build_probe_script({"chip": "ascend-a2", "cards": "8"})
        self.assertIn('EXPECTED_CARDS="${EXPECTED_CARDS:-8}"', script)

    def test_script_syntax_valid_bash(self):
        for tags in ({"chip": "ascend-a2", "cards": 8}, {"chip": "nvidia-h100"}, {}):
            script = build_probe_script(tags)
            proc = subprocess.run(
                ["bash", "-n"], input=script.encode("utf-8"), capture_output=True
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))

    def test_npu_parse_awk_counts_devices(self):
        """用 fake npu-smi 验证 awk：两行布局（名行+总线行）与仅名行布局都能计数。"""
        import os
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            fake = bin_dir / "npu-smi"
            fake.write_text(
                "#!/usr/bin/env bash\n"
                'echo "| 0       910B4     | OK              | 77.1         50      |"\n'
                'echo "| 0       0         | 0000:C1:00.0    | 33            0 / 0   |"\n'
                'echo "| 1       910B4     | OK              | 78.2         51      |"\n'
                'echo "| 1       0         | 0000:C2:00.0    | 5             0 / 0   |"\n',
                encoding="utf-8",
            )
            fake.chmod(0o755)
            env = dict(
                os.environ,
                PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            )
            proc = subprocess.run(
                ["bash", "-c", NPU_PARSE_AWK], capture_output=True, text=True,
                timeout=30, env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            cards = [l for l in proc.stdout.splitlines() if l.startswith("CARD ")]
            self.assertEqual(len(cards), 2)
            self.assertIn("CARD 0 910B4 33", cards)
            self.assertIn("CARD 1 910B4 5", cards)

            # 仅名行布局（无总线行）：仍能逐卡计数，利用率记 n/a
            fake.write_text(
                "#!/usr/bin/env bash\n"
                'echo "| 0       310P3     | OK              | 10.0         40      |"\n'
                'echo "| 1       310P3     | OK              | 20.0         41      |"\n',
                encoding="utf-8",
            )
            proc = subprocess.run(
                ["bash", "-c", NPU_PARSE_AWK], capture_output=True, text=True,
                timeout=30, env=env,
            )
            cards = [l for l in proc.stdout.splitlines() if l.startswith("CARD ")]
            self.assertEqual(len(cards), 2)
            self.assertIn("CARD 0 310P3 n/a", cards)
            self.assertIn("CARD 1 310P3 n/a", cards)

    def test_script_executes_and_emits_valid_json(self):
        import json
        import os
        import tempfile

        script = build_probe_script({"chip": "ascend-a2", "cards": 8})
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ)
            env["WS_ROOT"] = td
            env["EXPECTED_CARDS"] = "8"
            # 让 pip/网络探针快速失败（127.0.0.1:9 无监听），测试保持离线
            env["HTTPS_PROXY"] = "http://127.0.0.1:9"
            env["https_proxy"] = "http://127.0.0.1:9"
            proc = subprocess.run(
                ["bash", "-s"], input=script.encode("utf-8"),
                capture_output=True, timeout=90, env=env,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
        # 字符串必须带引号、数字/布尔必须是 JSON 原生类型
        self.assertIsInstance(data["uname"], str)
        self.assertIsInstance(data["kernel"], str)
        self.assertIsInstance(data["npu_model"], str)
        self.assertIsInstance(data["pip_index_url"], str)
        self.assertIsInstance(data["workspace_exists"], bool)
        self.assertIsInstance(data["workspace_writable"], bool)
        self.assertIsInstance(data["has_proxy"], bool)
        self.assertIsInstance(data["disk_free_gb"], int)
        self.assertIsInstance(data["npu_count"], int)
        self.assertIs(data["workspace_exists"], True)
        self.assertIs(data["workspace_writable"], True)
        self.assertIs(data["pip_index_reachable"], False)


if __name__ == "__main__":
    unittest.main()
