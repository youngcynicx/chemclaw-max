# ChemClaw：由 LLM 驱动的多 Agent 任务入口

日期：2026-10-20

修订：2026-10-21，依据六页实验记录实测修正模型预算、完成协议和来源读取。

## 本轮目标

由模型理解完整目标、选择工具、委派专业角色、观察回执并调整后续行动。复杂语义通过模型处理，Core 集中提供权限、来源、版本、事务及幂等能力。

## 已实现的执行路径

```text
网页自然语言任务 + 附件
  -> 桥接层保存原件，assistant.job.submit 持久保存任务，立即返回任务编号
  -> 常驻 worker 领取任务，创建或恢复相同主运行，读取同用户/项目会话历史
  -> 在任务租约范围内完成文本转写或视觉 OCR
  -> Pi 主 Agent：自主检索 / agent.delegate
       -> Core 签发受项目范围限制的子运行令牌
       -> 专业 Pi Agent 选择工具并执行，观察错误后可调整参数
       -> Core 保存子运行结果并撤销令牌
  -> 主 Agent 综合回执，返回引用及待核对事项
  -> assistant.job.complete：保存回复、完成任务或等待输入，关闭残留子运行
```

配置模型后，统一任务框使用此路径。没有配置模型时保留原有基础 Core 路由。模型运行失败后不会重新通过旧路由执行用户请求，避免重复写入。

## 模块分工

- `runtime/pi/runtime.mjs`：复用 Pi Agent 循环，提供动态委派、工具回执、共享预算与中止。
- `runtime/pi/models.mjs`：模型适配，以及明确用于离线测试的 scripted provider。
- `runtime/pi/prompts/*.md`：共同规则和各专业角色的工作方法，替代 JavaScript 中的角色提示分支。
- `runtime/agents/*.json`：角色声明、工具需求、运行预算、委派深度和启用配置。
- `src/chemclaw_lab/orchestration.py`：会话归属、运行生命周期、子任务关系与预算。没有语义分类或固定业务 DAG。
- `src/chemclaw_lab/agent_jobs.py`：持久任务、租约、请求去重、取消与恢复、模型检查点与增量进度。数据库 Schema v13。
- `runtime/pi/recovery.mjs`：复用 Pi 参数校验，结构化反馈缺失字段；保存工具调用身份，恢复消息和回执。
- `frontend/dev_server.py`：浏览器会话、原件上传、常驻后台 worker、租约内解析和受限 Pi 进程。
- 前端：后台任务历史、刷新恢复、实时工具/委派进度、取消和原件核对入口；保存失败的上传文件可继续重试。

新增专业角色通常增加一个 manifest 和提示文件即可；需要新的执行能力时，再扩展 Core 工具。声明工具不会扩大用户实际权限。

## 保留的精确执行边界

Core 校验每次调用的有效项目权限、角色工具范围、证据来源、版本和事务。Agent 执行身份明确为 agent，不能使用用户的 human 身份代签确认。主子运行共享调用预算和 trace，父任务结束后关闭未完成的子任务。

Agent 提交带来源的实验记忆条目；chemistry_notebook 默认由 Core 采纳并按明确样品关联，人工界面保留后续质疑与纠错。自动采纳与人工确认是不同来源状态，Agent 不代签 human 确认。显式 review 模式仍兼容旧审核流程；预约等业务提案仍按独立规则处理，也不代表实验已执行。

## 本机启用

确认 Node.js 20+ 与 `runtime/pi` 的锁定依赖已安装。必要时设置 `CHEMCLAW_NODE_EXE`。

在项目目录执行一次，由本机数据库维护者启用已交付的主 Agent 和专业角色：

```powershell
.venv\Scripts\python.exe scripts\configure_agent_runtime.py --db <已有数据库路径> --enable-defaults
```

也可以通过 `--roles orchestrator b_query` 只启用指定角色。加载 manifest 本身仍不会自动启用角色。

随后沿用现有 `run_frontend_demo.py` 或 Core + 前端桥启动方式，在设置中配置模型。已运行的旧 Python 服务需要重启才能加载新代码。

## 验证命令

