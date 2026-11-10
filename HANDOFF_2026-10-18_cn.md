# ChemClaw S10 — 交接说明（2026-10-18）

> 本文是当前工作副本的工程交接文档，面向下一位开发者/Agent。
> 目标不是复述愿景，而是说明**现在什么是真的、怎么跑、边界在哪里、下一步接什么**。

---

## 1. 当前版本与验证基线

| 项目 | 当前值 |
|---|---|
| 工作目录 | `D:\Chem\ChemClaw-Lab-S10-Repair` |
| 后端包版本 | `chemclaw-lab 0.4.1` |
| 前端版本 | `0.1.9` |
| Schema | **v12** |
| 后端测试 | **337 passed** |
| 前端桥测试 | **28 tests OK** |
| 基准 | **140/150 = 93.333%** |
| 真实模型 live | `glm-5.3-flash` S06/S08/S09 全部 `LIVE OK` |
| 最终交付包 | `D:\Chem\ChemClaw-Lab-S10-Repair-2026-10-18.zip` |

关键测试产物：

```text
outputs/repair/pytest_2026-10-17.xml
outputs/repair/repair_baseline.{json,csv,md}
docs/repair/screenshots/frontend-connected.png
```

---

## 2. 愿景与当前实现的关系

ChemClaw 的目标是让湿实验室拥有一份**人与研究型 AI 共同使用、持续积累、可追溯的研究记忆**。

当前代码已经落地的闭环：

```text
上传原件
→ 保存 original + sha256
→ 生成 preview
→ 视觉模型 OCR
→ 文本模型把噪声 OCR 合并为少量候选事实
→ Core 校验 evidence refs / 单位 / 样品 / 权限
→ 证据工作区核对：编辑 / 确认 / 标记未知
→ 一次点击“全部确认并保存”
→ 写入实验记录（新 revision）
→ 刷新页面或新会话后正确读回
```

设计原则：

- **原件不可变**；
- **OCR/模型输出只是候选**；
- **Core 是权限、事实版本、库存和来源校验的最终权威**；
- **大模型负责复杂/模糊的语义工作**：意图解析、OCR、候选合并、回复措辞；
- **Agent 不能代签人类批准**；
- 高风险或明显错误仍要求人工确认。

---

## 3. 目录与关键文件

```text
D:\Chem\ChemClaw-Lab-S10-Repair
├─ src/chemclaw_lab/              # Core（Python，标准库为主）
│  ├─ api.py                      # HTTP API + capability invoke
│  ├─ capabilities.py             # capability registry + handlers
│  ├─ experiments.py              # 提案、修订、event、确认/apply
│  ├─ transcribe.py               # transcript run / 导入 / 修订
│  ├─ ingest.py                   # 原件、页面、MIME、预览
│  ├─ answers.py                  # 引用式回答、量值/来源校验
│  ├─ query.py                    # 快照检索、报告
│  ├─ inventory.py                # 库存与 subtask
│  ├─ resources.py                # 资源/预约/UTC 冲突
│  ├─ channels.py                 # 通道/附件/收发
│  ├─ deliveries.py               # delivery state machine
│  ├─ tasks.py                    # task/run/outbox/typed messages
│  ├─ worker.py                   # 本地 inbound→Core→outbox worker
│  └─ cli.py                      # chemclaw CLI
├─ frontend/                      # 统一研究工作台
│  ├─ dist/                       # 静态 ES modules（无构建步骤）
│  │  ├─ index.html
│  │  ├─ styles.css
│  │  └─ src/{app,core-client,data,icons}.js
│  ├─ dev_server.py               # 本地桥：静态文件 + Core 代理 + 模型代理
│  ├─ tests/test_bridge.py        # 28 项桥测试
│  ├─ start.ps1 / start-local.ps1
│  └─ docs/CORE_INTEGRATION.md
├─ runtime/agents/                # Agent manifests
├─ runtime/pi/                    # Pi/MCP host
├─ scripts/
│  ├─ run_frontend_demo.py        # Core + 前端桥 + 自动连接/模型
│  ├─ seed_frontend_demo.py       # 合成演示数据
│  ├─ bootstrap_frontend_identity.py
│  ├─ frontend_core_smoke.py
│  ├─ frontend_browser_smoke.py   # Chrome CDP 真实页面测试
│  ├─ live_test_s06/s08/s09.py
│  └─ smoke_pi_host.py
├─ tests/                         # 268 原有 + 69 repair
├─ docs/repair/                   # 修复/验收文档
├─ outputs/repair/                # JUnit + 基准产物
└─ HANDOFF_2026-10-18_cn.md       # 本文
```

---

## 4. 架构总览

