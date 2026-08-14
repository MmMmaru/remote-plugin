"""SSH 传输原语。纯标准库 + 系统 ssh（BatchMode）。

端点带 ``container`` 时（模式 A），一切远端命令统一包装为
``docker exec -i <container> ...``（对宿主机做 ssh，再由 ssh 侧跑 docker exec）。
"""
from __future__ import annotations

import shlex
import subprocess
from typing import Any

from .config import Endpoint, RemotePluginError

DEFAULT_TIMEOUT_SECONDS = 300
PIPE_TIMEOUT_SECONDS = 3600


class SSHError(RemotePluginError):
    """SSH 执行失败或超时（fail closed）。"""


def _ssh_base(endpoint: Endpoint) -> list[str]:
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "LogLevel=ERROR",
        # 优先 curve25519：sntrup761x25519 的 KEX 大包在部分链路会触发
        # PMTUD 黑洞导致握手长时间挂起（实测 192.168.9.166），故显式首选。
        "-o", "KexAlgorithms=curve25519-sha256",
        "-p", str(endpoint.port),
        f"{endpoint.user}@{endpoint.host}",
    ]


def _wrap_remote(endpoint: Endpoint, argv: list[str]) -> str:
    """构造远端 shell 命令行字符串（单个参数传给 ssh，避免二次解析破坏引用）。

    无容器：``bash -s`` / ``bash -c '<script>'``；有容器：``docker exec -i <container> ...``。
    """
    inner = " ".join(shlex.quote(a) for a in argv)
    if endpoint.container:
        return "docker exec -i " + shlex.quote(endpoint.container) + " " + inner
    return inner


def ssh_argv(endpoint: Endpoint, remote_cmd: str) -> list[str]:
    """构造 ``ssh ... 'bash -c <remote_cmd>'``（含 docker exec 包装）的完整 argv。

    供流式/长驻等需要 ``Popen`` 的场景直接复用，保证与 ssh_run/ssh_pipe 同一套
    BatchMode 参数与 KEX 优先项。
    """
    return [*_ssh_base(endpoint), _wrap_remote(endpoint, ["bash", "-c", remote_cmd])]


def ssh_run(
    endpoint: Endpoint,
    script: str,
    timeout_sec: int = DEFAULT_TIMEOUT_SECONDS,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess:
    """在远端（或经 docker exec 的容器内）执行 shell 脚本。

    - ``input_bytes is None``：``bash -s``，``script`` 作为 stdin 传给 bash。
    - ``input_bytes`` 非 None：``bash -c script``，``input_bytes`` 作为远端
      命令的 stdin 透传（二进制流场景，如 tar/git bundle）。

    返回 ``subprocess.CompletedProcess``；超时抛 ``SSHError``。
    """
    if input_bytes is None:
        remote = _wrap_remote(endpoint, ["bash", "-s"])
        stdin: bytes = script.encode("utf-8")
    else:
        remote = _wrap_remote(endpoint, ["bash", "-c", script])
        stdin = input_bytes
    cmd = [*_ssh_base(endpoint), remote]
    try:
        return subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired as e:
        raise SSHError(
            f"ssh 执行超时（>{timeout_sec}s）: {endpoint.user}@{endpoint.host}:{endpoint.port}"
        ) from e
    except OSError as e:
        raise SSHError(f"ssh 调用失败: {e}") from e


def ssh_pipe(endpoint: Endpoint, local_cmd: list[str], remote_cmd: str) -> int:
    """本地命令 stdout → ssh stdin 管道传输（tar/bundle 二进制流）。

    返回远端退出码（int）；本地命令或远端命令失败时抛 ``SSHError``。
    """
    local_proc = subprocess.Popen(
        local_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    remote = _wrap_remote(endpoint, ["bash", "-c", remote_cmd])
    cmd = [*_ssh_base(endpoint), remote]
    try:
        result = subprocess.run(
            cmd,
            stdin=local_proc.stdout,
            capture_output=True,
            timeout=PIPE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        local_proc.kill()
        raise SSHError(
            f"ssh 管道传输超时: {endpoint.user}@{endpoint.host}:{endpoint.port}"
        ) from e
    finally:
        if local_proc.stdout is not None:
            local_proc.stdout.close()

    local_rc = local_proc.wait()
    local_err = local_proc.stderr.read().decode("utf-8", "replace") if local_proc.stderr else ""
    if local_rc != 0:
        raise SSHError(f"本地命令失败（{local_rc}）: {' '.join(local_cmd)}\n{local_err}")
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace") if result.stderr else ""
        raise SSHError(f"远端命令失败（{result.returncode}）: {remote_cmd}\n{err}")
    return result.returncode
