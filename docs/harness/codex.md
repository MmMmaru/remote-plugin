# Codex CLI 接入 remote-plugin

> 对应 PRD 6.2：Codex CLI 的接入方式是 **AGENTS.md 引用 skill**；agent 经自身
> shell 工具调用 `remote` CLI。**无需任何 harness 侧配置**（不装插件、不配 MCP）。

## 接入方式

1. 把仓库内置 skill 挂到 Codex 可见的位置：在仓库根目录 `AGENTS.md` 中用 `@`
   引用 `skills/remote-plugin/SKILL.md`，Codex 启动时即加载其指令。
2. agent 执行远程操作时，经自身 bash/shell 工具调用仓库根目录的 `remote`
   可执行脚本（`./remote <子命令>`），与人类在终端用的是同一条 CLI。

## 最小接入片段

在仓库根目录 `AGENTS.md` 追加以下内容（可整段复制）：

```markdown
# remote-plugin 接入

远程机器操作一律走仓库内置 skill：@skills/remote-plugin/SKILL.md

执行任何远程操作前先阅读该 skill 并遵守其规则；所有远程操作经 shell 工具调用
`remote` CLI（仓库根目录 `./remote`），不裸用 ssh，不自行换源。
```

## 使用示例

```bash
# agent 经 shell 工具执行：先查占用，再同步，再后台编译
./remote machines
./remote sync 192.168.9.166
./remote run 192.168.9.166 --background --task "编译验证" --cards 0,1 \
  --cmd "pip install --no-deps -e . --no-build-isolation" --timeout 3600
```

## 验证

```bash
./remote machines   # 预期：stdout 单行 JSON，含各机 tags 与占用
```