```powershell
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m unittest discover -s frontend\tests -v
node --test runtime/pi/*.test.mjs
node frontend/tests/test_jobs_ui.mjs
.venv\Scripts\python.exe scripts\smoke_pi_host.py
.venv\Scripts\python.exe scripts\smoke_agent_orchestration.py
.venv\Scripts\python.exe scripts\smoke_agent_orchestration.py --jobs
```

真实模型验收使用隔离数据库。凭据从环境变量或已有本机配置读取，不写进脚本与报告：

```powershell
.venv\Scripts\python.exe scripts\smoke_agent_orchestration.py --jobs --live --config <已有本机UI配置路径>
```

该验收经过真实浏览器桥 HTTP、Pi 和 Core，检查委派运行、文本原件、待核验提案、第二轮页面引用、令牌撤销，以及未确认事实和库存没有被擅自修改。

上述命令及旧实测记录对应当时的先审核流程；下述样品知识升级没有重复六图或真实模型验收，不将历史结果追认为新流程验收。

## 当前实现范围

常驻 Python worker 负责后台调度，每个任务启动一个受限 Pi 进程。浏览器关闭或刷新不会取消任务；任务、会话和主子运行检查点由 Core 保存。worker 每 15 秒续租，租期 60 秒。重启后加载默认配置，或在设置中重新连接并配置模型，即可在租约到期后领取原任务。凭据只在内存中。

刷新会从同一用户和项目的服务器任务列表恢复。相同 requestId 与相同请求复用任务及原件，不同内容返回冲突。清除模型会暂停该连接下的执行，重新配置后恢复。取消立即使主子执行令牌失效，已完成操作和回执保留。

工具执行前保存调用意图，执行后先保存回执，再进行下一轮模型调用。重启沿用 runId/toolCallId，已确认回执不会重复执行。通用工具的业务事务与回执缓存尚未全部原子提交；如果写入可能成功但没有可靠回执，会停在 needs_review 并显示 operation ID，要求核对实际对象。不能承诺任意故障下所有写入都自动恢复。

HTTP 同步会话接口仍兼容旧客户端；新网页使用异步 jobs 接口。后台调度目前串行领取任务，模型内部的独立工具调用和专业角色可并行执行。尚未加入定时触发、文件夹监听或跨机器任务调度。

库存旧能力内部的确定性解析器仍保留。LLM 现在能够自主查询、调用结构化业务工具和选择专业角色，旧解析器的进一步简化可以独立进行。PDF、Office、谱图等专用解析器仍沿用现有支持范围。

## 六页实测后的运行修正

持久任务使用完成协议 v3：支持本轮正常结束的完整可见正文（natural），也支持本轮成功的 `agent.respond` 回执及相符的显式状态（explicit）。自然正文必须是当前用户边界后最后一条消息、stopReason=stop、非空且不含工具调用，工具 journal 不得有未完成意图。模型只有思考、达到长度上限、生成中断或历史中存在一段回复，均不构成本轮完成。形式完成不等于内容验收，宿主不根据本地关键词猜测实验答案。真实 pending、未知写入、权限和运行错误仍约束最终状态。

v2曾强制显式工具提交，实测会误拒已经完整生成的自然语言答复，v3去除了这项形式约束。旧检查点缺少可核验提交时保留对象引用并标明未完成，不重跑已执行工具；旧v2伪成功不会因为新规则被追认。新v3自然终答恢复还需有明确本轮边界、版本和完全相同的已保存正文。数值化的 usage、stopReason 和 error_code 用于诊断，不公开模型隐藏思考。

模型没有形成完整可见终答时，在同一运行、截止时间和预算内最多进行一次协议修复。修复由 LLM 核对真实来源与回执，只开放已注册的只读工具及 `agent.respond`；不能重放或新增业务写入。正常结束的可见正文无需额外形式重试。修复次数写入检查点，重启不会重置；仍无完整答复则如实报告未完成。

检查点是执行控制面。网络、HTTP、序列化或大小限制导致保存失败时，整棵 Agent 树停止，host 返回 `503 checkpoint_unavailable`，worker 保留任务租约等待恢复。写前意图未保存时不执行写入；写后回执无法保存且结果不确定时转入人工核对，不盲目重放。

