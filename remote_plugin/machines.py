"""T1 机器查询与 verify（verify / machines / status + 三个 CLI handler）。

依赖 T0 提供的 ``config`` / ``ssh`` / ``output`` API（只复用，不重写）。
"""
from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, ssh
from .config import Machine, RemotePluginError
from .probes import NPU_PARSE_AWK, build_probe_script, norm_cards

VERIFY_TIMEOUT_SEC = 180
STATUS_TIMEOUT_SEC = 60

_STATUS_PROBE_SCRIPT = r"""#!/usr/bin/env bash
# remote-plugin status 探针：实时负载 + 内存 + CPU + NPU 利用率（labeled lines）
LOAD_VALS="$(uptime 2>/dev/null | sed -n 's/.*load average: //p' | tr ',' ' ')"
echo "LOAD $LOAD_VALS"
echo "CPUS $(nproc 2>/dev/null || echo 0)"
echo "CPU_MODEL $(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2 | sed 's/^ *//')"
awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{printf "MEM_TOTAL_KB %d\nMEM_AVAIL_KB %d\n", t, a}' /proc/meminfo
if command -v npu-smi >/dev/null 2>&1; then
  echo NPU_BEGIN
  __NPU_PARSE__
  echo NPU_END
else
  echo NPU_SMI_MISSING
fi
"""


@dataclass
class VerifyResult:
    """``verify_machine`` 的返回。status ∈ ok|needs_up|unreachable|degraded。"""

    status: str
    facts: dict
    facts_path: Path


@dataclass
class MachineView:
    """``list_machines`` 的单机一览。"""

    alias: str
    mode: str
    tags: dict
    verify_status: str | None
    verified_at: str | None
    jobs: list
    npu_cards: list | None = None  # 每卡实测占用（HBM/AICore），来自最近一次 verify

    @property
    def busy(self) -> bool:
        return bool(self.jobs)


@dataclass
class MachineStatus:
    """``machine_status`` 的单机详情；probe=False 时 load/mem/cpu/npu 为 None。"""

    alias: str
    mode: str
    host: str
    port: int
    user: str
    workspace_root: str
    tags: dict
    note: str
    verify_status: str | None = None
    verified_at: str | None = None
    reachable: bool = True
    probe_error: str | None = None
    load: dict | None = None
    mem: dict | None = None
    cpu: dict | None = None
    npu: list | None = None
    npu_smi: bool | None = None


# ---------------------------------------------------------------- verify


