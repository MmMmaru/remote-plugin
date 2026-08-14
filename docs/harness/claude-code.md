# Claude Code 接入 remote-plugin

> 对应 PRD 6.2：Claude Code 的接入方式是 **skill 放 `.claude/skills/`**；agent 经
> 自身 Bash 工具调用 `remote` CLI。**无需任何 harness 侧配置**。

## 接入方式

1. 把仓库内置 skill 复制到 Claude Code 的 skill 目录
   `.claude/skills/remote-plugin/`（目录内放 `SKILL.md`）。Claude Code 会自动
   发现该 skill，任务匹配时由 agent 读取其指令。
2. agent 执行远程操作时，经自身 Bash 工具调用仓库根目录的 `remote` 可执行脚本
   （`./remote <子命令>`）。

## 最小接入片段

在仓库根目录执行以下命令（可整段复制）：

```bash
mkdir -p .claude/skills/remote-plugin
cp skills/remote-plugin/SKILL.md .claude/skills/remote-plugin/SKILL.md
# 验证：Claude Code 会话中应能看到 remote-plugin skill
```

## 使用示例

```bash
# agent 经 Bash 工具执行：先查占用，再同步，再后台编译
./remote machines
./remote sync 192.168.9.166 --worktree main
./remote run 192.168.9.166 --worktree main --background --task "编译验证" --cards 0,1 \
  --cmd "pip install --no-deps -e . --no-build-isolation" --timeout 3600
```

## 验证

```bash
./remote machines   # 预期：stdout 单行 JSON，含各机 tags 与占用
```
