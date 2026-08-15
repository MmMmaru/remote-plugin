"""把插件入口安装为可全局发现的 ``remote`` 命令。"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

from . import config

__all__ = ["InstallError", "InstallResult", "install_launcher", "cli_install"]


class InstallError(config.RemotePluginError):
    """安装入口失败；已有非本插件目标时拒绝覆盖。"""


@dataclass(frozen=True)
class InstallResult:
    """全局入口安装结果。"""

    install_path: Path
    command_name: str
    already_exists: bool

    @property
    def path(self) -> Path:
        """兼容调用方对“安装路径”的简写访问。"""
        return self.install_path

    def to_dict(self) -> dict[str, object]:
        """转为 CLI 可输出的 JSON 对象。"""
        return {
            "status": "ready",
            "install_path": str(self.install_path),
            "command_name": self.command_name,
            "already_exists": self.already_exists,
        }


def _same_launcher(target: Path, source: Path) -> bool:
    """判断已有目标是否是指向同一插件入口的符号链接。"""
    if not target.is_symlink():
        return False
    try:
        return target.resolve(strict=False) == source
    except (OSError, RuntimeError):
        # 损坏的链接或循环链接不能被视为本插件链接，必须 fail closed。
        return False


def _resolve_source(source: Path) -> Path:
    """校验并解析插件入口，避免安装悬空链接。"""
    candidate = Path(source).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InstallError(f"插件入口不存在或无法解析: {candidate}: {exc}") from exc
    if not resolved.is_file():
        raise InstallError(f"插件入口不是普通文件: {candidate}")
    if not resolved.name:
        raise InstallError(f"插件入口缺少命令名: {candidate}")
    return resolved


def install_launcher(
    source: Path,
    bin_dir: Path = Path.home() / ".local/bin",
) -> InstallResult:
    """原子创建全局入口符号链接，并对已有非插件文件拒绝覆盖。

    ``source`` 是插件入口脚本；命令名取其文件名，因此本插件入口 ``remote``
    会安装为 ``<bin_dir>/remote``。符号链接创建使用 ``os.symlink``，该操作
    在目标已出现时以 ``FileExistsError`` 失败，不会覆盖并发创建的文件。
    """
    source_path = _resolve_source(source)
    try:
        install_dir = Path(bin_dir).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise InstallError(f"安装目录无法解析: {bin_dir}: {exc}") from exc

    try:
        install_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InstallError(f"创建安装目录失败 {install_dir}: {exc}") from exc

    command_name = source_path.name
    target = install_dir / command_name
    if os.path.lexists(target):
        if _same_launcher(target, source_path):
            return InstallResult(target, command_name, already_exists=True)
        raise InstallError(f"安装目标已被占用，拒绝覆盖: {target}")

    try:
        # symlink 本身是原子创建且不会覆盖已有路径；竞态由下方分支重新核验。
        os.symlink(str(source_path), str(target))
    except FileExistsError as exc:
        if _same_launcher(target, source_path):
            return InstallResult(target, command_name, already_exists=True)
        raise InstallError(f"安装目标已被占用，拒绝覆盖: {target}") from exc
    except OSError as exc:
        raise InstallError(f"创建符号链接失败 {target} -> {source_path}: {exc}") from exc

    return InstallResult(target, command_name, already_exists=False)


def _plugin_entry() -> Path:
    """返回当前插件仓库内的 ``remote`` 入口脚本。"""
    return Path(__file__).resolve().parent.parent / "remote"


def cli_install(_args: argparse.Namespace) -> dict[str, object]:
    """实现 ``remote install`` 子命令。"""
    return install_launcher(_plugin_entry()).to_dict()
