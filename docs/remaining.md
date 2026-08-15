# remote-plugin 收尾

- [x] 删除 `remote sync`、`pull`、`run` 的 `worktree` 参数；默认直接使用远端 `workspace_root`。
- [x] `sync` 从当前目录定位 workspace，递归发现 Git 仓库，并纳入 workspace 内已注册的 Git worktree。
- [x] 保留原有 bundle/mirror/materialize/HEAD + dirty + sha256 抽检校验链路。
- [ ] 将 `remote` 安装为全局可发现命令（本次最小改动暂不展开）。
