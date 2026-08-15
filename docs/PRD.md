# remote-plugin PRD

> 版本：v0.2（CLI-only 重构）
> 状态：待实施

## 1. 定位与目标

通用远程开发插件，提供三个功能域：

1. **远程机器管理**：手写 JSON 注册 + verify 验证；容器与直接 SSH 两种端点模式；基于 Job 的占用管理
2. **远程代码同步**：git 递归传输 + 指定路径传输两种方法；字节级一致
3. **远程执行与日志**：远程 bash 执行；后台任务与日志查询

架构原则：

- **唯一形态是 CLI**：`remote <子命令>`。人类在终端直接用；agent 通过自身 harness 的 bash/shell 工具调用同一条 CLI。**不做 MCP server**，任何能跑 shell 的 harness（Codex、Claude Code、deepseek-harness 等）零配置接入
- **纯标准库 + 系统 ssh**：零第三方依赖，不引入 paramiko；文件与代码传输走 ssh 管道，不用 scp/sftp/rsync/sshpass/expect
- **默认在容器内开发**：整个 workspace 位于容器内，默认路径 `/vllm-workspace`；VM 宿主机仅为容器维护面，不持有代码副本
- 输出契约：进度走 stderr，最终结果 stdout 单行 JSON，方便 agent 解析

## 2. 数据模型

所有配置与状态均为 JSON 文件，变更加载即生效，无数据库。

### 2.1 Machine（机器）

一个 entry = 一个可执行远程工作的目标。两种端点模式：

**模式 A：VM + 容器（默认）**。SSH 到 VM 宿主机，由系统在 VM 上拉起受管容器，工作面在容器内；后续对容器的所有操作经 `docker exec` 完成（容器本身无需 sshd/端口映射）。

```json
{
  "alias": "192.168.9.166",
  "mode": "container",
  "host": "192.168.9.166",
  "port": 22,
  "user": "root",
  "container": {
    "image": "quay.io/ascend/vllm-ascend:nightly-main",
    "name": "xrs_vllm_main",
    "ssh_port": 46000,
    "workspace_root": "/home/x50063850/vllm-ascend-workspace"
  },
  "tags": {
    "chip": "ascend-a2",
    "cards": 8,
    "os": "linux"
  },
  "note": "A2 机器，8 卡"
}
```

**模式 B：直接 SSH 端点**。目标已存在且可直接 SSH（容器或裸机均可，系统不区分）。

```json
{
  "alias": "dev-box",
  "mode": "ssh",
  "host": "173.131.1.2",
  "port": 46000,
  "user": "root",
  "workspace_root": "/vllm-workspace",
  "tags": {
    "chip": "ascend-a3",
    "cards": 8,
    "os": "linux"
  }
}
```

字段约束：

- `alias`：唯一标识，字母数字与 `-_`（直接用 IP 也可以），必填
- `mode`：`container` | `ssh`，默认 `container`
- `workspace_root`：容器/目标内工作区根路径，默认 `/vllm-workspace`
- `tags`：字典形式，描述机器硬件与系统，驱动 verify 的可选探针（见 3.2）与容器设备挂载（见 3.4）：
  - `chip`：加速器型号，如 `ascend-a2` / `ascend-a3` / `nvidia-h100`；`ascend-*` 触发 npu-smi 探针
  - `cards`：卡数量，verify 时与 npu-smi 实测数量交叉校验
  - `os`：系统，如 `linux` / `linux-arm64`
  - 允许追加自定义键（如 `region`、`owner`），系统原样透传展示
- `password`：可选敏感字段，仅用于首次 `up` 免密引导；**含该字段的配置文件禁止入 git**（放用户级或加 .gitignore），`up` 完成后建议删除该字段；日常操作一律走 SSH key（`BatchMode=yes`）

### 2.2 占用（并入 Job，无独立 lease）

机器与卡的占用**由 Job 表达**，不设独立的 lease 数据与命令：

