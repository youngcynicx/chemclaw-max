# ChemClaw 开发与测试交接（2026-10-24）

写给新窗口的开发 Agent。当前用户要求是保存交接，准备继续开发和测试；本轮两项修复已完成，不要从旧交接中的“下一轮修复”重复开始。先读本文，再按需读专项文档、核对代码与服务，直接推进有价值的下一步。

本文包含开发判断和评测结论，只给开发 Agent 使用，不作为被测 ChemClaw 或独立研究 AI 的输入。

**最新接续已完成，请先读第11节。第1–10节保留本日此前交接历史；其中“尚未做修复后独立外部AI”和“下一轮留出”已由第11节更新。**

## 1. 用户目标与开发方向

ChemClaw 是实验室中人与多个 Agent 共用的研究记忆：原件→转写→按样品持久整理→查询/比较/规划→后续纠错→外部强 AI 接续。

- 第一要义是快速闭环、形成论文发表材料。避免过度防御、扩展基础设施或反复确认例行可逆步骤。
- 保持 **LLM 主导、多 Agent 协作、本地薄代码**。语义归属、计划完成、事实拆分由模型依据来源决定；本地负责权限、版本、引用、结构校验、保存和投影，不加化学关键词分类器或案例答案分支。
- 人的界面暂时保持现状，重点优化外部强 AI 获取完整、结构化、可追溯研究信息的能力。
- 实验知识默认可用、默认采纳。人工审核是之后的质疑、修改、纠错，不重新变成所有知识可查询的前置审批；库存等业务审批仍按各自规则。
- 已允许按需子 Agent 协作，包括独立外部研究 AI；不要只为了展示多 Agent 增加调用。
- 使用中文简洁沟通；不要每次重新询问已授权的测试、例行修复或只读检查。

## 2. 目录、环境与当前服务

| 用途 | 路径或地址 |
|---|---|
| **真实源码根目录** | `D:\Chem\ChemClaw-Lab-S10-Repair` |
| 最新交接本文 | `D:\Chem\ChemClaw-Lab-S10-Repair\HANDOFF_2026-10-24_cn.md` |
| 历史交接 | 根目录 `HANDOFF_2026-10-23_cn.md`、`HANDOFF_2026-10-18_cn.md` |
| 用户论文目录，可能是新窗口cwd | `C:\Users\yu\Desktop\Chemclaw 论文 (1)` |
| 常用前端 / Core | `http://127.0.0.1:4174/` / `http://127.0.0.1:8876/` |
| 常用项目 | `proj_lab` |
| 常用数据库 | `D:\Chem\ChemClaw-Lab-S10-Repair-test\ui-demo.db` |
| 常用私有模型配置 | `D:\Chem\ChemClaw-Lab-S10-Repair-test\ui-config.json` |
| 主库专属外部reader配置 | `D:\Chem\ChemClaw-Lab-S10-Repair-test\external-ai-reader-p1-2026-10-23.json` |
| Python | 源码根目录 `.venv\Scripts\python.exe` |
| Node | `C:\Users\yu\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe` |

所有源码命令显式使用源码根目录作为workdir。该目录**没有Git仓库**，没有提交/PR；不要在桌面论文目录另起一套源码。此前未发现适用AGENTS.md，新窗口仍检查是否新出现。

`frontend/dist`就是维护的原生JavaScript源码，没有另一个前端build步骤。裸python可能落到Windows Store别名，优先使用上述venv。

私有配置和案例目录中的reader JSON含真实key，只能按需在程序内读取；不要输出全文、把key放命令行、论文、截图或ZIP。文中只记配置路径，不包含凭据。

**交接时实查（2026-10-24 19:39，Asia/Shanghai）：** 前端首页/Core health均200；`assistant_jobs`和`agent_runs`没有queued/running/cancelling任务。两端监听PID102664，命令确认为本项目`run_frontend_demo.py`及上述ui-demo.db；PID仅为快照，不能据此在下一窗口直接kill。最新Core已部署，提示在后续新Pi运行中从文件加载。临时案例服务已停止。

