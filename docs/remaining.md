# remote-plugin 收尾

- [x] 删除 `remote sync`、`pull`、`run` 的 `worktree` 参数；默认直接使用远端 `workspace_root`。
- [x] `sync` 从当前目录定位 workspace，递归发现 Git 仓库，并纳入 workspace 内已注册的 Git worktree。
- [x] 保留原有 bundle/mirror/materialize/HEAD + dirty + sha256 抽检校验链路。
- [x] 新增 `remote install`：原子创建 `~/.local/bin/remote` 符号链接，幂等且对非插件占用 fail closed。
- [x] 增量 bundle：复用上次 snapshot parent；按远端 parity 对 repo 做 `skip` / `delta` / `full` 决策，并在 JSON 结果中报告传输方式。
- [x] PPU 重负载实测：旧实现 full bundle 39,515,215 字节 / 132.15 秒；仅修改根仓库后新实现 delta bundle 492 字节 / 4.56 秒，远端 HEAD 与 marker 清理均校验通过。
