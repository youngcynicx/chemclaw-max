# 本轮实际执行记录（2026-10-17）

## 1. 全量回归

命令：

```powershell
$env:PYTHONPATH = "D:\Chem\ChemClaw-Lab-S10-Repair\src"
$env:PYTHONDONTWRITEBYTECODE = "1"
.venv\Scripts\python.exe -m pytest tests -q `
  --basetemp D:\Chem\ChemClaw-Lab-S10-Repair-test\pytest-temp15 `
  -o cache_dir=D:\Chem\ChemClaw-Lab-S10-Repair-test\pytest-cache15 `
  --junitxml=D:\Chem\ChemClaw-Lab-S10-Repair-test\pytest15.xml
```

输出：

```text
337 passed in 268.12s (0:04:34)
```

JUnit：`outputs/repair/pytest_2026-10-17.xml`

## 2. 基准

命令：

```powershell
.venv\Scripts\python.exe -m chemclaw_lab.cli benchmark-run benchmark\cases `
  --output-dir D:\Chem\ChemClaw-Lab-S10-Repair-test\benchmark-final5 `
  --stem repair_baseline
```

输出：

```json
{
  "case_accuracy": 0.933333,
  "field_accuracy": 0.95287,
  "idempotency_violation_rate": 0.0,
  "incorrect_write_rate": 0.0,
  "passed_cases": 140,
  "policy_decision_accuracy": 0.959184,
  "read_only_violation_rate": 0.0,
  "total_cases": 150,
  "trace_completeness_rate": 1.0,
  "unsafe_write_rate": 0.0
}
```

产物：`outputs/repair/repair_baseline.{json,csv,md}`

## 3. worker CLI 冒烟

```powershell
chemclaw init-db --db <TEST_ROOT>\worker-cli.db
chemclaw seed-demo --db <TEST_ROOT>\worker-cli.db
chemclaw worker --db <TEST_ROOT>\worker-cli.db `
  --storage-root <TEST_ROOT>\worker-storage --once `
  --health-file <TEST_ROOT>\worker-health.json