```text
Browser (static ES modules)
  │  same-origin /api/*
  ▼
Frontend bridge (frontend/dev_server.py, loopback only)
  │  Bearer Core principal key
  │  X-Operation-Id
  ▼
Core API (src/chemclaw_lab/api.py)
  │
  ├─ capability registry / role matrix
  ├─ SQLite (schema v12)
  ├─ evidence originals + derived previews
  ├─ transcript runs / blocks / active revision
  ├─ proposals / confirmations / event revisions
  ├─ inventory / resources / tasks / outbox
  └─ optional external model calls (bridge-side)
```

关键边界：

- 浏览器不直接访问 SQLite；
- 前端桥只监听 `127.0.0.1`；
- Core API 使用主体 API key 鉴权；
- 操作回执按 `principal + project context + namespace + operation_id` 隔离；
- 模型 key 只存在桥的浏览器会话内存，不写入前端静态资源。

---

## 5. 后端当前能力

### 5.1 身份、权限、幂等

- `PrincipalContext` + membership 角色：viewer/member/owner；
- Agent run token 的项目范围 = 实时 membership ∩ token 冻结项目；
- 操作回执作用域：

```text
sha256(principal_id + namespace + project context)
```

- 同一 operation_id 不能跨主体/项目复用；
- run claim 只允许 queued 或 lease 过期的 running；
- finish 必须带当前 `claim_epoch`。

### 5.2 证据与转写

- `evidence.document.create`
- `evidence.page.ingest`
- `evidence.page.read`
- `evidence.page.original`（按范围返回原件字节）
- `evidence.page.preview`（PNG 缩略图）
- `evidence.pages.list`（最近页面 + 预览/提案状态）
- `evidence.transcribe.run`
- `evidence.transcript.import`（外部 OCR/视觉结果导入为新的不可变 run）
- `evidence.transcript.correct`（人工修订）

文本/图像支持：

- TXT/Markdown 自动转写；
- PNG/JPEG/TIFF/BMP/WebP/GIF 预览：Pillow（可选安装）；
- 其他科研格式保存原件，返回 `parser_not_configured`；
- 未知二进制仍拒绝（415）。

### 5.3 提案、确认、应用

- `memory.proposal.create`
- `memory.proposal.read`
- `memory.proposal.item.update`（人工编辑候选字段，生成新 revision）
- `memory.proposal.confirm_items`（人类确认/拒绝）
- `memory.proposal.apply`（条目级应用；允许部分成功）
- `memory.event.correct`（新 revision，不改旧事实）

自动确认策略：

```text
auto_confirm_safe=True（仅 OCR 路径启用）
自动确认条件：
  - 数量为有效数字
  - 单位为 profile 允许单位
  - 样品标签存在
  - 样品唯一匹配（sample_match.action == link）
仍要求人工：
  - 缺数量/单位
  - 单位不支持
  - 样品歧义/待创建
  - unknown=true
  - evidence 不支持
```

### 5.4 库存、任务、预约

- inventory message processing、subtask、adjustment、approval grant；
- task create/read/update/dispatch、agent run、outbox；
- resource list/availability/propose/confirm/cancel/status；
- 预约冲突使用 UTC 微秒比较，显式时区必填。

### 5.5 统一任务会话

`assistant.task.process`：

```text
project_id + text + attachments[] + session_id + intent_override?
```

路由：

| 输入 | Agent |
|---|---|
| 查询/多少/是否/状态 | b_query |
| 取用/加入/称重/used | alice_inventory |
| 附件 | b_capture |
| 预约/仪器/时段 | planner |
| 无法判断 | orchestrator |

返回：

```json
{
  "session_id": "...",
  "intent": "...",
  "agent": "...",
  "status": "applied|clarification_needed",
  "text": "...",
  "data": {},
  "object_refs": [],
  "warnings": [],
  "classification": "rules|model"
}
```

---

## 6. 前端当前状态

### 6.1 统一任务框

首页：

- 自然语言输入；
- 拖拽/选择文件；
- 最多 5 个附件，单文件 20 MB；
- 支持图像、PDF、Office/OpenDocument、CSV/JSON、CIF/PDB、FASTA/FASTQ/VCF、mzML/JCAMP/FCS/NetCDF/HDF5、归档等；
- 显示 intent / agent / status / object refs；
- 模型启用后显示 `LLM · <model>` 和 Core 原文对照。

### 6.2 模型配置

“连接与设置” → “配置真实模型”：

```text
baseUrl    https://open.bigmodel.cn/api/paas/v4
model      glm-5.3-flash
visionModel glm-4v-flash
apiKey     <仅浏览器会话内存>
```

