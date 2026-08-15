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
- 需要知道哪台机器空闲、谁在用、占用了哪些资源

## Do not use this skill when

- 纯本地编码/本地测试，不涉及任何远程机器
- 任务明确属于机器本身的生命周期维护（`up`/`down`）之外的领域

## NPU 与 PPU 能力边界

当前插件对 NPU 的支持比 PPU 完整。`tags.chip` 以 `ascend-` 开头时，插件才会
启用 Ascend/NPU 专属探针；PPU 应配置为 `chip: "ppu"`，不能把 PPU 当作 NPU
使用。

- PPU 当前可用：SSH 连通性检查、工作区初始化、代码同步、通用 `remote run`、
  Job/日志管理，以及不依赖 NPU 的 shell、Python 和 CPU 侧冒烟命令。
- PPU 当前不可用或不应执行：`npu-smi`、`torch_npu`、NPU 型号/卡数交叉校验、
  NPU HBM/AICore 利用率探测，以及依赖 `ASCEND_RT_VISIBLE_DEVICES` 的命令。
- `remote verify <alias>` 在 PPU 上只验证通用环境和工作区，不会提供有效的
  `npu_cards` 结果；`remote status <alias> --probe` 的负载、内存和 CPU 信息仍可用，
  但 NPU 利用率字段对 PPU 没有意义。
- `tags.cards` 和 `--cards` 原本用于声明 NPU 卡占用。PPU 尚无等价的卡级探针；
  PPU 的短命令可以不填 `--cards`，长任务只有在已明确资源语义时才声明占用。

因此，在 PPU 上做验证时优先使用 `remote verify`、`remote sync` 和不含 NPU
依赖的 `remote run`。不要用 NPU 专属示例判断 PPU 是否可用。

## 核心规则

1. **一切远程操作走 `remote` CLI，不裸用 ssh。** 不手写 `ssh user@host ...`。
   `remote` 统一封装机器解析、超时、fail-closed 与输出契约；裸 ssh 无法获得
   占用、机器档案、日志等配套能力。
2. **先读机器档案。** 对目标 `<alias>`，先读
   `<repo>/.remote/state/docs/<alias>.md`（简称 `state/docs/<alias>.md`）：
   OS/芯片/卡数、workspace_root、pip index 可达性、proxy 等网络事实。
   档案缺失或过期时先 `remote verify <alias>` 刷新。
3. **干活前查占用。** `remote machines` 查看各机 running jobs 的 owner/task/资源占用，
   避开被占用的机器与资源；`remote status <alias> [--probe]` 看单机详情。
4. **长任务显式声明任务。** 后台任务必须带 `--background --task "<做什么>"`；NPU
   任务再用 `--cards <卡号>` 声明卡占用（如 `--cards 0,1`），其他 agent 才能经
   `remote machines` 看到并避让。PPU 没有已实现的等价卡级占用语义时不要填写
   `--cards`，但仍需填写 `--task`。
5. **编译/安装前确认网络事实。** 先读档案中的 pip index / proxy / apt mirror，
   按档案既定源执行。**环境不确定（proxy 怎么配、镜像源不可达、报错与档案不符）时
   停下来问人类**，不盲目重试、不擅自换源。`remote up` 拉取镜像失败且报错提示
   网络因素时同样照此办理：按提示换镜像源或配 proxy；提示宿主机无 proxy 时，
   向用户问清可用的 proxy/镜像源再重试。
6. **sync 只同步代码。** `remote sync` 绝不隐式触发编译/install；默认按 repo 做增量
   bundle：结果中的 `bundle_transfer.modes` 为 `skip`、`delta` 或 `full`。首次同步、
   parity 丢失或远端漂移时出现 `full` 是预期的 fail-closed 回退。编译必须由
   `remote run` 显式发起，例如：
   `remote run <alias> --background --task "编译验证" --cmd "pip install --no-deps -e . --no-build-isolation" --timeout 3600`。
7. **输出契约。** 进度走 stderr，最终结果是 stdout 单行 JSON；按 `status`/`exit_code`
   字段判定结果，不要依赖整段输出猜。

## 命令速查