- 一台机器（或其中几张卡）的占用 = 其上 `status=running` 的 Job 集合；Job 记录 `owner`（哪个 agent/人）、`task`（在做什么）、`cards`（占了哪几张卡）
- 需要长时间占机时，用占位 Job 表达：`remote run <alias> --background --cmd "sleep 28800" --task "编译占位"`
- 失联 reconcile：查询时发现本地记录 running 但远端进程已不存在（或机器不可达），标记 `stale` 并不再计入占用
- 占用是 **advisory（提示式）**：`run` 不阻塞，但返回中附带该机器当前 running jobs 的占用提示，由 agent/人自行避让

### 2.3 Workspace（同步根目录）

远端同步根目录就是配置中的 `<workspace_root>`，不再创建或选择 `main` 子目录，也不再向
CLI/API 暴露 `worktree` 参数。

- `remote sync` 从当前目录向上定位本地 workspace（优先取最近的 `.remote` 目录），直接把
  整个 workspace 对齐到远端 `<workspace_root>`。
- 快照递归发现 workspace 内的 Git 仓库；每个仓库按相对 workspace 的路径 materialize。
- 各仓库通过 `git worktree` 注册、且路径位于 workspace 内的 worktree 自动作为独立仓库节点
  纳入同步，即使父仓库的 `.gitignore` 忽略了其目录；workspace 外的 worktree 不同步。
- 其他目录和文件仍由所属仓库的 `.gitignore` 规则筛选；插件运行目录（`.remote`）和远端
  运行时目录（`.remote-mirrors`、`.remote-logs`）不作为业务代码清理。
- `remote run` 缺省 cwd 为远端 `<workspace_root>`；`--cwd` 仍可显式指定绝对路径。

### 2.4 Job（远程任务）

```json
{
  "job_id": "j-20260813-140530-01",
  "machine": "a3-01",
  "cards": [0, 1],
  "owner": "agent-<session-id>",
  "task": "vllm 编译验证",
  "command": "pip install -e .",
  "cwd": "/vllm-workspace",
  "status": "running|done|failed|stopped|timeout",
  "exit_code": null,
  "started_at": "...",
  "finished_at": null,
  "stdout_log": "state/jobs/j-20260813-140530-01/stdout.log",
  "stderr_log": "state/jobs/j-20260813-140530-01/stderr.log"
}
```

- `job_id` 按时间生成：`j-<yyyyMMdd>-<HHmmss>-<两位序号>`，可读、可排序，序号解决同秒冲突
- `cards` 记录该任务占用的 NPU 卡号（来自 `--cards` 参数或 `ASCEND_RT_VISIBLE_DEVICES`），`machines` 查询据此展示**卡级占用**；不指定则为整机或未声明
- `owner` / `task` 记录占用者与用途，是 `machines` 占用展示的依据（见 2.2）；`owner` 默认取环境变量中的 session 标识，缺省为本地用户名
- 前台命令同样生成 Job 记录，便于审计与日志回看
- 日志全文落盘本地状态目录，终端/agent 只看到截断预览，全文用 `logs` 查询

### 2.5 机器档案（env doc）

verify 探测结果写成 Markdown 文档 `state/docs/<alias>.md`，供人类和 agent 直接阅读：

- OS / 内核 / CPU / 内存 / 磁盘余量
- 按 tags 的可选探针结果（如 `chip: ascend-*`：npu-smi 型号与卡数、每卡 HBM/AICore 占用、torch/torch_npu 版本）
- 网络事实：pip index 可达性与实测延迟、apt mirror、http_proxy 等代理 env、DNS
- SSH 实际用户、workspace_root 可写性

## 3. 功能域 1：机器管理

### 3.1 注册

**注册 = 人类手写 JSON 文件**，不提供交互式 add 命令。

- 文件内容为**机器对象数组**（元素结构见 2.1）
- 项目级：从 cwd 向上查找最近的 `.remote/machines.json`（可入 git，无密钥）
- 用户级：`~/.config/remote-plugin/machines.json`（私有机器）
- 合并取并集，alias 冲突时项目级优先并告警

### 3.2 verify

