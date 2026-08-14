"""T1 探针脚本生成（纯函数，可单测）。

生成一次性 bash 探针脚本，远端经 ``ssh ... bash -s`` 执行。脚本约定：

- stdout 输出**单个 JSON 对象**（全部探测事实），供 ``verify_machine`` 解析。
- stderr 保持干净（各探针内部自行吞错）。
- 必做探针：uname/内核/OS/CPU/内存、workspace_root 存在性与可写性、磁盘余量。
- 网络探针（所有机器）：pip index 可达性与延迟、代理 env、apt mirror、DNS。
- 可选探针（按 ``tags.chip``）：``ascend-*`` → npu-smi 型号/卡数（含与
  ``tags.cards`` 交叉校验，输出 ``cards_match``）、torch/torch_npu 版本。
"""
from __future__ import annotations

from typing import Any

# npu-smi 解析片段（bash + awk）：输入 `npu-smi info` 输出，每卡打印一行
# `CARD <idx> <model> <aicore%>`。兼容两种常见布局（设备名行 + 总线行 / 仅设备名行）。
NPU_PARSE_AWK = r"""npu-smi info 2>&1 | awk -F'|' '
  /^\|/ {
    if ($2 ~ /^ *[0-9]+ +[^ ]*[A-Za-z]/) {
      if (model != "") { printf "CARD %d %s n/a\n", idx, model }
      n = split($2, t, " "); idx = t[1]; model = t[2]; next
    }
    if ($2 ~ /^ *[0-9]+ +[0-9]+/) {
      if (model != "") { u = $4; gsub(/^ +| +$/, "", u); split(u, a, " "); printf "CARD %d %s %s\n", idx, model, a[1]; model = "" }
    }
  }
  END { if (model != "") printf "CARD %d %s n/a\n", idx, model }
'"""

_BASE_SCRIPT = r"""#!/usr/bin/env bash
# remote-plugin verify 探针脚本（remote_plugin.probes.build_probe_script 生成）
WS_ROOT="${WS_ROOT:-/vllm-workspace}"
EXPECTED_CARDS="${EXPECTED_CARDS:-__EXPECTED_CARDS_DEFAULT__}"

facts="$(mktemp)"
trap 'rm -f "$facts"' EXIT

# json_str <raw>：转义为带引号的 JSON 字符串字面量（引号/反斜杠转义，换行压平为空格）
json_str() { printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n' ' ')"; }
# put <key> <json_value>：写入一行 `key<TAB>value`
put() { printf '%s\t%s\n' "$1" "$2" >> "$facts"; }

# ---------- 必做探针 ----------
put uname "$(json_str "$(uname -a 2>/dev/null || true)")"
put kernel "$(json_str "$(uname -r 2>/dev/null || true)")"
if [ -r /etc/os-release ]; then
  OS_ID="$(sed -n 's/^ID=//p' /etc/os-release | tr -d '"')"
  OS_VERSION="$(sed -n 's/^VERSION_ID=//p' /etc/os-release | tr -d '"')"
  put os "$(json_str "${OS_ID} ${OS_VERSION}")"
fi
put cpu_model "$(json_str "$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2 | sed 's/^ *//' || true)")"
MEM_KB="$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0)"
put mem_mb "$(( MEM_KB / 1024 ))"

if [ -d "$WS_ROOT" ]; then
  put workspace_exists true
  if [ -w "$WS_ROOT" ]; then
    put workspace_writable true
  else
    put workspace_writable false
  fi
else
  put workspace_exists false
  put workspace_writable false
fi

DF_TARGET="$WS_ROOT"
[ -d "$DF_TARGET" ] || DF_TARGET="/"
DF_OUT="$(df -k "$DF_TARGET" 2>/dev/null | tail -n1)"
put disk_free_gb "$(printf '%s' "$DF_OUT" | awk '{printf "%d", $4/1024/1024}')"
put disk_usage_pct "$(printf '%s' "$DF_OUT" | awk '{gsub("%","",$5); print $5+0}')"

__ASCEND_PROBES__

# ---------- 网络探针（所有机器） ----------
PROXY_ENV="$(env | grep -iE '^(http|https|ftp|all|no)_proxy=' | sort || true)"
put has_proxy "$([ -n "$PROXY_ENV" ] && echo true || echo false)"
put proxy_env "$(json_str "$PROXY_ENV")"

if command -v python3 >/dev/null 2>&1; then
  put python_version "$(json_str "$(python3 -V 2>&1 | sed 's/^Python //' || true)")"
  PIP_INDEX="$(python3 -m pip config get global.index-url 2>/dev/null | tr -d '\r' || true)"
  [ -z "$PIP_INDEX" ] && PIP_INDEX="https://pypi.org/simple/"
  put pip_index_url "$(json_str "$PIP_INDEX")"
  PIP_RESULT="$(python3 - "$PIP_INDEX" <<'PYEOF' 2>/dev/null || true
import sys, time, urllib.request
url = sys.argv[1]
t0 = time.time()
try:
    with urllib.request.urlopen(url, timeout=8) as r:
        print("ok %d %.1f" % (r.status, (time.time() - t0) * 1000))
except Exception as e:
    print("fail %s" % type(e).__name__)
PYEOF
)"
  case "$PIP_RESULT" in
    ok*) put pip_index_reachable true
         put pip_index_latency_ms "$(printf '%s' "$PIP_RESULT" | awk '{print $3}')" ;;
    *)   put pip_index_reachable false
         put pip_index_latency_ms -1 ;;
  esac
  DNS_HOST="$(printf '%s' "$PIP_INDEX" | sed -E 's#^[a-z]+://##; s#/.*##')"
  DNS_OK="$(python3 - "$DNS_HOST" <<'PYEOF' 2>/dev/null || true
import socket, sys
try:
    socket.getaddrinfo(sys.argv[1], None)
    print("true")
except Exception:
    print("false")
PYEOF
)"
  put dns_ok "$([ "$DNS_OK" = "true" ] && echo true || echo false)"
else
  put python_version '""'
  put pip_index_url "https://pypi.org/simple/"
  put pip_index_reachable false
  put pip_index_latency_ms -1
  put dns_ok false
fi

APT_SRC="$(grep -rhE '^deb[[:space:]]+' /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null | awk '{print $2}' | sort -u | tr '\n' ';')"
put apt_mirror "$(json_str "$APT_SRC")"

# ---------- JSON 组装（单行输出） ----------
{
  printf '{'
  first=1
  while IFS=$'\t' read -r k v; do
    if [ "$first" = "1" ]; then first=0; else printf ','; fi
    printf '"%s":%s' "$k" "$v"
  done < "$facts"
  printf '}\n'
}
"""

