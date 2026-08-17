# remote-plugin 实施规格（任务拆分）

> 依据 `docs/PRD.md` v0.2。所有任务共用代码风格：纯标准库、系统 ssh、CLI 输出"进度 stderr / 结果 stdout 单行 JSON"。
> 端到端验证采用文档形式：每步标注 **[本地]**（子代理开发时自验）或 **[真机-串行]**（验收阶段按 workflow.md 的顺序对真机执行）。

## 测试机器

已写入 `<workspace>/.remote/machines.json`（数组格式）：

- alias `192.168.9.166`，模式 A（VM+容器），root@192.168.9.166:22
- 容器：`quay.io/ascend/vllm-ascend:nightly-main`，名 `xrs_vllm_main`（经 `docker exec` 访问，无需 sshd）
- workspace_root `/home/x50063850/vllm-ascend-workspace`；tags：chip=ascend-a2、cards=8、os=linux

真机 e2e 的串行顺序（避免并行冲突）：**T2 up → T1 → T3 → T4 → T5 →（T2 down 需用户确认，默认不执行）**。

## 任务总览与并行关系

```
T0 骨架（config + ssh + CLI 框架）   ← 唯一前置，串行先行
   ├─ T1 机器查询与 verify
   ├─ T2 up / down（容器生命周期）
   ├─ T3 run / jobs / logs / stop（执行与日志）
   ├─ T4 sync 方法 B（指定路径传输）
   ├─ T5 sync 方法 A（git 递归传输）
   └─ T6 skill 与 harness 接入
```

T1–T6 开发互不依赖，可全部并行；子代理只做 **[本地]** 验证，**[真机-串行]** 步骤在验收阶段统一执行。

---

## T0 骨架（前置，阻塞其他任务）

**目标**：仓库结构、配置加载、SSH 传输原语、CLI 入口框架。

**文件架构**：

```
remote-plugin/
  remote_plugin/
    __init__.py
    config.py        # machines.json 加载与合并
    ssh.py           # ssh 传输原语
    output.py        # stdout 单行 JSON / stderr 进度契约
    cli.py           # argparse 分发（子命令分发表预定义，惰性 import）
    install.py       # 本地全局 remote 入口安装器
  remote             # 可执行入口脚本
  tests/
  docs/{PRD.md, spec.md, workflow.md}
```

**关键函数**：

- `config.load_machines(start_dir: Path | None) -> dict[str, Machine]`
  - 从 `start_dir`（缺省 cwd）**向上查找最近的 `.remote/machines.json`**，与 `~/.config/remote-plugin/machines.json` 合并
  - 文件格式：**机器对象 JSON 数组**（见 PRD 2.1）
  - 输出：alias → Machine；alias 冲突时项目级优先并 stderr 告警；非法 JSON / 缺字段报明确行号与字段名
  - 字段校验：alias/mode/host/port 必填；tags 必须为 dict（chip/cards/os + 自定义键透传）；`password` 为可选敏感字段——加载后只驻留内存供 `up` 使用，发现含 password 的项目级文件被 git 跟踪时 stderr 告警
- `config.resolve_endpoint(machine: Machine, state_dir: Path) -> Endpoint`
  - 输出 `{host, port, user, workspace_root}`；模式 A 优先读 `state/endpoints/<alias>.json`（up 写入的容器直连端点），缺失回退宿主机端点
- `ssh.ssh_run(endpoint, script: str, timeout_sec: int, input_bytes: bytes | None) -> CompletedProcess`
  - `ssh -o BatchMode=yes ... bash -s`，stdin 传 script / 二进制流；超时强杀
- `ssh.ssh_pipe(endpoint, local_cmd: list[str], remote_cmd: str) -> int`
  - 本地命令 stdout → ssh stdin 的管道传输（tar/bundle 用）
- `output.emit(obj: dict) -> None`：stdout 单行 JSON；`output.progress(obj: dict) -> None`：stderr 进度
- `cli.py` 预定义分发表 `COMMANDS = {"install": ("remote_plugin.install", "cli_install"), ...}`，惰性 import；新增子命令需同步注册分发表与 argparse

