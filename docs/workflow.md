# remote-plugin 执行工作流

> 本文档描述从初始化到验收的完整执行流程。配套：`docs/PRD.md`（需求）、`docs/spec.md`（任务拆分与 e2e）。
> 测试机器：`<workspace>/.remote/machines.json` 中的 `192.168.9.166`（A2，8 卡）。

## 阶段 0：初始化（主代理直接执行）

1. `cd remote-plugin && git init`
2. 写 `.gitignore`（`state/`、`__pycache__`、`.temp`、`.log`）
3. 写根 `AGENTS.md`（项目约定：纯标准库、输出契约、单文件 ≤600 行、commit 规范）与 `PROGRESS.md`
4. 首次提交：`chore: init remote-plugin with PRD and spec`

## 阶段 1：T0 骨架（主代理直接执行，约 200 行）

实现 spec.md T0：`config.py` / `ssh.py` / `output.py` / `cli.py` + 入口脚本。

**防并行冲突设计**：`cli.py` 预定义全部子命令分发表并惰性 import；后续子代理**只新建自己的模块文件、实现约定函数名，禁止修改 `cli.py`、`config.py`、`ssh.py`、`output.py`**。

T0 自验：compileall + spec.md T0 的 4 条 **[本地]** 步骤。通过后提交 `feat: T0 skeleton`。

## 阶段 2：子代理并行（AgentSwarm，6 个 coder 子代理）

公共提示词模板（`{{item}}` 为任务差异部分）：

> 你在 `<workspace>/remote-plugin` 仓库工作。这是一个 CLI-only 远程开发插件。先全读 `docs/PRD.md`、`docs/spec.md`，再读 `remote_plugin/config.py`、`ssh.py`、`output.py`、`cli.py` 了解 T0 约定。
>
> 你的任务：**{{item}}**
>
> 硬性约束：纯标准库 + 系统 ssh；单文件 ≤600 行；只允许新建/修改你任务名下的文件与 `tests/` 下你的测试文件，**禁止改 `cli.py`、`config.py`、`ssh.py`、`output.py`**；函数签名与 spec.md 一致；纯函数配硬编码 unittest；**不 commit**；只执行你任务 e2e 中标注 **[本地]** 的步骤，**[真机-串行]** 步骤不许执行。
>
> 完成后汇报：新建文件清单、函数与 spec 的对应关系、unittest 结果、[本地] e2e 逐步结果。

各任务 `{{item}}`：

- **T1**：实现 spec.md T1——`machines.py` + `probes.py`（verify/machines/status、tags 探针、机器档案 Markdown）
- **T2**：实现 spec.md T2——`updown.py` + `bootstrap.py`（免密引导→docker→pull/run/exec 进容器→工作区初始化，幂等，密码不落盘）
- **T3**：实现 spec.md T3——`runner.py` + `jobs.py`（时间格式 job_id、卡占用、截断预览、stale reconcile、超时强杀）
- **T4**：实现 spec.md T4——`sync_paths.py`（tar|ssh 定向传输 + sha256 抽检）
- **T5**：实现 spec.md T5——`sync_git.py` + `snapshot.py`（synthetic snapshot、bundle、mirror materialize、fail closed；参考实现见 spec，裁剪重写不照抄）
- **T6**：实现 spec.md T6——`skills/remote-plugin/SKILL.md` + `docs/harness/` 三份接入文档

## 阶段 3：最终验收（主代理直接执行）

### 3.1 静态门

- `python3 -m compileall remote_plugin` + 全套 unittest
- `git diff` 核对无人越权改 T0 文件
- 每个子命令 `--help` 可用、输出单行 JSON、错误无堆栈

### 3.2 真机 e2e（串行，顺序固定）

对 `192.168.9.166` 按以下顺序执行 spec.md 的 **[真机-串行]** 步骤：

1. **T2 up**（最先；密码取自 machines.json 的 `password` 字段，免交互）→ 免密/容器(docker exec)/工作区全部就绪
2. **T1**：verify / machines / status --probe / cards 交叉校验
3. **T3**：前台 run → 后台占坑（cards 0,1）→ machines 占用展示 → logs → stop → 超时 → stale reconcile
4. **T4**：`--paths` 定向同步 → 远端 sha256 比对 → LF 行尾验证
5. **T5**：fixture 仓库（主 repo + submodule，dirty）整树同步 → no_change 快路径 → submodule 版本切换 → sha256 抽检
6. **T6**（可选）：任一 harness 挂载 skill 跑 agent 任务
7. **T2 down**：默认**不执行**；需要时先问用户

任何一步失败：记录现象与日志路径，修复后从失败步骤续跑。

### 3.3 收尾

- 汇总报告：每任务的本地/真机 e2e 结果、遗留项
- 按任务分 commit（`feat: T1 ...` 等），更新 `PROGRESS.md`
- 若 spec/PRD 在验收中被修正，同步更新文档后再提交

## 变更规则

- 实施中发现 spec 与现实冲突：先改 spec.md 再改代码，保持文档即时正确
- 新增机器：手写进 `.remote/machines.json`，跑 `remote verify` 即可
