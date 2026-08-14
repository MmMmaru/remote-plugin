"""probes 模块单元测试（纯本地，无网络）。"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from remote_plugin.probes import NPU_PARSE_AWK, build_probe_script
from tests.fake_ssh import BASH, msys_path


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
                [BASH, "-n"], input=script.encode("utf-8"), capture_output=True
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))

    def test_npu_parse_awk_counts_devices(self):
        """用 fake npu-smi 验证 awk：A2 两行布局（名行+总线行）与仅名行布局都能解析。

        A2 `npu-smi info` 每卡两行，第二行 $3=Bus-Id、$4="AICore% Mem HBM"。
        注意：经 stdin（bash -s）喂脚本——Windows 上 `bash -c` 的 argv 会丢单引号。
        """
        import os
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            fake = bin_dir / "npu-smi"
            fake.write_text(
                "#!/usr/bin/env bash\n"
                'echo "| 0       910B4     | OK              | 77.1         50      0    / 0                |"\n'
                'echo "| 0                  | 0000:C1:00.0    | 33            0    / 0      3425 / 65536      |"\n'
                'echo "| 1       910B4     | OK              | 78.2         51      0    / 0                |"\n'
                'echo "| 1                  | 0000:C2:00.0    | 5             0    / 0      999  / 32768      |"\n',
                encoding="utf-8",
            )
            fake.chmod(0o755)
            env = dict(
                os.environ,
                PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            )
            proc = subprocess.run(
                [BASH, "-s"], input=NPU_PARSE_AWK.encode("utf-8"),
                capture_output=True, timeout=30, env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            cards = [l for l in proc.stdout.decode("utf-8", "replace").splitlines()
                     if l.startswith("CARD ")]
            self.assertEqual(len(cards), 2)
            # AICore% + HBM(used/total) 都解析出来
            self.assertIn("CARD 0 910B4 33 3425 65536", cards)
            self.assertIn("CARD 1 910B4 5 999 32768", cards)

            # 仅名行布局（无总线行）：仍能逐卡计数，利用率/显存记 n/a 0 0
            fake.write_text(
                "#!/usr/bin/env bash\n"
                'echo "| 0       310P3     | OK              | 10.0         40      0    / 0                |"\n'
                'echo "| 1       310P3     | OK              | 20.0         41      0    / 0                |"\n',
                encoding="utf-8",
            )
            proc = subprocess.run(
                [BASH, "-s"], input=NPU_PARSE_AWK.encode("utf-8"),
                capture_output=True, timeout=30, env=env,
            )
            cards = [l for l in proc.stdout.decode("utf-8", "replace").splitlines()
                     if l.startswith("CARD ")]
            self.assertEqual(len(cards), 2)
            self.assertIn("CARD 0 310P3 n/a 0 0", cards)
            self.assertIn("CARD 1 310P3 n/a 0 0", cards)

            # 紧凑布局（used/total 间无空格，A2 实测布局之一）：0/0 与 3425/65536
            fake.write_text(
                "#!/usr/bin/env bash\n"
                'echo "| 0       910B3     | OK              | 93.1         48      0/0                  |"\n'
                'echo "| 0                  | 0000:C1:00.0    | 7             0/0          3425/65536     |"\n'
                'echo "| 1       910B3     | OK              | 94.2         49      0/0                  |"\n'
                'echo "| 1                  | 0000:C2:00.0    | 0             0/0          0/65536         |"\n',
                encoding="utf-8",
            )
            proc = subprocess.run(
                [BASH, "-s"], input=NPU_PARSE_AWK.encode("utf-8"),
                capture_output=True, timeout=30, env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            cards = [l for l in proc.stdout.decode("utf-8", "replace").splitlines()
                     if l.startswith("CARD ")]
            self.assertIn("CARD 0 910B3 7 3425 65536", cards)
            self.assertIn("CARD 1 910B3 0 0 65536", cards)

            # 无 AICore% 列布局（部分驱动版本第二行只有 Mem/HBM）：AICore 记 n/a
            fake.write_text(
                "#!/usr/bin/env bash\n"
                'echo "| 0       910B3     | OK              | 93.1         48      0/0                  |"\n'
                'echo "| 0                  | 0000:C1:00.0    | 0/0          3425/65536                  |"\n',
                encoding="utf-8",
            )
            proc = subprocess.run(
                [BASH, "-s"], input=NPU_PARSE_AWK.encode("utf-8"),
                capture_output=True, timeout=30, env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            cards = [l for l in proc.stdout.decode("utf-8", "replace").splitlines()
                     if l.startswith("CARD ")]
            self.assertIn("CARD 0 910B3 n/a 3425 65536", cards)

    def test_script_executes_and_emits_valid_json(self):
        import json
        import os
        import tempfile

        script = build_probe_script({"chip": "ascend-a2", "cards": 8})
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ)
            # 脚本由本地 bash 执行：Windows 路径须转 MSYS 形式
            env["WS_ROOT"] = msys_path(td)
            env["EXPECTED_CARDS"] = "8"
            # fake npu-smi：验证 npu_cards 逐卡 JSON 数组组装
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            fake = bin_dir / "npu-smi"
            fake.write_text(
                "#!/usr/bin/env bash\n"
                'echo "| 0       910B3     | OK              | 93.1         48      0/0                  |"\n'
                'echo "| 0                  | 0000:C1:00.0    | 7             0/0          3425/65536     |"\n'
                'echo "| 1       910B3     | OK              | 94.2         49      0/0                  |"\n'
                'echo "| 1                  | 0000:C2:00.0    | 3             0/0          999/65536      |"\n',
                encoding="utf-8",
            )
            fake.chmod(0o755)
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
            # 让 pip/网络探针快速失败（127.0.0.1:9 无监听），测试保持离线
            env["HTTPS_PROXY"] = "http://127.0.0.1:9"
            env["https_proxy"] = "http://127.0.0.1:9"
            proc = subprocess.run(
                [BASH, "-s"], input=script.encode("utf-8"),
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
        # 不可达时测速输出 null + 原因字段，而不是 0（0 会被误读为"真的极慢"）
        self.assertIsNone(data["pip_index_speed_kbps"])
        self.assertIsInstance(data["pip_index_speed_note"], str)
        self.assertTrue(data["pip_index_speed_note"])
        # 每卡占用 JSON 数组：index/model/aicore_pct/hbm_used_mb/hbm_total_mb
        self.assertEqual(data["npu_count"], 2)
        self.assertEqual(data["cards_match"], False)  # 实测 2 ≠ 配置 8
        cards = data["npu_cards"]
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0], {"index": 0, "model": "910B3", "aicore_pct": 7,
                                    "hbm_used_mb": 3425, "hbm_total_mb": 65536})
        self.assertEqual(cards[1]["hbm_used_mb"], 999)

    def test_pip_speed_snippet_measures_and_marks_unmeasurable(self):
        """抽出探针脚本内嵌的测速 python 片段，对本地 HTTP server 验证：

        - 正常响应 → `ok <status> <latency> <bps>=0`（实测速度）；
        - 空响应体（无有效负载）→ bps = -1（无法测量，bash 侧转 null+note）。
        """
        import http.server
        import re
        import threading

        script = build_probe_script({})
        m = re.search(r"<<'PYEOF'[^\n]*\n(.*?)\nPYEOF", script, re.S)
        self.assertIsNotNone(m)
        snippet = m.group(1)

        payload = b"x" * 65536

        class Handler(http.server.BaseHTTPRequestHandler):
            empty = False

            def do_GET(self):
                self.send_response(200)
                body = b"" if self.empty else payload
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/simple/"

        import sys as _sys

        def run_snippet(u: str) -> str:
            # 片段含 UTF-8 中文注释：必须按字节喂，避免 text=True 走本地代码页
            r = subprocess.run([_sys.executable, "-", u], input=snippet.encode("utf-8"),
                               capture_output=True, timeout=30)
            return r.stdout.decode("utf-8", "replace")

        r = run_snippet(url)
        parts = r.split()
        self.assertEqual(parts[0], "ok")
        self.assertEqual(parts[1], "200")
        self.assertGreaterEqual(int(parts[3]), 0)  # 实测速度 bps

        Handler.empty = True
        r = run_snippet(url)
        parts = r.split()
        self.assertEqual(parts[0], "ok")
        self.assertEqual(int(parts[3]), -1)  # 无有效负载 → 无法测量

        # 连接被拒绝 → fail <异常名>（bash 侧转 unreachable + null）
        server.shutdown()
        r = run_snippet(url)
        self.assertTrue(r.startswith("fail "), r)


if __name__ == "__main__":
    unittest.main()
