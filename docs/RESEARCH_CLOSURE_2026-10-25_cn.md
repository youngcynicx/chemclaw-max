# ChemClaw：研究记忆闭环与运行修复（2026-10-25）

用户本轮要求继续开发，暂不考虑 direct/single/multi 比较。因此本轮只处理交接遗留的运行契约、并发连接和研究知识缺口，不新增比较组、分数或优势结论。语义仍由 LLM 判断；Core 只验证权限、结构、主体、版本和引用。未改人的 UI，未引入化学关键词解析、样品答案分支或新 Agent 框架。

项目根目录：`D:\Chem\ChemClaw-Lab-S10-Repair`。本轮证据入口：[phase-summary.json](D:/Chem/ChemClaw-Lab-S10-Repair/work/research-closure-2026-10-25/phase-summary.json)。

## 1. 产品改动及确定性验证

### 完成状态：自然正文有效，失败是否恢复需要真实声明

此前交接将 legacy/stop 的 partial 主要归因于缺少 `agent.respond`，这一归因不准确。正常结束的自然正文是有效交付；durable 路径使用 natural，旧非 durable 路径使用 legacy。历史读取先失败、后成功，而没有显式最终状态时，receipt 汇总仍会保留 partial，不能仅从后来的成功或正文推断恢复。

[runtime.mjs](D:/Chem/ChemClaw-Lab-S10-Repair/runtime/pi/runtime.mjs) 现在只对 durable、已有有效自然答复、缺显式状态且真实回执汇总仍为 partial 的情形，使用现有有界 LLM 收尾，让模型只读核对并通过 `agent.respond` 声明实际状态。复用原任务期限及工具预算，不重放写入或委派。默认 `maxCompletionRepairs=1`；原来允许配置至 2 的能力保留，不能宣传为所有配置最多一次。

恢复已 settled 的自然 partial 时，不再把推导出的 completed 当作模型显式状态；不触发新模型、工具或收尾重放。Core 的真实 pending、致命子任务及权限边界继续约束最终状态。独立代码审阅还发现恢复时会丢失 `status_declaration=missing` 诊断，随后已修复：根据真实回执重算，不复制可能错误的旧标志。

新增 [status-finalization.test.mjs](D:/Chem/ChemClaw-Lab-S10-Repair/runtime/pi/status-finalization.test.mjs)。最终 Node 全组 **119 通过，2.019455 秒**；独立审阅复跑其中 12 项通过。本轮四次真实模型接续的根均 `repair_attempts=0`，没有实际触发该兜底收尾的证据；有界恢复与不重放目前由确定性模型夹具验证。参见 [独立最终代码审查](D:/Chem/ChemClaw-Lab-S10-Repair/work/research-closure-2026-10-25/code-review-final.json)。

### 并发连接：增大监听队列

[api.py](D:/Chem/ChemClaw-Lab-S10-Repair/src/chemclaw_lab/api.py) 的 `ChemClawHTTPServer.request_queue_size` 从继承的 5 显式设为 128。未增加业务重试或修改认证、幂等与响应协议。

Windows 上使用真实 Node native fetch、隔离库及 run-token 的 `memory.event.read`，人为给 accept 线程施加 20ms 延迟后，同一批 24 个并发请求：旧队列成功 5、拒绝 19；新队列成功 24、拒绝 0。无延迟的控制中两种队列均 90/90。该结果证实短时连接队列容量问题，**不能追溯证明历史 9 次 fetch failed 就由它造成**；旧日志没有底层 socket 原因，该历史根因仍未知。

新增 [test_http_transport.py](D:/Chem/ChemClaw-Lab-S10-Repair/tests/repair/test_http_transport.py)。交接相关 Python 回归加新传输测试 **175 通过，82.68 秒**；另有认证等定向测试。详见 [传输诊断](D:/Chem/ChemClaw-Lab-S10-Repair/work/cross-page-evidence-fix-2026-10-24/transport/diagnosis.json) 与 [最终测试摘要](D:/Chem/ChemClaw-Lab-S10-Repair/work/research-closure-2026-10-25/tests-final.json)。这些测试没有真实模型调用。

### 提示：按来源补齐并读回，保留全部已有信息

改动仅位于 [common.md](D:/Chem/ChemClaw-Lab-S10-Repair/runtime/pi/prompts/common.md)、[b_capture.md](D:/Chem/ChemClaw-Lab-S10-Repair/runtime/pi/prompts/b_capture.md)、[orchestrator.md](D:/Chem/ChemClaw-Lab-S10-Repair/runtime/pi/prompts/orchestrator.md)、[b_query.md](D:/Chem/ChemClaw-Lab-S10-Repair/runtime/pi/prompts/b_query.md)：