- **CLI**：`remote verify <alias>`
- **内核函数**：`verify_machine(machine: Machine) -> VerifyResult`
- **功能**：对单台机器执行注册验证与环境探测，写入/刷新机器档案文档
- **必做探针**：SSH 连通（BatchMode）、`uname -a`、workspace_root 存在且可写（不存在则提示需 `up`）、磁盘余量
- **可选探针（按 tags）**：`tags.chip` 以 `ascend-` 开头 → npu-smi（并校验实测卡数与 `tags.cards` 一致）、Python/torch/torch_npu 版本；网络探针（所有机器默认开启）→ pip index 可达性、代理 env、apt mirror
- **输出（stdout 单行 JSON）**：`{status: ok|needs_up|unreachable|degraded, facts: {...}, doc: "state/docs/<alias>.md"}`
- 只读，不做任何修复性变更

### 3.3 查询

- **broadcast**：`remote machines` → 所有机器一览：alias、模式、tags、占用状态（空闲 / running jobs 的 owner、task、卡占用 / stale）、每卡实测利用率（`npu_cards`：HBM 用量/总量 + AICore%，来自最近一次 verify）、最近 verify 结论
- **点对点**：`remote status <alias>` → 单机详情 + 可选 `--probe` 实时 SSH 查负载与每卡 NPU 利用率/显存
- **内核函数**：`list_machines() -> list[MachineView]`、`machine_status(alias: str, probe: bool) -> MachineStatus`
- 这是 agent "一条命令看机器占用" 的入口

### 3.4 up / down（容器生命周期 + 工作区初始化，合并为一步）

- **CLI**：`remote up <alias> [--password-env NAME | --password-stdin]` / `remote down <alias>`；密码来源优先级：配置 `password` 字段 > `--password-env` > `--password-stdin`
- **内核函数**：`machine_up(machine: Machine, password: str | None) -> Endpoint`、`machine_down(machine: Machine) -> None`
- **`up` 流程（模式 A，按序执行）**：
  1. **免密引导**：初始只有账户 + 密码（来自配置 `password` 字段或 stdin/env，绝不进日志与 state）→ 将本地公钥写入 VM `authorized_keys`，之后所有 VM 操作走免密 key 登录
  2. **容器**：确认 docker 可用 → 拉取 `container.image`（不存在时）→ 创建/复用名为 `container.name` 的容器（按 `tags.chip` 挂载对应加速卡设备）→ 校验容器可 `docker exec` → 写入本地 endpoint 状态（记录宿主机 + 容器名），后续所有操作 = **SSH 到宿主机 + `docker exec` 进容器**（容器无需 sshd、无需端口映射）
  3. **工作区初始化**（原 `init`，并入本步）：经 `docker exec` 在容器内创建 `workspace_root` 与 git mirror 缓存目录；设置 `core.autocrlf=false`、`core.eol=lf` 等同步前置配置
- **模式 B 的 `up`**：只做免密配置校验 + 上述第 3 步工作区初始化

> 说明：模式 A 的容器访问**统一经 `docker exec`**（SSH 到宿主机后执行 `docker exec -i <container> <cmd>`），不要求在容器内跑 sshd、不做端口映射；`container.ssh_port` 字段保留但不再使用（向后兼容）。
- `down`：停止并移除受管容器，不动 VM 上其他资源
- 镜像、容器名等全部来自 JSON 配置，**系统不做镜像策略推断**（无 rc/main/stable 选择器）
- 幂等：已存在且健康的容器直接复用；漂移时返回 `needs_repair` 而非自动重建
- 首次变更性操作（sync/run 写路径）发现未完成 `up` 时返回 `blocked: need up`，由人类执行一次 `up` 即可，无多道 consent 闸

## 4. 功能域 2：代码同步

本地工作树为 source of truth：committed + staged + unstaged + untracked 非 ignored，**不要求 commit/push**。

### 4.1 方法 A：git 递归传输（默认，整树同步）