`evidence.page.read` 的 Agent 默认视图保留每个 block 的原文、顺序、ID、风险提示及页面版本，减少重复的嵌套元数据；`view=full` 可取得完整视图。权限先校验，投影不改变原件、人工界面或底层缓存。OCR 提示要求保留涂改、箭头与批注对应关系，长度截断的视觉输出不保存为完整转写。

原生 `open.bigmodel.cn` / `api.z.ai` 的 GLM-5.3 系列启用明确的 reasoning profile：默认 `low`，可选 `high` / `max`，保持模型要求的 thinking enabled。其他模型维持兼容路径，不注入此模型专属参数。单次输出默认 16384、上下文默认 128000；本机配置可指定 `modelReasoningEffort`、`modelMaxTokens`、`modelContextWindow`，启动参数分别为 `--reasoning-effort`、`--max-tokens`、`--context-window`。不支持的显式设置提前拒绝，避免静默忽略。

专业提示要求以当前可读原始来源核验历史摘要中的线索，保留参数所对应的样品、温度、压力和处理阶段。模型自行决定读取、委派和汇总方式；宿主没有增加实验化学的关键词分支、标准答案或固定推理流程。

桥接层在运行前按当前用户权限重读本会话最近的原件转写，向主、子 Agent 提供最多 8 页、96000 UTF-8 字节的完整块引用上下文。只装入当前项目的整页原文，不做语义抽取，不截断块；超出预算或不可读取的页面明确列入 source_omitted_pages，模型仍可按需调用工具。历史助手摘要不再承担保存实验参数的职责。agent.respond 的字段说明要求最终答案全文，过程消息中的表格不会被误认为已经交付。

Pi 总时限从 Core 返回的角色 manifest 派生，HTTP 在该时限之外留 15 秒返回余量；旧调用无 manifest 时仍为 240 秒。主 Agent 默认 300 秒，专业角色原有时限保持，给最长 240 秒的子任务及最终综合留出空间。独立原件由模型自主选择按页或小组并行委派。

通用盲测入口 `scripts/benchmark_notebook_images.py` 接收图片目录与仅含问题的 JSON，在空白隔离库中经过真实 OCR、异步任务、Pi 和 Core 测试。标准答案只供评测端使用；脚本不接收答案字段，不自动确认提案。公开导出包含逐页转写、工具回执、提案及回复，并移除凭据与隐藏思考。

## 样品知识的默认采纳与 LLM 整理契约（2026-10-21）

用户提供的原件/OCR 默认作为可用信息。LLM 负责样品归属、化学分类、时间与语义字段，Core 负责权限、证据版本、数据形状、采纳事务和可追溯关联。chemistry_notebook 默认采纳可用条目并建立或关联明确样品，不再要求每条记录先人工确认。人工审核转为之后质疑、改正和追溯；自动采纳不是 human 已复核，也不表示库存消耗或预约已执行。

共同、整理、查询和主 Agent 提示遵守同一字段契约：

- `fields.sample_label={normalized,raw?,uncertain?}` 表示明确主样品；`fields.related_sample_labels=[{normalized,raw?,uncertain?},...]` 表示一条共享条件/比较中所有明确相关样品。兼容读入的 `fields.sample_labels:string[]` 不作为首选输出。不同样品分别有不同数值时拆项；不把整张比较表归给一个标签。Core 保存真实 `sample_id`/`sample_ids`；模型不能编造 ID。
- `occurred_at` 为有来源的发生日期/时间字符串或 `null`。仅有日期保存日期，不添 `00:00`、时区或年份；部分日期/相对阶段放 `fields.date_context` 并注明精度，不能用上传时间代替发生时间。
- `fields` 接受可扩展 JSON。LLM 按内容选择 `reagents`、`temperature`、`duration`、`product_mass`、`bet_surface_area`、`pore_volume`、`adsorption_capacity` 等语义字段或新增字段，保留单位、条件、原值和计算依据。`amount` 仅用于物料量，不能把温度、时长、产物质量或表征结果都当库存消耗。这里是契约示例，不是本地关键词分类表。
- 每项携带 `item_key`、`event_kind`、`raw_text`、真实 `evidence_refs`；继承跨块标题/上次条件时同时引用支撑块。`unknown` 为布尔值，`unknowns` 为具体缺口；缺少可选元数据不让整条已知事实失效。歧义样品不自动合并，其他已知样品继续归卡。
- 默认不主动设置 `needs_confirmation=true` 或 `acceptance_mode='review'`。以 Core 回执区分采纳、归卡、歧义及失败，不能看到 `proposal` 名称便宣称所有内容都不可用。显式旧审核模式和其他业务审批边界保留。