- 未知数值不等于未知主体；明确适用于双方的未知及原因应关联双方。
- 核对所有相关生命周期，包括误标 completed 的旧排队；真实执行计划与历史状态说明分别表达。
- 将来源已有方法、条件、数值写入 fields；按实际字段路径、主体和引用读回。
- 用来源覆盖核对定位遗漏；补全替代项保留日期时间精度、全部已知条件和具体未知，不让新有效条目继承旧目标的 superseded。
- catalog 只定位 ID；项目 expanded 卡片及上下文才提供知识和状态投影。

最后一次真实运行后又增加一段通用说明：`memory.event.read.is_current/current_revision` 只表示同一事件自身的修订版本，`supersedes_revision_id` 是版本纠错链；跨事件 `state_updates` 的有效性应从样品/项目 `current_state`、`lifecycle_state` 及 basis 判断。旧事件当前版本仍可 `is_current=true`，但生命周期已经 superseded。该新增段已获 [独立契约审查通过](D:/Chem/ChemClaw-Lab-S10-Repair/work/research-closure-2026-10-25/prompt-contract-review.json)，**尚未再做真实模型行为验证**。

## 2. 四次连续开发接续：保留失败及修复成本

全部从已保存的六页文字模拟快照另建隔离副本，通过生产 durable Pi 路径运行 `glm-5.3-flash`，不向主库写入。这是连续开发及来源反馈纠错，尤其后两轮明确给了实际缺口、事件 ID 和来源块，**不是盲测、独立留出成功或泛化证据**。没有新化学实验、扫描件或 OCR。

| 分支 | 秒 | 原始模型状态 | 事件/提案变化 | 实际结果 |
|---|---:|---|---|---|
| curation | 32.50 | completed / natural | 21→21 / 7→7 | 仅三次成功读取，无委派和写入；原缺口未修复，不能以 completed 记作语义通过 |
| curation-v2 | 183.81 | completed / explicit | 21→23 / 7→8 | 补 A 方法、取代错分说明；两次子工具 schema 错误后恢复；遗漏共同未知及队列，并在替代测量中丢了原分钟时间 |
| curation-targeted | 155.86 | completed / natural | 23→27 / 8→9 | 给具体读回反馈后，补共同未知及原因、关闭旧队列、保存正确 observation；新说明误继承 superseded，时间遗漏尚在 |
| curation-final | 184.69 | needs_review / explicit | 27→29 / 9→10 | 两项最终修复真实通过；一次 invalid_state_subject 后修正成功；needs_review 来自模型误读版本标志，非真实审批阻塞 |

本轮四段记录耗时合计 **556.86 秒**，不是总项目时间或纯推理时间，不包含前轮额外 202.94 秒补缺、开发测试及独立审阅。所有根收尾 repair_attempts=0，四轮未见 transport 错误。实际需要委派的后三轮均只有 root＋`b_capture`；虽然提示提供查询复核工作流，**没有实际调用 b_query**。独立验收由本开发任务另一 Codex Agent 只读完成，不是人类盲评或新的外部研究任务。

完整 provider 模型调用数/token 仍为 `null`。每个 completion 的 usage 只反映可见回执范围，不应相加充当全量消耗。原始运行状态、失败工具回执、各轮最终话术及早期语义缺陷均保留，没有替换旧实验或挑成功结果改分。

## 3. 最终真实持久状态

最终库：[after-curation.db](D:/Chem/ChemClaw-Lab-S10-Repair/work/research-closure-2026-10-25/curation-final/lab/after-curation.db)。实际读回：[after-curation-readback.json](D:/Chem/ChemClaw-Lab-S10-Repair/work/research-closure-2026-10-25/curation-final/lab/after-curation-readback.json)。独立验收：[final-review.md](D:/Chem/ChemClaw-Lab-S10-Repair/work/research-closure-2026-10-25/curation-final/review/final-review.md)、[完整字段及状态证据](D:/Chem/ChemClaw-Lab-S10-Repair/work/research-closure-2026-10-25/curation-final/review/actual-readback-proof.json)。

主体 A 石榴-07：`smp_a0c38250401f4f82a7afc82db6514199`；主体 B 岩鹭-12：`smp_84d7076df2f84c669d9bb1135edaa7d6`。以下事件均 revision 1，所引转写均 revision 1。

