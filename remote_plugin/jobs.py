"""T3：Job 记录、任务列表（含 stale reconcile）、日志查询、停止。

纯标准库 + 系统 ssh。日志全文落盘本地 ``state/jobs/<job_id>/``；
``jobs()`` 是任务列表与详情的唯一查询入口（PRD 2.2/2.4/5.2）。
"""
from __future__ import annotations

import datetime
import getpass
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config as _config
from . import output as _output
from . import ssh as _ssh

_JOB_ID_RE = re.compile(r"j-\d{8}-\d{6}-\d{2}")

RECONCILE_TIMEOUT_SEC = 20
STOP_TIMEOUT_SEC = 60

_SESSION_ENV_KEYS = ("CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "SESSION_ID")

#: 可注入时钟（测试用）：new_job_id 同秒序号递增依赖它。
_clock = datetime.datetime.now
_last_ts: str = ""
_last_seq: int = 0


@dataclass
class Job:
    """一条远程任务记录（PRD 2.4；remote_pid/remote_log_dir 为内部辅助字段）。"""

    job_id: str = ""
    machine: str = ""
    cards: list[int] | None = None
    owner: str = ""
    task: str | None = None
    command: str = ""
    cwd: str = ""
    status: str = "running"
    exit_code: int | None = None
    started_at: str = ""
    finished_at: str | None = None
    stdout_log: str = ""
    stderr_log: str = ""
    remote_pid: int | None = None
    remote_log_dir: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def now_iso() -> str:
    """当前时间 ISO 格式（含时区）。"""
    return _clock().astimezone().isoformat(timespec="seconds")


def new_job_id() -> str:
    """``j-<yyyyMMdd>-<HHmmss>-<两位序号>``；同秒序号递增，可读可排序。"""
    global _last_ts, _last_seq
    ts = _clock().strftime("%Y%m%d-%H%M%S")
    if ts == _last_ts:
        _last_seq += 1
        if _last_seq > 99:
            _last_seq = 1
    else:
        _last_ts = ts
        _last_seq = 1
    return f"j-{ts}-{_last_seq:02d}"


def default_owner() -> str:
    """owner：session 环境变量优先（agent-<session-id>），缺省本地用户名。"""
    for key in _SESSION_ENV_KEYS:
        val = os.environ.get(key)
        if val:
            return f"agent-{val}"
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


# ---- 存储 ----

def jobs_dir() -> Path:
    d = _config.state_dir() / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def job_dir(job_id: str) -> Path:
    return _config.state_dir() / "jobs" / job_id


def _meta_path(job_id: str) -> Path:
    return job_dir(job_id) / "meta.json"


def save_job(job: Job) -> None:
    """写 meta.json（先写临时文件再原子替换）。"""
    d = job_dir(job.job_id)
    d.mkdir(parents=True, exist_ok=True)
    tmp = _meta_path(job.job_id).with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(job.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, _meta_path(job.job_id))


def load_job(job_id: str) -> Job:
    path = _meta_path(job_id)
    if not path.is_file():
        raise _config.RemotePluginError(f"job 不存在: {job_id}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise _config.RemotePluginError(f"job 记录损坏 {path}: {e}") from e
    if not isinstance(data, dict):
        raise _config.RemotePluginError(f"job 记录损坏 {path}: 顶层必须是对象")
    fields = {k: v for k, v in data.items() if k in Job.__dataclass_fields__}
    return Job(**fields)


def _iter_jobs() -> list[Job]:
    """按 job_id 降序（新→旧）扫描本地全部任务记录。"""
    base = _config.state_dir() / "jobs"
    if not base.is_dir():
        return []
    items: list[Job] = []
    for d in sorted(base.iterdir(), reverse=True):
        if not d.is_dir() or not (d / "meta.json").is_file():
            continue
        try:
            items.append(load_job(d.name))
        except _config.RemotePluginError as e:
            _output.progress({"warning": f"跳过损坏的 job 记录 {d.name}: {e}"})
    return items


# ---- stale reconcile ----

def _aliveness_script(pids: list[int]) -> str:
    lines = ["set -u"]
    for p in sorted({int(x) for x in pids if x}):
        lines.append(
            f"if kill -0 {p} 2>/dev/null; then printf 'alive {p}\\n'; "
            f"else printf 'dead {p}\\n'; fi"
        )
    return "\n".join(lines) + "\n"


def _parse_alive(stdout: bytes) -> set[int]:
    alive: set[int] = set()
    for line in stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "alive":
            try:
                alive.add(int(parts[1]))
            except ValueError:
                pass
    return alive


def _mark_stale(jobs: list[Job], reason: str) -> None:
    finished = now_iso()
    for j in jobs:
        j.status = "stale"
        j.finished_at = j.finished_at or finished
        save_job(j)
        _output.progress({"reconcile": f"{j.job_id} -> stale（{reason}）"})


def _reconcile(jobs: list[Job]) -> None:
    """本地 running 但远端进程消失/机器不可达 → 标记 stale（PRD 2.2）。"""
    running = [j for j in jobs if j.status == "running"]
    if not running:
        return
    by_machine: dict[str, list[Job]] = {}
    for j in running:
        by_machine.setdefault(j.machine, []).append(j)
    machines = _config.load_machines()
    for alias, js in by_machine.items():
        machine = machines.get(alias)
        if machine is None:
            _mark_stale(js, "机器配置不存在")
            continue
        try:
            endpoint = _config.resolve_endpoint(machine, _config.state_dir())
        except _config.ConfigError as e:
            _mark_stale(js, f"端点解析失败: {e}")
            continue
        pids = [j.remote_pid for j in js if j.remote_pid]
        if not pids:
            continue  # 无 pid 的 running job 无法核实，保持 running
        try:
            cp = _ssh.ssh_run(
                endpoint, _aliveness_script(pids), timeout_sec=RECONCILE_TIMEOUT_SEC
            )
        except _ssh.SSHError:
            _mark_stale(js, "机器不可达")
            continue
        alive = _parse_alive(cp.stdout)
        for j in js:
            if j.remote_pid is not None and j.remote_pid not in alive:
                _mark_stale([j], "远端进程不存在")


def jobs(machine: str | None = None) -> list[Job]:
    """唯一查询入口：列出任务（可按 machine 过滤），并 reconcile stale。"""
    items = _iter_jobs()
    _reconcile(items)
    if machine is not None:
        items = [j for j in items if j.machine == machine]
    return items


def running_jobs(machine: str) -> list[Job]:
    """某机器当前 running 的 Job（占用提示用，advisory，不 SSH）。"""
    return [j for j in _iter_jobs() if j.machine == machine and j.status == "running"]


# ---- 日志 ----

def job_tail(job_id: str, tail: int, stream: str) -> str:
    """读本地日志（不 SSH）；stream 为 ``stdout``|``stderr``，返回最后 tail 行。"""
    load_job(job_id)
    if stream not in ("stdout", "stderr"):
        raise _config.RemotePluginError(f"stream 必须是 stdout|stderr，实际 {stream!r}")
    path = job_dir(job_id) / f"{stream}.log"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if tail and tail > 0:
        lines = text.splitlines()
        text = "\n".join(lines[-tail:])
    return text


# ---- 停止 ----

def _stop_script(pid: int, log_dir: str) -> str:
    lines = [
        "set -u",
        f"PID={int(pid)}",
        'kill -TERM -- -"$PID" 2>/dev/null || kill -TERM "$PID" 2>/dev/null',
        "sleep 2",
        'if kill -0 "$PID" 2>/dev/null; then kill -KILL -- -"$PID" 2>/dev/null; fi',
    ]
    if log_dir:
        q = shlex.quote(log_dir)
        lines += [
            f"printf 'stopped\\n' > {q}/status 2>/dev/null || true",
            f"printf 'done\\n' > {q}/done 2>/dev/null || true",
        ]
    return "\n".join(lines) + "\n"


def job_stop(job_id: str) -> Job:
    """远端杀进程组 → stopped（PRD 5.2）。已 stopped 幂等返回。"""
    job = load_job(job_id)
    if job.status == "stopped":
        return job
    if job.status != "running":
        raise _config.RemotePluginError(
            f"job {job_id} 当前状态 {job.status}，无法 stop"
        )
    if job.remote_pid is None:
        raise _config.RemotePluginError(f"job {job_id} 无远端进程信息，无法 stop")
    machine = _config.load_machines().get(job.machine)
    if machine is None:
        raise _config.RemotePluginError(
            f"无法解析 job {job_id} 的机器 {job.machine!r}（配置缺失）"
        )
    endpoint = _config.resolve_endpoint(machine, _config.state_dir())
    _ssh.ssh_run(endpoint, _stop_script(job.remote_pid, job.remote_log_dir),
                 timeout_sec=STOP_TIMEOUT_SEC)
    job.status = "stopped"
    job.finished_at = now_iso()
    save_job(job)
    return job


# ---- 实时跟随（--follow 才 SSH）----

def ssh_argv(endpoint: _config.Endpoint, remote_cmd: str) -> list[str]:
    """构造 ssh 命令，复用 ssh.py 的统一构造（含 BatchMode/KEX/docker exec 包装）。"""
    return _ssh.ssh_argv(endpoint, remote_cmd)


def _follow_remote(job: Job, stream: str) -> None:
    machine = _config.load_machines().get(job.machine)
    if machine is None:
        raise _config.RemotePluginError(
            f"无法解析 job {job.job_id} 的机器 {job.machine!r}（配置缺失）"
        )
    endpoint = _config.resolve_endpoint(machine, _config.state_dir())
    remote_log = shlex.quote(f"{job.remote_log_dir}/{stream}.log")
    done = shlex.quote(f"{job.remote_log_dir}/done")
    script = (
        f"tail -F -n +1 {remote_log} &\n"
        "T=$!\n"
        f"while [ ! -e {done} ]; do sleep 1; done\n"
        'kill "$T" 2>/dev/null\n'
        'wait "$T" 2>/dev/null\n'
        "exit 0\n"
    )
    subprocess.run(ssh_argv(endpoint, script))


def _follow_local(job_id: str, stream: str) -> None:
    path = job_dir(job_id) / f"{stream}.log"
    pos = 0
    while True:
        data = path.read_bytes() if path.is_file() else b""
        if len(data) > pos:
            sys.stdout.buffer.write(data[pos:])
            sys.stdout.buffer.flush()
            pos = len(data)
        try:
            cur = load_job(job_id)
        except _config.RemotePluginError:
            break
        if cur.status != "running":
            break
        time.sleep(0.5)


def _follow(job: Job, stream: str) -> None:
    """--follow：running 且有远端日志 → SSH 实时跟；否则本地轮询（等结束）。"""
    if job.remote_log_dir and job.status == "running":
        _follow_remote(job, stream)
    else:
        _follow_local(job.job_id, stream)


# ---- CLI handlers ----

def cli_jobs(args) -> dict:
    return {"jobs": [j.as_dict() for j in jobs(args.machine)]}


def cli_logs(args) -> dict | None:
    job = load_job(args.job_id)
    stream = "stderr" if args.stderr else "stdout"
    if args.follow:
        _follow(job, stream)
        return None
    text = job_tail(args.job_id, args.tail, stream)
    return {
        "job_id": job.job_id,
        "status": job.status,
        "stream": stream,
        "tail": args.tail,
        "log": text,
    }


def cli_stop(args) -> dict:
    return job_stop(args.job_id).as_dict()
