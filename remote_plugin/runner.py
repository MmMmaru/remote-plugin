"""T3：远程执行（run）。

纯标准库 + 系统 ssh。默认 cwd=workspace_root；前台返回截断预览 + exit_code，
``--background`` 立即返回。日志保留策略由 ``--logs {none|tail|full}`` 控制：

- ``none``（前台默认）：不落盘、不记录 job；预览在返回 JSON 中即时给出。
- ``tail``：只保留合并日志（stdout+stderr）最后 200 行，落盘 ``tail.log``。
- ``full``（后台默认）：合并日志全量落盘 ``full.log``。

远端日志统一写合并文件 ``<workspace_root>/.remote-logs/<job_id>/combined.log``；
后台任务的日志由本地 fetcher 子进程在任务结束后一次性拉取（cat 全量 /
tail 后 N 行），随后删除远端目录。运行中实时查看用 ``remote logs --follow``
（直接 SSH 跟踪远端合并日志）。
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
TAIL_LOG_LINES = 200
QUICK_SSH_TIMEOUT_SEC = 60
CLEANUP_TIMEOUT_SEC = 30
DEFAULT_TIMEOUT_SEC = 600

LOG_MODES = ("none", "tail", "full")

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
              timeout_sec: int, background: bool,
              self_clean: bool = False) -> str:
    """生成远端 launcher 脚本：命令写盘→cd→env→set -m 后台起→PID 标记。

    - 前台：命令输出走 SSH 会话（本地 capture），``wait`` 传播退出码。
    - 后台：stdout/stderr 合并重定向到远端 ``combined.log``，waiter 负责
      超时杀进程组并写 done 标记。
    - ``self_clean``：结束后删除远端 ``log_dir``（前台任务、以及无本地
      fetcher 收尾的 ``logs=none`` 后台任务使用；有本地 fetcher 的任务由
      fetcher 在检测到 done 之后删除，避免删掉 done 标记）。
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
        lines += [': > "$LOG_DIR/combined.log"']
    if background:
        run_line = 'bash "$LOG_DIR/cmd.sh" >>"$LOG_DIR/combined.log" 2>&1 < /dev/null &'
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
        ]
        if self_clean:
            lines.append('  rm -rf "$LOG_DIR"')
        lines += [") &"]
    else:
        lines += [
            'wait "$PID"',
            'RC=$?',
            'printf \'done\\n\' > "$LOG_DIR/done"',
        ]
        if self_clean:
            lines.append('rm -rf "$LOG_DIR"')
        lines += ['exit "$RC"']
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


def _tail_bytes(data: bytes, lines: int) -> bytes:
    """取合并文本的最后 lines 行（保留原始换行与内容）。"""
    split = data.decode("utf-8", "replace").splitlines(keepends=True)
    if len(split) <= lines:
        return data
    return "".join(split[-lines:]).encode("utf-8")


def _merged(out: bytes, err: bytes) -> bytes:
    """stdout/stderr 合并（stdout 段在前，中间补换行避免粘连）。"""
    if out and err and not out.endswith(b"\n"):
        return out + b"\n" + err
    return out + err


def _write_log(job: _jobs.Job, data: bytes) -> None:
    """按 job.log 的文件名写本地合并日志（tail.log/full.log）。"""
    d = _config.state_dir() / "jobs" / job.job_id
    (d / Path(job.log).name).write_bytes(data)


def _cleanup_remote(endpoint: _config.Endpoint, log_dir: str) -> None:
    """超时/失败时 best-effort 杀掉远端进程组并清理日志目录（读 pid 文件）。"""
    q = shlex.quote(log_dir)
    script = (
        f'PID=$(cat {q}/pid 2>/dev/null || true)\n'
        'if [ -n "$PID" ]; then '
        'kill -TERM -- -"$PID" 2>/dev/null || kill -TERM "$PID" 2>/dev/null; '
        'sleep 1; kill -KILL -- -"$PID" 2>/dev/null; fi\n'
        f"printf 'done\\n' > {q}/done 2>/dev/null || true\n"
        f"rm -rf {q} 2>/dev/null || true\n"
    )
    try:
        _ssh.ssh_run(endpoint, script, timeout_sec=CLEANUP_TIMEOUT_SEC)
    except _ssh.SSHError:
        pass


def _run_foreground(job: _jobs.Job, endpoint: _config.Endpoint, env: dict,
                    log_dir: str, timeout_sec: int) -> None:
    script = _launcher(job.command, job.cwd, env, log_dir, timeout_sec,
                       background=False, self_clean=True)
    try:
        cp = _ssh.ssh_run(endpoint, script, timeout_sec=timeout_sec)
    except _ssh.SSHError as e:
        _cleanup_remote(endpoint, log_dir)
        job.status = "timeout" if "超时" in str(e) else "failed"
        job.exit_code = 124 if job.status == "timeout" else None
        job.finished_at = _jobs.now_iso()
        if job.logs != "none":
            _write_log(job, b"")
            _jobs.save_job(job)
        return
    out, err, pid = _split_marker(cp.stdout, cp.stderr)
    job.remote_pid = pid
    job.exit_code = cp.returncode
    job.status = "done" if cp.returncode == 0 else "failed"
    job.finished_at = _jobs.now_iso()
    combined = _merged(out, err)
    job.preview = preview_text(combined.decode("utf-8", "replace"))
    if job.logs != "none":
        data = combined if job.logs == "full" else _tail_bytes(combined, TAIL_LOG_LINES)
        _write_log(job, data)
        _jobs.save_job(job)


