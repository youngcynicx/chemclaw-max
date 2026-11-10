# ChemClaw-Lab S10 交付包说明

> **2026-10-17 修复轮更新**：本工作副本已升级到 schema v12 / package 0.4.1，
> 全量回归 337 passed（268 原有 + 69 repair），基准仍为 140/150。
> G1 修复详情见 `docs/repair/G1_report_cn.md`；统一前端位于 `frontend/`，
> 已通过前端桥连接修复后 Core（12 条读取路由 + 受控任务会话 + 可配置真实模型 +
> 28 项桥测试）；真实
> `glm-5.3-flash` 的 s06/s08/s09 Pi live 均通过。G2–G4 的真实平台/OCR
> 验收状态见 `docs/repair/G2_G4_status_cn.md`，不得把本地测试绿视为全部完成。

> 交付日期：2026-10-14 ｜ 阶段：S10 消息通道集成（计划 §16）｜ 执行：Alice
> 本包自包含：源码 + 测试 + 测试结果 + 基准报告 + 阶段文档。解压后可复现全部结论。

## 1. 项目一句话

ChemClaw-Lab 是一个**实验室知识库的确定性记账中枢**：以「事件提案 → 人工确认 →
不可变审计账本」为核心语义，覆盖库存（S00-S01）、契约/身份/幂等（S02）、证据
摄取与转写（S03）、实验记忆（S04）、查询报告（S05）、agent 运行时（S06）、
跨域联动（S07）、故障恢复（S08）、扩展机制（S09），本轮交付 **S10 消息通道集成**。

## 2. 本包内容

| 路径 | 内容 |
|---|---|
| `src/chemclaw_lab/` | 全部源码（schema v11、能力层、通道域 channels.py、投递 worker、HTTP API、MCP、agent host） |
| `tests/` | 全部测试（268 个用例；S10 矩阵在 `tests/test_s10_channels.py`） |
| `runtime/agents/` | agent manifest（含 S09 planner） |
| `benchmark/cases/` | 基准用例（150 条） |
| `outputs/s10-benchmark/` | **S10 基准结果**（json / csv / md） |
| `test_results_s10.txt` | **全量测试真实输出**（268 passed，逐用例 PASS 明细） |
| `delivery_docs/s10_contracts_cn.md` | S10 契约演进说明（凭据引用化 / 显式绑定 / 追加不改写 / 先落账本 / 跨重投幂等 / 人工闸门） |
| `delivery_docs/S10_report_cn.md` | S10 验收报告（逐验收点 → 测试映射 + 修复记录） |
| `pyproject.toml`、`requirements-dev.txt`、`uv.lock` | 依赖清单（锁文件保证可复现） |

未包含：`.venv`、`.pytest_cache`、`__pycache__`、`.git`（纯环境/缓存产物）。

## 3. 测试结果汇总（本轮实测）

| 项 | 结果 |
|---|---|
| 全量回归 | **268 passed / 0 failed**（219.24s，明细见 `test_results_s10.txt`） |
| S10 新增矩阵 | 15/15 绿（`tests/test_s10_channels.py`） |
| 基准复现 | **140/150 = 93.333%**（case_accuracy 0.933333，与 S03 以来逐期一致） |
| unsafe_write_rate | 0% |
| incorrect_write_rate | 0% |
| idempotency_violation_rate | 0% |
| trace_completeness_rate | 100% |

S10 交付物：schema **v11**（`channel_accounts` / `channel_identities` /
`channel_messages` 三张新表，纯增量）+ 5 个 owner 能力 + 平台签名 webhook
端点（`POST /v2/channels/{account}/webhook`）+ 通道投递 worker 扩展
（`target_from_payload` / `create_delivery_kwargs` / `content_gate` 人工闸门）+
QQBotAdapter（dry-run 零网络 / 缺凭据永久失败）。

## 4. 如何复现

```bash
# 1) 环境（推荐 uv，任意 Python ≥3.11 亦可）
uv venv .venv
uv pip install -e . -r requirements-dev.txt

# 2) 全量测试（预期：268 passed）
.venv\Scripts\python.exe -m pytest tests -v

# 3) 基准复现（预期：140/150 = 93.333%）
.venv\Scripts\chemclaw.exe benchmark-run benchmark/cases ^
  --output-dir outputs/repro --stem repro

# 4) 只跑 S10 通道矩阵（预期：15 passed）
.venv\Scripts\python.exe -m pytest tests\test_s10_channels.py -v
```

## 5. 边界与遗留

- `wecom_bot` 平台：schema/账户注册就绪，真实适配器待企微凭据；
- QQ 真实收发 live：待真实 QQ 机器人凭据（`CHEMCLAW_QQ_APP_ID` / `CHEMCLAW_QQ`），
  当前 dry-run 与离线 webhook 链路已全绿；
- 长期遗留（S03 起）：真实 50 页语料、3 份研究文档、用户侧人工试点。