- **CLI**：`remote sync <alias>`
- **内核函数**：`sync_git(machine: Machine, local_root: Path) -> SyncResult`
- **流程**（继承 vllm remote-code-parity 主线，裁剪后）：
  1. postorder 递归（叶子 submodule → 父 submodule → 根 repo）为每个 repo 构造**确定性 parentless synthetic snapshot commit**：临时 index 全量 add，剔除 ignore 与子模块路径，子模块 gitlink 替换为子 snapshot id；真实 HEAD 记为 `source_head`
  2. 每个 repo 生成 git bundle，经 **ssh 二进制流**送入容器内 mirror（`<workspace_root>/.remote-mirrors/`），fetch 后更新 parity ref
  3. materialize：workspace 内各 repo 目录 fetch + 强制对齐到 snapshot ref，子模块 URL 改写为容器内 mirror 路径并递归显式展开
  4. 按既有校验逻辑检查容器内各 repo commit id、dirty 状态和 sha256 抽检，任一不符即 fail closed
- **输出**：`{status: ready|no_change|blocked|failed, snapshots: {repo: sha}, remote_heads: {repo: sha}, changed_paths: [...]}`
- no-change 快路径：snapshot 与上次一致时单次 SSH 校验后直接返回

### 4.2 方法 B：指定文件/路径传输

- **CLI**：`remote sync <alias> --paths src/foo.py tests/`
- **内核函数**：`sync_paths(machine: Machine, paths: list[Path], local_root: Path) -> SyncResult`
- **功能**：将指定文件/目录经 `tar | ssh` 覆盖到 workspace_root 对应相对路径；不做 git 语义，不进 mirror
- 用于热修补、单文件调试；大目录请用方法 A
- **反向（产物拉回）**：`remote pull <alias> <remote_path>... --dest <dir>` 把远端文件/目录经 `tar | ssh` 二进制流拉回本地（相对路径按 workspace_root 解析，支持容器内绝对路径；远端 sha256 清单 + 本地重算比对，不一致 fail closed），用于 profiling/benchmark 产物下载

### 4.3 字节级一致（明确验收项）

- 问题：现有 parity 在 Windows 发起传输时行尾 LF 被改写
- 措施：bundle/tar 一律走 ssh stdin/stdout **二进制流**；容器内 git 配置 `core.autocrlf=false`、`core.eol=lf`；materialize 后按 sha256 抽检文件内容一致性
- 验收：从 Windows 客户端同步含 shell 脚本的仓库，远端 `sha256sum` 与本地逐一相等

### 4.4 边界

- sync **只做代码对齐**，绝不隐式触发编译/install；无 changed-paths trigger 矩阵
- 编译由 agent 或用户在 run 中显式执行（见 5.3）

## 5. 功能域 3：远程执行与日志

### 5.1 远程执行

- **CLI**：`remote run <alias> --cmd "..." [--cwd <path>] [--env K=V ...] [--cards 0,1] [--task "..."] [--timeout 600] [--background]`
- **内核函数**：`run_remote(machine: Machine, command: str, cwd: str | None, env: dict, cards: list[int] | None, task: str | None, timeout_sec: int, background: bool) -> Job`
- **功能**：经 ssh 在容器内执行命令；默认 cwd 为 workspace_root；`--background` 立即返回 job_id
- **输出（前台）**：截断后的 stdout/stderr 预览 + exit_code + 日志文件路径（预览 head/tail 各 4000 字符，无多层 envelope）

### 5.2 任务与日志

- **CLI**：`remote jobs [--machine <alias>]` / `remote logs <job-id> [--tail 200] [--stderr]` / `remote stop <job-id>`
- **内核函数**：`jobs(machine: str | None) -> list[Job]`、`job_tail(job_id: str, tail: int, stream: str) -> str`、`job_stop(job_id: str) -> Job`
- 任务列表与详情统一走 `jobs` 一个入口，可按 machine 过滤，含 running 任务的机器与卡占用
- 日志全文落盘 `state/jobs/<job-id>/`，本地查询不再 SSH（`--follow` 实时跟踪除外）
- 后台任务超时上限默认 30min 可配，超时杀进程并保留日志

