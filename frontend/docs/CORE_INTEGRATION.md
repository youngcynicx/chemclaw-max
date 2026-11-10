# ChemClaw 网页前端 · 本地 Core 接入

此处是修复后 S10 Core（schema v12 / 0.4.1）的读取 + 单一受控任务入口适配器。网页默认使用独立的示例数据；连接成功后由界面明确显示真实状态。读取路由不改变业务事实；唯一写入入口 `assistant.task.process` 仍由 Core 的权限、来源支持、幂等与 run lease 规则强制执行。前端接入不等于 G2–G4 真实 OCR/平台验收完成。

## 运行

在项目根目录执行：

```powershell
cd D:\Chem\ChemClaw-Lab-S10-Repair\frontend
..\.venv\Scripts\python.exe dev_server.py --port 4173
```

打开 `http://127.0.0.1:4173`。在网页连接面板输入已启动 Core 的本机根地址（例如 `http://127.0.0.1:8000`）、用户 API key 和确切的项目 ID。前端不假定存在项目列表接口。连接过程中会读取 health、可用能力，并通过该项目的库存交易读取验证项目权限。

仅本地 Python 服务提供 `/api/` 桥。部署到静态托管的预览只展示示例；静态站无法直接访问设备 Core，也不会把 API key 发往站点以外的服务。

## 浏览器接口契约

`CoreClient` 位于 `dist/src/core-client.js`，通过同源桥调用。所有方法返回 Promise，失败抛出带 `message`、可选 `code` 的 Error。公开状态为 `connected`、`projectId`。API key 不写入 localStorage、sessionStorage、cookie 或文件；只存在连接请求及本地服务内存中。界面应在连接后立即清空密码输入框。

| 方法 | 返回的主要字段 |
|---|---|
| `connect({endpoint, apiKey, projectId})` | `{connected, health, projectId, capabilities, readOnly:true, inventoryScope:'lab_shared'}` |
| `disconnect()` | `{connected:false}`；同时清除客户端状态 |
| `inventory()` | `{kind:'chemicals', items:[...], projectId, scope:'lab_shared', readOnly:true}` |
| `tasks()` | `{tasks:[...], projectId, readOnly:true}`，桥再次筛除其他项目与无项目任务 |
| `search(q)` | Core 原始 `{candidates, snapshot_version, ...}` 加 `projectId, readOnly:true` |
| `samples()` | `{samples:[...], scope, projectId, readOnly:true}`，仅当前项目 |
| `timeline(sampleId)` | Core 样品时间线；sample 归属由 Core 复查 |
| `event(eventId, history)` | 当前事件或完整不可变修订链 |
| `page(pageId)` | 页面元数据与当前转写；不含原图字节 |
| `answer(q)` | `{status, claims, gaps, rendered_text, ...}`；来源不支持时显示 needs_review/partial |
| `resources()` | 当前项目与实验室共享资源 |
| `bookings(status?)` | 当前项目预约记录；只读 |
| `providers()` | 识别 provider 与云处理 scope |
| `task({text,attachments,sessionId})` | 统一任务会话：Core 路由到 b_query / alice_inventory / b_capture / planner；配置模型后由模型做意图解析与回复措辞，返回 `intent/agent/status/text/data/modelUsed` |
| `configureModel({baseUrl,model,apiKey})` | 测试并启用 OpenAI-compatible 模型；key 只存本地桥会话内存 |
| `clearModel()` | 清除模型，任务框回到规则解析 |
| `modelStatus()` | `{configured, model, baseUrl}`，不含 key |
| `health()` | `{health:{...}, projectId, readOnly:true}` |

`connect()` 额外返回 `coreVersion`、`features`（inventory/search/samples/timeline/answer/event/page/providers/resources/bookings）和 `missingReadCapabilities`。旧 Core 若缺少扩展读取能力仍可连接，但对应视图会显示未支持，而不是用演示数据冒充。

`inventory()` 的 items 使用 Core 现有字段：`chemical_id,name,formula,location,amount,unit,threshold,hazard_level,revision`。库存化学品表在该版 Core 中为**全实验室共享**；即使带 `project_id`，后端仍返回共享库存。界面必须标为“实验室共享库存”，不得冒充项目独占库存。

`tasks()` 保留 Core 原始任务字段。`search()` candidates 可能包含 `object_type,object_id,revision,chunk,hit_reason,score,page_id,block_id,page_region,payload`。检索结果是候选记录，不是经过验证的模型回答或已发布结论；不得在 UI 中自动标记为来源支持已验证。

## 已确认的上游调用