**E2E 验证**：

1. **[本地]** 以 `<workspace>/.remote/machines.json`（真实文件）为输入：`cd remote-plugin && python3 -c "from remote_plugin import config; print(config.load_machines())"`
   - 预期：解析出 alias `192.168.9.166`，tags 为 dict（chip=ascend-a2 / cards=8 / os=linux），workspace_root 正确
2. **[本地]** 在 `remote-plugin/.remote/` 放一个冲突 alias 的 JSON → 预期项目级（就近）生效且 stderr 有告警
3. **[本地]** 构造缺 `host` 的数组元素 / 非法 JSON → 预期报错含元素下标与字段名，无异常堆栈
4. **[本地]** `remote install --help`、`remote verify --help`、`remote up --help` 等全部子命令可用（占位实现报"未实现"但解析正常）

## T0.1 全局入口安装

- `install.InstallResult`：记录 `install_path: Path`、`command_name: str`、
  `already_exists: bool`，`to_dict()` 返回 CLI JSON。
- `install.install_launcher(source, bin_dir)`：校验入口文件，创建安装目录，以原子
  符号链接安装；目标已存在且不是同一插件链接时拒绝覆盖。
- `install.cli_install(args)`：从当前插件目录定位根入口 `remote`，调用安装器并返回结果。
- **[本地]** 在临时目录执行首次安装、重复安装、普通文件占用、其他链接占用和悬空链接
  占用测试；每种占用场景均保持原文件不变。

---

## T1 机器查询与 verify

**依赖**：T0。**文件架构**：`remote_plugin/machines.py`（verify/machines/status）、`remote_plugin/probes.py`（探针脚本生成）。

**关键函数**：

- `machines.verify_machine(machine: Machine) -> VerifyResult`
  - 输出 `{status: ok|needs_up|unreachable|degraded, facts: dict, facts_path: Path}`
  - 必做探针：SSH 连通、uname、workspace_root 可写（缺失 → `needs_up`）、磁盘余量
  - tags 驱动：`chip` 以 `ascend-` 开头 → npu-smi 型号/卡数（与 `tags.cards` 交叉校验，不符 → `degraded`）、torch/torch_npu 版本
  - 网络探针（所有机器）：pip index 可达性与延迟、代理 env、apt mirror
  - 结果写 `state/docs/<alias>.facts.json`（agent 读取的结构化 facts）；
    `state/docs/<alias>.md` 由人类维护，verify 不创建、不覆盖
- `machines.list_machines() -> list[MachineView]`：alias/mode/tags/占用（读 state/jobs 的 running 记录）/最近 verify 结论
- `machines.machine_status(alias: str, probe: bool) -> MachineStatus`：`probe=True` 实时 SSH 查负载与 npu-smi
- `probes.build_probe_script(tags: dict) -> str`：纯函数，可单测

**E2E 验证**：

1. **[本地]** `build_probe_script` 单测：`{"chip":"ascend-a2","cards":8}` → 脚本含 npu-smi 与卡数校验；`{"chip":"nvidia-h100"}` → 不含 npu-smi
2. **[真机-串行，T2 之后]** `remote verify 192.168.9.166`
   - 预期：`status: ok`；`state/docs/192.168.9.166.facts.json` 含 A2 SoC 型号、实测 8 卡、torch/torch_npu 版本、pip index 延迟、代理 env、磁盘余量
3. **[真机-串行]** `remote machines` → 该机行显示 chip=ascend-a2/cards=8/os=linux、占用（来自 jobs）、verify=ok
4. **[真机-串行]** 临时把 machines.json 的 `cards` 改为 4 → verify 报 `degraded` 并注明"实测 8 ≠ 配置 4"；改回
5. **[真机-串行]** `remote status 192.168.9.166 --probe` → 实时负载与 npu-smi 利用率
6. **[本地]** 对不存在 alias / 不可达 IP verify → `unreachable`，无堆栈

---

## T2 up / down（容器生命周期 + 免密引导 + 工作区初始化）

