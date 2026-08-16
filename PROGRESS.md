# remote-plugin 开发进度

> 按 `docs/workflow.md` 推进。测试机器：`192.168.9.166`（A2，8 卡，VM 登录 `admin123`）。

## 阶段 0：初始化

- [x] git init
- [x] .gitignore / AGENTS.md / PROGRESS.md
- [x] 首次提交 `chore: init remote-plugin with PRD and spec`

## 阶段 1：T0 骨架

- [x] config.py / ssh.py / output.py / cli.py + 入口脚本 + tests
- [x] T0 自验（compileall + 4 条 [本地]）
- [x] 提交 `feat: T0 skeleton`

## 阶段 2：子代理并行（T1–T6 全部完成）

- [x] T1 machines.py + probes.py（verify/machines/status、npu-smi 探针、档案 Markdown）
- [x] T2 updown.py + bootstrap.py（免密引导→docker→**pull/run/exec**→工作区初始化；无 sshd）
- [x] T3 runner.py + jobs.py（job_id、卡占用、截断预览、stale reconcile、超时强杀）
- [x] T4 sync_paths.py（tar|ssh 定向传输 + sha256 抽检）
- [x] T5 sync_git.py + snapshot.py（synthetic snapshot、bundle、mirror materialize、fail closed）
- [x] T6 skills/remote-plugin/SKILL.md + docs/harness/ 三份接入文档

> 阶段 2 由 6 个并行 coder 子代理完成，主代理在阶段 3.1 修了 2 处 T5 测试缺陷。

## 阶段 3：最终验收

- [x] 3.1 静态门：`compileall` + 158 条 unittest 全绿；无越权改 T0；10 子命令 `--help`；错误单行 JSON 无堆栈
- [x] 3.2 真机 e2e（T2 up → T1 → T3 → T4 → T5 全绿，MTU 已由客户端降 MTU 解决）
  - T2 `up`：免密引导 → docker → 复用容器 → docker exec → 工作区初始化，`status: ok`
  - T1 `verify`：`ok`，实测 8 卡 910B3、cards_match=true、torch_npu 2.10.0.post2；`machines`/`status --probe` 正常
  - T3 `run`：前台 exit 0；后台占坑（cards 0,1）在 `machines` 可见；`jobs/logs/stop` 正常；`--timeout 5` → timeout；杀进程 → stale
  - T4 `sync --paths`：`ready`，7 文件 50684 字节，sha256 与远端逐文件一致
  - T5 `sync` git 整树：`ready`（snapshot=remote_head）；二次 `no_change`；远端 t5-test HEAD 一致
  - 冒烟：容器内 `import torch_npu` exit 0
- [x] 3.3 收尾：按任务分 commit；本文档更新

## 里程碑：容器模型修正（sshd → docker exec）

按用户要求，将模式 A 的容器访问从「容器内 sshd + 端口映射」改为「SSH 到宿主机 + `docker exec` 进容器」：

- `config.Endpoint` 增加 `container` 字段；`resolve_endpoint`（模式 A）返回宿主机 + 容器名
- `ssh.py` 集中 `ssh_argv`（含 KEX 修复 + docker exec 包装），`jobs.py` 复用
- `bootstrap.ensure_container` = pull → run → 校验 `docker exec`；镜像/设备漂移降为**告警**（仅「容器未运行/不可 exec」才 `needs_repair`）
- 同步更新 `docs/PRD.md` / `docs/spec.md` / `docs/workflow.md`

## PPU 兼容性验证（2026-08-15）

- [x] 注册 PPU 裸机 SSH 端点：`8.130.213.80:1016`，工作区 `/root/xrs/vllm-workspace`。
- [x] `remote up ppu`：SSH 已免密，工作区和 `.remote-mirrors` 初始化成功。
- [x] `remote verify ppu`：通用环境检查为 `ok`；PPU 不启用 Ascend/NPU 专属探针。
- [x] `remote sync ppu`：snapshot commit 与远端 HEAD 一致（现已直接同步 workspace_root）。
- [x] `remote run ppu`：不调用 NPU 命令、不声明 NPU 卡占用的通用冒烟测试 exit code 0。
- [x] 更新 `SKILL.md`：明确 PPU 可用范围及 `npu-smi`、`torch_npu`、NPU 卡级探针等当前限制。

## 遗留项