def verify_machine(machine: Machine) -> VerifyResult:
    """对单台机器执行注册验证与环境探测，写 `state/docs/<alias>.facts.json`。

    Markdown 档案由人类维护；verify 不创建、不修改 `state/docs/<alias>.md`。

    - 必做探针：SSH 连通、uname、workspace_root 可写（缺失 → needs_up）、磁盘余量
    - tags.chip 以 ``ascend-`` 开头：npu-smi 型号/卡数（与 tags.cards 交叉校验，
      不符 → degraded）、torch/torch_npu 版本
    - 网络探针（所有机器）：pip index 可达性与延迟、代理 env、apt mirror、DNS
    """
    st = config.state_dir()
    facts_path = st / "docs" / f"{machine.alias}.facts.json"
    facts_path.parent.mkdir(parents=True, exist_ok=True)
    ep = config.resolve_endpoint(machine, st)
    ws = ep.workspace_root
    tags = machine.tags or {}
    script = build_probe_script(tags)
    header = (
        f"export WS_ROOT={shlex.quote(ws)}\n"
        f"export EXPECTED_CARDS={shlex.quote(str(norm_cards(tags.get('cards')) or ''))}\n"
    )
    try:
        r = ssh.ssh_run(ep, header + script, timeout_sec=VERIFY_TIMEOUT_SEC)
    except ssh.SSHError as e:
        facts = {"error": str(e), "endpoint": f"{ep.user}@{ep.host}:{ep.port}",
                 "verified_at": _now()}
        _write_facts(facts_path, "unreachable", facts)
        return VerifyResult("unreachable", facts, facts_path)

    if r.returncode == 255:  # ssh 自身失败（DNS/拒绝/认证），脚本未运行
        err = (r.stderr or b"").decode("utf-8", "replace").strip()[:2000]
        facts = {"error": f"ssh 连接失败（rc=255）: {err}",
                 "endpoint": f"{ep.user}@{ep.host}:{ep.port}",
                 "verified_at": _now()}
        _write_facts(facts_path, "unreachable", facts)
        return VerifyResult("unreachable", facts, facts_path)

    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()[:2000]
        facts = {"error": f"probe 脚本失败（rc={r.returncode}）", "stderr": err,
                 "verified_at": _now()}
        _write_facts(facts_path, "degraded", facts)
        return VerifyResult("degraded", facts, facts_path)

    out = (r.stdout or b"").decode("utf-8", "replace")
    facts = _parse_facts(out)
    if facts is None:
        facts = {"error": "probe 输出无法解析为 JSON", "raw": out[:2000],
                 "verified_at": _now()}
        _write_facts(facts_path, "degraded", facts)
        return VerifyResult("degraded", facts, facts_path)

    status, issues = _judge(machine, facts, ws)
    facts["verified_at"] = _now()
    _write_facts(facts_path, status, facts)
    return VerifyResult(status, facts, facts_path)


def _judge(machine: Machine, facts: dict, ws: str) -> tuple[str, list[str]]:
    """根据探测事实判定状态：needs_up > degraded > ok。"""
    if facts.get("workspace_exists") is False:
        return "needs_up", [f"workspace_root 不存在: {ws}（需先执行 up）"]

    issues: list[str] = []
    if facts.get("workspace_exists") is True and facts.get("workspace_writable") is not True:
        issues.append(f"workspace_root 不可写: {ws}")

    tags = machine.tags or {}
    chip = str(tags.get("chip", ""))
    if chip.startswith("ascend-"):
        if facts.get("npu_smi_ok") is not True:
            issues.append("npu-smi 不可用或无法解析（tags.chip=ascend-*）")
        else:
            got = facts.get("npu_count")
            want = norm_cards(tags.get("cards"))
            if want is not None and got != want:
                issues.append(f"实测卡数 {got} ≠ 配置 {want}（tags.cards）")

    if issues:
        return "degraded", issues
    return "ok", []


def _parse_facts(out: str) -> dict | None:
    text = out.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- 结构化 facts