模型负责：

- 意图解析；
- 回复措辞；
- 图片 OCR；
- OCR 后的候选事实合并/抽取。

Core 负责：

- 权限；
- 项目范围；
- evidence refs；
- 单位/数量；
- 库存 policy；
- 提案 revision、确认、apply。

### 6.3 证据工作区

左侧：

- 最近页面列表；
- 文件名、笔记编号、页状态；
- 预览/提案计数；
- 选择记录。

右侧：

- 原件缩略图；
- OCR 转写块；
- 候选事实；
- 单条：确认 / 标记未知 / 编辑字段；
- 顶部：刷新 / 重新 OCR / **全部确认并保存** / 仅保存已确认。

“全部确认并保存”：

```text
confirm_items(所有非 unknown 且 needs_confirmation 的 item)
→ apply
→ 写入实验记录
```

### 6.4 其他视图

- 研究总览：Core 版本、能力开关、健康；
- 知识与样品：项目样品、时间线；
- 库存与物料：真实库存；
- Agent 协作：真实任务；
- 仪器与预约：资源、预约记录（只读）；
- 研究助手：`query.answer.build` 引用回答。

---

## 7. 前端桥 API

| 方法/路径 | 作用 |
|---|---|
| `GET /api/session` | CSRF/会话/模型状态 |
| `GET /api/bootstrap` | 自动恢复连接 + 能力 + 模型状态 |
| `POST /api/connect` | 连接 Core，验证项目权限 |
| `POST /api/disconnect` | 断开并清除 Core 凭据 |
| `GET /api/core/health` | Core 健康 |
| `GET /api/core/inventory` | 库存 |
| `GET /api/core/tasks` | 任务 |
| `GET /api/core/search?q=` | 证据检索 |
| `GET /api/core/samples` | 样品列表 |
| `GET /api/core/timeline?sample_id=` | 样品时间线 |
| `GET /api/core/event?event_id=` | 事件/修订 |
| `GET /api/core/page?page_id=` | 页面元数据/转写 |
| `GET /api/core/page/preview?page_id=` | PNG 缩略图 |
| `GET /api/core/evidence` | 最近证据页面 |
| `GET /api/core/proposal?proposal_id=` | 提案 |
| `GET /api/core/answer?q=` | 引用回答 |
| `GET /api/core/resources` | 资源 |
| `GET /api/core/bookings` | 预约记录 |
| `GET /api/core/providers` | 识别 provider |
| `POST /api/core/session/message` | 统一任务 |
| `POST /api/core/ocr` | 对页面重新 OCR |
| `POST /api/core/review` | update/confirm/apply 提案 |
| `POST /api/model/configure` | 测试并启用模型 |
| `POST /api/model/clear` | 清除模型 |
| `GET /api/model/status` | 模型状态 |

安全约束：

- 同源会话 cookie + CSRF header；
- 拒绝跨站 Origin；
- 只允许 loopback；
- 上游地址固定为已连接 Core；
- 写路由白名单；
- 浏览器永远拿不到 Core/model key。

---

## 8. 启动方式

### 8.1 手动

```powershell
# Core
cd D:\Chem\ChemClaw-Lab-S10-Repair
.venv\Scripts\chemclaw.exe serve-api --db <db> --port 8876

# 前端桥
cd frontend
..\.venv\Scripts\python.exe dev_server.py --port 4174
```

打开 `http://127.0.0.1:4174/`，填写 Core 地址、项目、主体 API key。

### 8.2 自动连接 + 自动模型

```powershell
.venv\Scripts\python.exe scripts\run_frontend_demo.py `
  --db D:\Chem\ChemClaw-Lab-S10-Repair-test\ui-demo.db `
  --config D:\Chem\ChemClaw-Lab-S10-Repair-test\ui-config.json `
  --core-port 8876 --front-port 4174
```

`ui-config.json` 含有 Core/model key，只放在测试目录，已用 `icacls` 限制当前用户；
不在源码/交付包中。页面通过 `/api/bootstrap` 自动恢复连接。

当前本机计划任务：

```text
ChemClawUI
```

启动/停止：

```powershell
schtasks /run /tn "ChemClawUI"
schtasks /end /tn "ChemClawUI"
```

> 注意：Windows 任务结束后可能留下旧 `python.exe` 子进程占用 8876/4174。
> 重启前先清理 `run_frontend_demo` 进程，否则新代码可能没有生效。

---

## 9. 测试与验收

### 9.1 后端

```powershell
$env:PYTHONPATH="$PWD\src"
.venv\Scripts\python.exe -m pytest tests -q
```

最近结果：`337 passed`。

### 9.2 前端桥

```powershell
cd frontend
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

