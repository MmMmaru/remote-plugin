# remote-plugin

CLI-only 远程开发插件。人类与 agent 经自身 harness 的 bash/shell 工具调用同一条 `remote` CLI，
在远程机器/容器上完成：机器管理、代码同步、远程执行与日志。**不做 MCP server**，任何能跑 shell 的
harness（Codex / Claude Code / deepseek-harness 等）零配置接入。

## 原理

- **唯一形态是 CLI**：`remote <子命令>`；进度走 stderr，最终结果 stdout 单行 JSON，便于 agent 解析。
- **纯标准库 + 系统 ssh**：零第三方依赖，不引入 paramiko/scp/sftp/rsync/sshpass/expect。
- **默认在容器内开发**：模式 A = SSH 到宿主机 + `docker exec` 进容器（容器无需 sshd/端口映射）；
  模式 B = 直接 SSH 端点。
- **占用由 Job 表达**：机器/卡的占用 = 其上 `status=running` 的 Job 集合，`remote machines` 即可见 owner/task/卡。
- **字节级一致**：代码经 `tar|ssh` 或 `git bundle|ssh` 二进制流传输，远端 sha256 抽检，fail-closed。
- **增量同步**：`sync` 会按 repo 查询远端 parity；未变化 repo 跳过，恰好基于上次 snapshot 变化的 repo 发送 delta bundle，首次或状态漂移时回退 full bundle。

## 运行

```bash
# 仓库根目录内直接用（或把仓库根加入 PATH）
./remote <子命令> [参数]
# 安装为全局可发现命令（~/.local/bin/remote）
./remote install
```

`remote install` 会在 `~/.local/bin` 原子创建指向当前插件入口的符号链接。重复执行
是幂等的；若目标已是其他文件或其他链接，命令会拒绝覆盖并返回错误。

机器注册 = 手写 `<repo>/.remote/machines.json`（机器对象数组）。含密码字段的该文件禁止入 git。
状态目录从 cwd 向上找最近的 `.remote/state`；找不到任何 `.remote` 时默认落到
remote-plugin 仓库自身的 `.remote/state`（`remote` 入口脚本所在目录下）。

## 三个功能域

### 1. 机器管理

```bash
./remote up <alias> [--password-env NAME | --password-stdin]  # 从0拉起：pull→run→exec（幂等）
./remote verify <alias>          # 环境探测，写 state/docs/<alias>.md 机器档案
./remote machines                # 所有机器一览：tags、占用、最近 verify 结论
./remote status <alias> [--probe]  # 单机详情；--probe 实时查负载/NPU利用率/显存
./remote down <alias>            # 停删受管容器（默认不执行，先问人类）
```

### 2. 代码同步

```bash
./remote sync <alias>                            # 方法A：整个 workspace git 递归同步
./remote sync <alias> --paths src/a.py tests/    # 方法B：workspace 内指定路径热修补
./remote pull <alias> <remote_path>... --dest <dir>  # 从 workspace 拉回产物（sha256 校验）
```

### 3. 远程执行与日志

```bash
./remote run <alias> --cmd "..." [--cwd <path>] [--env K=V] \
             [--cards 0,1] [--task "编译"] [--timeout 600] [--background] \
             [--logs none|tail|full]
./remote jobs [--machine <alias>]   # 任务列表（含 stale 标记）
./remote logs <job-id> [--tail 200]
./remote stop <job-id>
```

日志保留策略 `--logs`（默认：前台 `none`、后台 `full`）：

- `none`：不落盘、不记录 job（jobs 列表不出现），结果以 run 返回的合并预览
  `preview`（stdout+stderr 截断）为准。
- `tail`：只保留合并日志最后 200 行，落盘 `state/jobs/<job_id>/tail.log`。
- `full`：合并日志全量落盘 `state/jobs/<job_id>/full.log`（后台默认，运行中
  streamer 持续同步）。

日志为 stdout+stderr 合并保存（`remote logs --stderr` 已废弃忽略）；远端
`.remote-logs/` 暂存目录在任务结束后自动删除。

`sync` 默认从当前目录定位 workspace（优先取最近的 `.remote` 根目录），直接同步到远端
`workspace_root`，不再创建或选择 `main` 目录。仓库发现只扫描 workspace 一级目录
（root 本身 + 直接子目录的 `.git`）；已注册的 worktree 由各仓库
`git worktree list` 查询纳入，submodule 由 `.gitmodules` 递归处理。普通目录仍遵循
所属仓库的 `.gitignore` 规则。同步结果中的 `bundle_transfer` 会报告每个 repo 的
`full`、`delta` 或 `skip` 决策。

例如将 `vllm-seu` 的分支 worktree 放在 workspace 内：

```bash
git -C vllm-seu worktree add -b feature .worktrees/vllm-seu-feature
./remote sync <alias>
```

## 快速示例

```bash
# 查空闲机器
./remote machines

# 同步代码到机器，再后台编译（显式声明占用）
./remote sync 192.168.9.166
./remote run 192.168.9.166 --background --task "编译验证" --cards 0,1 \
    --cmd "pip install --no-deps -e . --no-build-isolation" --timeout 3600
./remote logs <job-id> --tail 200
```

## 输出契约

- 进度/事件：stderr，单行 JSON（如 `{"phase":"ready"}`）。
- 最终结果：stdout，单行 JSON（含 `status`/`exit_code`/`preview` 等）。
  `--logs none`（前台默认）不生成 Job 记录，结果不含 `job_id`；保留日志的任务
  含 `job_id`/`logs`/`log`（合并日志相对路径）等字段。
- 错误：stdout 单行 JSON `{"status":"error","error":"..."}`，exit=1，无堆栈。

## 配套

- `docs/PRD.md` / `docs/spec.md` / `docs/workflow.md`：需求、任务拆分与执行流程。
- `skills/remote-plugin/SKILL.md`：给 agent 的使用指引（经 harness 的 bash 调 `remote`）。
- `state/docs/<alias>.md`：机器档案（OS/芯片/卡数/网络事实），干活前先读。

## 开发

纯标准库；`python3 -m unittest discover -s tests` 全绿。