def _write_facts(path: Path, status: str, facts: dict) -> None:
    """写入 verify 的结构化结果，不触碰人类维护的 Markdown。"""
    payload = dict(facts)
    payload["verify_status"] = status
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_verify_summary(
    st: Path, alias: str
) -> tuple[str | None, str | None, list | None]:
    """读最近 verify 结论（status / verified_at / 每卡占用 npu_cards）。

    优先读结构化 facts `state/docs/<alias>.facts.json`；旧档案无 facts 时
    回退解析 Markdown（此时 npu_cards 为 None）。
    """
    doc = st / "docs" / f"{alias}.md"
    facts_path = st / "docs" / f"{alias}.facts.json"
    status = verified = None
    npu_cards: list | None = None
    if facts_path.is_file():
        try:
            data = json.loads(facts_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            if data.get("verify_status"):
                status = str(data["verify_status"])
            if data.get("verified_at"):
                verified = str(data["verified_at"])
            cards = data.get("npu_cards")
            if isinstance(cards, list):
                npu_cards = cards
    if status is None and doc.is_file():
        try:
            for line in doc.read_text(encoding="utf-8").splitlines():
                if line.startswith("- verify_status: "):
                    status = line[len("- verify_status: "):].strip()
                elif line.startswith("- verified_at: "):
                    verified = line[len("- verified_at: "):].strip()
        except OSError:
            pass
    return (status or None), (verified or None), npu_cards


# ---------------------------------------------------------------- machines 一览


def list_machines() -> list[MachineView]:
    """所有机器一览：alias/mode/tags/占用（state/jobs 的 running 记录）/最近 verify。"""
    machines = config.load_machines()
    st = config.state_dir()
    jobs_by_machine = _running_jobs_by_machine(st)
    views = []
    for alias in sorted(machines):
        m = machines[alias]
        vstatus, vat, vcards = _read_verify_summary(st, alias)
        views.append(
            MachineView(
                alias=alias,
                mode=m.mode,
                tags=dict(m.tags or {}),
                verify_status=vstatus,
                verified_at=vat,
                jobs=jobs_by_machine.get(alias, []),
                npu_cards=vcards,
            )
        )
    return views


def _running_jobs_by_machine(st: Path) -> dict[str, list[dict]]:
    """读 `state/jobs/<job_id>/meta.json`（或 meta / 目录内任意 *.json）的 running 记录。

    T3 jobs 模块写 Job 记录时保持该布局即可被本函数识别：meta 文件为 JSON 对象，
    含 status/machine/owner/task/cards 字段。
    """
    jobs_dir = st / "jobs"
    out: dict[str, list[dict]] = {}
    if not jobs_dir.is_dir():
        return out
    for job_dir in sorted(jobs_dir.iterdir()):
        if not job_dir.is_dir():
            continue
        job = _read_job_meta(job_dir)
        if job is None or job.get("status") != "running":
            continue
        machine = job.get("machine")
        if not machine:
            continue
        out.setdefault(str(machine), []).append(
            {
                "job_id": job.get("job_id") or job_dir.name,
                "owner": job.get("owner", ""),
                "task": job.get("task", ""),
                "cards": job.get("cards"),
            }
        )
    return out


def _read_job_meta(job_dir: Path) -> dict | None:
    for name in ("meta.json", "meta"):
        p = job_dir / name
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            return data if isinstance(data, dict) else None
    for p in sorted(job_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and "status" in data and "machine" in data:
            return data
    return None


# ---------------------------------------------------------------- status 详情


def machine_status(alias: str, probe: bool) -> MachineStatus:
    """单机详情；probe=True 时实时 SSH 查负载、内存、CPU 与 npu-smi 利用率。"""
    machines = config.load_machines()
    m = machines.get(alias)
    if m is None:
        raise RemotePluginError(f"机器 '{alias}' 未注册（machines.json 中不存在）")
    st = config.state_dir()
    vstatus, vat, _ = _read_verify_summary(st, alias)
    base = dict(
        alias=alias,
        mode=m.mode,
        host=m.host,
        port=m.port,
        user=m.user,
        workspace_root=m.effective_workspace_root(),
        tags=dict(m.tags or {}),
        note=m.note,
        verify_status=vstatus,
        verified_at=vat,
    )
    if not probe:
        return MachineStatus(**base)

    ep = config.resolve_endpoint(m, st)
    script = _STATUS_PROBE_SCRIPT.replace("__NPU_PARSE__", NPU_PARSE_AWK)
    try:
        r = ssh.ssh_run(ep, script, timeout_sec=STATUS_TIMEOUT_SEC)
    except ssh.SSHError as e:
        return MachineStatus(reachable=False, probe_error=str(e), **base)
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()[:1000]
        return MachineStatus(
            reachable=False, probe_error=f"probe 失败（rc={r.returncode}）: {err}", **base
        )
    parsed = _parse_status_output((r.stdout or b"").decode("utf-8", "replace"))
    return MachineStatus(reachable=True, **parsed, **base)


def _as_num_or_str(value: str) -> Any:
    """数值字符串转 int，否则原样返回（如 ``n/a``）。"""
    s = value.strip()
    if s.isdigit():
        return int(s)
    return s


def _parse_status_output(out: str) -> dict:
    """解析 status 探针的 labeled lines → {load, mem, cpu, npu, npu_smi}。"""
    res: dict[str, Any] = {}
    npu: list[dict] = []
    in_npu = False
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("LOAD "):
            parts = line.split()[1:]
            if len(parts) >= 3:
                res["load"] = {"1m": float(parts[0]), "5m": float(parts[1]), "15m": float(parts[2])}
        elif line.startswith("CPUS "):
            res.setdefault("cpu", {})["cores"] = int(line.split()[1])
        elif line.startswith("CPU_MODEL "):
            res.setdefault("cpu", {})["model"] = line[len("CPU_MODEL "):].strip()
        elif line.startswith("MEM_TOTAL_KB "):
            res.setdefault("mem", {})["total_gb"] = round(int(line.split()[1]) / 1024 / 1024, 1)
        elif line.startswith("MEM_AVAIL_KB "):
            res.setdefault("mem", {})["available_gb"] = round(int(line.split()[1]) / 1024 / 1024, 1)
        elif line == "NPU_BEGIN":
            in_npu = True
            res["npu_smi"] = True
            res["npu"] = npu
        elif line == "NPU_END":
            in_npu = False
        elif line == "NPU_SMI_MISSING":
            res["npu_smi"] = False
            res["npu"] = []
        elif in_npu and line.startswith("CARD "):
            parts = line.split()
            if len(parts) >= 4:
                # CARD <idx> <model> <aicore%> [<hbm_used_mb> <hbm_total_mb>]
                card: dict[str, Any] = {
                    "index": int(parts[1]),
                    "model": parts[2],
                    "aicore_pct": _as_num_or_str(parts[3]),
                }
                if len(parts) >= 6:
                    card["hbm_used_mb"] = int(parts[4])
                    card["hbm_total_mb"] = int(parts[5])
                npu.append(card)
    return res


# ---------------------------------------------------------------- CLI handlers


def cli_verify(args) -> dict:
    machines = config.load_machines()
    machine = machines.get(args.alias)
    if machine is None:
        # 不存在 alias：无法探测，返回 unreachable（只写结构化 facts）
        st = config.state_dir()
        facts_path = st / "docs" / f"{args.alias}.facts.json"
        facts = {
            "error": f"alias '{args.alias}' 未注册（machines.json 中不存在该机器）",
            "verified_at": _now(),
        }
        _write_facts(facts_path, "unreachable", facts)
        return {
            "status": "unreachable",
            "facts": facts,
            "facts_file": str(facts_path),
        }
    result = verify_machine(machine)
    return {
        "status": result.status,
        "facts": result.facts,
        "facts_file": str(result.facts_path),
    }


def cli_machines(args) -> dict:
    views = list_machines()
    return {
        "machines": [
            {
                "alias": v.alias,
                "mode": v.mode,
                "tags": v.tags,
                "verify_status": v.verify_status,
                "verified_at": v.verified_at,
                "busy": v.busy,
                "jobs": v.jobs,
                "npu_cards": v.npu_cards,
            }
            for v in views
        ],
        "count": len(views),
    }


def cli_status(args) -> dict:
    st = machine_status(args.alias, bool(args.probe))
    return {
        "alias": st.alias,
        "mode": st.mode,
        "host": st.host,
        "port": st.port,
        "user": st.user,
        "workspace_root": st.workspace_root,
        "tags": st.tags,
        "note": st.note,
        "verify_status": st.verify_status,
        "verified_at": st.verified_at,
        "reachable": st.reachable,
        "probe_error": st.probe_error,
        "load": st.load,
        "mem": st.mem,
        "cpu": st.cpu,
        "npu": st.npu,
        "npu_smi": st.npu_smi,
    }


# ---------------------------------------------------------------- helpers


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