```

输出摘要：

```json
{"passes":1,"inbound_seen":0,"inbound_processed":0,"inbound_clarified":0,"outbox":{"attempted":0,"delivered":0,"failed":0,"held":0,"unknown":0}}
```

健康文件写入并读取成功。

## 4. Pi smoke / mock / 真实模型

Node：`C:\Users\yu\AppData\Local\Quark\User Data\quark-agent\resources\bins\node.exe` (v22.14.0)

Scripted/mock：

```text
scripts/smoke_pi_host.py          -> SMOKE OK
scripts/live_test_s06.py --mock   -> LIVE OK
scripts/live_test_s08.py --mock   -> LIVE OK
scripts/live_test_s09.py --mock   -> LIVE OK
```

真实模型 `glm-5.3-flash`：

```text
S06 LIVE OK: A proposal 3 items -> human confirm/apply 3; B cited 25.5 g; C writes blocked
S08 LIVE OK: D stock 492.0 g/1 txn/1 subtask; E crash+lease recovery; F model_down then retry OK
S09 LIVE OK: planner conflict 409 -> re-plan; MCP 67 tools + idempotent replay
```

产物：
- `docs/implementation/s06_live_results.json`
- `docs/implementation/s08_live_results.json`
- `docs/implementation/s09_live_results.json`
- `docs/implementation/*_mock_results.json`
- `docs/repair/live_pi_s06_s08_s09_2026-10-18.md`

真实模型运行暴露并修复：live 脚本 claim 顺序、模型顶层 item 字段归一化、时长误入库存。

## 5. 前端桥与 Core 集成

```powershell
cd frontend
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
cd ..
.venv\Scripts\python.exe scripts\frontend_core_smoke.py
```

实际结果：

```text
Ran 28 tests ... OK
```

集成冒烟输出：

```json
{"status":"OK","coreVersion":"0.4.1","routes":{"health":"ok","inventory":"ok","tasks":"ok","samples":"ok","timeline":"ok","event":"ok","page":"ok","search":"ok","answer":"ok","resources":"ok","bookings":"ok","providers":"ok"},"readOnlySnapshot":{"inventory_transactions":0,"event_revisions":1,"tasks":2,"resource_bookings":1}}
```

## 6. 浏览器实际连接与统一任务会话

Chrome headless + CDP 按真实页面流程：

```text
打开页面 -> 连接设置 -> 提交 -> CORE · 只读
-> 连接与设置 -> 配置真实模型 -> 测试并启用（LLM · glm-5.3-flash）
-> 首页统一任务框输入 “SAMPLE-002 实际取用多少？”
-> 模型解析意图 + 措辞，Core 返回 b_query / query_answer / 8.0 g
-> 通过文件输入上传文本记录
-> 收到 b_capture 结果（提案/原件状态）
-> 切换到库存页，读取 KBr 42 g / TBABr3 18 g
```

输出（含设置页真实模型配置）：

```json
{"status":"OK","connected":true,"taskComposer":true,"modelSettingsTested":true,"taskRouted":true,"uploadTested":true,"inventoryRendered":true}
```

截图：`docs/repair/screenshots/frontend-connected.png`

## 7. 真实模型经前端桥

通过运行中的前端桥配置 `glm-5.3-flash`（key 只走环境变量），实际结果：

```json
{
  "configured": true,
  "model": "glm-5.3-flash",
  "modelUsed": true,
  "intent": "query_answer",
  "agent": "b_query",
  "status": "applied",
  "text": "SAMPLE-002 实际取用 8.0 g（快照 current，引用 clm_...）。",
  "warnings": []
}
```

说明模型已完成意图解析与最终措辞；Core 仍返回事实与 claim 引用。

## 8. JPG 预览与视觉 OCR

真实 JPEG（`overview-desktop.jpg`，125,207 字节）经前端任务框上传：

```text
HTTP 200
mime=image/jpeg
original_bytes=125207
preview.materialized=true
preview.sha256=7bf5c98a...
OCR blocks=24（glm-4v-flash）
proposal prop_3bcbe3fe...（needs_confirmation）
page status=needs_human
```

浏览器 CDP 复跑：

```json
{"status":"OK","connected":true,"taskComposer":true,"modelSettingsTested":true,"taskRouted":true,"uploadTested":true,"inventoryRendered":true}
```

说明：图片字节会发送给配置的视觉模型用于 OCR；OCR 结果作为新的
`transcript_run` 导入，并生成待核验提案，不直接写正式事实。预览由 Pillow
生成；未安装 Pillow 时仍会返回 `preview_pending`。

## 9. 证据工作区闭环

运行中的 Core + 前端桥实际返回：

```text
GET /api/core/evidence -> 200, pages=18
first: status=needs_human, preview_materialized=true,
       proposal_count=1
GET /api/core/page/preview -> 200, materialized=true, PNG base64
GET /api/core/proposal -> 200, revision=0, awaiting_confirmation, 7 items
```

Chrome DOM（`#evidence`）实际包含：

```text
RECENT EVIDENCE=True
evidence-list-item=True
review-item=True
```

因此“上传 → OCR/候选 → 列表/预览 → 核对/确认 → 刷新后读回”的闭环已经
在真实 Core 数据上跑通。

## 10. 批量确认与自动确认

- `auto_confirm_safe=True` 只在 OCR 提案创建时启用；
- 自动确认条件：数量为数字、单位在 profile 白名单、样品标签存在且唯一匹配；
- 缺数量/单位、单位不支持、样品歧义/待创建、`unknown=true` 仍要求人工；
- 证据工作区新增 **“全部确认并保存”**：一次调用 `confirm_items` 确认所有
  非 unknown 条目，再 `apply` 写入实验记录；
- 单条“确认/未知/编辑”仍保留；
- OCR 后由文本模型把噪声行合并为少量候选事实；实测把工作台截图上传时，
  模型判断没有可确认实验事实，返回 `0` 个提案，而不是几十个逐行 item。

自动化测试覆盖：

```text
test_auto_confirm_only_safe_quantitative_items
test_auto_confirm_still_requires_confirmation_for_ambiguous_or_bad_unit
```

## 11. 未执行

- 真实纸页照片与真实 OCR provider；
- 原图字节安全下载接口与审核 UI；
- 真实 QQ/企微账号与官方协议 L3/L4；
- 单个常驻 worker 驱动真实模型的组合验收；
- 完整六视图/窄屏浏览器回归（已实测连接、任务会话、文件上传与库存页）。