常用配置是glm-5.3-flash主模型、glm-4v-flash视觉，max tokens 16384、context 128000；此前自动推理适配为low/zai。参数以私有配置的非秘密字段为准，本轮启动主/视觉探测通过。

需要重启时先核对实际监听进程和活动任务；后台启动用`Start-Process -WindowStyle Hidden`。入口命令：

```powershell
.venv\Scripts\python.exe -u scripts\run_frontend_demo.py --db D:\Chem\ChemClaw-Lab-S10-Repair-test\ui-demo.db --config D:\Chem\ChemClaw-Lab-S10-Repair-test\ui-config.json --no-browser
```

六张业务表重启前后逐行摘要一致：samples 12、event_revisions 617、memory_proposals 20、evidence_pages 27、chemicals 6、inventory_transactions 5。这是本轮快照，不要求以后用户新增记录仍相等。所有新模拟验收均在隔离案例库，未写主库；人的界面未改。

## 3. 已完成的基础能力：不要重做

前期已有默认采纳、真实样品卡与共同事件、来源声明、未知项、人工纠错与历史版本、原文逐块读取、后台任务恢复/取消及run-token隔离。

### 外部AI P0/P1

- 普通读取始终检查当前权限、取得新结果；不把旧幂等缓存作为新的读取结果。
- 共享HTTP/MCP读取契约、紧凑样品目录、`query.sample.context`聚合、精确事件/原文版本与块读取。
- 外部独立service viewer与只读客户端。普通读取不启动ChemClaw内部模型。
- context包含预算、完整性、遗漏计数、快照指纹、来源与版本；不能把部分返回当成全部事实。
- P2固定游标、增量、更多语义关联尚未做，暂不是优先项。

### 外部交接运行器

入口`scripts/run_external_handoff.py`，实现`src/chemclaw_lab/external_handoff.py`。独立AI只获四工具：目录`query.samples.list`、聚合`query.sample.context`、版本`memory.event.read`、原文`evidence.page.read`。

- 手动独立Codex：prepare→execute→finish，保存完整请求、工具结果、最终答复与摘要；不提供数据库、源码、参考答案或写工具。
- 自动兼容Chat API：run；模型自主工具循环，记录实际调用与供应商usage；当前支持openai-chat，明确不支持Responses路径。
- 未观测的模型调用/token为null，不能写成0；手动运行器耗时含调度等待，不等于纯推理耗时。
- 完成的run不覆盖、不自动挑最佳答案。任务包不包含reader凭据。

完整命令见`docs/EXTERNAL_HANDOFF_RUNNER_2026-10-23_cn.md`。

## 4. 本轮已完成的两项修复

### 4.1 事实主体与仅提及对象

每个新Pi item必须有`fields.subject_sample_labels`字符串数组，作为唯一事实主体。`[]`明确未归属，不走旧sample_match兜底。仅比较、引用、背景或“另一对象无更正”放`fields.mentioned_sample_labels`，不据此关联样品。

旧`sample_label/related_sample_labels`仍为Core旧调用兼容；有canonical主体时，旧标签不能扩张主体。顶层canonical标签也沿用形状归一化进入fields，嵌套同名值优先。读侧提供`subject_sample_ids`。

数值专属不同样品时由LLM拆项；共同备注不复制某主体独有数值，引用其真实事件与来源即可。真正共享的操作/条件/计划仍保存一个共同事件。核心不按字段名、化学名或数字猜主体。

### 4.2 明确完成、取代与当前状态

初始状态：`fields.state={"status":"pending"}`。允许pending/in_progress/completed/cancelled/superseded/unknown。

后续事实用通用字段明确状态更新，例如以下均为**占位符，必须换成实际读回ID/版本**：

```json
{
  "subject_sample_labels": ["样品乙"],
  "state_updates": [{
    "target_event_id": "evt_原计划真实ID",
    "target_revision": 1,
    "status": "completed",
    "subject_sample_labels": ["样品乙"],
    "reason": "本条来源明确支持乙完成该计划"
  }]
}
```