| 同源桥路由 | 修复后 Core 调用 |
|---|---|
| `POST /api/connect` | `GET /health`、`GET /v2/capabilities`、`POST /v2/capabilities/inventory.query.list/invoke`，`kind:transactions, project_id:明确项目` |
| `GET /api/core/health` | `GET /health` |
| `GET /api/core/inventory` | `inventory.query.list`，`kind:chemicals, project_id:明确项目` |
| `GET /api/core/tasks` | `GET /v2/tasks`，再按明确项目过滤 |
| `GET /api/core/search?q=...` | `query.events.search`，`project_ids:[明确项目], text:q, limit:50` |
| `GET /api/core/samples` | `query.samples.list`，`project_ids:[明确项目]` |
| `GET /api/core/timeline?sample_id=...` | `query.sample.timeline`，Core 复查 sample 项目 |
| `GET /api/core/event?event_id=...&history=0\|1` | `memory.event.read`，Core 复查事件项目 |
| `GET /api/core/page?page_id=...` | `evidence.page.read`，Core 复查页面项目；不返回原图字节 |
| `GET /api/core/answer?q=...` | `query.answer.build`，`project_ids:[明确项目]` |
| `GET /api/core/resources` | `resource.list`，`project_ids:[明确项目]` |
| `GET /api/core/bookings?status=...` | `resource.booking.status`，`project_ids:[明确项目]` |
| `GET /api/core/providers` | `evidence.provider.list` |
| `POST /api/core/session/message` | `assistant.task.process`，`project_id:明确项目`；唯一受控写入入口 |
| `POST /api/model/configure` | 测试并保存浏览器会话内存中的模型配置（baseUrl/model/apiKey） |
| `POST /api/model/clear` | 清除模型配置 |
| `GET /api/model/status` | 模型是否配置、模型名、baseUrl；永不返回 key |

能力调用由本地桥生成随机 `X-Operation-Id`，不接受浏览器传入操作编号、能力名或任意上游路径。查询能力虽然通过 POST 调用，但仅查询业务数据；Core 仍会持久化自己的能力操作回执和查询审计日志。任务会话通过同一个 `assistant.task.process` 入口执行，可能创建证据/提案或按现有策略应用低风险库存；高风险操作仍返回 `clarification_needed`，不会自动批准。

## 连接边界

- 服务只监听 `127.0.0.1`；上游根地址只接受 `127.0.0.1`、`localhost` 或 `[::1]`。`localhost` 固定转为 `127.0.0.1`，避免 DNS 和代理偏移；不接受用户名密码、子路径、查询、片段。系统 HTTP 代理被禁用，HTTP 重定向被拒绝。
- 本地浏览器会话使用 HttpOnly、SameSite=Strict cookie，加同源内存令牌。敏感读取同时要求 cookie 与 `X-ChemClaw-Session`，连接和断开还验证 Origin。没有 CORS 开放。每次请求校验 Host，拒绝外部站点和 DNS rebinding 形式的访问。
- 连接配置上限 8 KB，上游响应上限 4 MB，上游超时 8 秒。服务不记录请求地址、查询、请求头、正文或 API key。会话空闲 30 分钟后失效；断开或终止服务会丢弃内存连接。
- 只开放一个业务写路由：`POST /api/core/session/message` -> `assistant.task.process`。它要求 member/owner 权限、同源请求和当前连接项目；响应明确区分 applied / clarification_needed / failed。审批代签、正式预约提交、渠道绑定等入口仍未开放。
- 模型配置只存在本地桥的浏览器会话内存。模型可用时，任务文字与 Core 结果会发送到该服务；**附件字节不发送**。非本机模型地址必须是 https；本机 Ollama 等允许 http://127.0.0.1。模型失败时回退到 Core 文本并标记 `model_reply_failed`。
- 上传支持图像（JPG/PNG/TIFF/…）、PDF、Office/OpenDocument、CSV/JSON、CIF/PDB、FASTA/FASTQ/VCF、mzML/JCAMP/FCS/NetCDF/HDF5 与常见归档；单文件 20 MB、最多 5 个。TXT/Markdown 自动转写，其他类型保存原件并返回 `parser_not_configured`；未知二进制返回 415。Core capability body 上限为 64 MB。
- 没有读取 Core 任意文件或原图字节的接口。`evidence.page.read` 只返回页面元数据、当前转写和预览 artifact 标识；真实原图查看仍需 Core 增加有范围检查的字节下载路由。
- 同一浏览器同源标签页共享一个连接会话。不同浏览器 cookie 会话分别隔离。本地其他可信进程仍可直接访问本机 Core；本服务不替代操作系统隔离或 Core 身份权限。

## 验证

```powershell
py -3.12 -m unittest discover -s frontend/tests -v
```

测试使用本机假 Core，验证接口映射、项目过滤、能力白名单、会话隔离、Origin/Host 检查、地址与重定向限制及静态目录边界；不会修改真实 Core 或实验数据。
