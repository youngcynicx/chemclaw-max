# ChemClaw · 统一研究工作台

最新模型/API配置、思考强度、知识归卡与答复阅读能力见 [2026-10-22 升级说明](docs/MODEL_SETTINGS_2026-10-22_cn.md)。以下部分内容为早期版本说明，以升级说明及实际回执为准。

一个围绕“原始证据 → 候选字段 → 样品记忆 → 人与 Agent 协作”组织的网页前端。现有证据、知识、库存、任务与资源能力在同一工作空间中呈现，保留来源、版本、事实类型与项目边界。

视觉以纸白、深墨与低饱和绿色为主，使用编辑式排版、细网格、关系图和轻量交互。以 Awwwards、Webby、FWA 作品的完成度作为设计参考；此项目没有获得这些奖项。参考与设计取舍见 `docs/` 中的设计说明。

## 启动与预览

需要 Python 3.10 或以上，推荐 Windows 上已安装的 Python 3.12。前端不需要安装 Node 依赖或运行构建。

```powershell
cd D:\Chem\ChemClaw-Lab-S10-Repair\frontend
..\.venv\Scripts\python.exe dev_server.py --port 4173
```

打开 **http://127.0.0.1:4173**。也可以在 PowerShell 中运行随附的 `start.ps1`（会自动优先使用上层 `.venv` 的 Python）。服务仅监听本机；静态文件位于 `dist/`。ES modules 需要 HTTP 服务，不要直接双击 `index.html`。

连接本机修复后的 Core：

```powershell
# 终端 1：启动 Core HTTP API（使用已初始化的数据库）

最新模型/API配置、思考强度、知识归卡与答复阅读能力见 [2026-10-22 升级说明](docs/MODEL_SETTINGS_2026-10-22_cn.md)。以下部分内容为早期版本说明，以升级说明及实际回执为准。
cd D:\Chem\ChemClaw-Lab-S10-Repair
.venv\Scripts\chemclaw.exe serve-api --db <chemclaw.db> --port 8765

# 终端 2：启动前端桥

最新模型/API配置、思考强度、知识归卡与答复阅读能力见 [2026-10-22 升级说明](docs/MODEL_SETTINGS_2026-10-22_cn.md)。以下部分内容为早期版本说明，以升级说明及实际回执为准。
cd frontend
..\.venv\Scripts\python.exe dev_server.py --port 4173
```

首次连接可在工作副本根目录创建一个本地主体凭据（只打印一次 secret）：

```powershell
cd D:\Chem\ChemClaw-Lab-S10-Repair
.venv\Scripts\python.exe scripts\bootstrap_frontend_identity.py `
  --db <chemclaw.db> --project-id <project> --principal-id hum_frontend --role member
```

`viewer` 只能读取；首页统一任务会话需要 `member` 或 `owner`。

在网页“连接你的 Core”中填写 `http://127.0.0.1:8765`、上一步输出的 `apiKey` 和项目 ID。`start-local.ps1` 可同时启动 Core API 与前端桥。

如需“打开即已连接 + 已配置模型”，使用工作副本根目录的：

```powershell
.venv\Scripts\python.exe scripts\run_frontend_demo.py `
  --db <db> --config <local-ui-config.json> --core-port 8876 --front-port 4174
