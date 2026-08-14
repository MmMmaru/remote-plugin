"""T2 测试共用 fake ssh 层与测试数据（纯本地，无网络）。

用法：按插入顺序注册匹配规则（``exact=True`` 全等匹配 script.strip()，否则子串匹配），
未命中回退到默认 rc=0；response 为 list 时按调用次数依次弹出（末项重复）。
"""
from __future__ import annotations

import json
import subprocess

IMAGE = "quay.io/ascend/vllm-ascend:nightly-main"
PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI test@local"
WS = "/home/x50063850/vllm-ascend-workspace"


def cp(returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def docker_inspect_healthy(image=IMAGE, port=46000, devices=("/dev/davinci0", "/dev/davinci_manager")):
    """健康容器的 docker inspect stdout（bytes JSON）。"""
    return json.dumps([
        {
            "State": {"Running": True},
            "Config": {"Image": image},
            "HostConfig": {
                "PortBindings": {"22/tcp": [{"HostIp": "", "HostPort": str(port)}]},
                "Devices": [
                    {"PathOnHost": d, "PathInContainer": d, "CgroupPermissions": "m"}
                    for d in devices
                ],
            },
        }
    ]).encode("utf-8")


class FakeSSH:
    """记录每次 ssh_run 调用并可按规则返回编程结果。"""

    def __init__(self):
        self.calls: list[dict] = []
        self._rules: list[tuple[str, object, bool]] = []
        self.default = cp()

    def on(self, needle, response=None, exact=False):
        self._rules.append((needle, response if response is not None else cp(), exact))
        return self

    def ssh_run(self, endpoint, script, timeout_sec=300, input_bytes=None):
        self.calls.append({
            "endpoint": endpoint,
            "script": script,
            "timeout_sec": timeout_sec,
            "input_bytes": input_bytes,
        })
        s = script.strip()
        for idx, (needle, response, exact) in enumerate(self._rules):
            hit = (s == needle) if exact else (needle in script)
            if not hit:
                continue
            if isinstance(response, list):
                if len(response) > 1:
                    self._rules[idx] = (needle, response[1:], exact)
                response = response[0]
            return response
        return self.default