同项旧排队/未测事件另显式superseded。目标必须真实已存在、同项目、版本当前；更新主体须同时属于来源和目标主体。本地解析并保存canonical subject_sample_ids；不能在同一提案里用未保存item_key代替目标ID。

共享计划部分完成只关闭已完成主体。旧字段、旧事件与原文保留。旧任意status字符串或日期不自动解释成已完成；未给规范状态的旧plan是unknown，不自动迁移历史数据。

样品卡、timeline、context共用状态投影：顶层current_state、事件lifecycle_state、basis（事件ID/版本/知识时间/证据引用）；context relations含state_update边及applied。as-of只看对应知识快照。同一时刻冲突断言返回unknown及具体问题，不随机挑一条。

普通质量字段纠错不重新校验无关历史状态关系。共享记录在每张卡复制后附加该主体状态，避免A completed被B pending覆盖。状态问题与证据读取不完整分开记录。

### 4.3 提示和实际读回验收

common、b_capture、orchestrator、b_query提示已更新；主Agent必须亲自读取最终current_state，枚举全部pending/in_progress，补齐有依据的同项旧状态取代，保留其他真实未知。

已报告测量必须在fields中实际保存值、单位与条件。`raw_text`含值、状态completed或最终答复表格不能证明结构化完整。整理/验收最终提供真实fields路径、该路径读回值、主体、事件版本与来源。缺值时由LLM补齐：有纠错权限优先版本纠错；只有提案权限则补完整记录并显式superseded旧不完整条目，声明知识补全而非又做一次实验。本轮没有新增写权限或本地化学解析器。

## 5. 关键源码和回归

| 文件 | 职责 |
|---|---|
| `src/chemclaw_lab/knowledge_state.py` | 新增：规范状态校验与纯投影，不查询权限外记录、不解释自然语言 |
| `src/chemclaw_lab/experiments.py` | 主体解析、提案形状兼容、采纳/纠错时状态校验 |
| `src/chemclaw_lab/agent_gateway.py` | Pi新item的fields及canonical主体必填schema |
| `src/chemclaw_lab/sample_context.py`、`read_contracts.py` | 外部current_state、状态边、预算、问题与共享schema |
| `src/chemclaw_lab/sample_cards.py`、`query.py` | 人的卡片/timeline同源状态投影与subject IDs |
| `src/chemclaw_lab/external_handoff.py` | 外部消费者主体/提及及当前状态读取指导 |
| `src/chemclaw_lab/schemas/capability_envelope_v1.json` | 规范状态错误码 |
| `runtime/pi/prompts/{common,b_capture,orchestrator,b_query}.md` | LLM写入与读回要求 |
| `scripts/check_handoff_views.py` | 卡片/context事件、字段、引用及当前状态只读比较 |
| `scripts/run_handoff_case.py` | 真实Pi/Core案例，支持条件子集、分开audits；不是产品语义处理器 |

最终Python **139项通过，61.30秒**；Pi/Node此前 **107项通过**，后续无JavaScript变更，仅提示文本有最后增强。记录在修复工作区`verification/final-test-results.json`。当前没有待修的已知测试失败。

旧page测试的4项失败已针对P0/P1契约更新：只读新鲜结果、完整/精简原文投影差别及fixture自身block_kind不一致。仍核对原文、顺序、几何、质量标记与版本；没有通过放宽生产权限让测试通过。另测Pi必须canonical、Core旧写兼容。

确有相关代码变更时使用以下针对性回归；交接阅读本身无需重跑：

```powershell
.venv\Scripts\python.exe -m pytest tests/repair/test_subject_identity.py tests/repair/test_knowledge_state.py tests/repair/test_subject_current_state.py tests/repair/test_agent_orchestration.py tests/repair/test_agent_page_payload.py tests/repair/test_external_handoff.py tests/repair/test_handoff_view_comparison.py tests/repair/test_external_read_contracts.py tests/repair/test_proposal_normalization.py tests/repair/test_default_knowledge.py tests/repair/test_sample_context.py tests/repair/test_sample_cards_upgrade.py -q
```