```

`local-ui-config.json` 只放本机测试目录，包含 Core key、模型 key、模型地址/名称和项目；
页面通过 `/api/bootstrap` 自动取得已连接状态，浏览器不接触 key。

`dist/` 可独立部署为静态体验预览。静态预览不提供本机 Core 桥；如需连接 Core，使用上述本地服务。项目默认保留演示模式，切换真实连接需要明确填写 Core 地址、API key 与项目 ID。

## 六个工作视图

| 视图 | 当前可体验的交互 |
|---|---|
| 研究总览 | 研究关系图、待复核记录、样品与任务入口、工作动态 |
| 知识与样品 | 项目切换、样品筛选、样品时间线、来源跳转与示例 JSON 导出 |
| 证据工作区 | 合成原页与候选字段对照、字段定位、编辑、逐项演示核对、修订历史、本地文件草稿 |
| 库存与物料 | 实验室共享物料列表、余量与阈值、筛选、物料详情；保留各自单位 |
| Agent 协作 | 任务状态与步骤、任务详情、待人工处理环节；演示助手提供有明确标识的预设回答 |
| 仪器与预约 | 示例设备时段、浏览器内预约草稿与草稿删除；不占用实际设备 |

全局搜索支持 `Ctrl/Cmd + K`，按 `Esc` 关闭面板。页面适配桌面与窄屏，支持键盘焦点、状态提示和减少动画的系统偏好。

## 两种数据模式

### 演示工作区

初始项目、实验笔记、样品、物料与任务均为**合成示例**，不是实际实验记录。它们用于完整体验页面之间的联系。

- 字段编辑与演示核对保存在当前浏览器的 localStorage，刷新后仍可回看本地修订；可在设置中“重置演示”。原始合成页面不会随字段编辑被改写。
- 用户选择的文件只在浏览器建立草稿与预览，不会自动上传 Core 或第三方。上传文件引用与预约草稿只在本次页面打开期间保留。
- 没有执行真实 OCR 或大模型调用。演示研究助手使用少量预设回答；不确定性、计划值与实测候选分别保留。
- 演示确认不发布真实实验结论，不创建历史领用，不扣减库存。导出内容带示例标识。

### 已连接 · 读取 + 受控任务会话

点击“连接你的 Core”，输入已运行的本机 Core 根地址、有效用户 API key 和准确项目 ID。只接受 `127.0.0.1`、`localhost` 或 `[::1]` 地址。API key 不写入浏览器持久存储或项目文件，只在连接请求与本地服务内存中使用。

真实连接与演示数据分别呈现。读取失败会显示错误，不能以示例数据伪装为真实结果；断开后可返回演示。

| 已接入读取 | 现有 Core 能力与实际范围 |
|---|---|
| 服务状态 | `/health`，读取 Core 版本、数据库状态与 schema v12 服务 |
| 库存 | `inventory.query.list`，化学品表为**全实验室共享**，界面如实标注 |
| 项目任务 | `/v2/tasks`，由 Core 鉴权后，桥再次仅保留所选项目的任务 |
| 项目样品 | `query.samples.list`，仅返回当前项目样品与事件数 |
| 样品时间线 | `query.sample.timeline`，按 Core 快照语义返回事件 |
| 证据检索 | `query.events.search`，明确传入 `project_ids:[当前项目]`，展示原始候选及来源字段 |
| 事件与修订 | `memory.event.read`，可读取当前事实或完整修订历史 |
| 页面元数据/转写 | `evidence.page.read`，只返回元数据与当前转写，不返回原图字节 |
| 引用式回答 | `query.answer.build`，返回 claims/status/gaps；来源不支持时显示 `needs_review/partial` |
| 资源与预约 | `resource.list`、`resource.booking.status`，只读；不接受网页提交预约 |
| 识别 provider | `evidence.provider.list`，展示已配置 provider 与云处理策略 |
| 证据工作区 | `evidence.pages.list` + `evidence.page.preview` + `memory.proposal.read`：最近记录、原件缩略图、转写块、候选字段 |
| 候选核对 | `memory.proposal.item.update` / `confirm_items` / `apply`：编辑字段（新 revision）、单条确认、标记未知、**全部确认并保存**（一次点击） |
| 自动确认 | `auto_confirm_safe`：仅“数量+单位有效+样品唯一匹配”的低风险条目自动确认；缺单位/数量、样品歧义、未知标记仍要求人工 |
| 模型抽取 | OCR 后由文本模型把噪声行合并成少量候选事实；界面/说明文字没有实验事实时不生成提案 |
| 重新 OCR | `evidence.page.original` + 视觉模型 + `evidence.transcript.import`：旧图片可重新识别 |
| 统一任务会话 | `assistant.task.process`：默认**确定性规则**分类，可在设置中启用真实模型；路由到 b_query / alice_inventory / b_capture / planner。查询返回引用回答，库存按现有策略应用或待确认，文本上传生成待核验提案，图片/PDF 仅保存原件 |

读取路由本身不改变业务状态。唯一开放的业务入口是同源 `POST /api/core/session/message`，它只调用 `assistant.task.process`：需要 member 权限，不能代签批准，高风险操作仍返回澄清/待确认。使用前请在连接表单填写 `member`（或 owner）主体的 API key。

### 真实模型（可选）

连接 Core 后，在 **“连接与设置” -> “配置真实模型”** 中填写：

- 模型地址，例如 `https://open.bigmodel.cn/api/paas/v4`
- 对话模型，例如 `glm-5.3-flash`
- 视觉模型（图片 OCR），例如 `glm-4v-flash`
- API key

点击“测试并启用”会实际调用一次模型。启用后：

