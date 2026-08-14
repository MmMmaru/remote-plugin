# remote-plugin 重构验收报告（2026-08-14）

> 依据 `docs/refactor_PRD.md`。全程仅用 `remote` CLI（经 Bash 调用 `py -3 remote ...`），
> 机器：9.166（192.168.9.166，A2，8×910B3，容器 `xrs_vllm_main`）。

## 任务耗时记录

| 步骤 | 开始 | 结束 | 耗时 | 结论 |
|---|---|---|---|---|
| 环境摸清（CLI/remote/遗留项） | 18:11 | 18:24 | ~13min | 完成 |
| 旧 harness 删除 + AGENTS.md 改写（子代理B） | 18:24 | 19:05 | ~40min | 完成，commit 973c00d + 5105b12，已 push refactor-remotedev |
| remaining 5 项 + 行数要求（子代理A） | 18:24 | ~19:00 | ~35min | 完成，7 commits（afb5b78..e4b3685），已 push origin/main |
| remote pull 子命令 | 19:07 | 19:13 | ~6min | 完成，db2fbd7，174 测试全绿，已 push |
| 容器配置 verify/up | 18:24 | — | — | 阻塞：网络不通 |
| 源码同步 sync | — | — | — | 待执行 |
| 全量编译 vllm-ascend | — | — | — | 待执行 |
| 验收1：服务 + curl | — | — | — | 待执行 |
| 验收2：profiling + 下载 | — | — | — | 待执行 |
| 验收3：benchmark + 下载 | — | — | — | 待执行 |

## 验收记录

（待填：每验收的命令、关键输出、日志/产物路径）

### 验收命令预案（取自 workspace scripts/）

- 服务：`PYTHONPATH=<ws>/main/vllm-ascend:<ws>/main/vllm VLLM_VERSION=0.24.0 vllm serve
  /home/weights/Qwen/Qwen3-30B-A3B --served-model-name qwen --port 8010 -tp 2
  --enable-expert-parallel 等`（参照 scripts/debug_vllm.sh；profiling 时需
  `VLLM_TORCH_PROFILER_DIR=/vllm-profile`）
- 验收1 curl：`POST /v1/chat/completions`（scripts/send_req_2vllm.sh）
- 验收2 profiling：`POST /start_profile` → 发请求 → `POST /stop_profile` →
  `torch_npu.profiler.analyse("/vllm-profile")`（scripts/collect_profile.sh）→ `remote pull` 回 `.temp/`
- 验收3 benchmark：`vllm bench serve --dataset-name random --random-input-len 4096
  --random-output-len 2048 --num-prompts 50 --max-concurrency 32`（scripts/bench_serve.sh）
  → 结果 `remote pull` 回 `.temp/`

## 踩坑记录

1. **Windows Git Bash 下 `python`/`python3` 是 Windows Store 占位 stub**：`./remote` 的
   shebang `#!/usr/bin/env python3` 直接报 "Python was not found"。需用 `py -3 remote ...`
   调用。（后续考虑在 README/SKILL.md 注明 Windows 调用方式。）
2. **192.168.9.166 网络不可达（18:24 起）**：本机无任何到 192.168.9.0/24 的路由，
   ping 100% 丢包，ssh 22 端口 connection timed out。前一日（8-13）e2e 全绿，
   说明是客户端网络/VPN 变化。已挂 6 分钟轮询，恢复后继续。
3. **Windows 上跑 unittest 泄漏杂散目录 + 13 fail**（子代理 A 已修，commit afb5b78）：
   根因① Windows CreateProcess 优先命中 System32 的 WSL bash/bsdtar 而非 Git 工具；
   ② 测试把 Windows 反斜杠临时路径嵌进交给 bash 的脚本，被当成普通文件名 mkdir 到 repo 根。
   另修两个真实产品 bug：sync_paths 的 sha256 键在 Windows 客户端用反斜杠（远端永远比对失败）、
   sha256sum 二进制模式输出 ` *` 标记未剥离。
4. **子代理 A 曾 37 分钟空转**（反复跑测试、零编辑）：TaskStop 后 resume 给出严格顺序指令
   才落地。教训：委派多步骤任务时要求"每完成一项立即落地编辑"。
5. **repo-init skill 既有损坏**（非本次造成）：其 scripts import `.agents/lib/vaws_local_state`，
   该目录早已不存在，profile 流程此前已不可用。本次按范围外处理，仅把 SKILL.md 路由指向
   remote-plugin；如需修复另开任务。

## 下载命令调研结论

- 现状：`sync --paths` 仅支持 本地→远端 单向；`remote run` 的 stdout 按 UTF-8 文本解码
  落日志（runner.py），传二进制会损坏；`remote logs` 只能取 job 日志。
- 结论：**需要单独的 `remote pull` 子命令**（远端 `tar -cf -` | ssh | 本地 `tar -x`，
  与 sync_paths 同模式的反向二进制流），用于把 profiling/benchmark 产物拉回本地 `.temp/`。

### remote pull 设计（待实施）

- CLI：`remote pull <alias> <remote_path>... --dest <local_dir> [--worktree <id>]`
- 远端路径：相对于 worktree 目录解析（也允许容器内绝对路径）；多个路径打同一个 tar。
- 传输：远端 `tar -C <base> -cf - -- <rels>` 经 ssh stdout **二进制流**到本地
  `tar -x -C <dest>`（ssh.ssh_pipe 的反向用法，不用 scp/sftp）。
- 校验：远端先 `sha256sum` 清单，本地解包后重算比对，不一致 fail closed。
- 输出契约不变：进度 stderr、结果 stdout 单行 JSON `{status, files, bytes, dest}`。
- 内核函数：`pull_paths(machine, worktree, remote_paths, dest) -> PullResult`。
