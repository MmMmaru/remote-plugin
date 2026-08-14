"""T2 免密引导与容器生命周期引导（bootstrap 层）。

- ``push_pubkey``：把本地公钥写入远端 authorized_keys；密码引导走系统 ssh 的
  ``SSH_ASKPASS`` + ``setsid`` 技巧（临时 askpass 脚本 echo 密码，用完即删），
  不使用 sshpass/expect/paramiko。
- ``ensure_container``：docker 可用性 → 拉镜像 → 创建/复用容器（按 tags.chip 挂
  设备）→ 容器 sshd 就绪 → 容器内写公钥免密。幂等：健康容器复用；漂移抛
  ``NeedsRepairError`` 不自动重建。

纯标准库 + 系统 ssh；密码绝不写入 state/、日志或任何持久文件。
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import output, ssh
from .config import ContainerCfg, Endpoint, RemotePluginError

# 密码引导的 ssh 基础参数（与 ssh.py 的 BatchMode 版本对应，仅把 BatchMode 关掉）
_ASKPASS_SSH_OPTS = [
    "-o", "BatchMode=no",
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "LogLevel=ERROR",
    # 与 ssh.py 一致：优先 curve25519 规避 sntrup761 KEX 黑洞挂起
    "-o", "KexAlgorithms=curve25519-sha256",
]

# 容器 sshd 启动命令（镜像内 sshd 作为 PID1，缺 host key 时先生成）
_CONTAINER_SSHD_CMD = (
    "mkdir -p /run/sshd; "
    "[ -f /etc/ssh/ssh_host_rsa_key ] || ssh-keygen -A; "
    "exec /usr/sbin/sshd -D"
)

# 幂等追加公钥的 shell 片段：read 一行，已存在则跳过（避免重复行）
_AUTH_KEYS_INNER = (
    "umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
    "chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys; "
    "read -r line; grep -qF -- \"$line\" ~/.ssh/authorized_keys 2>/dev/null "
    "|| printf '%s\\n' \"$line\" >> ~/.ssh/authorized_keys"
)


class NeedsRepairError(RemotePluginError):
    """容器漂移：不自动重建，需人工修复后重试。"""


def local_pubkey() -> str:
    """读取本地公钥（id_ed25519 / id_rsa / id_ecdsa 依次尝试），供 VM 与容器免密写入。"""
    for name in ("id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub"):
        path = Path.home() / ".ssh" / name
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    raise RemotePluginError(
        "未找到本地公钥（~/.ssh/id_ed25519.pub / id_rsa.pub / id_ecdsa.pub），请先 ssh-keygen"
    )


def push_pubkey(endpoint: Endpoint, pubkey: str, password: str | None) -> None:
    """把 pubkey 写入远端 authorized_keys（幂等）。

    - ``password is None``：BatchMode 直写（要求目标已免密；未免密时报错）。
    - ``password`` 非 None：SSH_ASKPASS + setsid 密码引导，临时 askpass 脚本用完即删，
      密码绝不落盘/进日志。
    """
    line = pubkey.strip()
    if not line:
        raise RemotePluginError("pubkey 为空")
    data = (line + "\n").encode("utf-8")
    if password is None:
        r = ssh.ssh_run(endpoint, _AUTH_KEYS_INNER, timeout_sec=60, input_bytes=data)
    else:
        r = _password_ssh(endpoint, _AUTH_KEYS_INNER, password, input_bytes=data, timeout_sec=120)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip() if r.stderr else ""
        raise ssh.SSHError(
            f"公钥写入失败（{endpoint.user}@{endpoint.host}:{endpoint.port}）: {err[:300]}"
        )


def ensure_container(vm: Endpoint, container: ContainerCfg, tags: dict) -> None:
    """模式 A 容器引导：docker 可用 → 拉镜像（无则 pull）→ 创建/复用 → sshd → 容器免密。

    幂等：健康容器直接复用并回 ``already_ready``；漂移抛 ``NeedsRepairError``，
    不自动重建。镜像/容器名全部来自配置，不做镜像策略推断。
    """
    if not container.name or not container.image:
        raise RemotePluginError("container.name / container.image 配置缺失")
    _check_docker(vm)
    _ensure_image(vm, container.image)
    desired_devices = _desired_devices(vm, tags)
    state = _inspect_container(vm, container.name)
    if state is None:
        _create_container(vm, container, tags, desired_devices)
    else:
        ok, reason = _container_healthy(state, container, desired_devices)
        if not ok:
            raise NeedsRepairError(f"容器 {container.name} 漂移（{reason}），不自动重建")
        output.progress({"step": "container", "status": "already_ready", "name": container.name})
    _inject_pubkey(vm, container.name)
    _wait_sshd(vm, container)
    output.progress({"step": "container", "status": "ready", "name": container.name})


# --------------------------------------------------------------------------
# 内部实现（供测试打桩）
# --------------------------------------------------------------------------

def _check_docker(vm: Endpoint) -> None:
    r = ssh.ssh_run(vm, "docker version --format '{{.Server.Version}}'", timeout_sec=60)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip() if r.stderr else ""
        raise RemotePluginError(
            f"docker 不可用（{vm.user}@{vm.host}）: {err[:300] or 'daemon 未运行或未安装'}"
        )
    output.progress({"step": "container", "status": "docker_ok"})


def _ensure_image(vm: Endpoint, image: str) -> None:
    r = ssh.ssh_run(vm, f"docker image inspect {shlex.quote(image)} >/dev/null 2>&1", timeout_sec=60)
    if r.returncode == 0:
        return
    output.progress({"step": "container", "status": "pulling", "image": image})
    r = ssh.ssh_run(vm, f"docker pull {shlex.quote(image)}", timeout_sec=1800)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip() if r.stderr else ""
        raise RemotePluginError(f"docker pull 失败 {image}: {err[-300:]}")
    output.progress({"step": "container", "status": "pulled", "image": image})


def _desired_devices(vm: Endpoint, tags: dict) -> set[str]:
    """按 tags.chip 计算期望挂载的设备节点集合；非 ascend 芯片返回空集。"""
    chip = str(tags.get("chip") or "")
    if not chip.startswith("ascend-"):
        return set()
    script = (
        "for d in /dev/davinci* /dev/davinci_manager /dev/hisi_hdc /dev/devmm_svm; "
        "do [ -e \"$d\" ] && echo \"$d\"; done"
    )
    r = ssh.ssh_run(vm, script, timeout_sec=60)
    out = r.stdout.decode("utf-8", "replace") if r.stdout else ""
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def _create_container(vm: Endpoint, container: ContainerCfg, tags: dict, devices: set[str]) -> None:
    chip = str(tags.get("chip") or "")
    if chip.startswith("ascend-"):
        flags = "".join(f" --device {shlex.quote(p)}" for p in sorted(devices))
    elif chip.startswith("nvidia-"):
        flags = " --gpus all"
    else:
        flags = ""
    cmd = (
        "docker run -d --name " + shlex.quote(container.name)
        + " --restart unless-stopped"
        + f" -p {container.ssh_port}:22"
        + flags
        + " --entrypoint bash " + shlex.quote(container.image)
        + " -c " + shlex.quote(_CONTAINER_SSHD_CMD)
    )
    output.progress({"step": "container", "status": "creating", "name": container.name})
    r = ssh.ssh_run(vm, cmd, timeout_sec=300)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip() if r.stderr else ""
        raise RemotePluginError(f"docker run 失败（{container.name}）: {err[-300:]}")
    output.progress({"step": "container", "status": "created", "name": container.name})


def _inspect_container(vm: Endpoint, name: str) -> dict | None:
    """docker inspect 单个容器；不存在返回 None。"""
    r = ssh.ssh_run(vm, f"docker inspect {shlex.quote(name)} 2>/dev/null || true", timeout_sec=60)
    out = r.stdout.decode("utf-8", "replace").strip() if r.stdout else ""
    if r.returncode != 0 or not out:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    return data[0]


def _container_healthy(state: dict, container: ContainerCfg, desired_devices: set[str]) -> tuple[bool, str]:
    """健康判定：运行中 + 镜像一致 + 端口映射(ssh_port->22)存在 + 设备挂载不缺失。"""
    st = state.get("State") or {}
    if st.get("Running") is not True:
        return False, "容器未运行"
    cfg = state.get("Config") or {}
    if cfg.get("Image") != container.image:
        return False, f"镜像漂移 {cfg.get('Image')!r} != {container.image!r}"
    hc = state.get("HostConfig") or {}
    host_ports = []
    for b in (hc.get("PortBindings") or {}).get("22/tcp") or []:
        if isinstance(b, dict) and b.get("HostPort"):
            host_ports.append(str(b["HostPort"]))
    if str(container.ssh_port) not in host_ports:
        return False, f"缺少端口映射 {container.ssh_port}->22（现有 {host_ports or '无'}）"
    if desired_devices:
        actual = {
            d.get("PathOnHost") for d in (hc.get("Devices") or [])
            if isinstance(d, dict) and d.get("PathOnHost")
        }
        if not desired_devices.issubset(actual):
            return False, f"设备挂载漂移（期望 {sorted(desired_devices)} 实际 {sorted(actual)}）"
    return True, ""


def _inject_pubkey(vm: Endpoint, name: str) -> None:
    """docker exec 把本地公钥写入容器 root authorized_keys（容器免密）。"""
    key = local_pubkey()
    script = f"docker exec -i {shlex.quote(name)} sh -c {shlex.quote(_AUTH_KEYS_INNER)}"
    last_err = ""
    for _ in range(5):  # 容器刚启动时 exec 可能短暂报 not running，重试几次
        r = ssh.ssh_run(vm, script, timeout_sec=60, input_bytes=(key + "\n").encode("utf-8"))
        if r.returncode == 0:
            return
        last_err = r.stderr.decode("utf-8", "replace").strip() if r.stderr else ""
        time.sleep(1)
    raise RemotePluginError(f"容器公钥注入失败（{name}）: {last_err[-300:]}")


def _wait_sshd(vm: Endpoint, container: ContainerCfg) -> None:
    """等待容器 sshd 在 container.ssh_port 免密可达（BatchMode 探测）。"""
    ep = Endpoint(
        host=vm.host,
        port=container.ssh_port,
        user=vm.user,
        workspace_root=container.workspace_root,
    )
    output.progress({"step": "sshd", "status": "waiting", "port": container.ssh_port})
    last = ""
    for _ in range(20):
        try:
            r = ssh.ssh_run(ep, "true", timeout_sec=15)
            if r.returncode == 0:
                output.progress({"step": "sshd", "status": "ready", "port": container.ssh_port})
                return
            last = r.stderr.decode("utf-8", "replace").strip() if r.stderr else ""
        except ssh.SSHError as e:
            last = str(e)
        time.sleep(2)
    raise RemotePluginError(f"容器 sshd 未就绪（{ep.host}:{ep.port}）: {last[:200]}")


# --------------------------------------------------------------------------
# SSH_ASKPASS + setsid 密码引导
# --------------------------------------------------------------------------

def _make_askpass_script(password: str) -> str:
    """创建临时 askpass 脚本（echo 密码），返回路径；调用方负责删除。"""
    fd, path = tempfile.mkstemp(prefix="rp-askpass-", suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env python3\nimport sys\nsys.stdout.write(")
            f.write(repr(password))
            f.write(")\n")
        os.chmod(path, 0o700)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def _run_ssh(cmd: list[str], env: dict, input_bytes: bytes | None, timeout_sec: int) -> subprocess.CompletedProcess:
    """真正执行 ssh 子进程（独立函数便于测试打桩）。"""
    return subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=timeout_sec, env=env)


def _password_ssh(
    endpoint: Endpoint,
    remote_cmd: str,
    password: str,
    input_bytes: bytes | None = None,
    timeout_sec: int = 120,
) -> subprocess.CompletedProcess:
    """SSH_ASKPASS + setsid 的密码认证 ssh 执行。

    - 临时 askpass 脚本 echo 密码，``SSH_ASKPASS_REQUIRE=force`` 强制走 askpass；
    - ``setsid -w`` 脱离控制终端，ssh 不会读 tty；
    - 用完删除临时脚本；密码不出现在命令行/日志/state。
    """
    askpass = _make_askpass_script(password)
    try:
        ssh_cmd = [
            "ssh",
            *_ASKPASS_SSH_OPTS,
            "-p", str(endpoint.port),
            f"{endpoint.user}@{endpoint.host}",
            remote_cmd,
        ]
        env = dict(os.environ)
        env["SSH_ASKPASS"] = askpass
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env.setdefault("DISPLAY", ":0")
        if shutil.which("setsid"):
            cmd = ["setsid", "-w", *ssh_cmd]
        else:  # OpenSSH >= 8.4 时 SSH_ASKPASS_REQUIRE=force 本身即可
            cmd = ssh_cmd
        try:
            return _run_ssh(cmd, env, input_bytes, timeout_sec)
        except subprocess.TimeoutExpired as e:
            raise ssh.SSHError(
                f"密码引导 ssh 超时（>{timeout_sec}s）: {endpoint.user}@{endpoint.host}:{endpoint.port}"
            ) from e
        except OSError as e:
            raise ssh.SSHError(f"密码引导 ssh 调用失败: {e}") from e
    finally:
        try:
            os.unlink(askpass)
        except OSError:
            pass
