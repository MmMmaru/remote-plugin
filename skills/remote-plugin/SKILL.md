---
name: remote-plugin
description: 指导 agent 经 remote CLI（而非裸 ssh）在远程机器/容器上查占用、同步代码、编译与跑任务；适用于需要操作远程机器完成开发、编译验证、冒烟测试等场景。
---

# Remote Plugin

remote-plugin 是一个 CLI-only 远程开发插件。**唯一形态是 CLI**：人类与 agent 都经
自身 harness 的 bash/shell 工具调用同一条 `remote` 命令（仓库根目录的可执行脚本
`./remote`）。不做 MCP server，任何能跑 shell 的 harness 零配置接入。

## Use this skill when

- 任务需要在远程机器/容器上执行命令：编译、安装、冒烟、跑服务、查日志
- 任务需要先把本地代码同步到远程，再在远程执行
- 需要知道哪台机器空闲、谁在用、占用了哪些卡

## Do not use this skill when

- 纯本地编码/本地测试，不涉及任何远程机器
- 任务明确属于机器本身的生命周期维护（`up`/`down`）之外的领域

## 核心规则

1. **一切远程操作走 `remote` CLI，不裸用 ssh。** 不手写 `ssh user@host ...`。
   `remote` 统一封装机器解析、超时、fail-closed 与输出契约；裸 ssh 无法获得
   占用、机器档案、日志等配套能力。
2. **先读机器档案。** 对目标 `<alias>`，先读
   `<repo>/.remote/state/docs/<alias>.md`（简称 `state/docs/<alias>.md`）：
   OS/芯片/卡数、workspace_root、pip index 可达性、proxy 等网络事实。
   档案缺失或过期时先 `remote verify <alias>` 刷新。
3. **干活前查占用。** `remote machines` 查看各机 running jobs 的 owner/task/卡占用，
   避开被占用的机器与卡；`remote status <alias> [--probe]` 看单机详情。
4. **长任务显式声明占用。** 后台任务必须带 `--background --task "<做什么>" --cards <卡号>`
   声明占用（如 `--cards 0,1`），其他 agent 才能经 `remote machines` 看到并避让；
   不声明等于占用不可见。
5. **编译/安装前确认网络事实。** 先读档案中的 pip index / proxy / apt mirror，
   按档案既定源执行。**环境不确定（proxy 怎么配、镜像源不可达、报错与档案不符）时
   停下来问人类**，不盲目重试、不擅自换源。
6. **sync 只同步代码。** `remote sync` 绝不隐式触发编译/install；编译必须由
   `remote run` 显式发起，例如：
   `remote run <alias> --background --task "编译验证" --cmd "pip install --no-deps -e . --no-build-isolation" --timeout 3600`。
7. **输出契约。** 进度走 stderr，最终结果是 stdout 单行 JSON；按 `status`/`exit_code`
   字段判定结果，不要依赖整段输出猜。

## 命令速查

| 命令 | 用途 |
|---|---|
| `remote machines` | 所有机器一览：tags、占用（owner/task/卡）、最近 verify 结论 |
| `remote status <alias> [--probe]` | 单机详情；`--probe` 实时查负载与 NPU 利用率 |
| `remote verify <alias>` | 环境探测，刷新 `state/docs/<alias>.md` 机器档案 |
| `remote up <alias> [--password-env NAME | --password-stdin]` | 从 0 拉起/复用容器 + 免密引导 + 工作区初始化（幂等） |
| `remote down <alias>` | 停止并移除受管容器（默认不执行，需先问人类） |
| `remote sync <alias> [--worktree <id>]` | 方法 A：整树 git 同步（字节级一致，不改行尾） |
| `remote sync <alias> --paths <file>... [--worktree <id>]` | 方法 B：指定路径定向覆盖（热修补） |
| `remote run <alias> --cmd "..." [--worktree <id>] [--cwd <path>] [--env K=V] [--cards 0,1] [--task "..."] [--timeout 600] [--background]` | 远程执行命令 |
| `remote jobs [--machine <alias>]` | 任务列表（唯一查询入口，含 stale 标记） |
| `remote logs <job-id> [--tail 200] [--stderr]` | 读任务日志（本地落盘） |
| `remote stop <job-id>` | 停止后台任务 |

> `remote` 可执行脚本位于仓库根目录：在仓库根目录内用 `./remote <cmd>`，
> 或把仓库根目录加入 PATH 后直接用 `remote <cmd>`。

## 从 0 拉起机器（bootstrap）

机器三种起始状态都收敛到同一条命令 `remote up <alias>`（幂等，可重复执行）：

1. **无镜像**：`remote up` 会 `docker pull` 拉取 `container.image`。
2. **有镜像、无容器**：`remote up` 会 `docker run` 创建名为 `container.name` 的容器（按 `tags.chip` 挂载加速卡设备）。
3. **有镜像、有容器**：`remote up` 复用现有容器，做 `docker exec` 可执行校验 + 工作区初始化，并回 `already ready`；容器未运行/不可 exec 才报 `needs_repair`（不自动重建）。

`up` 同时完成：免密引导（把本地公钥写入宿主机 `authorized_keys`）、
工作区初始化（`workspace_root/main`、`.remote-mirrors`、`core.autocrlf=false`、`core.eol=lf`）。

## 机器未注册时

`remote` 的所有命令都依赖 `.remote/machines.json`（项目级，从 cwd 向上查找）。
目标机器不在其中时，**不要凭空猜配置，经对话问人类补齐后写进 `.remote/machines.json`**：

1. 问人类要：`alias`、`mode`（container/ssh）、`host`、`port`、`user`、密码（仅首次 `up` 用）、
   `container.image/name/workspace_root`、`tags`（chip/cards/os）。
2. 把该机条目追加进 `.remote/machines.json`（机器对象数组）。密码字段用完 `up` 后建议让人类删除。
3. `remote up <alias>` 拉起/复用，再继续正常流程。

> 系统不提供交互式 `remote add` 命令；注册 = 写 `.remote/machines.json`（含密码的该文件禁止入 git）。

## 标准流程

以"找空闲机器 → 同步代码 → 编译/冒烟"为例：

1. `remote machines` → 选择空闲机器（无 running jobs，或占用与你需要的卡不冲突）。
2. 读 `state/docs/<alias>.md`；缺失/过期先 `remote verify <alias>`。确认
   workspace_root、pip index、proxy 等网络事实。
3. `remote sync <alias> --worktree <id>` 同步代码（只对齐代码，不做编译）。
4. 长任务显式声明占用并后台执行：
   `remote run <alias> --worktree <id> --background --task "编译验证" --cards 0,1 --cmd "pip install --no-deps -e . --no-build-isolation" --timeout 3600`
   记录返回的 `job_id`。
5. `remote logs <job-id> --tail 200` 跟踪进度；结束后 `remote jobs --machine <alias>`
   核对占用是否释放。
6. 任何环境不确定（proxy、源不可达、报错与档案不符）→ **停下问人类**，
   不盲目重试、不换源。

## 边界与约定

- 远端路径一律以 `workspace_root` + worktree 解析，禁止写死绝对路径。
- 密码/敏感字段不落盘：不把密码写进命令、日志或 state。
- 所有 SSH 操作有超时上限，半开连接 fail closed；长任务用 `--timeout` 显式声明。
- 前台命令同样生成 Job 记录；任务查询统一走 `remote jobs`。
