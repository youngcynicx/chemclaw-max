# S10 修复台账（issue matrix）

状态取值：`completed` / `partial` / `not_verified` / `waiting_input`。  
“完成”只表示本轮范围内代码、正式测试和本地可复现证据齐全，不代表 G2–G4 真实验收通过。

| 编号 | 问题 | 代码位置 | 正式测试 | 迁移/兼容 | 状态 |
|---|---|---|---|---|---|
| F01 | 跨主体复用 operation ID | `capabilities._operation_storage_namespace/_authorize_replay` | `tests/repair/test_auth_scope.py::test_operation_receipt_is_principal_scoped` | 旧回执保留；新 key 不命中旧缓存 | completed |
| F02 | 跨项目更正/报告泄露 | `capabilities._event_project_id/_validate_correction_changes`、`experiments.correct_event`、`query.save_report/read_report` | `test_auth_scope.py::test_correction_cannot_cross_project_or_rewrite_project`、`test_report_refuses_cross_project_sources` | 旧报告补 manifest，读取时重授权 | completed |
| F03 | run 项目范围未限制工具 | `agents.principal_from_token`、`agent_gateway.invoke_agent_tool` | `test_auth_scope.py::test_run_token_scope_is_enforced_for_every_tool` | token 项目集与实时 membership 取交集 | completed |
| F04 | Agent 自行批准/冒用人类 | `auth.require_human`、`approval_grants`、`inventory.approval.grant`、Alice manifest | `tests/repair/test_approval_grants.py`（3 条） | 旧自批记录未批量改判 | completed（G1）；真实人工 UI 见 G3 |
| F05 | 提交后回执前崩溃重扣 | `capabilities.invoke_capability` 稳定业务键、`inventory.message.process` | `tests/repair/test_atomic_operations.py`（3 条） | 旧 operation 不清空 | partial：关键路径已消除已知窗口，未改造成全局 UoW |
| F06 | 未到期 run 被接管/旧 worker 写业务 | `tasks.claim_agent_run/finish_agent_run`、`agents.claim_run`、`agent_gateway` | `tests/repair/test_run_fencing.py`（2 条）+ 修正后的 S06 用例 | 旧运行按 lease/epoch 处理 | completed |
| F07 | 单位对象读成 null/量纲混加 | `answers._decode_amount`、`units.py`、`proposal._extract_fields` | `tests/repair/test_quantities.py`（4 条） | 旧 JSON 原样保留；新读取统一解码 | completed |
| F08 | `value_matches=True` 硬设 | `answers._refs_open/_source_supports/_fact_claim` | `tests/repair/test_claim_support.py`（4 条） | 旧 claim 不补造来源 | completed |
| F09 | 提案时间进入过去快照/旧转写贴新版本 | `experiments.apply_proposal`、`query._search_fulltext` | `tests/repair/test_knowledge_snapshots.py`（3 条） | 旧知识时间不伪造；台账未批量回填 | completed（检索路径） |
| F10 | HTTP 附件解码旁路/空文件 | `channels.normalize_attachments/save_attachment`、`api` webhook、capability handler | `tests/repair/test_ingress_security.py`（6 条，含 HTTP 同字节） | 旧空附件保留错误历史 | completed |
| F11 | 缺验签仍启用/跨作者撤回编辑 | `channels.verify_webhook_signature/ingest_channel_event` | `test_ingress_security.py::test_edit_and_recall_require_the_original_author`、`test_production_platform_without_verify_secret_is_disabled` | 旧事件不改写 | completed |
| F12 | 永久失败重试/丢 ACK 重发 | `deliveries.process_outbox_once`、`tasks.mark_delivery`、`channels._map_receipt` | `tests/repair/test_delivery_states.py`（6 条） | 旧 `failed` 按 `next_retry_at` 回填 state | completed（本地状态机） |
| F13 | 等价时区预约绕过冲突 | `resources._parse_iso/_overlapping_confirmed/propose_booking` | `tests/repair/test_booking_timezones.py`（4 条） | 旧预约仅迁移带时区值 | completed |
| F14 | smoke 断言失效/跨设备路径 | `scripts/_node_runtime.py`、`scripts/smoke_pi_host.py`、live scripts、schema v12 | `test_migrations.py` + smoke 静态检查 | 旧脚本绝对路径移除 | partial：Pi smoke 缺 Node，未实际执行 |
| E01 | 正常消息入队后无消费者 | `worker.process_inbound_once`、CLI `worker` | `tests/repair/test_worker_e2e.py`（6 条） | 新增 inbound→outbox 消费；撤权后 queued 事件不继续 | partial：L1 本地文字闭环；B/Alice 模型流程未接 |
| E02 | dispatch 后无生产 Pi runner | `runtime/pi/server.mjs`、`scripts/smoke_pi_host.py`、`scripts/live_test_s06/s08/s09.py` | `smoke_pi_host` + `live_test_s06/s08/s09 --mock` 与真实 glm-5.3-flash live 均通过 | Node 通过 `CHEMCLAW_NODE_EXE` 发现 | partial：真实模型 B/query/observer/跨域/故障/MCP 已验证；常驻 worker 驱动真实模型的组合仍未验 |
| E03 | 缺真实识别器/审核界面 | `evidence.page.original/preview/pages.list`、`memory.proposal.item.update`、视觉模型 OCR、`evidence.transcript.import`、Pillow 预览、证据工作区 | 125 KB JPEG：preview materialized=true、24 blocks、1 pending proposal；工作区 18 条页面/预览/提案可读 | 原 OCR/转写历史不改 | partial：证据列表、预览、候选编辑/确认/未知已通；字段定位高亮与 OCR 质量基准未完成 |
| E04 | QQ 请求/协议不完整 | `channels.QQBotAdapter` 已修正 token 主机/真实 ID/uncertain | 仅离线分类测试 | — | partial / waiting_input（真实 QQ 账号 L3/L4） |

## 旧数据待复核

- 旧 `operation_results` 未按新 scope key 迁移；保留原结果，不自动重放。
- 旧“Agent 调用但 confirmed_by=人类”的记录保留原审计，未批量认定有效人工批准。
- 旧 `value_matches=true` 不作为新支持证明，读取时重新计算。
- 旧错误的 `knowledge_as_of` 未批量补造；历史查询以现有记录为准。
- 旧空附件/缺失原件保留错误状态，需按原 payload 可核对字节恢复或要求重传。
- 旧 `failed` 投递迁移为 `retry_wait` 或 `failed_permanent`；无法证明未发送的不会自动排队。
