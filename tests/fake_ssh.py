"""T2 测试共用 fake ssh 层与测试数据（纯本地，无网络）。

用法：按插入顺序注册匹配规则（``exact=True`` 全等匹配 script.strip()，否则子串匹配），
未命中回退到默认 rc=0；response 为 list 时按调用次数依次弹出（末项重复）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

# Windows 上 CreateProcess 会优先命中 System32 的 WSL bash / bsdtar（而非 PATH
# 里的 Git Bash / Git tar）；shutil.which 只搜 PATH，可拿到正确的 GNU 版本。
BASH = shutil.which("bash") or "bash"
TAR = shutil.which("tar") or "tar"


def msys_path(p) -> str:
    """Windows 绝对路径转 Git Bash(MSYS) 形式：``C:\\a\\b`` → ``/c/a/b``；其余原样 POSIX 化。

    凡测试把本地临时目录嵌进「远端脚本」并交给本地 bash 执行时，必须先过本函数，
    否则 bash 会把 `C:\\...` 当作含反斜杠的普通文件名，在 cwd 下创建杂散目录。
    """
    s = str(p)
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        s = "/" + s[0].lower() + s[2:]
    return s.replace("\\", "/")


def local_path(s: str) -> Path:
    """``msys_path`` 的逆变换：``/c/a/b`` → ``C:\\a\\b``；非 MSYS 形式原样返回。"""
    if len(s) >= 3 and s[0] == "/" and s[2] == "/" and s[1].isalpha():
        return Path(s[1].upper() + ":" + s[2:])
    return Path(s)

IMAGE = "quay.io/ascend/vllm-ascend:nightly-main"
PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI test@local"
WS = "/home/x50063850/vllm-ascend-workspace"


def cp(returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def docker_inspect_healthy(image=IMAGE, devices=("/dev/davinci_manager", "/dev/hisi_hdc")):
    """健康容器的 docker inspect stdout（bytes JSON）。无 sshd/端口（docker exec 模型）。"""
    return json.dumps([
        {
            "State": {"Running": True},
            "Config": {"Image": image},
            "HostConfig": {
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
