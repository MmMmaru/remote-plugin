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
- [~] 3.2 真机 e2e：**T2 up 已通全链路**（免密引导 → docker → 复用容器 → docker exec → 工作区初始化）；T1/T3/T4/T5 被网络 MTU 黑洞阻断（见遗留项）
- [ ] 3.3 收尾：按任务已分 commit；汇总报告见会话

## 里程碑：容器模型修正（sshd → docker exec）

按用户要求，将模式 A 的容器访问从「容器内 sshd + 端口映射」改为「SSH 到宿主机 + `docker exec` 进容器」：

- `config.Endpoint` 增加 `container` 字段；`resolve_endpoint`（模式 A）返回宿主机 + 容器名
- `ssh.py` 集中 `ssh_argv`（含 KEX 修复 + docker exec 包装），`jobs.py` 复用
- `bootstrap.ensure_container` = pull → run → 校验 `docker exec`；镜像/设备漂移降为**告警**（仅「容器未运行/不可 exec」才 `needs_repair`）
- 同步更新 `docs/PRD.md` / `docs/spec.md` / `docs/workflow.md`

## 遗留项

1. **网络 PMTUD 黑洞（阻塞真机 e2e T1/T3/T4/T5）**：到 `192.168.9.166` 的路径有效 MTU 约 1428 字节（`ping -M do -s 1400` 通、`-s 1450` 不通），且 ICMP 分片报文被丢弃 → 任何 >~1.4KB 的 SSH 传输（大脚本、git bundle、tar、大 stdout，甚至纯 `ssh 'echo <3000 字符>'`）都会挂起。属**网络基础设施问题**，非插件代码缺陷。修复需客户端 root 降低 MTU/MSS（`sudo ip link set eth0 mtu 1400` 或 `ip route ... advmss 1388`），或网络管理员修复隧道 MTU——本环境无 root（`sudo` 需密码）。
2. **账户修正**：`machines.json` 原 `user: root` 实际应为 `admin123`（已改，密码仍 `Huawei@123`）。
3. **内核行数**：合计 ~3.3K 行，超出 PRD「≤1500 行」目标（约 2.2×）；单文件 ≤600 约束满足。
4. **后台超时默认值**：PRD 5.2 写「默认 30min」，但 `cli.py` `--timeout` 默认 600s（T3 子代理已标出，需裁决）。
