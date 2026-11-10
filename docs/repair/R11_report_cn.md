# R11 生产 worker 报告（L1 本地）

## 交付

- `src/chemclaw_lab/worker.py`
  - `process_inbound_once`: 消费 `inbound_events(status=queued)`；
  - `process_outbox_once`: 按 outbox 的 channel account/thread 选择 adapter；
  - `run_worker`: 常驻循环、`--once`、stop file、health file。
- CLI：`chemclaw worker --db ... --storage-root ... [--once]`。
- `deliveries.process_outbox_once(adapter_factory=...)`：一项逻辑 delivery 一个 adapter，不再全局 adapter 混发所有账户。
- 每个 inbound 使用稳定 operation id `inbound:<inbound_id>`；回复 outbox 对同一 inbound 最多一条。

## 支持范围（离线）

| 输入 | 行为 |
|---|---|
| 文本库存操作（取用/加入/称取/used/add 等） | 通过 Core `inventory.message.process` 记账，回复原线程 |
| 文本查询（库存/多少/用量/？） | 走 `answers.build_answer`，回复带 snapshot/claim 的文本 |
| 附件/照片 | 不做 OCR；回复说明纸页流程待验证 |
| 项目不明确 | 不猜，回复要求明确项目 |
| 未绑定作者/空文字 | 归档并回复，不执行写操作 |

## 验证

`tests/repair/test_worker_e2e.py` 6 条：

1. ingress → worker → 扣库存 → outbox → delivered；
2. worker 重启重跑同一 inbound 不二次扣库存/不重复 reply；
3. 查询 ingress → 引用式回复；
4. 项目不明 → 澄清，库存不变；
5. 通道绑定撤销后，已入队消息不再执行写操作；
6. 单条业务失败不杀死 worker，后续消息继续处理。

实际：`6 passed`；全量 `337 passed`。

## 关联验证

- `scripts/smoke_pi_host.py`：真实 Pi host（scripted provider）驱动 Core，`SMOKE OK`。
- `scripts/live_test_s06/s08/s09.py --mock`：`LIVE OK`。
- 真实 `glm-5.3-flash` 下 s06/s08/s09 均 `LIVE OK`：B capture proposal+人工
  确认应用、B query 引用回答、observer 写入拒绝、跨域库存/投递丢失、
  Pi 进程崩溃+lease 恢复、模型 outage 重试、planner 冲突重排、MCP 67 工具
  与幂等回放。详见 `docs/repair/live_pi_s06_s08_s09_2026-10-18.md`。
- `scripts/frontend_core_smoke.py`：前端桥连接修复后 Core，12 条读取路由通过，
  读取前后业务表计数不变。
- `scripts/frontend_browser_smoke.py`：Chrome CDP 实测首页统一任务框，自然语言
  查询路由到 b_query，文本文件上传路由到 b_capture。
- 前端桥单测：28 项通过。

## 未完成

- B/Pi 模型执行器、heartbeat/finish 的真实 runner；
- 纸页 ingest → transcribe → proposal → 人工核验的自动工作流；
- 真实平台 adapter 的 worker 集成（QQ/企微账号）；
- 多 lane/并发认领与真实进程中断演练。
