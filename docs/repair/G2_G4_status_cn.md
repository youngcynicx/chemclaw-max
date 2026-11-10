# G2–G4 状态与待验证矩阵（2026-10-17）

本文件防止把“本地测试绿”误报为“S10 已全部完成”。

## G2 生产文字闭环

### 已实现（L1 本地）

`src/chemclaw_lab/worker.py` + CLI：

```powershell
chemclaw worker --db <db> --storage-root <data> --once
chemclaw worker --db <db> --storage-root <data> --poll-seconds 1 `
  --health-file <dir>\worker-health.json --stop-file <dir>\worker.stop
```

消费路径：

```text
channel webhook / capability ingress
  -> inbound_events(queued)
  -> worker.process_inbound_once
  -> capabilities.invoke_capability(... operation_id=inbound:<id>)
  -> outbox(channel_reply, account/thread/recipient)
  -> worker.process_outbox_once(adapter_factory)
  -> webhook_test / QQ dry-run adapter
```

已验证（`tests/repair/test_worker_e2e.py`，6 条）：

- 文字库存输入经真实 ingress 自动扣库存并回复原线程；
- worker 重启后重跑同一 inbound 不二次扣库存、不重复创建 reply；
- 文字查询返回带 snapshot/claim 的引用式回复；
- 项目不明确时要求澄清，不猜项目；
- 通道身份撤销后 queued 事件不再写业务；
- 单条业务失败不杀死 worker，后续消息继续处理。

### Pi / 前端附加验证

本机使用 Quark 自带 Node v22.14.0，并用真实 `glm-5.3-flash` 运行：

```text
scripts/live_test_s06.py  -> LIVE OK
scripts/live_test_s08.py  -> LIVE OK
scripts/live_test_s09.py  -> LIVE OK
scripts/smoke_pi_host.py  -> SMOKE OK
```

关键实际结果：

- S06：B capture 144.2s，创建 3 item 提案；人类确认并应用 3 条；
  B query 35.8s、5 次工具调用，回答引用 25.5 g；observer 57.3s，
  写尝试全部被拒绝。
- S08：D 跨域扣减 8 g，首次投递 5 条全失败、第二轮 5 条全送达，
  库存 492.0 g、交易 1、子任务 1；E 杀死 Pi、lease 过期、epoch 1→2
  重新认领后恢复并创建 1 个提案；F 模型 outage 首次 `model_down`，
  重试成功。
- S09：planner 两次运行，预约冲突 409 后重排成功，无实验事实副作用；
  MCP `tools/list` 67 个授权工具，`tools/call` 幂等回放通过。
- Mock 结果单独写入 `s06/s08/s09_mock_results.json`，历史真实结果未被覆盖。
- 前端桥连接修复后 Core：12 条读取路由通过；首页统一任务会话
  `assistant.task.process` 实测自然语言查询 -> b_query，文本文件上传 ->
  b_capture 并生成待核验提案；Chrome headless + CDP 完成连接、任务提交、
  文件上传与库存页读取（KBr 42 g / TBABr3 18 g）。
- 前端桥可配置真实 `glm-5.3-flash`：模型完成意图解析与最终措辞，
  `modelUsed=true`；Core 仍返回事实/claim 引用。模型 key 只在桥会话内存，
  附件字节不发送给模型。

详见 `docs/repair/live_pi_s06_s08_s09_2026-10-18.md` 与
`docs/repair/frontend_integration_cn.md`。

### 未验证

- 真实纸页/照片 → 实际 OCR provider → proposal 的流程（当前 transcript 为
  `text_input`/fixture）；
- 原图字节安全下载接口与审核 UI；
- 单个常驻 worker 进程同时驱动真实模型 B/Alice 的组合验收；
- 完整多视图/窄屏浏览器回归（已实测连接与库存页，未覆盖全部六个视图）。

状态：`partial`，可声明“L1 文字库存/查询闭环 + scripted Pi/MCP + 前端只读连接通过”，
不可声明 G2 真实模型验收通过。

## G3 纸页与人机界面

现状：

- Core 新增 `evidence.page.original`，按项目和页面范围返回原始字节；
- 前端桥可用配置的视觉模型（实测 `glm-4v-flash`）对 JPEG 做 OCR，
  通过 `evidence.transcript.import` 写成新的 `needs_human` transcript run，
  再生成待核验提案；
- JPEG 预览由可选 Pillow 生成（实测 `materialized=true`）；未安装时
  `preview_pending`；
- 仍没有专门的原页/转写并排审核 UI；证据视图尚未渲染原图或高亮框。

未完成验收：

- 原页/转写并排、字段定位、unknown 集中澄清的 UI；
- OCR 质量基准、人工修正成本、真实/人工比例；
- 研究者与只读 Agent 同 snapshot 一致性的界面呈现。

状态：`partial`（OCR/预览链路已通，审核 UI 和 OCR 质量验收未完成）。

## G4 真实消息通道

已做：

- 生产平台缺验签配置拒绝启用；
- edit/recall 作者校验；
- QQ token 主机改为 `https://bots.qq.com/app/getAppAccessToken`；
- 出站 recipient 要求 `user:<openid>` / `group:<openid>`，不再向 `@me` 猜测；
- 响应缺真实 message_id 时返回 uncertain，不自造 `qq-*`；
- 附件统一走 R08 摄取。

未做/未验证：

- 真实 QQ webhook 或 WebSocket 接收路线（当前仍是出站 adapter + 本地 webhook）；
- QQ 真实事件 payload/opcode/签名/ACK/游标恢复；
- 附件授权引用下载；
- 真实账号 L3/L4（收发、断网、重启、迟到回执、核对）；
- 企业微信 adapter 未实现。

状态：`partial / waiting_input`（真实平台账号与官方协议验收）。

## 验收等级声明

| 等级 | 当前 |
|---|---|
| L0 单元状态机/权限/解码 | 通过（337 tests） |
| L1 HTTP + 本地假平台 + 常驻 worker | 文字库存/查询闭环通过；纸页流程未通过 |
| L2 固定官方协议离线契约 | QQ 仅部分；无官方协议 fixture 验收 |
| L3 真实账号 happy path | 未验证 |
| L4 真实故障恢复 | 未验证 |
