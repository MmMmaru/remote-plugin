"""T3：远程执行（run）。

纯标准库 + 系统 ssh。默认 cwd=workspace_root；前台返回截断预览
（head/tail 各 4000 字符）+ exit_code + 日志路径；``--background`` 立即返回。
日志全文落盘本地 ``state/jobs/<job_id>/``，后台任务由常驻 streamer 持续同步。
"""
from __future__ import annotations

import base64
import shlex
import subprocess
from pathlib import Path

from . import config as _config
from . import jobs as _jobs
from . import output as _output
from . import ssh as _ssh

PREVIEW_LIMIT = 4000
QUICK_SSH_TIMEOUT_SEC = 60
CLEANUP_TIMEOUT_SEC = 30
DEFAULT_TIMEOUT_SEC = 600

#: 远端日志暂存目录（workspace_root 下；PRD 7 的 state/ 在本地）
_REMOTE_LOG_ROOT = ".remote-logs"


def preview_text(text: str, limit: int = PREVIEW_LIMIT) -> dict:
    """截断预览（纯函数）：head/tail 各 limit 字符，超长标 truncated。"""
    if len(text) <= limit:
        return {"head": text, "tail": "", "truncated": False}
    return {"head": text[:limit], "tail": text[-limit:], "truncated": True}


def _cards_from_env(env: dict) -> list[int] | None:
    """cards 缺省取 --env 里的 ASCEND_RT_VISIBLE_DEVICES（PRD 2.4）。"""
    raw = env.get("ASCEND_RT_VISIBLE_DEVICES")
    if not raw:
        return None
    try:
        cards = [int(x) for x in str(raw).split(",") if x.strip() != ""]
    except ValueError:
        return None
    return cards or None


def _launcher(command: str, cwd: str, env: dict, log_dir: str,
              timeout_sec: int, background: bool) -> str:
    """生成远端 launcher 脚本：命令写盘→cd→env→set -m 后台起→PID 标记。

    - 前台：命令输出走 SSH 会话（本地 capture），``wait`` 传播退出码。
    - 后台：输出重定向到远端日志文件，waiter 负责超时杀进程组并写 done 标记。
    """
    b64 = base64.b64encode(command.encode("utf-8")).decode("ascii")
    q_log = shlex.quote(log_dir)
    q_cwd = shlex.quote(cwd)
    lines = [
        "set -u",
        f"LOG_DIR={q_log}",
        'mkdir -p "$LOG_DIR" || exit 92',
        f"printf '%s\\n' {b64} | base64 -d > \"$LOG_DIR/cmd.sh\" || exit 93",
        f"mkdir -p -- {q_cwd} || exit 94",
        f"cd {q_cwd} || exit 95",
    ]
    for k, v in env.items():
        lines.append(f"export {shlex.quote(str(k))}={shlex.quote(str(v))}")
    if background:
        lines += [': > "$LOG_DIR/stdout.log"', ': > "$LOG_DIR/stderr.log"']
    if background:
        run_line = 'bash "$LOG_DIR/cmd.sh" >>"$LOG_DIR/stdout.log" 2>>"$LOG_DIR/stderr.log" < /dev/null &'
    else:
        run_line = 'bash "$LOG_DIR/cmd.sh" < /dev/null &'
    lines += [
        "set -m",
        run_line,
        "PID=$!",
        'printf \'%s\\n\' "$PID" > "$LOG_DIR/pid"',
        'printf \'__RP_PID__=%s\\n\' "$PID"',
    ]
    if background:
        lines += [
            "(",
            "  ELAPSED=0",
            '  while kill -0 "$PID" 2>/dev/null; do',
            f'    if [ "$ELAPSED" -ge {int(timeout_sec)} ]; then',
            '      kill -TERM -- -"$PID" 2>/dev/null',
            "      sleep 2",
            '      kill -KILL -- -"$PID" 2>/dev/null',
            '      printf \'timeout\\n\' > "$LOG_DIR/status"',
            "      break",
            "    fi",
            "    sleep 1",
            "    ELAPSED=$((ELAPSED+1))",
            "  done",
            '  printf \'done\\n\' > "$LOG_DIR/done"',
            ") &",
        ]
    else:
        lines += [
            'wait "$PID"',
            'RC=$?',
            'printf \'done\\n\' > "$LOG_DIR/done"',
            'exit "$RC"',
        ]
    return "\n".join(lines) + "\n"


