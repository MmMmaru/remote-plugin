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

## 遗留项

1. **网络 MTU 黑洞（已解决）**：到 `192.168.9.166` 的路径有效 MTU ≈1428 且 PMTUD 被吞（>1.4KB SSH 传输挂起）。由用户在客户端降 MTU（`sysctl tcp_mtu_probing=1` 或 `ip link set eth0 mtu 1400`）解决，非插件代码缺陷。
2. **账户修正**：`machines.json` 原 `user: root` 实际应为 `admin123`（已改，密码仍 `Huawei@123`）。
3. **内核行数**：合计 ~3.3K 行，超出 PRD「≤1500 行」目标（约 2.2×）；单文件 ≤600 约束满足。
4. **后台超时默认值**：PRD 5.2 写「默认 30min」，但 `cli.py` `--timeout` 默认 600s（T3 子代理已标出，需裁决）。
5. **npu-smi 利用率解析**：A2 的 `npu-smi info` 布局下 AICore% 取 `n/a`（best-effort），卡数与型号解析正确。
6. **镜像漂移**：现有容器跑旧 nightly（64aed8655de9），配置写 `nightly-main`（当前 ade04e75aa4a）——按新策略仅告警不复建。
