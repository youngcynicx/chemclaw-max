# 六页纵向留出案例与跨页引用修复

2026-10-24。接续修复后独立外部 AI 交接，增加一个不同标签、不同记录布局的文字模拟案例。原始输入、任务、评分点及模型所用产品/提示先冻结，原始失败不覆盖。

## 留出运行

案例为两片导电薄膜“石榴-07 / 岩鹭-12”，六页原始文字按三个阶段各提供两页：共同热处理和两项计划→单片测量、另一片排队→后者补测、共同取消剥离、进炉前称量录入订正。条件包括共同事实、专属数值、比较中的仅提及对象、不同测量环境、部分完成与真实未知。所有记录均明确标为模拟，不是湿实验或 OCR 结果。

`benchmark/handoff_v2/input/` 是被测模型唯一的原文/研究任务输入；`evaluation/rubric.json` 不进入模型输入。每阶段只提供自然研究交接问题，无具体答案。三个条件使用相同模型 glm-5.3-flash、相同单响应上限；single 使用现有 Pi/Core 单角色能力并集，multi 使用现有主 Agent / b_capture 等委派路径，direct 每阶段得到累计原文，不使用持久记忆。

| 条件 | 原始答案 /24 | 持久状态 /10 | 三阶段原始耗时合计 | 保留的工具调用 / 错误 |
|---|---:|---:|---:|---:|
| direct | 23 | 不适用 | 40.07 秒 | 0 / 0 |
| single | 23 | 9 | 333.19 秒 | 27 / 3 |
| multi | 23 | 7 | 470.36 秒 | 47 / 5 |

这是一个开发者设计的留出模拟案例，由独立 Agent 按冻结 rubric 评阅，不是人类盲评。案例设计者知晓前期缺陷；不能据此主张泛化、统计差异或多 Agent 整体优势。9 个原定阶段任务不等于9次供应商模型请求；完整模型调用和 token 均不可完整观测，保持 null。工具数为回执内按调用 ID 去重的可观测数。阶段耗时包含宿主/上传/工具/模型/结束处理，不是纯推理时间，也不是等总推理预算比较。

主要结果：

- 两个持久条件的实际主体和数值没有串卡；初始共同计划的部分完成、最终共同剥离取消和质量订正均有真实保存及来源依据。
- direct 的最终 JSON 有重复 `reason` 键：可解析，但普通解析丢失部分逐项未知原因，因此 C4 得1分。
- single 最终答复漏写四探针方法（C2得1分），方法在持久 fields 中仍能找到；其中后者方法仅保存在状态更新 reason，机器消费弱于独立方法参数，评阅如实记限界，没有临时强加专用键规则。早期固含量/产率未知说明仍未归属样品，context 单独消费存在语义遗漏（P5得1分）。
- multi 最终将旧排队条目标为 completed，而非 rubric 所要求的 superseded（C3得1分）；岩鹭测量方法只在 raw_text，未保存在任何 fields（P2得1分）。旧排队状态及一条仍为 plan/unknown 的备注使 P3得1分，未知说明语义遗漏使P5得1分。最终实际 pending/in_progress 为0，不能因此宣称所有语义验收通过。
- multi 最后阶段原始状态为 partial，`completion.mode=legacy, stopReason=stop`；子 b_capture 返回 completed，但根 Agent 没走显式最终响应路径。完整答复和所有写入/读回仍保留，不改记 completed。根 schema_violation、子状态主体错误和原轮内纠错均保留。

## 两次观察失败与接续边界

新运行器最初误用人的前端桥读取 `query.sample.context`，被桥的白名单拒绝。single/multi 的首阶段模型已经完成，但后置观察分别记录 harness_failed；未将其当作 Core 接口故障，也未重跑首阶段。

修复仅让观察使用各条件专属只读 reader；增加严格恢复，拒绝已启动但结果不确定的阶段。11项无模型回归覆盖真实 HTTP 读取和恢复边界。原 arrival 条目、回执和 `.db` 保持不变，补读另存 `arrival-observation-recovery-1-*`，模型调用为0；后续只接续尚未运行的四个原定阶段。

原始产品/提示/角色/原文/任务/rubric全程一致；唯一变化是观察运行器，旧文件与原postcheck保存在 `harness-repair-original/`。恢复期间postcheck仍为 unchanged=true。这个偏差已记录，不能把整个运行描述为所有开发代码均从头到尾不变。

## 留出后修复：同项目跨页证据

跨页提案反复被旧 Core 校验要求“所有块属于提案单页”而拒绝，模型只能拆页重写。固定版本运行结束、快照与评分保存且服务停止后，才修改产品：