### 已有空字段记录的一次性 LLM 补齐建议

以下为通用离线补齐机制，本机本轮执行结果见后节。旧提案先经 Core 默认采纳/backfill 获得稳定的 `event_id`/`event_revision`，再按用户/项目权限只读导出当前事件 revision、全部原始 items、当前 OCR 块及引用；将其交给 LLM，要求按上面契约输出 `memory.event.correct` 的修订参数，而不是从旧摘要或页面标题的关键词猜样品。补齐的语义仍由 LLM 作出，不添加本地样品名/化学分类 parser。下面仅为形状示例，标签、时间与 block 必须替换为实际原文证据：

```json
{
  "contract": "chemistry_notebook_enrichment.v1",
  "project_id": "<existing project>",
  "corrections": [
    {
      "event_id": "<existing event>",
      "expected_revision": 1,
      "changes": {
        "fields": {
          "sample_label": {"raw": "SAMPLE-A", "normalized": "SAMPLE-A", "uncertain": false},
          "related_sample_labels": [],
          "temperature": {"raw": "120 °C", "normalized": 120, "unit": "°C"},
          "duration": {"raw": "24 h", "normalized": 24, "unit": "h"},
          "date_context": {"precision": "unknown"}
        }
      },
      "reason": "根据所引原文补齐当前已有条目的明确样品与条件",
      "correction_source": {
        "kind": "llm_enrichment",
        "evidence_refs": [{"page_id": "<actual page>", "transcript_revision": 1, "block_id": "<actual block>"}]
      }
    }
  ],
  "unresolved": [{"event_id": "<existing event>", "field": "occurred_at", "reason": "原文没有完整发生日期，保留现值"}]
}
```

执行端只做结构校验、引用/权限/revision 校验和 Core 调用，记录稳定批次操作 ID 与每项回执；逐项调用 `memory.event.correct`，外层 `contract`/`project_id`/`unresolved` 是离线批次元数据，不作为工具参数。`changes.fields` 按 Core 合并规则补充字段，不得丢弃原值与修正历史；有明确日期时增加 `changes.occurred_at`，未知时省略，不能用 `null` 默默覆盖已有明确时间。Core 追加不可变事件 revision 并重新关联样品，知识卡读取最新事件；旧 `items_json` 可保留初版来源，展示通过 `knowledge_events`/`current_event` 取得当前值。同一事件不重建、不重复采纳，不直接 SQL 改库。版本冲突或来源变化时停下该项、重读并交回 LLM；未知/歧义只列入 `unresolved`，不伪造标签。修订来源标明 LLM 补齐，不冒充人工审核。此补齐不会重跑 OCR、不调用 human 确认，也不能替代本轮新流程的后续验证。


## 2026-10-22 本机样品知识升级结果

本机 proj_lab 已迁移旧记录，并纳入用户明确授权的六页模拟 MOF 资料（复用原图与转写，未重跑 OCR）。LLM 按完整原页追加 308 个知识版本，生成 6 张有内容的样品卡；另有 2 条未明确归属资料保留。界面与原件纠错通过 Core 追加版本，转写块采用新 ID，避免破坏旧引用。人工审核不再是知识可用性的前置条件。

`fields.record_scope` 区分 sample_knowledge/source_metadata，由 LLM 判断；`source_kind`/`source_notice` 保留模拟来源。UI 将非样品内容与没有资料的历史标签折叠保留。手动 OCR 后使用已有持久 Agent 任务继续组织样品卡。

本轮 18 项定向检查及语法/编译检查通过，未重复六图基准或整套回归。库存数量、版本和库存提案数量与迁移前一致；数据库及原件均保留备份与历史。