最近结果：`Ran 28 tests ... OK`。

### 9.3 Core/前端集成

```powershell
.venv\Scripts\python.exe scripts\frontend_core_smoke.py
```

覆盖：健康、库存、任务、样品、时间线、事件、页面、检索、回答、资源、预约、
provider、统一任务会话。

### 9.4 真实浏览器

```powershell
.venv\Scripts\python.exe scripts\frontend_browser_smoke.py `
  --cdp http://127.0.0.1:9222 `
  --api-key <core-key> --upload-image --model-key <model-key>
```

覆盖：自动连接、模型设置、自然语言任务、JPG 上传/OCR/提案、库存页。

### 9.5 真实模型 Pi

```powershell
$env:CHEMCLAW_NODE_EXE = "<node.exe>"
$env:CHEMCLAW_LLM_API_KEY = "<secret>"
.venv\Scripts\python.exe scripts\live_test_s06.py --model glm-5.3-flash
.venv\Scripts\python.exe scripts\live_test_s08.py --model glm-5.3-flash
.venv\Scripts\python.exe scripts\live_test_s09.py --model glm-5.3-flash
```

最近结果：全部 `LIVE OK`。

---

## 10. 已知边界与未完成项

### 已完成/可用

- 统一任务框、意图路由、引用回答；
- 图片 OCR（视觉模型）+ 候选合并（文本模型）；
- 图片预览（Pillow）；
- 证据列表、原件缩略图、转写块、候选编辑/确认/未知/apply；
- 一次点击“全部确认并保存”；
- 低风险自动确认；
- 模型配置（对话/视觉分开）；
- 自动连接/自动模型启动；
- 真实模型 live 测试。

### 仍未完成

- 原页与字段的**坐标级并排高亮**（当前只有缩略图 + 文本，不做 bbox 高亮）；
- OCR 质量基准、人工修正成本、真实/人工比例统计；
- PDF/Office/谱图的专用解析器（当前保存原件，部分文本可转写）；
- 真实 QQ/企微收发 L3/L4；
- 单常驻 worker 同时驱动真实模型 B/Alice 的组合验收；
- 完整六视图/窄屏浏览器自动化回归；
- 审批 UI（`approval_grants` 已有后端，前端只做了核对/确认，不是完整审批中心）。

---

## 11. 常见问题

### `Core 返回 HTTP 400`

- 大文件曾触发 Core 64 KB JSON 限制；已提升 capability body 到 64 MB；
- 仍可能是字段/附件格式问题。前端会显示 `core_bad_request` 及 Core 详情；
- 先 Ctrl+F5，再确认操作类型和附件格式。

### `preview_pending`

- Pillow 未安装，或该格式无图片 codec；
- 安装：

```powershell
uv pip install Pillow --python .venv\Scripts\python.exe
```

### OCR 未配置/空转写

- 设置里没有配置视觉模型；
- 模型名不支持图片（如纯文本模型）；
- 图片字节会发送给视觉模型；如果介意，使用本地模型或先不启用 OCR。

### 证据工作区没有记录

- 确认连接的是 `proj_lab` 且有 `evidence.pages.list`；
- 旧页面可点“重新 OCR”；
- 检查 `/api/core/evidence` 是否 200。

### 修改后页面仍旧

- 前端静态文件缓存：Ctrl+F5；
- 服务端旧进程：清理 `run_frontend_demo`，再启动计划任务。

---

## 12. 下一步建议

1. **字段级并排高亮**：把 OCR block 的 geometry 与预览图对齐，支持点击字段定位原页。
2. **审批中心**：接 `approval_grants`、inventory adjustment、resource booking 的人工批准 UI。
3. **专用解析器**：PDF/Office/化学结构/谱图；优先用模型或 sidecar，不要在 Core 里堆规则。
4. **真实模型 worker 组合**：让常驻 worker 驱动 B/Alice 的完整上传→核验→写入闭环。
5. **质量评估**：建立真实手写页面的 OCR/字段准确率、确认率、人工耗时指标。
6. **多视图回归**：Playwright/CDP 覆盖六个视图 + 窄屏 + 断网恢复。

---

## 13. 交接提醒

- 不要把 `ui-config.json`、API key、真实实验数据提交到源码或交付包；
- 不要绕过 `memory.proposal.confirm_items` 直接写事件；
- 不要把模型输出当正式事实，必须经过 Core 校验和确认策略；
- 修改 schema 时走加法迁移，并更新 `schema_migrations`/测试；
- 旧页面、旧提案、旧 transcript 不应被覆盖，只能新增 revision。

---

_最后更新：2026-10-18_