1. **网络 MTU 黑洞（已解决）**：到 `192.168.9.166` 的路径有效 MTU ≈1428 且 PMTUD 被吞（>1.4KB SSH 传输挂起）。由用户在客户端降 MTU（`sysctl tcp_mtu_probing=1` 或 `ip link set eth0 mtu 1400`）解决，非插件代码缺陷。
2. **账户修正**：`machines.json` 原 `user: root` 实际应为 `admin123`（已改，密码仍 `Huawei@123`）。
3. **行数要求（已裁决：取消）**：原 PRD「内核 ≤1500 行 / 单文件 ≤600 行」约束已取消，相关文档表述与 `test_machines.py` 的 600 行断言已移除。
4. **后台超时默认值**：PRD 5.2 写「默认 30min」，但 `cli.py` `--timeout` 默认 600s（T3 子代理已标出，需裁决）。
5. **npu-smi 利用率解析（已解决）**：A2 紧凑布局（`0/0` 无空格）与无 AICore% 列的布局曾导致 AICore%/HBM 解析错位或 n/a；awk 已统一撑开斜杠并按列数判定（7 段含 AICore%、6 段记 n/a），`machines`/`status --probe` 均可展示每卡 HBM 用量/总量与 AICore%。
6. **镜像漂移**：现有容器跑旧 nightly（64aed8655de9），配置写 `nightly-main`（当前 ade04e75aa4a）——按新策略仅告警不复建。

## 2026-08-15 workspace 同步收尾

- 删除 `worktree` 参数，`sync`/`sync --paths`/`pull`/`run` 默认直接使用远端 `workspace_root`。
- 新增 workspace 根定位与 Git 仓库发现：递归发现非 ignored 仓库，并纳入 workspace 内已注册的 Git worktree。
- 保留原有 snapshot → bundle/mirror → materialize → HEAD/dirty/sha256 抽检校验链路；为根仓库排除 `.remote-mirrors` 与 `.remote-logs` 运行目录，避免旧 dirty 校验误报。
- 更新 README、SKILL、PRD、spec 与 harness 文档；新增 workspace/worktree 单测与整链路回归。

## 2026-08-15 全局入口安装

- 新增 `remote_plugin/install.py`，提供 `install_launcher(source, bin_dir)` 与 `InstallResult`。
- 新增 `remote install`，原子创建 `~/.local/bin/remote` 符号链接；同一入口重复执行幂等，其他文件/链接占用时 fail closed。
- 新增安装器与 CLI 注册单测，更新 README、SKILL、PRD、spec、workflow 和遗留项。

## 2026-08-15 增量 bundle 同步

- `snapshot.build_snapshots` 支持读取上次 snapshot 作为 parent：内容不变复用旧 commit，内容变化生成 parented snapshot。
- `sync_git` 增加一次远端 parity probe，按仓库选择 `skip`、`delta` 或 `full` bundle；delta 通过临时 ref 生成 `parent..current` 范围包，避免 Git 拒绝裸 synthetic SHA。
- `SyncResult` 增加 `bundle_transfer` 统计；补充 snapshot parent/delta 与根变更跳过子仓库的单测。
- 本地 `tests.test_snapshot tests.test_sync_git` 24 条测试全绿；PPU full/delta 传输计时已完成。

## 2026-08-15 PPU 增量端到端验收

- 使用旧实现做同一 workspace 的 full baseline：4 个 repo bundle 合计 39,515,215 字节，wall time 132.15 秒，结果 `ready`。
- 只在 workspace 根增加临时 marker 后使用新实现：3 个 repo `skip`，根 repo `delta`；实际 delta bundle 492 字节，wall time 4.56 秒，结果 `ready`。
- 新实现 no-change 快路径 wall time 2.70 秒；最终删除 marker 并再次同步，PPU 远端 root/vllm-seu HEAD 与本地结果一致，marker 不存在。
- 原始计时日志保存在 workspace 根 `.log/remote-plugin-{old-full-baseline,incremental-delta,no-change,final-restore}.*`。

## 2026-08-16 worktree 一级扫描 + run 日志保留策略

- 仓库发现改为只扫描 workspace 一级目录（root + 直接子目录的 .git），深度 ≥2 的
  独立仓库不再发现；registered worktree 仍由 owner 的 `git worktree list` 查询覆盖，
  submodule 由 .gitmodules 递归处理。
- `remote run` 新增 `--logs {none|tail|full}`：默认前台 none（不落盘、不记录 job）、
  后台 full（合并日志全量落盘 full.log）；tail 只保留合并日志最后 200 行（tail.log）。
- 日志统一为 stdout+stderr 合并保存（远端 combined.log 单文件同步，streamer 由两个
  减为一个）；`remote logs --stderr` 废弃忽略；旧格式 stdout.log/stderr.log 不再读取。
- 远端 `.remote-logs/<job_id>/` 任务结束后自动删除（streamer/waiter/launcher self-clean），
  本地只按策略保留副本；`--logs none` 任务不产生 Job 记录。
- 单测新增/更新（workspace 一级扫描、runner 三策略、jobs 合并日志、launcher self-clean），
  全套 203 条测试全绿。
