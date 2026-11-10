# 前端工作台适配与连接（2026-10-17）

## 位置

- 前端工作副本：`D:\Chem\ChemClaw-Lab-S10-Repair\frontend`
- 静态入口：`frontend/dist/index.html`
- 本地桥：`frontend/dev_server.py`（读取 + 单一受控任务入口）
- 启动：`frontend/start.ps1`（单前端）或 `frontend/start-local.ps1`（Core + 前端）
- 身份引导：`scripts/bootstrap_frontend_identity.py`

原始前端包保留在 `D:\Chem\chemclaw desk v0.1.0`，未修改。

## 适配内容

### Core 新增能力

- `query.samples.list`：按 `project_ids` 列出样品、标签、事件数与最近知识时间；
  viewer/member/owner 可用，越界项目返回 `forbidden_scope`。
- `assistant.task.process`：首页统一任务入口，使用确定性规则分类（不调用大模型），
  然后路由到
  `b_query` / `alice_inventory` / `b_capture` / `planner`；查询返回引用回答，
  库存按现有策略应用或待确认，文本上传保存原件并生成待核验提案，
  图片/PDF 只保存原件，预约请求只读资源/预约并要求正式表单。

### 前端桥新增路由

| 路由 | Core 能力 |
|---|---|
| `GET /api/core/samples` | `query.samples.list` |
| `GET /api/core/timeline?sample_id=` | `query.sample.timeline` |
| `GET /api/core/event?event_id=&history=0\|1` | `memory.event.read` |
| `GET /api/core/page?page_id=` | `evidence.page.read`（元数据/转写，不含原图字节） |
| `GET /api/core/answer?q=` | `query.answer.build` |
| `GET /api/core/resources` | `resource.list` |
| `GET /api/core/bookings?status=` | `resource.booking.status` |
| `GET /api/core/providers` | `evidence.provider.list` |
| `GET /api/core/evidence` | `evidence.pages.list`：最近页面、预览/提案状态 |
| `GET /api/core/page/preview?page_id=` | `evidence.page.preview`：PNG 缩略图 |
| `GET /api/core/proposal?proposal_id=` | `memory.proposal.read` |
| `POST /api/core/review` | 人工核对：`memory.proposal.item.update` / `confirm_items` / `apply` |
| `POST /api/core/session/message` | `assistant.task.process`（唯一受控写入） |

读取路由限定 `project_ids:[已连接项目]` 或由 Core 复核对象归属；写入路由只允许
`assistant.task.process`，并要求 member/owner、同源请求和当前连接项目。
`connect` 返回 `coreVersion`、`features`（含 `task`）和
`missingReadCapabilities`，旧 Core 缺能力时降级显示，不用演示数据冒充。

### 真实模型配置

- `POST /api/model/configure`：baseUrl + model + visionModel + apiKey，先真实调用一次
  `/chat/completions`，成功后只保存在浏览器会话内存；
- `POST /api/model/clear`：清除模型；
- `GET /api/model/status`：返回是否配置、模型名、baseUrl，永不返回 key；
- 非本机模型必须 https；本机 Ollama 等允许 `http://127.0.0.1`；
- 模型只做意图解析、回复措辞和图片 OCR；Core 仍是权限、来源、库存和状态的事实来源；
- 任务文字与 Core 结果会发送给对话模型；图片 OCR 时图片字节发送给视觉模型；
  其他附件字节不发送；
- 图片 OCR 结果通过 `evidence.transcript.import` 成为新的不可变 transcript run；
  随后由文本模型把噪声 OCR 合并成少量候选事实（不是逐行建 item），
  再由 Core 校验 evidence refs/单位/样品并生成提案；
- 如果模型判断 OCR 是界面/说明文字、没有可确认实验事实，则不制造提案；
- `auto_confirm_safe` 只自动确认“数量+单位有效+样品唯一匹配”的低风险
  条目，缺单位/数量、单位不支持、样品歧义或 unknown 仍人工确认；
- 证据工作区提供“全部确认并保存”，一次点击完成 confirm_items + apply；
- 模型失败回退到 Core 文本，并加 `model_reply_failed`；
- 视觉 OCR 实测：`glm-4v-flash`，JPEG 125 KB -> 24 个转写块 + 1 个待核验提案，
  预览 materialized=true。

### 文件类型支持

- 图像：PNG、JPEG/JPG、GIF、BMP、TIFF、WebP、HEIC
- 文档：PDF、TXT、Markdown、CSV/TSV、JSON/JSONL、XML/HTML、YAML、LaTeX/RTF
- Office/OpenDocument：DOC/DOCX、XLS/XLSX、PPT/PPTX、ODT/ODS/ODP
- 归档：ZIP、GZ、TAR、7Z
- 化学/序列/谱图：CIF、PDB、MOL/SDF/SMILES、FASTA/FASTQ、VCF、BAM、
  mzML/mzXML、JCAMP-DX、FCS、NetCDF、HDF5、AB1、SPC 等
- 单文件 ≤20 MB，最多 5 个；Core capability body limit 提升到 64 MB
- TXT/Markdown 自动转写；图片在配置视觉模型后自动 OCR，保存转写块并生成
  待核验提案；其他类型保存原件并返回 `parser_not_configured`
- 图片预览：PNG 走零依赖 pnglite；JPEG/TIFF/BMP/WebP/GIF 走可选 Pillow，
  生成真实缩略图；Pillow 缺失时返回 `preview_pending`
- 未知二进制仍拒绝；JPEG 的 64 KB JSON 限制与 preview 幂等问题已修复

### 前端界面