| 当前内容 | 事件 ID | 实际字段/状态与来源 |
|---|---|---|
| A 测量完整补全 | evt_c3a0aea3f4755d5e90abcc7ae8b7a3ef | fields.sheet_resistance.normalized=86，unit=Ω/□；measurement_method=四探针；temperature=23℃、relative_humidity=45%；单次语境保留。fields.date_context={date:2026-10-21,time:11:05,precision:minute,timezone:null}，occurred_at/event_time_start=2026-10-21T11:05；来源 pg_7ee6585b1906_0001-t0001/t0003。自身 completed，旧9b950…和3f8f…均 superseded，basis 为本新事件 |
| B 测量保持 | evt_06e4925cdef75a80862630d934b4dec3 | 112 Ω/□、24℃、47%、四探针与单次语境未变；A 值没有转给 B |
| 双方固含量/产率未知 | evt_293be11d8e4056628cfb318726e5fe77 | fields.precursor_solid_content 与 film_formation_yield 均 value:null、unit:null、unknown:true，分别保存“前驱液那一批的固含量查不到”及“固含量缺失，无法计算成膜产率”；双方关联，来源 pg_9fa6011e9578_0001-t0011 |
| 双方面积/膜厚未知 | evt_b235a9e14fad586f86e27e03da12a54a | fields.substrate_area 与 film_thickness 均 value:null、unit:null、unknown:true，保留未补上的原因；双方关联，来源 pg_aa3544173acf_0002-t0007 |
| B 旧排队取代 | evt_fa15498473f85fa094c9321e6d56a72d | 作为 basis 将 evt_eb4c04d93cec526da862b693469f5a70 标为 superseded；真正执行计划 evt_22a75b65e0375470a981af68953e1112 仍 completed。历史 basis 自身 superseded 不会按当前契约自动撤销其已保存断言 |
| 双方剥离取消历史事实 | evt_c9056912a2375a808d60cc99a3aeec4d | event_kind=observation，自身 completed，fields.tape_peel_test.performed=false、status=cancelled，原 note 和 tape_supply 保留。来源 pg_7ee6585b1906_0001-t0009＋pg_b255cfeb8ede_0002-t0005；旧 d835…双方、旧28ade…/218e…的 A 状态均 superseded，basis 为本新事件 |

真实剥离计划 `evt_053dc56436eb56e99fd2bc3a3aa00601`、`evt_e8bf86a2c7da5718afc24c7992ad2bb4` 仍 cancelled，basis `evt_4f16b6f4318e568481e0d81832828beb` 未变。新历史说明自身 completed 表示该说明有效，**不表示剥离实验执行过**；performed=false 与取消状态明确保留。A 现行质量 13.4mg、旧12.4订正链及 B14.8mg 均未退化。

最终轮原27个事件的 fields、主体、类型、来源、版本均未改，只新增两条完整补全并更新读取投影的取代关系。卡片与研究上下文：B20事件、A21事件，两视图 fields、主体和生命周期一致，证据与状态 basis 均可解析。未知 observation 没有显式生命周期也可以有效，不要求人为填 completed。

原 needs_review 明确保留。根模型仅从 `memory.event.read` 读到旧版本 `is_current=true`、前驱版本为空，就误报跨事件生命周期未更新；实际样品读回显示取代已生效。成功提案 `prop_e97061ee5c134121a2b78d48fdef273a` 为 applied/default，两项 adopted，无 skipped；没有实际待批准写入。一次失败草稿试图用双方主体更新仅属于 A 的旧说明，被 invalid_state_subject 拒绝；原轮修正主体后成功，此错误不能抹去。

## 4. 冻结、部署及后续边界

各轮原 turns 的 canonical SHA256 均为 `3a8a2828f63b27aa04ca00547b1794d739508947d1f648b12b0c2d585198ce65`；每轮来源快照及运行中134文件映射不变。v2 使用最后一处诊断修补之前的 runtime；targeted/final 使用修补后的版本，不能追溯将新修补归因给 v2。最终真实运行的 common 哈希为 `5cd47a7587fda16d6c61f511228d85e68c0445a756aab6ec1bb23c7a81415abe`；其后才新增版本/生命周期说明，并保留精确旧字节。

[source-preservation.json](D:/Chem/ChemClaw-Lab-S10-Repair/work/research-closure-2026-10-25/source-preservation.json) 记录最终134项产品/运行器文件及额外 Python 新测试，共135文件的本地快照；原 runtime/API 字节从前日已归档 ZIP 的 patched 源码提取，记录成员及哈希，4份原提示也保留。快照不包含 DB 或私有 reader/model 配置。各实际阶段的数据库、原始结果和含密钥 reader 仍仅留本地工作目录，不应直接公开整个 work。

常用服务本轮开始时两端均未监听，已恢复；部署前重新确认实际进程命令、主库路径和无活动任务。4174/8876 当前均 HTTP200；监听队列修复已重启部署，Node runtime/提示在新运行时加载。所有本轮隔离案例服务已停止。六业务表逐行摘要从本轮开始到最终完全一致：samples12、event_revisions617、memory_proposals20、evidence_pages27、chemicals6、inventory_transactions5；主库无 queued/running/cancelling。见 [main-final.json](D:/Chem/ChemClaw-Lab-S10-Repair/work/research-closure-2026-10-25/main-final.json)。

本轮闭合的是指定来源快照上的具体缺口，不能据此声称整库所有自由字段完美、模型会自主发现所有遗漏或多 Agent 优于其他配置。下一步可在新的自然研究任务上冻结版本后做一次非比较的独立消费/接续验证，重点检查 LLM 能否自行选择正确状态投影并保留替代前全部信息；若采用真实研究员案例，先取得真实记录与任务，不能把模拟材料写成真实实验。暂不重跑旧比较、旧整组模型实验或已通过的全套回归。