def _split_marker(stdout: bytes, stderr: bytes) -> tuple[bytes, bytes, int | None]:
    """剥离 launcher 首行 PID 标记（``__RP_PID__=N``），返回 (out, err, pid)。"""
    pid: int | None = None
    out = stdout
    nl = stdout.find(b"\n")
    if nl >= 0:
        first = stdout[:nl].decode("utf-8", "replace")
        if first.startswith("__RP_PID__="):
            try:
                pid = int(first.split("=", 1)[1])
            except ValueError:
                pid = None
            out = stdout[nl + 1:]
    return out, stderr, pid


def _extract_pid(stdout: bytes) -> int | None:
    for line in stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("__RP_PID__="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _write_logs(job: _jobs.Job, stdout: bytes, stderr: bytes) -> None:
    d = _config.state_dir() / "jobs" / job.job_id
    (d / "stdout.log").write_bytes(stdout)
    (d / "stderr.log").write_bytes(stderr)


def _set_previews(job: _jobs.Job, stdout_text: str, stderr_text: str) -> None:
    job.stdout_preview = preview_text(stdout_text)
    job.stderr_preview = preview_text(stderr_text)


def _cleanup_remote(endpoint: _config.Endpoint, log_dir: str) -> None:
    """超时/失败时 best-effort 杀掉远端进程组（读 pid 文件，避免泄漏）。"""
    q = shlex.quote(log_dir)
    script = (
        f'PID=$(cat {q}/pid 2>/dev/null || true)\n'
        'if [ -n "$PID" ]; then '
        'kill -TERM -- -"$PID" 2>/dev/null || kill -TERM "$PID" 2>/dev/null; '
        'sleep 1; kill -KILL -- -"$PID" 2>/dev/null; fi\n'
        f"printf 'done\\n' > {q}/done 2>/dev/null || true\n"
    )
    try:
        _ssh.ssh_run(endpoint, script, timeout_sec=CLEANUP_TIMEOUT_SEC)
    except _ssh.SSHError:
        pass


def _run_foreground(job: _jobs.Job, endpoint: _config.Endpoint, env: dict,
                    log_dir: str, timeout_sec: int) -> None:
    script = _launcher(job.command, job.cwd, env, log_dir, timeout_sec,
                       background=False)
    try:
        cp = _ssh.ssh_run(endpoint, script, timeout_sec=timeout_sec)
    except _ssh.SSHError as e:
        _cleanup_remote(endpoint, log_dir)
        job.status = "timeout" if "超时" in str(e) else "failed"
        job.exit_code = 124 if job.status == "timeout" else None
        job.finished_at = _jobs.now_iso()
        _jobs.save_job(job)
        _write_logs(job, b"", b"")
        _set_previews(job, "", "")
        return
    out, err, pid = _split_marker(cp.stdout, cp.stderr)
    job.remote_pid = pid
    job.exit_code = cp.returncode
    job.status = "done" if cp.returncode == 0 else "failed"
    job.finished_at = _jobs.now_iso()
    _write_logs(job, out, err)
    _set_previews(job, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
    _jobs.save_job(job)


def _spawn_streamer(endpoint: _config.Endpoint, remote_log: str, done_file: str,
                    local_path: Path) -> None:
    """常驻子进程：tail 远端日志直到 done 标记，全文同步到本地日志文件。"""
    script = (
        f"tail -F -n +1 {shlex.quote(remote_log)} &\n"
        "T=$!\n"
        f"while [ ! -e {shlex.quote(done_file)} ]; do sleep 1; done\n"
        'kill "$T" 2>/dev/null\n'
        'wait "$T" 2>/dev/null\n'
        "exit 0\n"
    )
    fh = local_path.open("wb")
    try:
        subprocess.Popen(
            _jobs.ssh_argv(endpoint, script),
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as e:
        _output.progress({"warning": f"无法启动日志 streamer: {e}"})
    finally:
        fh.close()


def _start_background(job: _jobs.Job, endpoint: _config.Endpoint, env: dict,
                      log_dir: str, timeout_sec: int) -> None:
    script = _launcher(job.command, job.cwd, env, log_dir, timeout_sec,
                       background=True)
    try:
        cp = _ssh.ssh_run(endpoint, script, timeout_sec=QUICK_SSH_TIMEOUT_SEC)
    except _ssh.SSHError as e:
        job.status = "failed"
        job.exit_code = None
        job.finished_at = _jobs.now_iso()
        _jobs.save_job(job)
        _output.progress({"run": f"后台启动失败: {e}"})
        return
    pid = _extract_pid(cp.stdout)
    if cp.returncode != 0 or pid is None:
        job.status = "failed"
        job.exit_code = cp.returncode or None
        job.finished_at = _jobs.now_iso()
        _jobs.save_job(job)
        return
    job.remote_pid = pid
    job.remote_log_dir = log_dir
    _jobs.save_job(job)
    base = _config.state_dir() / "jobs" / job.job_id
    _spawn_streamer(endpoint, f"{log_dir}/stdout.log", f"{log_dir}/done",
                    base / "stdout.log")
    _spawn_streamer(endpoint, f"{log_dir}/stderr.log", f"{log_dir}/done",
                    base / "stderr.log")


def _fresh_job_id() -> str:
    sd = _config.state_dir()
    while True:
        jid = _jobs.new_job_id()
        if not (sd / "jobs" / jid).exists():
            return jid


def _occupancy(j: _jobs.Job) -> dict:
    return {
        "job_id": j.job_id,
        "owner": j.owner,
        "task": j.task,
        "cards": j.cards,
        "started_at": j.started_at,
    }


def run_remote(machine: _config.Machine, command: str = "",
               cwd: str | None = None, env: dict | None = None,
               cards: list[int] | None = None, task: str | None = None,
               timeout_sec: int = DEFAULT_TIMEOUT_SEC,
               background: bool = False) -> _jobs.Job:
    """远程执行命令（PRD 5.1）。

    默认 cwd=workspace_root；stdout/stderr 落盘 ``state/jobs/<job_id>/``；
    前台返回截断预览 + exit_code + 日志路径，``--background`` 立即返回。
    返回的 Job 附 ``stdout_preview``/``stderr_preview``/``running``（占用提示，advisory）。
    """
    env = dict(env or {})
    if cards is None:
        cards = _cards_from_env(env)
    ws = machine.effective_workspace_root()
    cwd = ws if cwd is None else cwd
    job_id = _fresh_job_id()
    job = _jobs.Job(
        job_id=job_id,
        machine=machine.alias,
        cards=cards,
        owner=_jobs.default_owner(),
        task=task,
        command=command,
        cwd=cwd,
        status="running",
        started_at=_jobs.now_iso(),
        stdout_log=f"state/jobs/{job_id}/stdout.log",
        stderr_log=f"state/jobs/{job_id}/stderr.log",
    )
    _jobs.save_job(job)
    endpoint = _config.resolve_endpoint(machine, _config.state_dir())
    log_dir = f"{ws.rstrip('/')}/{_REMOTE_LOG_ROOT}/{job_id}"
    if background:
        _start_background(job, endpoint, env, log_dir, timeout_sec)
    else:
        _run_foreground(job, endpoint, env, log_dir, timeout_sec)
    job.running = [_occupancy(j) for j in _jobs.running_jobs(machine.alias)]
    return job


def cli_run(args) -> dict:
    machines = _config.load_machines()
    machine = machines.get(args.alias)
    if machine is None:
        raise _config.ConfigError(
            f"未知机器 alias: {args.alias!r}（可用: {', '.join(sorted(machines)) or '无'}）"
        )
    job = run_remote(machine, args.cmd, args.cwd, args.env,
                     args.cards, args.task, args.timeout, args.background)
    payload = job.as_dict()
    payload["stdout_preview"] = getattr(job, "stdout_preview",
                                        {"head": "", "tail": "", "truncated": False})
    payload["stderr_preview"] = getattr(job, "stderr_preview",
                                        {"head": "", "tail": "", "truncated": False})
    payload["running"] = getattr(job, "running", [])
    return payload
