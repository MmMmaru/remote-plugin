"""T2 up / down：免密引导 → 容器生命周期 → 工作区初始化的编排层。

- ``machine_up``：① 免密引导（已免密跳过）→ ② 容器（docker→拉镜像→创建/复用→
  sshd→容器免密→写 state/endpoints）→ ③ 工作区初始化（workspace_root/main、
  .remote-mirrors、core.autocrlf=false、core.eol=lf）。幂等：健康容器复用回
  ``already_ready``；漂移抛 ``NeedsRepairError`` 不自动重建；模式 B 只做免密校验 + ③。
- ``machine_down``：停删受管容器，不动 VM 上其他资源；模式 B 无容器为 noop。
- ``cli_up`` / ``cli_down``：CLI handler，返回 JSON 可序列化 dict。

纯标准库 + 系统 ssh；密码绝不写入 state/、日志或任何持久文件。
"""
from __future__ import annotations

import json
import os
import shlex
import sys

from . import config, output, ssh
from .bootstrap import NeedsRepairError, ensure_container, local_pubkey, push_pubkey
from .config import ConfigError, Endpoint, Machine, RemotePluginError


def machine_up(machine: Machine, password: str | None) -> Endpoint:
    """up 编排：① 免密引导 → ② 容器 → ③ 工作区初始化。返回最终工作面端点。"""
    output.progress({"step": "up", "status": "start", "machine": machine.alias, "mode": machine.mode})
    vm = _vm_endpoint(machine)
    _ensure_passwordless(vm, password)
    if machine.mode == "container":
        if machine.container is None:
            raise ConfigError(f"machine {machine.alias} 缺 container 配置（mode=container）")
        ensure_container(vm, machine.container, machine.tags)
        ep = _container_endpoint(machine)
        _write_endpoint(machine, ep)
    else:
        ep = _ssh_endpoint(machine)
    _init_workspace(ep)
    output.progress({"step": "up", "status": "done", "endpoint": f"{ep.user}@{ep.host}:{ep.port}"})
    return ep


def machine_down(machine: Machine) -> None:
    """down：停删受管容器（docker rm -f），不动 VM 其他资源；并清理本地 endpoint 状态。"""
    output.progress({"step": "down", "status": "start", "machine": machine.alias})
    if machine.mode == "ssh":
        output.progress({"step": "down", "status": "noop", "reason": "模式 B 无受管容器"})
        return
    if machine.container is None:
        raise ConfigError(f"machine {machine.alias} 缺 container 配置（mode=container）")
    vm = _vm_endpoint(machine)
    r = ssh.ssh_run(vm, "docker version --format '{{.Server.Version}}'", timeout_sec=60)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip() if r.stderr else ""
        raise RemotePluginError(f"docker 不可用（{vm.user}@{vm.host}）: {err[:300] or 'daemon 未运行或未安装'}")
    name = machine.container.name
    r = ssh.ssh_run(vm, f"docker inspect {shlex.quote(name)} >/dev/null 2>&1", timeout_sec=60)
    if r.returncode == 0:
        output.progress({"step": "down", "status": "removing", "name": name})
        r = ssh.ssh_run(vm, f"docker rm -f {shlex.quote(name)}", timeout_sec=120)
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace").strip() if r.stderr else ""
            raise RemotePluginError(f"docker rm 失败（{name}）: {err[-300:]}")
        output.progress({"step": "down", "status": "removed", "name": name})
    else:
        output.progress({"step": "down", "status": "not_found", "name": name})
    _remove_endpoint_state(machine)


def cli_up(args) -> dict | None:
    """`remote up <alias> [--password-env NAME | --password-stdin]` handler。"""
    machines = config.load_machines()
    machine = machines.get(args.alias)
    if machine is None:
        raise ConfigError(f"未知 alias: {args.alias}（可用 remote machines 查看）")
    password = _resolve_password(machine, args)
    try:
        ep = machine_up(machine, password)
    except NeedsRepairError as e:
        return {"status": "needs_repair", "machine": args.alias, "reason": str(e)}
    ep_meta: dict = {"host": ep.host, "port": ep.port, "user": ep.user, "workspace_root": ep.workspace_root}
    if ep.container:
        ep_meta["container"] = ep.container
    return {
        "status": "ok",
        "machine": args.alias,
        "mode": machine.mode,
        "endpoint": ep_meta,
    }


def cli_down(args) -> dict | None:
    """`remote down <alias>` handler。"""
    machines = config.load_machines()
    machine = machines.get(args.alias)
    if machine is None:
        raise ConfigError(f"未知 alias: {args.alias}")
    machine_down(machine)
    return {"status": "ok", "machine": args.alias, "mode": machine.mode}


# --------------------------------------------------------------------------
# 内部实现（供测试打桩）
# --------------------------------------------------------------------------