**依赖**：T0。**文件架构**：`remote_plugin/updown.py`、`remote_plugin/bootstrap.py`。

**关键函数**：

- `updown.machine_up(machine: Machine, password: str | None) -> Endpoint`
  - ① 免密引导：密码经 stdin/env 传入（不落盘、不进日志），公钥写 VM `authorized_keys`；已免密则跳过
  - ② 容器：docker 可用性 → 拉镜像（无则 pull）→ 创建/复用 `xrs_vllm_main`（按 `tags.chip` 挂设备）→ 校验可 `docker exec` → 写 `state/endpoints/<alias>.json`（记录宿主机 + 容器名，**不含 sshd/端口映射**）
  - ③ 工作区初始化：`workspace_root`、`main/`、mirror 缓存目录；`core.autocrlf=false`、`core.eol=lf`
  - 幂等：健康容器复用并回 `already ready`；容器未运行/无法 exec → `needs_repair` 不自动重建；镜像版本、设备挂载差异仅告警（复用），NPU 可用性由 `verify` 兜底；模式 B 只做免密校验 + ③
- `updown.machine_down(machine: Machine) -> None`：停删受管容器，不动 VM 其他资源
- `bootstrap.push_pubkey(endpoint, pubkey: str, password: str | None) -> None`
- `bootstrap.ensure_container(vm: Endpoint, container: ContainerCfg, tags: dict) -> None`：pull → run → 校验 `docker exec`；健康容器复用，漂移回 `needs_repair`

**E2E 验证**：

1. **[本地]** 单测：up 流程的步骤编排用 fake ssh 层打桩——断言顺序为 免密→docker→(pull/run/exec 校验)→工作区；已免密时跳过密码路径；漂移返回 needs_repair
2. **[真机-串行，最先执行]** machines.json 已含 `password` 字段：`remote up 192.168.9.166`（免交互，密码取自配置）
   - 预期：全链路一次完成；`state/endpoints/192.168.9.166.json` 含 `container: xrs_vllm_main`；密码只存在于 machines.json，不出现在 state/ 与任何日志（grep 验证）
3. **[真机-串行]** 再次 `remote up 192.168.9.166` → `already ready` 秒回
4. **[真机-串行]** `ssh -o BatchMode=yes admin123@192.168.9.166 "docker exec xrs_vllm_main npu-smi info -l"` → 免密成功、容器内可见 8 卡
5. **[真机-串行]** 容器内 `/home/x50063850/vllm-ascend-workspace/.remote-mirrors` 存在；`git config --global core.autocrlf` 为 false
6. **[真机-串行，可选，需用户确认]** `remote down 192.168.9.166` → 容器删除，VM 上其他容器不受影响；**默认不执行**

---

## T3 run / jobs / logs / stop（执行与日志）

**依赖**：T0。**文件架构**：`remote_plugin/runner.py`、`remote_plugin/jobs.py`。

**关键函数**：

- `runner.run_remote(machine, command, cwd, env, cards, task, timeout_sec, background) -> Job`
  - 默认 cwd = `<workspace_root>`；不再通过 `worktree` 参数选择远端目录
  - stdout/stderr 落盘 `state/jobs/<job_id>/`；前台返回截断预览（head/tail 各 4000 字符）+ exit_code + 日志路径；`--background` 立即返回 job_id
  - 返回 JSON 附带该机当前 running jobs 占用提示（advisory，不阻塞）
- `jobs.new_job_id() -> str`：`j-<yyyyMMdd>-<HHmmss>-<两位序号>`
- `jobs.jobs(machine: str | None) -> list[Job]`：唯一查询入口；reconcile stale（本地 running 但远端进程消失/不可达 → `stale`）
- `jobs.job_tail(job_id, tail: int, stream: str) -> str`：读本地日志；`--follow` 才 SSH
- `jobs.job_stop(job_id) -> Job`：远端杀进程组 → `stopped`
- `owner` 默认取 `CLAUDE_SESSION_ID`/`CODEX_SESSION_ID` 等 env，缺省本地用户名

**E2E 验证**：

