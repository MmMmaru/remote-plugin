"""argparse 分发：子命令分发表预定义，惰性 import。"""
from __future__ import annotations

import argparse
import importlib
import sys
from typing import Any

from . import output
from .config import RemotePluginError

# 子命令分发表：subcommand -> (module, function)。惰性 import。
# 约定：handler 形如 `def cli_xxx(args: argparse.Namespace) -> dict | None`，
# 内部自行调用 config.load_machines() 等内核函数，返回 JSON 可序列化结果；
# 出错抛 RemotePluginError。`sync` 的 handler 在 sync_git.py 中，按 `--paths`
# 是否为空在方法 A（git 递归）与方法 B（指定路径）间分发。
COMMANDS: dict[str, tuple[str, str]] = {
    "install": ("remote_plugin.install", "cli_install"),
    "verify": ("remote_plugin.machines", "cli_verify"),
    "machines": ("remote_plugin.machines", "cli_machines"),
    "status": ("remote_plugin.machines", "cli_status"),
    "up": ("remote_plugin.updown", "cli_up"),
    "down": ("remote_plugin.updown", "cli_down"),
    "run": ("remote_plugin.runner", "cli_run"),
    "jobs": ("remote_plugin.jobs", "cli_jobs"),
    "logs": ("remote_plugin.jobs", "cli_logs"),
    "stop": ("remote_plugin.jobs", "cli_stop"),
    "sync": ("remote_plugin.sync_git", "cli_sync"),
    "pull": ("remote_plugin.pull", "cli_pull"),
}


def _parse_cards(value: str) -> list[int]:
    try:
        cards = [int(x.strip()) for x in value.split(",") if x.strip() != ""]
    except ValueError:
        raise argparse.ArgumentTypeError(f"cards 必须是逗号分隔整数: {value!r}")
    if not cards:
        raise argparse.ArgumentTypeError("cards 不能为空")
    return cards


def _parse_env(pairs: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise RemotePluginError(f"--env 必须是 K=V 形式: {pair!r}")
        k, v = pair.split("=", 1)
        if not k:
            raise RemotePluginError(f"--env 键不能为空: {pair!r}")
        env[k] = v
    return env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="remote",
        description="remote-plugin：CLI-only 远程开发插件",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("install", help="将 remote 入口原子安装到 ~/.local/bin")

    p = sub.add_parser("verify", help="验证单台机器并写结构化 facts（不覆盖 Markdown）")
    p.add_argument("alias")

    p = sub.add_parser("machines", help="列出所有机器一览（--probe 并发实时探测）")
    p.add_argument(
        "--probe", action="store_true",
        help="并发实时 SSH 探测所有机器（负载/内存/CPU/NPU 每卡利用率）",
    )

    p = sub.add_parser("status", help="单机详情")
    p.add_argument("alias")
    p.add_argument("--probe", action="store_true", help="实时 SSH 查负载与 NPU 利用率")

    p = sub.add_parser("up", help="容器生命周期 + 免密引导 + 工作区初始化")
    p.add_argument("alias")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--password-env", metavar="NAME", help="从环境变量 NAME 取密码")
    g.add_argument("--password-stdin", action="store_true", help="从 stdin 读密码")

    p = sub.add_parser("down", help="停止并移除受管容器")
    p.add_argument("alias")

    p = sub.add_parser("run", help="远程执行命令")
    p.add_argument("alias")
    p.add_argument("--cmd", required=True, help="要执行的命令")
    p.add_argument("--cwd", default=None)
    p.add_argument("--env", action="append", metavar="K=V", default=[], help="环境变量，可重复")
    p.add_argument("--cards", type=_parse_cards, default=None, help="占用卡号，如 0,1")
    p.add_argument("--task", default=None, help="任务描述")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--background", action="store_true")
    p.add_argument(
        "--logs", choices=("none", "tail", "full"), default=None,
        help="日志保留策略（默认：前台 none、后台 full）。"
             "none=不落盘、不记录 job（前台默认）；tail=只留合并日志最后 200 行"
             "（tail.log）；full=合并日志全量保留（full.log，后台默认）",
    )

    p = sub.add_parser("jobs", help="任务列表")
    p.add_argument("--machine", default=None)

    p = sub.add_parser("logs", help="查询任务日志")
    p.add_argument("job_id")
    p.add_argument("--tail", type=int, default=200)
    p.add_argument("--stderr", action="store_true",
                   help="已废弃：日志为 stdout/stderr 合并保存，此参数被忽略")
    p.add_argument("--follow", action="store_true")

    p = sub.add_parser("stop", help="停止任务")
    p.add_argument("job_id")

    p = sub.add_parser("sync", help="同步整个 workspace（--paths 指定路径）")
    p.add_argument("alias")
    p.add_argument("--paths", nargs="*", default=None, help="指定文件/路径（方法 B）")

    p = sub.add_parser("pull", help="从远端拉回文件/目录到本地（产物下载）")
    p.add_argument("alias")
    p.add_argument("remote_paths", nargs="+", help="远端文件/目录（相对 workspace_root 或容器内绝对路径）")
    p.add_argument("--dest", required=True, help="本地落点目录")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        module_name, func_name = COMMANDS[args.command]
        if hasattr(args, "env") and getattr(args, "env", None):
            args.env = _parse_env(args.env)
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            raise RemotePluginError(f"子命令 '{args.command}' 尚未实现")
        handler = getattr(module, func_name, None)
        if handler is None:
            raise RemotePluginError(f"子命令 '{args.command}' 尚未实现")
        result = handler(args)
        if result is not None:
            output.emit(result)
        return 0
    except RemotePluginError as e:
        output.emit({"status": "error", "error": str(e)})
        return 1
    except Exception as e:  # 错误无堆栈
        output.emit({"status": "error", "error": f"{type(e).__name__}: {e}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