_ASCEND_BLOCK = r"""# ---------- 可选探针：tags.chip 以 ascend- 开头 → NPU / torch ----------
if command -v npu-smi >/dev/null 2>&1; then
  put npu_smi_ok true
  NPU_CARD_LINES="$(__NPU_PARSE__)"
  NPU_COUNT="$(printf '%s\n' "$NPU_CARD_LINES" | grep -c '^CARD ' || true)"
  NPU_MODEL="$(printf '%s\n' "$NPU_CARD_LINES" | sed -n 's/^CARD [0-9]* \([^ ]*\).*/\1/p' | head -n1)"
  put npu_count "$NPU_COUNT"
  put npu_model "$(json_str "$NPU_MODEL")"
else
  put npu_smi_ok false
  put npu_count 0
  put npu_model '""'
fi
if [ -n "$EXPECTED_CARDS" ]; then
  if [ "$NPU_COUNT" = "$EXPECTED_CARDS" ]; then
    put cards_match true
  else
    put cards_match false
  fi
fi
if command -v python3 >/dev/null 2>&1; then
  put torch_version "$(json_str "$(python3 -c 'import torch; print(torch.__version__)' 2>/dev/null || true)")"
  put torch_npu_version "$(json_str "$(python3 -c 'import torch_npu; print(torch_npu.__version__)' 2>/dev/null || true)")"
fi
"""


def norm_cards(value: Any) -> int | None:
    """把 tags.cards 归一化为 int（容忍字符串形式）；bool/缺失返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def build_probe_script(tags: dict) -> str:
    """按 tags 生成一次性探针脚本（纯函数，无副作用）。"""
    chip = str(tags.get("chip", ""))
    ascend = chip.startswith("ascend-")
    expected = norm_cards(tags.get("cards"))
    block = _ASCEND_BLOCK if ascend else "# chip 非 ascend-*：跳过 NPU 与 torch 探针\n"
    script = _BASE_SCRIPT.replace("__ASCEND_PROBES__", block)
    script = script.replace("__NPU_PARSE__", NPU_PARSE_AWK)
    script = script.replace(
        "__EXPECTED_CARDS_DEFAULT__", str(expected) if expected is not None else ""
    )
    return script