- 模型负责意图解析和回复措辞；
- 业务执行、权限、来源校验、库存规则仍由 Core 完成；
- 任务文字与 Core 结果会发送到对话模型；
- 图片 OCR 时，**该图片字节会发送到配置的视觉模型**（这是 OCR 的必要条件）；其他附件字节不会发送给对话模型；
- JPEG/TIFF/BMP/WebP/GIF 预览通过 Pillow 生成；未安装 Pillow 时如实标记 `preview_pending`；
- API key 只保留在本地桥内存，不写入 localStorage、cookie 或文件；
- 模型失败时回退到 Core 的确定性文本，并显示 `model_reply_failed`，不伪造结果。

未配置模型时，任务框使用确定性规则，不调用大模型。

### 支持的研究文件类型

首页任务框支持拖拽/选择，单文件最大 20 MB、最多 5 个：

- 图像：PNG、JPEG/JPG、GIF、BMP、TIFF、WebP、HEIC
- 文档：PDF、TXT、Markdown、CSV/TSV、JSON/JSONL、XML、HTML、YAML、LaTeX、RTF
- Office/OpenDocument：DOC/DOCX、XLS/XLSX、PPT/PPTX、ODT/ODS/ODP
- 压缩/归档：ZIP、GZ、TAR、7Z
- 化学/结构：CIF、PDB、MOL、SDF、SMILES
- 序列/组学：FASTA/FA、FASTQ/FQ、VCF、BAM
- 谱图/仪器：mzML、mzXML、JCAMP-DX（JDX/DX）、FCS、NetCDF（CDF/NC）、HDF5（H5/HDF5）、AB1、SPC，以及常见 RAW/DTA/WIFF/BAF

文本类文件（TXT/Markdown）会自动转写并生成待核验提案；图片在配置视觉模型后会调用 OCR，保存转写块并生成待核验提案。其他类型会保存原件并返回 `parser_not_configured`，不会伪造解析结果。未知二进制类型仍拒绝（HTTP 415）。

详细接口、安全边界与返回字段见 [CORE_INTEGRATION.md](docs/CORE_INTEGRATION.md)。

## 仍需接入的功能

1. **原图字节读取与真实 OCR**：Core 目前只开放页面元数据/转写；需要受身份、项目和对象权限保护的原图/预览字节接口，再接真实识别 provider。
2. **人工核对与高风险写入**：任务框已能创建待核验提案并应用低风险库存；仍需专门的批准 UI 消费 `approval_grants`，不能由 Agent 代签。
3. **真实 Agent 运行**：接入生产 worker、任务创建、待处理交互、运行步骤与失败恢复；排队、执行、完成和投递分别显示。
4. **预约写入**：接入 `resource.booking.propose/confirm`，使用 UTC 规范时间与冲突回执；当前仍只读。
5. **组织与消息入口**：增加服务端项目列表、成员身份与权限管理，再接入 QQ、微信或企业微信的真实适配器与可靠投递回执。

这是统一前端及已有读取接口的交付，不代表上述生产全链路已经完成。

## 文件组织

```text
frontend/
  dist/                    # 可部署静态前端，入口 index.html
    src/                   # 应用、合成数据、图标、Core 客户端
    assets/                # 本地字体、合成笔记、标识
  dev_server.py            # 本机静态服务与白名单读取桥
  start.ps1                # Windows 启动入口
  tests/test_bridge.py     # 本地桥契约与访问边界测试
  docs/                    # 设计与 Core 接入说明
```

## 验证

运行本地桥测试：

```powershell
py -3.12 -m unittest discover -s tests -v
```

当前 **28 项测试通过**，覆盖接口映射、明确项目范围、会话隔离、跨站拒绝、上游地址/重定向限制、静态目录边界、读取路由 feature flags、统一任务会话写入、模型配置校验/测试、模型失败回退，以及 Windows 的 JavaScript MIME 回归。

另已使用修复后的 `ChemClaw-Lab-S10-Repair` 实际 Python Core（schema v12 / 0.4.1），在独立临时 SQLite 与文件目录中完成集成读取：连接、健康、库存、项目任务、样品列表、样品时间线、事件修订、页面元数据/转写、证据检索、引用式回答、资源、预约记录和 provider 列表均通过；读取前后的交易、事件、任务与预约数量保持一致。验证使用可抛弃合成身份与数据，不读取真实实验库，也没有使用 mock 替代该次实际 Core。

复跑命令：

```powershell
.venv\Scripts\python.exe scripts\frontend_core_smoke.py
```

结果输出 `status: OK` 与 12 条读取路由的 `ok`。另已通过设置页实际启用 `glm-5.3-flash` 并验证 `modelUsed=true`。此集成仍不证明手写 OCR、消息平台或生产 worker 已接通。