Node：在`runtime/pi`工作目录用上述node执行`--test`（package.json的test是`node --test *.test.mjs`，需按所用shell正确展开文件）。

## 6. 评测证据：首轮与修复验证必须分开

### 首轮原始先导，保持不变

根目录`work/publication-handoff-2026-10-23/`；冻结输入`benchmark/handoff_v1/`；报告`docs/HANDOFF_PILOT_RESULTS_2026-10-23_cn.md`。

双样品三阶段模拟碳材料文字，不是湿实验或OCR：共享活化→A测量/B排队→A质量录入纠错/B补测。direct/single/multi各3轮真实GLM，答案/24为23.5/23/23.5，持久状态/10为不适用/9/7.5。单案例开发者定性核查，不能当人类盲评或统计证据；**尚未证明多Agent整体优势**。

独立无父历史Codex完成初始及更新后两轮，四任务合计16/16（与三阶段24点量表分开），JSON一致且引用可解析。自动GLM外部工具循环另完成4模型/4工具请求、34.84秒、供应商20348 token。这些是不同验证组。

首轮共同标签顶层兼容bug用原6items精确离线重放验证：共享事件0→3、未归属3→0。没覆盖首轮数据库或重写分数。为了完成旧首轮，隔离resume曾加载冻结旧normalizer；**新验证不要误用--legacy-normalizer**。

### 10-24修复验证，包含失败过程

令`R=work/handoff-subject-state-fix-2026-10-24/`，`V=R/canonical-final-replay/`，均在源码根目录下。

| 运行 | 真实过程与结果 |
|---|---|
| R第一版 | 单/多Agent各3阶段，共6次；单仍误挂B数值，多漏取代旧排队。失败保留，推动最终canonical及待办枚举契约。 |
| V纠错阶段 | 分别从两条件自己的append快照新增2次纠错模型运行，前4轮复用；主体和计划状态均过，多Agent结构化数值过；单Agent B结果仅raw_text、fields缺值。 |
| 单通用审计v1 | 52.47秒，没有写入，仍错误判完整；原turns不动，失败保留在audit-structural-readback.json。 |
| 单字段路径续验v2 | 提示增强后70.28秒，1次proposal.create；LLM自行补B结构化值，显式取代两条不完整记录。十项保存后检查全过，两接口一致、人工纠错保留。 |

最后成功是补缺续验后的结果，**不能写成单Agent原纠错一轮即通过**。所有审计在audits[]单独记录。没有给模型具体样品/数值答案、评分规则、开发报告或新附件。

V运行中提示/schema曾变化，source-hash-postcheck.json如实记录，不能严格比较两条件耗时。最后字段续验期间最终源码/提示hash保持不变；5次保留proposal.create实参都离线通过最终Pi schema。这些是定向缺陷验证，不是新的独立对照基准。不要覆盖旧分数或只展示最佳路径。

关键产物：

- `R/structural-readback.json`、`single-subject-failure-trace.json`、`multi-queued-state-trace.json`：第一版证据。
- `V/variant-manifest.json`、`targeted-summary.json`：两个纠错阶段及边界。
- `V/audit-structural-readback.json`：第一次通用审计失败。
- `V/fields-proof-summary.json`：最后10项检查，all_checks_passed=true。
- `V/fields-proof-readback.json`：完整模型回执及实际卡片/context；`fields-proof-manifest.json`为该轮通用任务/提示哈希。
- `V/final-pi-schema-replay-check.json`、`fields-proof-source-postcheck.json`：实参和冻结核查。
- `R/verification/final-source.json`：最终代码/提示哈希。较早code-freeze.json只对应第一版。
- `R/verification/main-final-before.json`、`main-final-after.json`：主库摘要/服务检查。

## 7. 恢复案例与只读外部检查

最新单Agent快照：`V/single/fields-proof-v2.db`；同目录case.db也是最终态。失败审计另留audit-v1.db；multi最终为`V/multi/correction.db`。此前snapshot或correction.db不一定包含单Agent最后补缺。