- 证据工作区：真实记录列表（文件名、笔记编号、页状态、预览/提案计数）；
  左侧选择记录，右侧并排显示原件缩略图与转写块；候选事实支持单条确认、标记未知、
  编辑字段（新 proposal revision）、**全部确认并保存（一次点击）**；旧图片可直接
  “重新 OCR”；
- 研究总览：显示 Core 版本、能力开关与缺失读取能力；
- 首页统一任务框：自然语言输入、拖拽/选择文件、建议任务、对话式结果显示；
  意图、Agent、状态和对象引用分开显示，文件只在内存中读取后提交；
- 知识与样品：真实样品列表；点击查看时间线；
- 证据工作区：检索候选、事件修订、页面元数据/转写；
- 库存：真实共享库存；
- Agent 协作：真实项目任务；
- 仪器与预约：真实资源与预约记录（只读）；
- 研究助手：`query.answer.build` 的确定性引用回答，展示 status/claims/gaps，
  来源不支持时显示 `needs_review/partial`，不再只返回原始检索 JSON。

## 一体化本地启动（自动连接 + 自动模型）

`scripts/run_frontend_demo.py` 可一次启动 Core API 与前端桥，并通过
`--core-api-key`/`--config` 预置连接，通过 `--model-key`/`--config` 预置模型。
页面新增 `GET /api/bootstrap`：浏览器首次打开时自动取得已连接状态、项目、
能力与模型名，无需再手工点连接。

```powershell
.venv\Scripts\python.exe scripts\run_frontend_demo.py `
  --db D:\Chem\ChemClaw-Lab-S10-Repair-test\ui-demo.db `
  --config D:\Chem\ChemClaw-Lab-S10-Repair-test\ui-config.json `
  --core-port 8876 --front-port 4174
```

`ui-config.json` 字段：

```json
{
  "coreApiKey": "...",
  "modelKey": "...",
  "modelBase": "https://open.bigmodel.cn/api/paas/v4",
  "model": "glm-5.3-flash",
  "project": "proj_lab"
}
```

配置文件位于测试目录（不在源码/交付包内），已用 `icacls` 限制为当前用户；
key 只进入运行进程内存，不写入静态文件或浏览器 localStorage。浏览器
`/api/bootstrap` 只返回 `modelConfigured/modelName`，不返回 key。

## 实际验证

### 前端桥单元测试

```powershell
cd D:\Chem\ChemClaw-Lab-S10-Repair\frontend
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

结果：

```text
Ran 28 tests
OK
```

### 真实 Core 集成冒烟

```powershell
cd D:\Chem\ChemClaw-Lab-S10-Repair
.venv\Scripts\python.exe scripts\frontend_core_smoke.py
```

结果摘要：

```json
{
  "status": "OK",
  "coreVersion": "0.4.1",
  "features": {
    "inventory": true, "search": true, "samples": true,
    "timeline": true, "answer": true, "event": true,
    "page": true, "providers": true,
    "resources": true, "bookings": true
  },
  "routes": {
    "health": "ok", "inventory": "ok", "tasks": "ok", "samples": "ok",
    "timeline": "ok", "event": "ok", "page": "ok", "search": "ok",
    "answer": "ok", "resources": "ok", "bookings": "ok", "providers": "ok"
  },
  "readOnlySnapshot": {
    "inventory_transactions": 0,
    "event_revisions": 1,
    "tasks": 2,
    "resource_bookings": 1
  }
}
```

### 浏览器实际连接

使用 Chrome headless + CDP 在本机启动真实 Core 与前端桥，按真实页面流程：

```text
打开 http://127.0.0.1:4174/
-> 点击“连接与设置”
-> 填写 endpoint / projectId / apiKey
-> 提交连接
-> 页面进入 CORE · 只读
-> 连接与设置 -> 配置真实模型 -> 测试并启用
-> 首页统一任务框输入 “SAMPLE-002 实际取用多少？”
-> 收到 b_query / query_answer 结果
-> 通过文件输入上传文本记录
-> 收到 b_capture 结果（提案/原件状态）
-> 切换到“库存与物料”
```

实际读取到 Core 中的 KBr 42 g、TBABr3 18 g，并显示“实验室共享”。
CDP 输出：

```json
{"status":"OK","title":"ChemClaw — 研究工作台","connected":true,"taskComposer":true,"modelSettingsTested":true,"taskRouted":true,"uploadTested":true,"inventoryRendered":true}
```

可复跑的浏览器脚本：`scripts/frontend_browser_smoke.py`（需 Chrome CDP 与
`websocket-client` 开发依赖）。

截图：

- `docs/repair/screenshots/frontend-connected.png`（已连接首页统一任务框）
- `docs/repair/screenshots/frontend-overview.png`（演示总览页）

### Pi mock / MCP

本机发现可用 Node v22.14.0：

```text
C:\Users\yu\AppData\Local\Quark\User Data\quark-agent\resources\bins\node.exe
```

实际运行：

```text
scripts/smoke_pi_host.py           -> SMOKE OK
scripts/live_test_s06.py --mock    -> LIVE OK（写入 s06_mock_results.json）
scripts/live_test_s08.py --mock    -> LIVE OK（写入 s08_mock_results.json）
scripts/live_test_s09.py --mock    -> LIVE OK（写入 s09_mock_results.json）
```

MCP `tools/list` 返回 67 个已授权工具，`tools/call` 幂等回放通过。
这些是 scripted provider / L1 本地证据，不是真实模型或真实平台验收。

## 仍未开放

- 网页提交实验更正、库存调整、审批、预约、任务启动或消息发送；
- 原图字节下载（Core 只返回页面元数据/转写）；
- 真实 OCR/视觉 provider 与人工审核界面；
- 真实模型 Pi 运行（当前仅 mock/scripted）；
- QQ/企微真实收发与附件下载。