| 命令 | 用途 |
|---|---|
| `remote install` | 将当前插件入口原子安装为 `~/.local/bin/remote`，安装后可从任意目录调用 |
| `remote machines` | 所有机器一览：tags、占用（owner/task/资源）、以及 NPU 机器最近 verify 的每卡 HBM/AICore 实测 |
| `remote status <alias> [--probe]` | 单机详情；`--probe` 实时查通用负载，NPU 机器额外查 NPU 利用率/显存 |
| `remote verify <alias>` | 环境探测，刷新 `state/docs/<alias>.md` 机器档案 |
| `remote up <alias> [--password-env NAME | --password-stdin]` | 容器模式拉起/复用容器；SSH 模式只做免密引导与工作区初始化 |
| `remote down <alias>` | 停止并移除容器模式的受管容器；SSH/裸机模式为空操作 |
| `remote sync <alias>` | 方法 A：整个 workspace 的 Git 同步（字节级一致，不改行尾） |
| `remote sync <alias> --paths <file>...` | 方法 B：workspace 内指定路径定向覆盖（热修补） |
| `remote run <alias> --cmd "..." [--cwd <path>] [--env K=V] [--cards 0,1] [--task "..."] [--timeout 600] [--background]` | 远程执行命令，默认 cwd 为 workspace_root |
| `remote jobs [--machine <alias>]` | 任务列表（唯一查询入口，含 stale 标记） |
| `remote logs <job-id> [--tail 200] [--stderr]` | 读任务日志（本地落盘） |
| `remote stop <job-id>` | 停止后台任务 |

> `remote` 可执行脚本位于仓库根目录：首次在仓库根目录执行 `./remote install`，
> 即可通过 `~/.local/bin/remote` 从任意目录调用；也可把仓库根目录直接加入 PATH。

## 从 0 拉起机器（bootstrap）

容器模式的三种起始状态都收敛到同一条命令 `remote up <alias>`（幂等，可重复执行）：

1. **无镜像**：`remote up` 会 `docker pull` 拉取 `container.image`。
2. **有镜像、无容器**：`remote up` 会 `docker run` 创建名为 `container.name` 的容器（按 `tags.chip` 挂载加速卡设备）。
3. **有镜像、有容器**：`remote up` 复用现有容器，做 `docker exec` 可执行校验 + 工作区初始化，并回 `already ready`；容器未运行/不可 exec 才报 `needs_repair`（不自动重建）。

`up` 同时完成：免密引导（把本地公钥写入宿主机 `authorized_keys`）、
工作区初始化（`workspace_root`、`.remote-mirrors`、`core.autocrlf=false`、`core.eol=lf`）。

对于 `mode: "ssh"` 的裸机或已有容器端点，`up` 不执行 `docker pull`、`docker run`
或 `docker exec`，只完成 SSH 免密校验/引导和工作区初始化。

## 机器未注册时

`remote` 的所有命令都依赖 `.remote/machines.json`（项目级，从 cwd 向上查找）。
状态目录（jobs/机器档案/endpoints）同样向上找最近的 `.remote/state`；找不到任何
`.remote` 时默认落到 remote-plugin 仓库自身的 `.remote/state`。
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
3. `remote sync <alias>` 同步整个 workspace（只对齐代码，不做编译）；workspace 内已注册的 Git worktree 会自动纳入。
4. 长任务显式声明占用并后台执行。NPU 任务示例：
   `remote run <alias> --background --task "编译验证" --cards 0,1 --cmd "pip install --no-deps -e . --no-build-isolation" --timeout 3600`
   PPU 任务不要照搬 `--cards`，除非已经明确 PPU 的资源占用语义。
   记录返回的 `job_id`。
5. `remote logs <job-id> --tail 200` 跟踪进度；结束后 `remote jobs --machine <alias>`
   核对占用是否释放。
6. 任何环境不确定（proxy、源不可达、报错与档案不符）→ **停下问人类**，
   不盲目重试、不换源。

## 边界与约定

- 远端路径一律以 `workspace_root` 解析，禁止写死绝对路径；`pull` 的相对路径以该根目录为基准。
- 密码/敏感字段不落盘：不把密码写进命令、日志或 state。
- 所有 SSH 操作有超时上限，半开连接 fail closed；长任务用 `--timeout` 显式声明。
- 前台命令同样生成 Job 记录；任务查询统一走 `remote jobs`。
