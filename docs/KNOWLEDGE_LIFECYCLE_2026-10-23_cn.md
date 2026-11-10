# 多页知识整理与时间线修复（2026-10-23）

本轮承接 HANDOFF_2026-10-23_cn.md 的优先1、优先2。开发与审查由两个 Agent 协作；产品内仍由主模型选择 capture/query 和工具，本地仅处理权限、结构、版本及真实回执。没有加入化学答案、自然语言关键词路由或新的构建体系。

## 已完成

- `query.sample.timeline` 原先只按主 `sample_id` 查事件，漏掉 `sample_ids` 中的共同操作/计划。现同时读取明确的多样品关联，保留项目边界和目标历史版本，来源声明仍与样品事实分开。
- 时间线补出 `occurred_at`、`event_time_start`、真实 `evidence_refs`、`provenance`、`is_corrected` 和具体未知项；发生时间按已存字段排序，空缺不补造。
- `query.events.search` 同步支持共同样品、真实发生日期与字符串别名。截止日期包含当天带具体时间的事件，精确时间戳边界维持原比较行为。
- 样品卡去重保留 `related_events:[{event_id,revision}]`，不再只有缺少版本的 ID 列表。
- `knowledge_delivery` 补齐来源声明 `source_notes`、类型化事件搜索结果和去重卡片的逐事件版本读回。无版本或旧版本不证明当前写入已读回。保留原有四种 state，补出事件版本、复用和失败尝试信息；提示明确区分事件读回与全部事实整理完成。
- capture 提示明确共同事实只保存一次并关联全部样品；common 提示明确当前知识修订与原始转写/提案历史的区别，以及研究交接中的计划、实做、未知/null、来源和版本。语义判断仍交给 LLM。

## 真实模型开发验收

脚本：`scripts/verify_knowledge_lifecycle.py`。模型为现有 GLM-5.3-flash，使用新隔离库、两页模拟文本，不读取主库材料，不加载 handoff、六图、参考答案。不做新 OCR，也不把此记录作为 MOF 新分数。

实际回执：`work/lifecycle-2026-10-23/baseline/results.json`；数据库保留在同目录，仅供本地核查，不作为对外交付物。

| 回合 | 真实行为与结果 |
|---|---|
| 首次整理 | 主 Agent 委派 capture，2样品、1提案、4事件，写入并读回；模型曾漏 evidence_refs，按 schema 修正后成功，没有重跑选答案 |
| 第二页追加 | 复用2个样品，只增加返工与共同计划2事件；累计2提案、6事件版本行，verified，66.86秒 |
| 只读查询 | 没有新增样品、提案或事件，read_only，22.33秒；暴露 LC-B 时间线漏共同计划 |
| 人工纠错后重新核对 | 通过真实纠错 HTTP 接口将 LC-A 质量20→22 mg，事件升至revision 2；查询保留22 mg、原页20 mg和未知项，没有重新导入旧事实 |
| 时间线修复后单次只读复核 | 共同计划在两张时间线可见且为同一事件；LC-A 22 mg及修订依据正确；时间线事件集合与卡片完全一致，无写入，read_only，30.41秒 |

最终2样品、2提案、6个事件对象、7条事件版本行；没有人工审批确认、库存交易或残留活动 token。结构验收各项通过；另人工阅读了实际答复、字段、来源和修订，不能将结构通过等同于化学语义普遍正确。

首轮脚本的快照读取误用 `project_id`，被桥的项目校验拒绝；已修为 `project_ids`。恢复时从 Core 读取已完成任务，没有再向模型提交该回合，原错误保留于 harness_recoveries。四轮是开发探索记录，随后修复时间线并做一次定向复核；不是冻结版本的完整基准对比。后续新回合保存源码/提示哈希，首轮恢复的耗时保留null，没有估算。

## 相关自动验证

| 范围 | 结果 |
|---|---:|
| Core repair：样品卡、历史快照、默认知识、后台任务、Agent编排 | 54通过 |
| 旧S05查询测试 | 19通过 |
| Pi运行时 | 107通过 |
| Python前端桥 | 52通过 |
| 前端后台任务及Markdown/模型配置行为检查 | 21通过 |

新增覆盖共同事件在双方卡片/时间线/搜索中一致、当前纠错重分配、历史快照、跨项目拒绝、来源元数据分离、逐事件版本、字符串别名，以及纯日期/带时区时间/带空格时间的当天上界。

S05旧fixture会主动confirm→apply，默认采纳后导致15项在准备数据时失败；该helper现显式使用 `acceptance_mode=review`，保留其原有审核场景。产品默认采纳没有改变。repair与旧S05测试需分开运行：混排这两组时既有conftest装载会导致后续repair找不到lab fixture；本轮未扩展修改全仓测试基础设施。以上不是全仓库全部通过。

```powershell
# workdir: D:\Chem\ChemClaw-Lab-S10-Repair
.venv\Scripts\python.exe -m pytest tests/repair/test_sample_cards_upgrade.py tests/repair/test_knowledge_snapshots.py tests/repair/test_default_knowledge.py tests/repair/test_assistant_jobs.py tests/repair/test_agent_orchestration.py -q
.venv\Scripts\python.exe -m pytest tests/test_s05_query.py -q

# 手动选择运行，会使用现有API产生真实模型调用；必须使用全新目录。
.venv\Scripts\python.exe scripts/verify_knowledge_lifecycle.py --config D:\Chem\ChemClaw-Lab-S10-Repair-test\ui-config.json --run-dir work\lifecycle-new-run --verify-timelines
# --resume仅恢复本脚本已停止且任务终结的隔离库，复用完成回合。
```

## 常用工作台

已核对4174/8876监听者为本项目启动器、主库无queued/running/cancelling任务后，隐藏重启加载修复。启动主/视觉连接探测完成，Core健康接口与前端均HTTP200，Core与模型自动连接正常。

地址仍为 `http://127.0.0.1:4174/`，主库仍为 `D:\Chem\ChemClaw-Lab-S10-Repair-test\ui-demo.db`。重启前后样品、事件版本、提案、证据页、物料、库存交易六张业务表的全行摘要一致；核查记录在 `work/lifecycle-2026-10-23/main-restart-verification.json`。没有清库或导入本轮模拟资料到主项目。

## 留给下一轮的边界

- 首轮模型把同一共同操作保存成两条互相关联事件，两个样品卡均出现重复。原始验收数据如实保留，没有自动删记录。已改LLM提交约定，但新提示对首次写入防重复尚未重新联网测量，不能声称已彻底解决。
- 研究交接中的机器JSON/null约定已补提示，本轮真实验收交付的是表格与文本，未另外验证JSON输出。
- 当前验收证明本次小材料的追加、只读、纠错保护与修复后的共同计划读取；不证明所有跨页归属、来源过时冲突、部分失败恢复或化学推理问题都解决。
- 六图OCR/完整基准和其他供应商没有重跑。后续优先少量验证共同事实首次写入与来源转写纠错后的LLM处理；已有修订不得因重新整理恢复旧值。
