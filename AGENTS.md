# remote-plugin 项目约定

CLI-only 远程开发插件。人类与 agent 经自身 harness 的 bash/shell 工具调用同一条 `remote` CLI。

## 硬性约定

- **纯标准库 + 系统 ssh**：零第三方依赖，不引入 paramiko/scp/sftp/rsync/sshpass/expect。
- **输出契约**：进度走 stderr，最终结果 stdout 单行 JSON（供 agent 解析）。
- **密码不落盘**：密码只允许存在于用户手写配置的 `password` 字段（该文件已 .gitignore），绝不写入 `state/`、日志或任何其他文件。
- **远端路径**一律以 `workspace_root` 解析，禁止写死绝对路径；`pull` 的相对路径也以该根目录为基准。
- 所有 SSH 操作有超时上限，半开连接 fail closed。

## 目录结构

```
remote_plugin/   # 内核包（config/ssh/output/cli + 各任务模块）
remote           # 可执行入口脚本
tests/           # unittest
docs/            # PRD.md / spec.md / workflow.md
skills/          # 配套 agent skill
```

## 分工与禁改

`cli.py` 预定义全部子命令分发表并惰性 import。子代理**只新建自己的模块文件、实现约定函数名**，禁止修改 `cli.py`、`config.py`、`ssh.py`、`output.py`。

## commit 规范

- 骨架/初始化：`chore:`、`feat: T0 skeleton`
- 各任务：`feat: T1 ...`、`feat: T2 ...` 等按任务分 commit
- 文档修正与收尾：`docs:`、`chore:`

## 开发期验证边界

子代理开发期只执行 spec.md 中标注 **[本地]** 的步骤；**[真机-串行]** 步骤由验收阶段按 workflow.md 顺序统一执行。