各条件`results.json`含最新reader_config路径和endpoint，配置文件本身含key。不要拿主库reader去读模拟案例，或用另一次新注册reader去读不含该凭据的旧快照。

恢复时优先把目标快照复制为同目录新名字，保留冻结文件；用`scripts/serve_handoff_snapshot.py --db <副本> --reader-config <该快照兼容配置> --stop-file <全新stop路径> --ready-file <全新ready路径>`。该helper从reader配置取回原loopback端口，不调用模型；先核对端口空闲。旧stop已存在，不能直接复用。若需改端口，程序内复制reader配置并改base_url，不输出key。

启动隐藏后台，结束写stop文件即可。不要因案例端口已停就判接口损坏，不要停止常用4174/8876。

恢复后只读检查：

```powershell
.venv\Scripts\python.exe scripts/check_handoff_views.py --reader-config <案例reader配置> --out <新检查文件.json>
```

外部AI prepare/execute/finish用全新任务目录。不把本文、开发报告、数据库、源码或rubric交给独立AI；只给研究任务和四工具，让它自主查原文。context若超预算必须按遗漏继续读取，单看工具success不代表信息齐全。

## 8. 下一窗口建议顺序

1. 先读`docs/SUBJECT_AND_STATE_2026-10-24_cn.md`和最终fields-proof摘要，核对服务即可；不要立刻重跑六图或已完成整组模型调用。
2. 优先补一次**修复后最终态的独立外部强AI交接**：旧独立Codex发生在修复前，最新成功验收是内部真实读回，尚未重新取得独立外部AI结果。恢复隔离快照，只给新研究任务和四工具，检查它能直接消费主体、当前状态及依据，而非从原文额外猜状态。完整保存调用、答复和引用核查，不改旧16/16。
3. 然后做一个更自然、跨页的留出纵向案例：不同标签/布局、共享条件与专属数值、比较提及、部分完成→后续完成/取消、录入纠错、真实未知。先冻结原文/任务、产品代码提示、评分点，再运行；任务不带参考答案。小规模先查闭环，不先扩P2。
4. 论文对照用固定版本、同输入的direct/single/multi，保留失败与额外修复成本；如要比较效率需记录真实总模型/token，未知仍null。一次定向成功不能宣称泛化提升或多Agent全面优越。
5. 同时争取一个真实研究员纵向案例及查找/交接用时，明确区分真实实验和模拟记录。人的UI暂不改，优先论证可追溯共享记忆和外部AI接续价值。

以上是建议接续，不代表本轮已启动新模型实验。用户下一条有更明确任务时以其为准。

## 9. 已知边界与避免浪费的事项

- 主体/状态契约已实现；新Pi写入强制canonical，Core旧HTTP保兼容。旧错误记录不会自动重写，需LLM有来源地纠错或补全。
- 模型可能自称验收通过但实际fields缺值，本轮曾发生；用实际路径/版本/主体/引用读回判断，不能拿最终自然语言替代数据检查。最终成功目前仍只有一个定向案例。
- 状态投影严格引用目标版本；目标被后续纠错后，旧关系可报告revision mismatch，LLM需结合最新证据重申，不自动跨版本猜。
- 状态断言目前按承载事件的knowledge_as_of排序；以后若遇到“只改无关字段却改变冲突断言优先级”的真实案例，再考虑单独断言时间，不先为假设重构状态存储。
- 原共享未知条目可以保留历史；仍缺产率/BET/重复次数/误差时不能整体宣布已知或n=1。
- 本地work脚本与库是开发验证材料，不是要继续扩大的产品层。主流程仍为Pi多角色＋Core通用能力。
- 不要重新增加强制人工签字、固定化学ontology、自动关键词语义兜底或不必要的大型Agent框架。

## 10. 文档与用户可见产物

