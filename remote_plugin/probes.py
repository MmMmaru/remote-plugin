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
# `CARD <idx> <model> <aicore%> <hbm_used_mb> <hbm_total_mb>`。
#
# A2（910B3）`npu-smi info` 每卡两行：
#   | 0  910B3 | OK           | 93.1  48  0/0 |
#   | 0        | 0000:C1:00.0 | 0     0/0  3425/65536 |
# 第一行 $2=NPU+Name；第二行 Bus-Id 在 $3，$4 为 "[AICore%] Mem(used/total) HBM(used/total)"。
# A3（Ascend910）第二行的 $2 为 "Chip Phy-ID"（如 "0 0"），第二个字段就是全局逻辑卡 index。
# 不同驱动版本布局差异：used/total 间的 `/` 可能无空格（0/0 或 0 / 0），
# 部分版本第二行没有 AICore% 列——统一先把 `/` 撑开再按列数判定（7 段含
# AICore%，6 段无 AICore% 记 n/a），解析不出时 best-effort 记 n/a 0 0。
NPU_PARSE_AWK = r"""npu-smi info 2>&1 | awk -F'|' '
  /^\|/ {
    if ($3 ~ /^ *[0-9a-fA-F]+:/) {
      # 第二行：Bus-Id 在 $3；$4 = "[AICore%]  Mem(used/total)  HBM(used/total)"
      if (model != "") {
        u = $4
        gsub(/\//, " / ", u)          # 0/0 与 0 / 0 统一
        gsub(/^ +| +$/, "", u)
        m = split(u, a, " ")
        aicore = "n/a"; hbm_u = 0; hbm_t = 0
        if (m >= 7 && a[m-1] == "/") {
          # 7 段：AICore% Mem used / total HBM used / total
          aicore = a[1]; hbm_u = a[m-2]; hbm_t = a[m]
        } else if (m == 6 && a[5] == "/") {
          # 6 段（无 AICore% 列）：Mem used / total HBM used / total
          hbm_u = a[4]; hbm_t = a[6]
        }
        n = split($2, chip, " ")
        logical_idx = physical_idx
        if (n >= 2 && chip[2] ~ /^[0-9]+$/) {
          # A3：$2 = Chip Phy-ID，Phy-ID 直接是 0..15 的逻辑卡 index
          logical_idx = chip[2]
        }
        printf "CARD %d %s %s %s %s\n", logical_idx, model, aicore, hbm_u, hbm_t
        model = ""
      }
      next
    }
    if ($2 ~ /^ *[0-9]+ +[A-Za-z0-9]/) {
      # 第一行：NPU + Name；必须放在 Bus-Id 行之后，A3 Chip 行的 $2 也以数字开头
      if (model != "") printf "CARD %d %s n/a 0 0\n", physical_idx, model
      n = split($2, t, " "); physical_idx = t[1]; model = t[2]; next
    }
  }
  END { if (model != "") printf "CARD %d %s n/a 0 0\n", physical_idx, model }
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
    r = urllib.request.urlopen(url, timeout=8)
    latency_ms = (time.time() - t0) * 1000
    # 下载测速（best-effort，不影响可达性判断；测不出来记 -1 = 无法测量）
    n = 0
    speed_bps = -1
    try:
        t1 = time.time()
        while n < 131072 and time.time() - t1 < 6:  # 最多 128KB / 6s
            chunk = r.read(32768)
            if not chunk:
                break
            n += len(chunk)
        if n > 0:
            dt = time.time() - t1
            speed_bps = int(n / dt) if dt > 0 else -1
    except Exception:
        speed_bps = -1
    print("ok %d %.1f %d" % (r.status, latency_ms, speed_bps))
except Exception as e:
    print("fail %s" % type(e).__name__)
PYEOF
)"
  case "$PIP_RESULT" in
    ok*) put pip_index_reachable true
         put pip_index_latency_ms "$(printf '%s' "$PIP_RESULT" | awk '{print $3}')"
         PIP_BPS="$(printf '%s' "$PIP_RESULT" | awk '{print $4}')"
         if [ "$PIP_BPS" -ge 0 ] 2>/dev/null; then
           put pip_index_speed_kbps "$(printf '%s' "$PIP_BPS" | awk '{printf "%.0f", $1/1024}')"
         else
           # 测速失败/无有效负载：输出 null + 原因，而不是 0（0 会被误读为"真的极慢"）
           put pip_index_speed_kbps null
           put pip_index_speed_note "$(json_str "下载测速失败或无有效负载，无法测量")"
         fi ;;
    *)   put pip_index_reachable false
         put pip_index_latency_ms -1
         put pip_index_speed_kbps null
         put pip_index_speed_note "$(json_str "index 不可达，未测速")" ;;
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
  put pip_index_speed_kbps null
  put pip_index_speed_note "$(json_str "python3 不可用，未测速")"
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
  # 每卡占用（index/model/AICore%/HBM used/total）→ JSON 数组，供 machines 卡级展示
  NPU_CARDS_JSON="$(printf '%s\n' "$NPU_CARD_LINES" | awk '
    BEGIN { printf "["; c = 0 }
    /^CARD / {
      aicore = ($4 ~ /^[0-9]+(\.[0-9]+)?$/) ? $4 : "null"
      printf "%s{\"index\":%d,\"model\":\"%s\",\"aicore_pct\":%s,\"hbm_used_mb\":%d,\"hbm_total_mb\":%d}", (c++ ? "," : ""), $2, $3, aicore, $5 + 0, $6 + 0
    }
    END { printf "]\n" }')"
  put npu_cards "$NPU_CARDS_JSON"
else
  put npu_smi_ok false
  put npu_count 0
  put npu_model '""'
  put npu_cards '[]'
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