def _vm_endpoint(machine: Machine) -> Endpoint:
    """宿主机端点（docker 操作面）。注意：不能走 resolve_endpoint，否则可能命中容器端点。"""
    return Endpoint(
        host=machine.host,
        port=machine.port,
        user=machine.user,
        workspace_root=machine.workspace_root,
    )


def _ssh_endpoint(machine: Machine) -> Endpoint:
    """模式 B 直接端点。"""
    return Endpoint(
        host=machine.host,
        port=machine.port,
        user=machine.user,
        workspace_root=machine.workspace_root,
    )


def _container_endpoint(machine: Machine) -> Endpoint:
    """模式 A 工作面端点：宿主机 + 容器名（docker exec），workspace_root 取容器内路径。"""
    if machine.container is None:
        raise ConfigError(f"machine {machine.alias} 缺 container 配置（mode=container）")
    return Endpoint(
        host=machine.host,
        port=machine.port,
        user=machine.user,
        workspace_root=machine.effective_workspace_root(),
        container=machine.container.name,
    )


def _ensure_passwordless(endpoint: Endpoint, password: str | None) -> None:
    """① 免密引导：BatchMode 探测，已免密跳过；否则 SSH_ASKPASS+setsid 写公钥。"""
    output.progress({"step": "ssh", "status": "checking", "host": endpoint.host})
    try:
        r = ssh.ssh_run(endpoint, "true", timeout_sec=30)
    except ssh.SSHError:
        r = None
    if r is not None and r.returncode == 0:
        output.progress({"step": "ssh", "status": "already_passwordless"})
        return
    if not password:
        raise RemotePluginError(
            f"无法免密连接 {endpoint.user}@{endpoint.host}:{endpoint.port}；"
            "请提供密码（配置 password 字段 / --password-env / --password-stdin）"
        )
    output.progress({"step": "ssh", "status": "bootstrap", "method": "SSH_ASKPASS+setsid"})
    push_pubkey(endpoint, local_pubkey(), password)
    r = ssh.ssh_run(endpoint, "true", timeout_sec=30)
    if r.returncode != 0:
        raise ssh.SSHError("免密引导后仍无法免密连接（密码错误或 authorized_keys 未生效）")
    output.progress({"step": "ssh", "status": "passwordless_ok"})


def _init_workspace(endpoint: Endpoint) -> None:
    """③ 工作区初始化：workspace_root/main、.remote-mirrors；git 行尾配置（同步前置）。"""
    ws = endpoint.workspace_root
    quoted = shlex.quote(ws)
    script = (
        f"mkdir -p {quoted}/main {quoted}/.remote-mirrors\n"
        "git config --global core.autocrlf false\n"
        "git config --global core.eol lf\n"
    )
    output.progress({"step": "workspace", "status": "init", "workspace_root": ws})
    r = ssh.ssh_run(endpoint, script, timeout_sec=120)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip() if r.stderr else ""
        raise RemotePluginError(f"工作区初始化失败（{ws}）: {err[-300:]}")
    output.progress({"step": "workspace", "status": "ok", "workspace_root": ws})


def _write_endpoint(machine: Machine, ep: Endpoint) -> None:
    """写 state/endpoints/<alias>.json（模式 A 记录宿主机 + 容器名；也是 up 完成标记）。"""
    state = config.state_dir()
    ep_dir = state / "endpoints"
    ep_dir.mkdir(parents=True, exist_ok=True)
    path = ep_dir / f"{machine.alias}.json"
    payload = {
        "host": ep.host,
        "port": ep.port,
        "user": ep.user,
        "workspace_root": ep.workspace_root,
    }
    if ep.container:
        payload["container"] = ep.container
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    output.progress({"step": "endpoint", "status": "written", "file": str(path)})


def _remove_endpoint_state(machine: Machine) -> None:
    """best-effort 清理本地 endpoint 状态（容器已删，状态不应残留）。"""
    state = config.state_dir()
    ep_file = state / "endpoints" / f"{machine.alias}.json"
    if ep_file.is_file():
        ep_file.unlink()
        output.progress({"step": "down", "status": "endpoint_removed", "file": str(ep_file)})


def _resolve_password(machine: Machine, args) -> str | None:
    """密码来源优先级：配置 password 字段 > --password-env > --password-stdin。"""
    if machine.password:
        return machine.password
    env_name = getattr(args, "password_env", None)
    if env_name:
        value = os.environ.get(env_name, "")
        if not value:
            raise ConfigError(f"--password-env 指定的环境变量 {env_name} 未设置")
        return value
    if getattr(args, "password_stdin", False):
        line = sys.stdin.readline()
        if not line:
            raise ConfigError("--password-stdin 但 stdin 无输入")
        return line.rstrip("\r\n")
    return None