- 当前专项：`docs/SUBJECT_AND_STATE_2026-10-24_cn.md`。
- 外部P0/P1：`docs/EXTERNAL_AI_P0_P1_2026-10-23_cn.md`。
- 运行器：`docs/EXTERNAL_HANDOFF_RUNNER_2026-10-23_cn.md`。
- 原先导：`docs/HANDOFF_PILOT_RESULTS_2026-10-23_cn.md`及`HANDOFF_PILOT_PROTOCOL_2026-10-23_cn.md`。
- 早期生命周期：`docs/KNOWLEDGE_LIFECYCLE_2026-10-23_cn.md`。
- 模型配置：`frontend/docs/MODEL_SETTINGS_2026-10-22_cn.md`。
- 用户架构参考原文附件：`C:\Users\yu\.codex\attachments\080034ef-95a8-48b1-976a-bc544f043c67\Pasted text.txt`（需要复核论文架构时再读）。
- 桌面论文目录已有`ChemClaw_外部AI交接与最小对照_2026-10-23.md`及同日ZIP，保持原先导内容；最新专项为`ChemClaw_主体归属与当前状态修复_2026-10-24.md`。
- 本文同名副本也放桌面论文目录，便于用户带到新窗口；源码根目录版本作为后续维护入口。

新窗口启动提示可直接使用：

> 请阅读 D:\Chem\ChemClaw-Lab-S10-Repair\HANDOFF_2026-10-24_cn.md，按最新交接继续开发和测试 ChemClaw。保持LLM主导、多Agent协作和本地薄代码，以快速闭环和论文发表为优先；先核对实际状态，接续修复后外部AI验证与留出案例，不重复已完成修复。


## 11. 最新接续：外部独立交接、六页留出与跨页修复（本日后续）

本节是当前接续入口，优先于第8节旧建议。完整报告见 `docs/POSTFIX_EXTERNAL_HANDOFF_2026-10-24_cn.md` 和 `docs/HANDOFF_V2_RESULTS_2026-10-24_cn.md`。

### 已完成与原始结果

1. **修复后独立外部强AI交接已完成**。不继承开发历史的Codex子Agent仅获任务和四只读工具，目录1/context2/原文3/精确事件3，共9请求；39项实际fields声明、9项主体状态/basis与3页精确块全部匹配，另有独立Agent审阅。完整模型调用/token未知为null，332.57秒含调度等待，不是纯推理。目录 `work/postfix-independent-2026-10-24/`；旧16/16不改。临时服务已停。
2. **新留出六页、三阶段、direct/single/multi各一次**，目录 `benchmark/handoff_v2/`、`work/handoff-v2-frozen-2026-10-24/`。不同薄膜样品标签、共享条件、专属数值、跨页、比较提及、部分完成、后续完成/取消、录入订正、未知。自然研究任务不带答案/rubric。独立Agent按冻结rubric评阅（非人类盲评）：答案direct/single/multi均23/24；持久single9/10、multi7/10，不证明多Agent优势。原轮耗时40.07/333.19/470.36秒；全量模型调用/token未知，不能当等预算效率比较。
3. **观察harness错误已修复并保留**。首阶段single/multi模型completed但后置观察走人的桥调用context被白名单拒绝，两项仍保留harness_failed；运行器改用专属reader。11项无模型回归通过。严格resume只补读这两项，原turn/arrival.db未改、零模型；仅接续未运行的4个阶段。原产品/提示/角色/输入/rubric全程未变，唯一观察runner变更和原版均单列保存。后续postcheck unchanged=true。multi最后阶段为partial（legacy/stop），不能改记completed。
4. **真实产品修复：同项目跨页证据**。固定基线结束并停案例服务后才改产品。`experiments.py`的default notebook/adopt逐ref检查同项目（document与object均一致）、精确当前转写版本、真实当前run/块；每item有证据，整个proposal至少一条锚页ref。仅辅页item不必添加无关锚页ref。旧review单页历史策略与gateway fencing保留；`agent_gateway.py`仅一处schema说明变化。无新表/迁移/化学解析/语义兜底；读侧已有多页stale直接复用。
5. **原真实实参离线验证**：`work/cross-page-evidence-fix-2026-10-24/final/before-after-comparison.json`。相同来源快照的独立副本，原LLM实参（只换operation_id）由invalid_evidence变为9项采纳；模型调用0、原基线不变。只说明Core引用准入修复，不代表新模型成功、字段完整或改分。修复前两源码通过精确逆patch重建且哈希完全匹配原冻结才归档，修复后源码也匹配最终重放；见final/source-preservation.json。
6. **最终回归174项通过，80.13秒**，交接139项+新增cross_page_evidence24项+longitudinal_harness11项；测试文件在tests/repair。JavaScript/提示未改，Node全组本轮未重跑。独立代码审查没有发现范围内可操作缺陷，非语义正确性背书。
7. **单列一次通用补缺审计**：从multi最终快照另建 `work/cross-page-evidence-fix-2026-10-24/llm-audit/`，任务不含案例标签/数值/评分/具体缺陷，无新附件。202.94秒，状态仍partial；一次真实提案把岩鹭测量方法补进fields.measurement_method，保留值/温湿度/单读数，显式superseded旧不完整测量。原turns/来源快照/133文件哈希不变。旧排队仍completed、plan备注unknown、未知归属遗漏仍未闭环；不改原23/24与7/10，不继续自动挑最好结果。实际根9次fetch failed及子1次schema_violation保留，答复误称8次。独立核查在llm-audit/review。

