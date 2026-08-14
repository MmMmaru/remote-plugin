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

## 运行

```bash
# 仓库根目录内直接用（或把仓库根加入 PATH）
./remote <子命令> [参数]
```

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
./remote sync <alias> [--worktree <id>]          # 方法A：git 递归整树（字节级一致）
./remote sync <alias> --paths src/a.py tests/ [--worktree <id>]  # 方法B：指定路径热修补
```

### 3. 远程执行与日志

```bash
./remote run <alias> --cmd "..." [--worktree <id>] [--cwd <path>] [--env K=V] \
             [--cards 0,1] [--task "编译"] [--timeout 600] [--background]
./remote jobs [--machine <alias>]   # 任务列表（含 stale 标记）
./remote logs <job-id> [--tail 200] [--stderr]
./remote stop <job-id>
```

## 快速示例

```bash
# 查空闲机器
./remote machines

# 同步代码到机器，再后台编译（显式声明占用）
./remote sync 192.168.9.166 --worktree main
./remote run 192.168.9.166 --worktree main --background --task "编译验证" --cards 0,1 \
    --cmd "pip install --no-deps -e . --no-build-isolation" --timeout 3600
./remote logs <job-id> --tail 200
```

## 输出契约

- 进度/事件：stderr，单行 JSON（如 `{"phase":"ready"}`）。
- 最终结果：stdout，单行 JSON（含 `status`/`exit_code`/`job_id` 等）。
- 错误：stdout 单行 JSON `{"status":"error","error":"..."}`，exit=1，无堆栈。

## 配套

- `docs/PRD.md` / `docs/spec.md` / `docs/workflow.md`：需求、任务拆分与执行流程。
- `skills/remote-plugin/SKILL.md`：给 agent 的使用指引（经 harness 的 bash 调 `remote`）。
- `state/docs/<alias>.md`：机器档案（OS/芯片/卡数/网络事实），干活前先读。

## 开发

纯标准库；`python3 -m unittest discover -s tests` 全绿。