1. **[本地]** `new_job_id` 单测：同秒连发 3 个 → 序号 01/02/03 递增；格式正则 `j-\d{8}-\d{6}-\d{2}`
2. **[本地]** fake ssh 打桩：前台命令的截断预览（构造 >4000 字符输出验证 head/tail）；stale reconcile（远端无进程 → stale）
3. **[真机-串行]** `remote run 192.168.9.166 --cmd "echo hi && npu-smi info -l"` → `exit_code: 0`，预览含输出
4. **[真机-串行]** `remote run 192.168.9.166 --background --task "占坑" --cards 0,1 --cmd "sleep 600"` → 返回 `j-<日期>-<时分秒>-01`
5. **[真机-串行]** 另开终端 `remote machines` → 该机显示卡 0,1 被"占坑"占用及 owner；`remote jobs --machine 192.168.9.166` 列出该 job
6. **[真机-串行]** `remote logs <job-id> --tail 50` 读到日志；`remote stop <job-id>` → `stopped`，machines 不再计入占用
7. **[真机-串行]** `remote run 192.168.9.166 --timeout 5 --cmd "sleep 60"` → 5 秒后 `timeout`，日志保留
8. **[真机-串行]** 容器内手杀某 running job 的进程 → 下次 `remote jobs` 该记录为 `stale`

---

## T4 sync 方法 B（指定文件/路径传输）

**依赖**：T0。**文件架构**：`remote_plugin/sync_paths.py`。

**关键函数**：

- `sync_paths.sync_paths(machine: Machine, paths: list[Path], local_root: Path) -> SyncResult`
  - 本地 `tar` 打包指定路径（保留相对结构）→ `ssh | tar -x` 覆盖到 `<workspace_root>`；二进制流传输不改行尾；传输后 sha256 抽检
  - 输出 `{status: ready|failed, files: int, bytes: int, sha256_ok: bool}`
  - 校验：paths 必须存在且位于 local_root 内；空列表报错

**E2E 验证**：

1. **[本地]** 单测：paths 越界（`../x`）、不存在、空列表 → 明确报错；tar 打包清单含目录递归
2. **[真机-串行]** 修改 `remote_plugin/config.py` + `docs/PRD.md` + 新建一目录，执行
   `remote sync 192.168.9.166 --paths remote_plugin/config.py docs/PRD.md docs/`
   - 预期：`status: ready`、`sha256_ok: true`、files/bytes 计数正确
3. **[真机-串行]** 远端 `sha256sum` 与本地逐文件一致
4. **[真机-串行]** 同步一个 LF 行尾的 shell 脚本 → 远端 `od -c` 验证行尾仍为 LF（本地模拟 Windows 场景：以 `core.autocrlf=true` 的客户端配置同步，结果不变）

---

## T5 sync 方法 A（git 递归传输）

**依赖**：T0。**文件架构**：`remote_plugin/sync_git.py`、`remote_plugin/snapshot.py`（可纯本地单测）。

**关键函数**：

- `snapshot.build_snapshots(local_root: Path, extra_repositories: list[tuple[Path, str]] | None = None, parent_commits: dict[str, str] | None = None) -> SnapshotSet`
  - postorder 递归（叶子 submodule → 父 → 根）；每 repo 临时 index 全量 add、剔除 ignored 与子模块路径、gitlink 替换为子 snapshot id；记录 `source_head`
  - 对 `parent_commits` 中仍存在的旧 snapshot：内容不变时复用旧 commit，内容变化时以旧 commit 为 parent；无可用 parent 时生成 parentless commit
  - 输出：每 repo 的 snapshot sha + changed_paths + parent_commit