- `experiments.py`：default notebook / adopt 逐引用验证来源页与 evidence object 同项目、精确当前转写版本、匹配当前 run 及原文块；每个item自有有效证据，整个proposal至少一条引用来自锚页。辅页独有item不被迫附上无关锚页引用。
- `agent_gateway.py`：仅澄清schema里提案锚页版本与各ref版本的含义；字段形状和写权限未增加。
- 旧 review 单页历史版本策略、Agent run/claim fencing及业务审批保持原策略。没有新表、数据迁移、化学词表、数值猜测或语义归属分支；读侧已有多页来源及 stale 逻辑继续复用。

从原 single 首轮实际失败工具调用导出实参，在两个独立副本经过同一 Core 创建/采纳路径重放：修复前 invalid_evidence/404；修复后9项采纳。唯一参数变化是绕过旧缓存所需的新 operation_id；化学值、主体、来源和原实参哈希不变，模型调用为0，原库及基准产物未改。

这是引用校验缺陷的确定性修复验证，不是新的模型成功或24/10分提升，也不证明原提案的全部语义字段都完整。

最终针对性回归 **174项通过，80.13秒**，包含交接要求的139项和新增跨页24项、运行器11项。跨页回归覆盖外项目、错页/块/版本、旧run、无锚页、形状异常拒绝与事务原子性、采纳前辅页变更重验，以及辅页更新后卡片/context一致 stale 和精确旧块可读。独立代码审查未发现范围内的可操作缺陷。JavaScript与提示未改，本轮没有重复 Node 全组。

## 可复核产物

- `benchmark/handoff_v2/`：冻结原文、自然任务、独立评分点、外部研究任务和 manifest。
- `scripts/run_longitudinal_case.py`：多页真实 Pi/Core 运行器，及严格的观察失败恢复；不自动模型重试。
- `work/handoff-v2-frozen-2026-10-24/`：原始run、两条件三阶段快照、实际读回、恢复日志、前后哈希。
- 其 `evaluation-review/review-summary.md`、`*-scores.json` 和阶段 `*-proof.json`：独立 Agent 原始评分与字段/状态/引用依据。
- 其 `measured-stages.json`：原始各阶段状态、耗时、可观测工具数与成本边界。
- `work/cross-page-evidence-fix-2026-10-24/final/before-after-comparison.json`：原实参确定性前后复现；同目录保留完整回执、测试记录。
- `work/cross-page-evidence-fix-2026-10-24/code-review.json`：独立代码与本地测试审查。

留出评分与任何之后的通用补缺审计分开。恢复数据库和含凭据reader配置仅保留本地，不纳入公开材料。

## 留出之后的一次通用补缺审计

另从 multi 最终快照复制新库，用修复后产品做一次通用来源交付审计。任务没有样品标签、具体数值、评分或缺陷报告，不上传新附件；请求自主核查主体、所有原件、实际fields的数值/单位/方法/条件、状态和未知。原turns保持不动，新增运行只记在 `audits[]`。

202.94秒后原状态仍为 partial（legacy/stop）。确有一次成功提案，事件20→21、提案6→7：新增事件的 `fields.measurement_method="四探针"`，保留岩鹭112 Ω/□、24 ℃、47%RH与单读数语境，用显式superseded取代旧不完整测量。独立读回核查确认该补全有效。

这次审计**没有**修复旧排队仍completed、plan备注仍unknown和未知说明归属遗漏；不能宣称审计全面通过，也不替换原23/24、7/10。原答称8次瞬时读取失败，实际根轨迹为9次fetch failed，另子工具schema_violation一次，均保留；后续工具读回成功不抹去这些成本。完整模型调用/token仍null。

原三轮turns逐对象一致，来源快照哈希不变，审计前后133项产品/运行器哈希映射相等。完整审计位于 `work/cross-page-evidence-fix-2026-10-24/llm-audit/`，独立核查在其 `review/before-after-review.md`、`.json`和`before-after-proof.json`。临时服务已关闭，不追加第二次审计。

## 部署与版本保全

174项最终回归通过后，重新核对主工作台监听进程的真实入口和数据库，确认assistant_jobs/agent_runs没有queued/running/cancelling任务，再隐藏重启常用4174/8876。两端HTTP200；六表逐行摘要重启前后一致：samples12、event_revisions617、memory_proposals20、evidence_pages27、chemicals6、inventory_transactions5。人的UI与业务数据未改。

`final/source-preservation.json`同时核对修复前后两份产品文件。修复前文本通过work目录内精确逆patch重建，再验证与原preflight SHA-256完全一致，才保存为baseline-source；没有将其描述成事先存档的原件。修复后字节副本与最终重放哈希一致。源码根目录没有Git仓库，版本以这些来源文件与哈希清单复核。

下一步优先处理主Agent显式完成协议、补全审计的剩余语义缺口与读取并发的瞬时失败，并取得真实研究员的纵向案例及交接用时；不先扩P2或人的UI。本轮仍未证明多Agent整体优势。
