"""SSH 传输原语。纯标准库 + 系统 ssh（BatchMode）。"""
from __future__ import annotations

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
        "-p", str(endpoint.port),
        f"{endpoint.user}@{endpoint.host}",
    ]


def ssh_run(
    endpoint: Endpoint,
    script: str,
    timeout_sec: int = DEFAULT_TIMEOUT_SECONDS,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess:
    """在远端执行 shell 脚本。

    - ``input_bytes is None``：``ssh ... bash -s``，``script`` 作为 stdin 传给 bash。
    - ``input_bytes`` 非 None：``ssh ... bash -c script``，``input_bytes`` 作为远端
      命令的 stdin 透传（二进制流场景，如 tar/git bundle）。

    返回 ``subprocess.CompletedProcess``；超时抛 ``SSHError``。
    """
    if input_bytes is None:
        cmd = [*_ssh_base(endpoint), "bash", "-s"]
        stdin: bytes = script.encode("utf-8")
    else:
        cmd = [*_ssh_base(endpoint), "bash", "-c", script]
        stdin = input_bytes
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
    cmd = [*_ssh_base(endpoint), "bash", "-c", remote_cmd]
    try:
        remote = subprocess.run(
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
    if remote.returncode != 0:
        err = remote.stderr.decode("utf-8", "replace") if remote.stderr else ""
        raise SSHError(f"远端命令失败（{remote.returncode}）: {remote_cmd}\n{err}")
    return remote.returncode