def _fetcher_script(remote_log: str, done_file: str, cleanup_dir: str,
                    lines: int | None = None) -> str:
    """fetcher 远端脚本：等 done 后拉取合并日志并删除远端目录。

    纯函数便于本地 bash 真实执行测试。``lines`` 为 None 拉全量（full），
    否则只拉最后 ``lines`` 行（tail）。cat/tail 正常退出保证缓冲 flush。
    """
    fetch = f"cat {shlex.quote(remote_log)}" if lines is None else (
        f"tail -n {int(lines)} {shlex.quote(remote_log)}"
    )
    return (
        f"while [ ! -e {shlex.quote(done_file)} ]; do sleep 1; done\n"
        f"{fetch}\n"
        f"rm -rf {shlex.quote(cleanup_dir)}\n"
        "exit 0\n"
    )


def _spawn_log_fetcher(endpoint: _config.Endpoint, remote_log: str,
                       done_file: str, local_path: Path,
                       cleanup_dir: str, lines: int | None = None) -> None:
    """后台日志 fetcher 子进程：等 done 后拉取远端合并日志并删除远端目录。

    ``lines`` 为 None 时拉全量（full 模式），否则只拉最后 ``lines`` 行
    （tail 模式）。运行期间本地不保留任何日志；结束后一次性 cat/tail
    （正常退出保证缓冲 flush，规避 tail -F 被 kill 丢块缓冲的问题）。
    """
    script = _fetcher_script(remote_log, done_file, cleanup_dir, lines)
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
        _output.progress({"warning": f"无法启动日志 fetcher: {e}"})
    finally:
        fh.close()


def _start_background(job: _jobs.Job, endpoint: _config.Endpoint, env: dict,
                      log_dir: str, timeout_sec: int) -> None:
    script = _launcher(job.command, job.cwd, env, log_dir, timeout_sec,
                       background=True, self_clean=(job.logs == "none"))
    try:
        cp = _ssh.ssh_run(endpoint, script, timeout_sec=QUICK_SSH_TIMEOUT_SEC)
    except _ssh.SSHError as e:
        if job.logs != "none":
            job.status = "failed"
            job.exit_code = None
            job.finished_at = _jobs.now_iso()
            _jobs.save_job(job)
        _output.progress({"run": f"后台启动失败: {e}"})
        return
    pid = _extract_pid(cp.stdout)
    if cp.returncode != 0 or pid is None:
        if job.logs != "none":
            job.status = "failed"
            job.exit_code = cp.returncode or None
            job.finished_at = _jobs.now_iso()
            _jobs.save_job(job)
        return
    job.remote_pid = pid
    job.remote_log_dir = log_dir
    if job.logs == "none":
        return  # 不记录 job、不落盘日志
    _jobs.save_job(job)
    base = _config.state_dir() / "jobs" / job.job_id
    name = Path(job.log).name
    remote_log = f"{log_dir}/combined.log"
    done_file = f"{log_dir}/done"
    lines = None if job.logs == "full" else TAIL_LOG_LINES
    _spawn_log_fetcher(endpoint, remote_log, done_file, base / name,
                       log_dir, lines)


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
               background: bool = False,
               logs: str | None = None) -> _jobs.Job:
    """远程执行命令（PRD 5.1）。

    默认 cwd=workspace_root。日志保留策略 ``logs`` 为 ``none|tail|full``：
    缺省前台 ``none``（不落盘、不记录 job）、后台 ``full``（合并日志全量
    落盘 ``state/jobs/<job_id>/full.log``）；``tail`` 只保留合并日志最后
    TAIL_LOG_LINES 行（``tail.log``）。前台返回合并截断预览 ``preview``
    + exit_code；``none`` 模式不产生 job 记录，返回无 job_id。
    """
    if logs is None:
        logs = "full" if background else "none"
    if logs not in LOG_MODES:
        raise _config.RemotePluginError(
            f"--logs 必须是 {'|'.join(LOG_MODES)}，实际 {logs!r}"
        )
    env = dict(env or {})
    if cards is None:
        cards = _cards_from_env(env)
    ws = machine.effective_workspace_root()
    cwd = ws if cwd is None else cwd
    job_id = _fresh_job_id()
    log_name = "tail.log" if logs == "tail" else "full.log"
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
        logs=logs,
        log=f"state/jobs/{job_id}/{log_name}" if logs != "none" else "",
    )
    if logs != "none":
        _jobs.save_job(job)
    endpoint = _config.resolve_endpoint(machine, _config.state_dir())
    log_dir = f"{ws.rstrip('/')}/{_REMOTE_LOG_ROOT}/{job_id}"
    if background:
        _start_background(job, endpoint, env, log_dir, timeout_sec)
    else:
        _run_foreground(job, endpoint, env, log_dir, timeout_sec)
    if logs != "none":
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
                     args.cards, args.task, args.timeout, args.background,
                     getattr(args, "logs", None))
    preview = getattr(job, "preview",
                      {"head": "", "tail": "", "truncated": False})
    if job.logs == "none":
        # 不记录 job：输出不携带 job_id/占用等记录字段，只给即时结果
        return {
            "status": job.status,
            "exit_code": job.exit_code,
            "preview": preview,
            "logs": "none",
        }
    payload = job.as_dict()
    payload["preview"] = preview
    payload["running"] = getattr(job, "running", [])
    return payload