- `sync_git.sync_git(machine: Machine, local_root: Path) -> SyncResult`
  - snapshot 后一次查询远端 parity；按 repo 选择 `skip`（parity 等于当前 snapshot）、`delta`（parity 等于 parent，发送 `parent..current`）或 `full`（首次/缺失/漂移）
  - bundle → ssh 二进制流 → 容器内 mirror（`<workspace_root>/.remote-mirrors/`）→ parity ref → materialize 到 workspace 内各 repo 目录（子模块 URL 改写为容器内 mirror，递归显式展开）→ 按旧逻辑校验 commit/dirty/sha256，不符 fail closed
  - snapshot 还会发现 workspace 内 registered Git worktree，并将其作为独立 repo 节点同步
  - no-change 快路径：snapshot 与上次一致 → 单次 SSH 校验后回 `no_change`
  - 输出 `{status: ready|no_change|blocked|failed, snapshots: {...}, remote_heads: {...}, changed_paths: [...], bundle_transfer: {pushed: [...], skipped: [...], modes: {...}}}`
  - 绝不触发编译/install；未 `up` 过 → `blocked: need up`
- 参考实现（裁剪重写，不照抄）：`vllm-ascend-workspace/.agents/skills/remote-code-parity/scripts/remote_code_parity.py`

**E2E 验证**：

1. **[本地]** 单测：临时目录造"主 repo + 一层 submodule"，dirty（改跟踪文件 + 新未跟踪文件）→ `build_snapshots` 产物确定性（连跑两次 sha 相同）、gitlink 替换正确、`source_head` 记录正确
2. **[真机-串行]** 用上述 fixture 仓库：`remote sync 192.168.9.166`
   - 预期：`status: ready`；远端 workspace 内各 repo commit 与返回的 snapshots 一致；dirty 改动与未跟踪文件在远端可见
3. **[真机-串行]** 无改动再跑 → `no_change` 且只有一次轻量 SSH（用 ssh 调用计数或耗时佐证）
4. **[真机-串行]** 修改 submodule 内容后 sync → 远端可见；切 submodule 到另一 commit 后 sync → 版本正确落地
5. **[真机-串行]** 远端 sha256 抽检与本地一致（CRLF 验收：`core.autocrlf=true` 客户端配置下同步，远端仍 LF）
6. **[真机-串行]** 在同一远端先做一次 full baseline，再只修改根仓库文件重复同步；记录 wall time、`bundle_transfer.modes` 与 bundle 字节数，确认未变化子仓库为 `skip`、根仓库为 `delta`
7. **[人工]** 可选重负载：对 vllm-ascend-workspace 整树（含 vllm/vllm-ascend 子模块）同步一次，记录耗时与结果

---

## T6 skill 与 harness 接入

**依赖**：T1–T5 CLI 面稳定（可并行先写）。**文件架构**：

```
skills/remote-plugin/SKILL.md
docs/harness/{codex.md, claude-code.md, deepseek-harness.md}
```

**SKILL.md 要点**：

- 远程工作一律走 `remote` CLI，不裸用 ssh；先读 `state/docs/<alias>.facts.json`，再按需读人类维护的 `state/docs/<alias>.md`
- 干活前 `remote machines` 查占用；长任务 `--background --task "..." --cards ...` 声明占用
- 编译/安装前确认档案中的 pip index、proxy；**环境不确定就问人类**，不盲目重试换源
- sync 只同步代码，编译用 `remote run` 显式发起

**E2E 验证**：

1. **[本地]** 三份 harness 文档各含一条可复制的最小接入片段；SKILL.md frontmatter 合法（name/description）
2. **[真机-串行]** 在任一 harness 挂载 skill，给 agent 任务："查哪台机器有空闲卡，把当前仓库同步过去并跑 `import torch_npu` 冒烟"
   - 预期：agent 顺序使用 `remote machines` → `remote sync` → `remote run`，全程无裸 ssh、无配置改动；遇环境异常停下来问人

---

## 公共约束（所有任务遵守）

- 密码只允许存在于用户手写配置的 `password` 字段（该文件禁止入 git）；绝不写入 state/、日志或任何其他文件
- CLI 统一 `remote <cmd>`；进度 stderr、结果 stdout 单行 JSON
- 远端路径一律以 `workspace_root` 解析，禁止写死绝对路径
- 所有 SSH 操作有超时上限，半开连接 fail closed
- 子代理开发期只执行 **[本地]** 验证；**[真机-串行]** 步骤由验收阶段按序执行
