# remote-plugin 开发进度

> 按 `docs/workflow.md` 推进。测试机器：`192.168.9.166`（A2，8 卡）。

## 阶段 0：初始化

- [x] git init
- [x] .gitignore / AGENTS.md / PROGRESS.md
- [x] 首次提交 `chore: init remote-plugin with PRD and spec`

## 阶段 1：T0 骨架

- [x] config.py / ssh.py / output.py / cli.py + 入口脚本 + tests
- [x] T0 自验（compileall + 4 条 [本地]）
- [x] 提交 `feat: T0 skeleton`

## 阶段 2：子代理并行

- [ ] T1 machines.py + probes.py（verify/machines/status、探针、档案 Markdown）
- [ ] T2 updown.py + bootstrap.py（免密引导→docker→sshd→工作区初始化）
- [ ] T3 runner.py + jobs.py（job_id、卡占用、截断预览、stale reconcile、超时强杀）
- [ ] T4 sync_paths.py（tar|ssh 定向传输 + sha256 抽检）
- [ ] T5 sync_git.py + snapshot.py（synthetic snapshot、bundle、mirror materialize、fail closed）
- [ ] T6 skills/remote-plugin/SKILL.md + docs/harness/ 三份接入文档

## 阶段 3：最终验收

- [ ] 3.1 静态门（compileall + unittest + 越权核对 + --help）
- [ ] 3.2 真机 e2e（T2 up → T1 → T3 → T4 → T5）
- [ ] 3.3 收尾（汇总报告 + 按任务 commit + 更新本文档）

## 遗留项

- **真机 SSH KEX 挂起**：`192.168.9.166` 默认 KEX `sntrup761x25519` 握手会挂起（疑似大包触发 PMTUD 黑洞）；`-o KexAlgorithms=curve25519-sha256` 即正常。阶段 3 需在 `ssh.py::_ssh_base` 增加该 KEX 优先项并记录。