### 5.3 编译

- 系统**不提供写死的 build 工具**
- 编译 = 普通 `run` 调用，例如 `remote run a3-01 --cmd "pip install --no-deps -e . --no-build-isolation" --timeout 3600 --background`
- agent 执行编译前应读机器档案文档（网络事实）、先查 `machines` 占用并以 job 声明自己在用的机器/卡；环境不确定时走 skill 询问人类（见 6.1）

## 6. Agent 集成

### 6.1 配套 skill

- 仓库内置一个 skill（Markdown 指令包，可被 Claude Code / deepseek-harness / Codex 的 AGENTS.md 引用），指导 agent：
  - 远程工作一律通过 `remote` CLI（经自身 bash 工具调用），不直接裸用 ssh
  - 执行远程工作前先 `remote machines` 查占用（各机 running jobs 的 owner/task/卡），避开被占用的机器和卡
  - 编译/安装前读机器档案文档，确认 pip index、proxy 等网络事实
  - **遇到环境不确定（如 proxy 怎么配、镜像源不可达）时询问人类**，不盲目重试或换源

### 6.2 各 harness 接入

无需任何 harness 侧配置，agent 能跑 shell 即可：

| Harness | 接入方式 |
|---|---|
| Codex CLI | AGENTS.md 中引用 skill；agent 经 shell 工具调 `remote` CLI |
| Claude Code | skill 放 `.claude/skills/`；agent 经 Bash 工具调 `remote` CLI |
| deepseek-harness | skill 由 `ctx.skills` 发现；agent 经 bash 工具调 `remote` CLI |

## 7. 配置与状态

```
<repo>/.remote/machines.json        # 项目级机器注册（可入 git）
~/.config/remote-plugin/machines.json  # 用户级机器注册
<repo>/.remote/state/
  jobs/<job-id>/{meta,stdout,stderr}*.json|log
  docs/<alias>.md                   # 机器档案
  endpoints/<alias>.json            # 模式 A 解析出的容器直连端点
```

- 状态目录解析：从 cwd 向上找最近的 `.remote`，用其 `state/`；**找不到任何
  `.remote` 时，默认落到 remote-plugin 仓库自身的 `.remote/state`**（即 `remote`
  入口脚本所在目录下），不存在则创建，不报错。
- 所有变更一律写文件；CLI 只做读取与触发
- `state/` 不入 git（自动写 `.gitignore`）

## 8. 非目标（明确砍掉）

- MCP server 及相关的 resources、result envelope、hooks 层、read-ledger
- 远程 read/write/edit/multi_edit/apply_patch 工具（文件操作在本地完成，远端只过 run 与 sync）
- per-agent 独立容器；session 三件套（worktree+容器+lease 合体）
- 独立 lease 数据与 claim/release 命令（占用由 Job 表达）
- 镜像选择策略（rc/main/stable 推断）、apt/pip 源写死逻辑进内核（只写进机器档案文档）
- sync 隐式触发编译、consent 多道闸

## 9. 验收标准

1. **CRLF 修复**：Windows 客户端发起方法 A 同步，远端 sha256 与本地逐文件一致
2. **容器全链路**：手写 machines.json → `verify` → `up`（密码引导 → 免密 → docker → 工作区初始化，一步完成）→ `sync` → `run` 全绿
3. **直接 SSH 全链路**：模式 B 机器 `verify` → `sync --paths` → `run --background` → `logs` 全绿
4. **多 agent 占用可见**：agent A 在某机起后台 job 后，agent B 的 `remote machines` broadcast 能看到该 job 的 owner/task/卡占用并据此避让；失联 job 正确标记 `stale`
5. **同步两方法**：方法 A 含 submodule 递归与 dirty 树；方法 B 单文件热修
6. **agent 接入**：在至少一种 harness（Codex 或 Claude Code）中，agent 仅凭 skill 指引即可正确使用 `remote machines` / `remote run` / `remote sync` 完成一次远程任务
7. 纯标准库，`python3 -m compileall` 与 unittest 全绿
