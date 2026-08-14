# remaining 收尾（全部完成）

1. ✅ 卡级占用：`remote machines` 输出每卡 HBM 用量/总量 + AICore%（job 卡占用 +
   最近 verify 实测，`npu_cards` 字段）；`remote status --probe` 实时每卡占用。
   npu-smi awk 已按多布局健壮化（空格/紧凑斜杠、无 AICore% 列的版本），配硬编码
   样例单测。
3. ✅ 镜像拉取失败：判定网络因素（超时/连接拒绝/重置/DNS/TLS/x509/proxyconnect
   等）后提示"可换镜像源或配置 proxy"；宿主机无任何 proxy 环境变量时追加
   "请向用户询问可用的 proxy/镜像源"。保持单行 JSON 错误契约（exit=1，无堆栈）。
4. ✅ repo 作为 skill：`skills/remote-plugin/SKILL.md` 自洽（用途、触发场景、
   CLI 速查、占用礼仪、出错问人类），`docs/harness/` 三份接入文档齐备；补充了
   镜像拉取失败礼仪与 `.remote` 默认位置说明。
5. ✅ `.remote` 默认位置：向上找不到任何 `.remote` 时，state 默认落到
   remote-plugin 仓库自身的 `.remote/state`（`remote` 入口脚本所在目录下），
   不再落到 `~/.config`，也不报错。
6. ✅ `pip_index_speed_kbps`：确认此前的 `0` 是"测不出来"被误记（下载测速异常
   或无有效负载时 bps=0）。现在无法测量时输出 `null` 并附 `pip_index_speed_note`
   原因字段；可达且有效负载时保留实测值。配本地 HTTP server 单测。
