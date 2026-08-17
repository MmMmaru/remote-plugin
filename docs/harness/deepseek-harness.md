# deepseek-harness 接入 remote-plugin

> 对应 PRD 6.2：deepseek-harness 的 skill 由 **`ctx.skills` 发现**；agent 经自身
> bash 工具调用 `remote` CLI。**无需任何 harness 侧配置**。

## 接入方式

1. deepseek-harness 会话的 skill 目录暴露为 `ctx.skills`。仓库内置 skill
   （`skills/remote-plugin/SKILL.md`，frontmatter 含 `name: remote-plugin`）被
   会话 skill 目录收录后，agent 即可经 `ctx.skills` 发现并加载它，读取其指令。
2. 加载后，agent 执行远程操作时经自身 bash 工具调用仓库根目录的 `remote`
   可执行脚本（`./remote <子命令>`），与人类在终端用的是同一条 CLI。

## 最小接入片段

agent 侧最小接入（可整段复制）：

```markdown
1. 经 ctx.skills 加载 remote-plugin skill，读取其 SKILL.md 指令。
2. 所有远程操作一律经 bash 工具调用 remote CLI，不裸用 ssh：
   ./remote machines
   ./remote sync <alias>
   ./remote run <alias> --background --task "<做什么>" --cards 0,1 --cmd "<命令>" --timeout <秒>
3. 编译/安装前读 state/docs/<alias>.facts.json 中的 pip index / proxy；如存在，再读
   state/docs/<alias>.md 中的人类补充说明；
   环境不确定时停下来问人类，不盲目重试或换源。
```

## 使用示例

```bash
# agent 经 bash 工具执行：先查占用，再同步，再后台编译
./remote machines
./remote sync 192.168.9.166
./remote run 192.168.9.166 --background --task "编译验证" --cards 0,1 \
  --cmd "pip install --no-deps -e . --no-build-isolation" --timeout 3600
```

## 验证

```bash
./remote machines   # 预期：stdout 单行 JSON，含各机 tags 与占用
```
