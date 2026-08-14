"""machines.json 加载与合并、endpoint 解析。纯标准库。"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

_PROJECT_FILENAME = "machines.json"
_USER_CONFIG = Path.home() / ".config" / "remote-plugin" / "machines.json"


class RemotePluginError(Exception):
    """remote-plugin 领域错误基类；CLI 层捕获后转单行 JSON，不打印堆栈。"""


class ConfigError(RemotePluginError):
    """配置加载/校验错误，message 已含定位信息（文件、下标、字段）。"""


@dataclass
class ContainerCfg:
    """模式 A 的容器配置（PRD 2.1）。"""

    image: str = ""
    name: str = ""
    ssh_port: int = 22
    workspace_root: str = "/vllm-workspace"


@dataclass
class Machine:
    """一台可执行远程工作的目标（PRD 2.1）。"""

    alias: str
    mode: str = "container"
    host: str = ""
    port: int = 22
    user: str = "root"
    container: ContainerCfg | None = None
    workspace_root: str = "/vllm-workspace"
    tags: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    password: str | None = None

    def effective_workspace_root(self) -> str:
        """返回实际工作面根路径：模式 A 取容器内 workspace_root。"""
        if self.mode == "container" and self.container is not None:
            return self.container.workspace_root or self.workspace_root
        return self.workspace_root


@dataclass
class Endpoint:
    """解析出的 SSH 端点（PRD：resolve_endpoint 输出）。"""

    host: str
    port: int
    user: str
    workspace_root: str


def find_remote_dir(start_dir: Path | None = None) -> Path | None:
    """从 start_dir（缺省 cwd）向上查找最近的 `.remote` 目录；无则 None。"""
    cur = (start_dir or Path.cwd()).resolve()
    for d in [cur, *cur.parents]:
        candidate = d / ".remote"
        if candidate.is_dir():
            return candidate
    return None


def state_dir(start_dir: Path | None = None) -> Path:
    """解析状态目录 `<repo>/.remote/state`，无项目级则用用户级；不存在则创建。"""
    remote_dir = find_remote_dir(start_dir)
    if remote_dir is not None:
        path = remote_dir / "state"
    else:
        path = Path.home() / ".config" / "remote-plugin" / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _project_machines_paths(start_dir: Path | None = None) -> Iterator[Path]:
    """从最远到最近（就近优先覆盖）枚举项目级 machines.json。"""
    cur = (start_dir or Path.cwd()).resolve()
    for d in reversed([cur, *cur.parents]):
        candidate = d / ".remote" / _PROJECT_FILENAME
        if candidate.is_file():
            yield candidate


def load_machines(start_dir: Path | None = None) -> dict[str, Machine]:
    """加载并合并机器注册：项目级（就近优先）覆盖用户级，冲突时 stderr 告警。"""
    ordered: list[tuple[Path, str]] = []
    if _USER_CONFIG.is_file():
        ordered.append((_USER_CONFIG, "user"))
    for path in _project_machines_paths(start_dir):
        ordered.append((path, "project"))

    machines: dict[str, Machine] = {}
    for path, label in ordered:
        for m in _parse_file(path, label):
            if m.alias in machines:
                sys.stderr.write(
                    f"warning: alias '{m.alias}' 冲突，{label} 级({path}) 覆盖旧定义\n"
                )
            machines[m.alias] = m

    for path, label in ordered:
        if label == "project":
            _warn_password_tracked(path)
    return machines


def _parse_file(path: Path, label: str) -> list[Machine]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"读取 {label} 级配置失败 {path}: {e}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"{label} 级配置 JSON 非法 {path}:{e.lineno}:{e.colno}: {e.msg}"
        ) from e
    if not isinstance(data, list):
        raise ConfigError(f"{label} 级配置 {path} 顶层必须是数组")
    return [_parse_machine(item, idx, path, label) for idx, item in enumerate(data)]


def _parse_machine(item: Any, idx: int, path: Path, label: str) -> Machine:
    where = f"{label} 级配置 {path} 第 {idx} 个元素"
    if not isinstance(item, dict):
        raise ConfigError(f"{where} 必须是对象")
    alias = _require_str(item, "alias", where)
    mode = item.get("mode", "container")
    if mode not in ("container", "ssh"):
        raise ConfigError(f"{where}.mode 必须是 container|ssh，实际 {mode!r}")
    host = _require_str(item, "host", where)
    port = _require_int(item, "port", 22, where)
    user = item.get("user", "root")
    if not isinstance(user, str) or user == "":
        raise ConfigError(f"{where}.user 必须是非空字符串")

    container: ContainerCfg | None = None
    if mode == "container":
        c = item.get("container")
        if not isinstance(c, dict):
            raise ConfigError(f"{where}.container 缺失或非对象（mode=container 必填）")
        container = ContainerCfg(
            image=c.get("image", ""),
            name=c.get("name", ""),
            ssh_port=_require_int(c, "ssh_port", 22, f"{where}.container"),
            workspace_root=c.get("workspace_root", "/vllm-workspace"),
        )

    workspace_root = item.get("workspace_root", "/vllm-workspace")
    if not isinstance(workspace_root, str) or workspace_root == "":
        raise ConfigError(f"{where}.workspace_root 必须是非空字符串")

    tags = item.get("tags", {})
    if not isinstance(tags, dict):
        raise ConfigError(f"{where}.tags 必须是对象")

    password = item.get("password")
    if password is not None and not isinstance(password, str):
        raise ConfigError(f"{where}.password 必须是字符串")

    return Machine(
        alias=alias,
        mode=mode,
        host=host,
        port=port,
        user=user,
        container=container,
        workspace_root=workspace_root,
        tags=tags,
        note=item.get("note", ""),
        password=password,
    )


def _require_str(obj: dict, key: str, where: str) -> str:
    val = obj.get(key)
    if not isinstance(val, str) or val == "":
        raise ConfigError(f"{where}.{key} 必填且为非空字符串")
    return val


def _require_int(obj: dict, key: str, default: int, where: str) -> int:
    val = obj.get(key, default)
    if isinstance(val, bool) or not isinstance(val, int):
        raise ConfigError(f"{where}.{key} 必须是整数")
    return val


def _warn_password_tracked(path: Path) -> None:
    """best-effort：项目级文件含 password 且被 git 跟踪时 stderr 告警。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    if '"password"' not in text:
        return
    repo_dir = path.parent.parent  # `.remote` 上一级即仓库根
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "ls-files", "--error-unmatch", "--", str(path)],
            capture_output=True,
            text=True,
        )
    except Exception:
        return
    if result.returncode == 0:
        sys.stderr.write(
            f"warning: {path} 含 password 字段且已被 git 跟踪，请移除该字段或加入 .gitignore\n"
        )


def resolve_endpoint(machine: Machine, state_dir: Path) -> Endpoint:
    """解析机器 SSH 端点。模式 A 优先读 state/endpoints/<alias>.json，缺失回退宿主机。"""
    if machine.mode == "container":
        endpoint_file = Path(state_dir) / "endpoints" / f"{machine.alias}.json"
        if endpoint_file.is_file():
            try:
                data = json.loads(endpoint_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                raise ConfigError(f"endpoint 状态损坏 {endpoint_file}: {e}") from e
            return Endpoint(
                host=data.get("host", machine.host),
                port=data.get("port", machine.port),
                user=data.get("user", machine.user),
                workspace_root=data.get(
                    "workspace_root", machine.effective_workspace_root()
                ),
            )
        return Endpoint(
            host=machine.host,
            port=machine.port,
            user=machine.user,
            workspace_root=machine.effective_workspace_root(),
        )
    return Endpoint(
        host=machine.host,
        port=machine.port,
        user=machine.user,
        workspace_root=machine.workspace_root,
    )
