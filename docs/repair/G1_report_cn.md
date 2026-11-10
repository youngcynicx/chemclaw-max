# G1 正确性修复报告（2026-10-17）

## 1. 结论

G1 范围内 F01–F13 的已确认缺陷与本轮可离线完成的 F14 修复已落地；关键权限、事务窗口、量值、来源、历史、摄取、投递与预约行为均有正式回归测试。

- 全量测试：**337 passed**
- 原有基线：268 passed
- 新增 repair 测试：**69 条**（63 条 G1/前端/会话契约 + 6 条 worker L1 闭环）
- 基准：**140/150 = 93.333%**，unsafe/incorrect/idempotency violation 均为 0
- schema：v11 → **v12**（加法迁移）
- 真实模型：`glm-5.3-flash` 的 S06/S08/S09 live 全部通过
- 前端：只读桥 22 项测试 + 12 条 Core 读取路由 + 首页统一任务会话

## 2. 改动文件

| 领域 | 文件 |
|---|---|
| schema/迁移 | `src/chemclaw_lab/schema.py`、`db.py` |
| 身份/授权 | `auth.py`、`agents.py`、`agent_gateway.py`、`capabilities.py` |
| 业务事实 | `experiments.py`、`inventory.py`、`proposal.py` |
| 查询/量值 | `query.py`、`answers.py`、`units.py` |
| 通道/投递 | `channels.py`、`deliveries.py`、`tasks.py`、`api.py` |
| 预约 | `resources.py` |
| worker/CLI | `worker.py`、`cli.py` |
| 前端/会话 | `frontend/`、`assistant.task.process`、`query.samples.list` |
| 脚本 | `scripts/_node_runtime.py`、`scripts/smoke_pi_host.py`、`scripts/live_test_s06/s08/s09.py`、`scripts/frontend_core_smoke.py`、`scripts/frontend_browser_smoke.py`、`scripts/seed_frontend_demo.py` |
| 测试 | `tests/repair/*`，并对旧断言按新安全语义更新 |

## 3. 新增迁移

schema v12 新增：

- `approval_grants`：人工批准 Grant；
- `delivery_attempts`：每次真实发送尝试；
- `deliveries.state/error_code/logical_delivery_id/last_attempt_at/accepted_at/receipt_json`；
- `agent_run_tokens.claim_epoch/worker_id`；
- `query_reports.source_manifest_json/source_project_ids_json`；
- `resource_bookings.slot_start_utc/slot_end_utc/time_normalization`；
- `channel_messages.actor_principal_id/identity_version`；
- `inventory_adjustment_proposals.approval_id/approval_source`；
- `operation_results.authority_scope/project_context_json`。

迁移幂等；旧 delivery 状态、旧报告 manifest、带时区预约做回填；naive 预约标 `legacy_unknown`。

## 4. 关键修复与实测

| 场景 | 修复后实际结果 |
|---|---|
| A/B 复用同 operation id | B 得到自己的回执与业务结果，不返回 A 的身份/正文 |
| P1 成员更正 P2 事件 | `forbidden_scope`，事件 revision 不增加 |
| P1+P2 委托人的 run 只绑定 P1，查询 P2 | `forbidden_scope`；省略筛选只返回 P1 |
| Alice(agent) 调用 event.resolve | `human_approval_required`，库存不变；人类确认成功 |
| 业务提交后删除 operation_results 重放 | 同 event/transaction，库存只扣一次 |
| 未到期 run 被第二 worker 认领 | `run_lease_active`；过期后可接管并拒绝旧 epoch 写入 |
| 0.5 g + 200 mg | 派生 0.7 g，附 mg→g 换算规则 |
| 0.5 g + 2 mL | 不输出 2.5，输出 `dimension_mismatch` 并分别列示 |
| 来源文本 9 g 但结构化值为 0.5 g | `needs_review`，claim 标“待核对”，不当作 answered |
| 提案创建于 2020，2026 才应用 | 正式事件 `knowledge_as_of` 为应用时间，不是 2020 |
| HTTP webhook 附件 data_b64 | 与 capability 路径同字节、同 sha256 落盘；空/非法 base64 拒绝 |
| B 撤回 A 的消息 | `author_mismatch`，A 的原消息不变 |
| 发送成功后丢 ACK | `delivery_unknown`，后续 pass 不重发 |
| 09:00+08:00 与 01:00Z 预约 | 第二次确认 `conflict_detected`；相邻 10:00 允许 |
| 模型输出顶层 item 字段 | 归一化到 `fields`，不猜数值；时长不进入库存 |

## 5. 测试与日志

```powershell
$env:PYTHONPATH = "$PWD\src"
.venv\Scripts\python.exe -m pytest tests -q `
  --basetemp D:\Chem\ChemClaw-Lab-S10-Repair-test\pytest-temp15 `
  -o cache_dir=D:\Chem\ChemClaw-Lab-S10-Repair-test\pytest-cache15 `
  --junitxml=D:\Chem\ChemClaw-Lab-S10-Repair-test\pytest15.xml
```

实际输出：`337 passed in 268.12s`

基准：

```powershell
.venv\Scripts\python.exe -m chemclaw_lab.cli benchmark-run benchmark\cases `
  --output-dir D:\Chem\ChemClaw-Lab-S10-Repair-test\benchmark-final5 `
  --stem repair_baseline
```

实际输出：`140/150`，`case_accuracy=0.933333`。

产物已复制到 `outputs/repair/`。

## 6. 旧测试调整说明

不删除场景，只修改已被证明错误的预期：

1. `PRAGMA user_version` 11 → 12；
2. S06 “未到期 run 可被第二次 claim” → 先拒绝，lease 过期后才允许；
3. “发送超时后自动重发” → `delivery_unknown` 且不重发；
4. 附件测试使用真实 PNG 特征字节；
5. `value_matches` 精确字典断言 → 三项独立检查 + 聚合值。

## 7. 未完成

- F05 尚未把所有写 handler 重构为单一 Unit of Work；已消除已知库存/提案窗口。
- 真实纸页照片/OCR/原图字节接口未接入。
- 真实 QQ/企微账号 L3/L4 未验证。