### 当前服务与保留库

修复已部署到常用4174/8876，两端HTTP200；重启前重新核对监听命令确为本项目run_frontend_demo.py及ui-demo.db，且无活动任务。六业务表逐行摘要一致（12/617/20/27/6/5），见cross-page修复final/main-before-deploy.json和main-after-deploy.json。所有案例服务已停止，人的UI不变。未来重启仍按实际进程/任务核对，不使用历史PID。

v2基线库：`work/handoff-v2-frozen-2026-10-24/{single,multi}/notebook_close.db`；补缺后的新分支库：`work/cross-page-evidence-fix-2026-10-24/llm-audit/multi/audit-final.db`。前者原模型结果后者额外审计，不混用。各自results.json内reader_config仅供对应恢复，含key的JSON不公开。不要用已存在stop文件恢复，照第7节用全新生命周期路径及同快照兼容reader。

### 下一步（不要重做已完成实验）

优先处理主Agent显式agent.respond完成协议（本轮两次legacy/stop返回partial）、通用审计剩余语义缺口（历史排队取代、错误plan/unknown、明确共同未知未归属）、瞬时读取失败的并发来源，再安排冻结版本的下一案例或真实研究员纵向交接用时。保留LLM做语义判断，本地只做权限/结构/版本/引用校验；不要引入针对薄膜、样品标签或数值的分类器/答案分支。先读本轮独立review及实际fields/state，不只信最终答复。真实研究员数据需用户提供或授权来源；不可把这些文字模拟说成真实实验或OCR。

论文材料应同时保留失败、观察harness修复、额外202.94秒补缺成本和不可观测token。当前没有多Agent全面优势的证据。原10-23先导与本日早期缺陷分数全部保留。

## 12. 2026-10-25 最新接续入口

用户要求暂不考虑 direct/single/multi 对比，已按该方向处理运行完成契约、连接队列及研究记忆缺口。最新入口为 [HANDOFF_2026-10-25_cn.md](D:/Chem/ChemClaw-Lab-S10-Repair/HANDOFF_2026-10-25_cn.md)，详细实证为 [RESEARCH_CLOSURE_2026-10-25_cn.md](D:/Chem/ChemClaw-Lab-S10-Repair/docs/RESEARCH_CLOSURE_2026-10-25_cn.md)。本节优先于上文旧“下一步”。

更正旧归因：缺少 agent.respond 本身不构成失败，正常自然正文有效；历史 partial 与旧失败回执没有显式恢复声明有关。旧9次fetch failed底层根因仍未知，不能将受控队列压力复现当成历史定案。最终相关Python175、Node119通过；四次隔离开发接续闭合共同未知、旧队列、正确类型说明及时间精度。最后模型needs_review源于版本标志/生命周期混淆，实际独立读回修复通过且无待审批写入；原状态及失败材料保留。主服务4174/8876健康、六业务表不变，人的UI不改。不要重跑旧比较或已完成定点修复。
