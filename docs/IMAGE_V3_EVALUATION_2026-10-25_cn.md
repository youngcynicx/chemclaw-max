# ChemClaw v3 六图逐张上传评测（2026-10-25）

**内容评分 55.5/100；六图端到端研究交接未通过。** 六张图片均已各提交一次并保存原件及真实视觉转写，但 P04/P05 的任务收尾卡住后各取消一次，S2/S3 为 partial；S3 没有交付有效的人类摘要或 JSON。P06 已转写，新增事实、更正和生命周期更新却未落盘，独立只读 AI 因此无法通过样品记忆接续这些更新。

本轮没有修产品、调提示后重跑或重新上传，没有 direct/single/multi 对照。分数没有校准通过线，不能解释为可靠率或成功率。

## 运行方法与材料隔离

使用用户提供的 `C:\Users\yu\Desktop\Chemclaw 论文 (1)\ChemClaw_模拟手写记录_v3_2026-10-24\ChemClaw_v3_图像输入包.zip`，参考同目录 `ChemClaw_v3_评测者材料.zip`。六 PNG 与输入包、冻结清单及评测者参考的 SHA256 全部一致，参考版本为 `FROZEN_VISUAL_REFERENCE_V3_1`。

通过正式前端桥 `/api/core/jobs` 上传，使用生产持久 worker、真实视觉 OCR 和 Pi 多 Agent。新建空的隔离项目 `simulated_image_v3_20261025`，没有种入样品、实验值、库存或金标准。沿用当前真实配置：文本模型 glm-5.3-flash，视觉模型 glm-4v-flash；OCR 上限1024，主模型 max_tokens16384、context_window128000。默认五角色可用，实际执行只有 orchestrator 与 b_capture。

顺序严格为 P01→P02→P03→S1任务→P04→P05→S2任务→P06→S3任务；同一个新会话，每张上传任务结束并快照后才提交下一张。S1/S2 不接触未来页。被测输入只有六张原图、评测材料明确可公开的任务及固定的逐图整理说明，没有参考转写、gold、作者源文或评分答案。评测者与被测模型分开；没有外部检索或跨测试会话知识。

产品134项源码/提示/角色/运行器依赖、原图、执行脚本与公开任务在运行前后均保持冻结。所有真实失败、模型内部参数修正、并行委派、原始答复和取消轨迹均保留。

## 逐图与阶段结果

| 步骤 | 原始任务状态 | 耗时（秒） | 说明 |
|---|---|---:|---|
| P01 | completed | 209.50 | 转写和提案落盘；一次参数错误后在原轮修正 |
| P02 | completed | 265.84 | 跨页历史和状态整理落盘 |
| P03 | completed | 184.72 | 初始四样品/独立批次状态形成 |
| S1 | completed | 54.73 | 正式初始阶段答复及快照保留 |
| P04 | cancelled | 538.31 | 子输出截断；收尾快照超限、反复租约恢复；正常取消接口终止原任务 |
| P05 | cancelled | 416.08 | 同类快照超限；原任务内并行委派两个capture，一成功、一截断；正常取消一次 |
| S2 | partial | 182.16 | 最终状态声明再次截断，未交付完整阶段总结 |
| P06 | completed | 62.16 | 原件和转写保存；没有新知识写入，答复复述旧阶段状态 |
| S3 | partial | 303.81 | 两次提案校验失败后超时，无有效最终答复或 JSON |

九任务合计 **2217.31秒（约37分钟）**，包括 OCR、模型、工具、轮询、恢复循环及取消前等待，不是纯推理时间。另有独立外部 AI 屏障等待210.09和146.09秒。实际18个 Agent run 不是18次 provider 模型调用；全量模型调用、token和货币消耗不可由末次 usage 恢复，均保持 null。

这是一轮**含两次操作性取消的开发回归**。取消只终止原任务的恢复循环，保留已写事实和回执；没有人工构造完成答复、补知识或重传图片。运行器 manifest 的 completed 仅表示所有预定步骤已尝试并完成收尾，不表示模型研究任务成功。

原始答复：[S1](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/public/S1-response.md)、[S2](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/public/S2-response.md)、[S3](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/public/S3-response.md)。完整脱敏轨迹：[results.json](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/public/results.json)。

## 评分与实际知识覆盖

独立评测 Agent 按27项、每项权重×0/0.5/1评分，主 Agent 又确定性核验了编号、权重、乘积与总和。按材料“当前状态/阶段输出或状态”的口径，正确持久事实可以得内容分；没有把失败提案、未执行 respond 参数或未交付检查点当作答案。E1仅对实际 API 等价结构化读回给部分分，观察者生成的读回 JSON 不是系统交付的交接 JSON。

| 维度 | 得分 | 满分 |
|---|---:|---:|
| A 身份、投料与实验历史 | 18 | 20 |
| B 测量结果、条件与换算 | 10 | 20 |
| C 跨阶段更新 | 10 | 20 |
| D 未知、解释边界与证据 | 9.5 | 15 |
| E 人类/机器交接 | 4 | 15 |
| F 库存与机时状态 | 4 | 10 |
| 合计 | **55.5** | **100** |

完整逐项理由、事件 ID、版本、字段路径、生命周期和来源在 [scorecard.json](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/evaluation/scorecard.json)。数值目标逐格证据在 [numeric-audit.json](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/evaluation/numeric-audit.json)。

- 四个样品/批次身份、独立 B2 合成、主要投料修正、分段活化、前期 BET 与原始干态测量大体保留；S1边界和S2实际持久状态推进各得5分。
- B1 的继承说明误提及 AA，虽未另写一个 AA 数值，仍是结构化说明错误。当前 B1 低压容量仍为1.24，没有落实 P06 的1.12更正；原73.92 cm³(STP)/g保留，但3.30 mmol/g换算没有成为当前机器字段。
- P06 的湿气第1/5次工作容量、保持率、B2新回单、M08到柜、BK41取消/BK42确认及实际运行均没有形成当前知识。S3改值与闭环项均0分。
- 最终没有人类摘要与交接 JSON；不能把“没有发现二者矛盾”记为一致通过，也没有由评测者补造替代 JSON。

冻结选择的28个当前数值目标：**15个正确落盘、12个缺失、1个旧值，覆盖53.6%**。这是所选数值目标的持久覆盖，不是整页/OCR准确率；正式S3答复缺失，所以该答复没有可计为正确的数值目标。

| 实际状态 | S1 | S2 | S3 |
|---|---:|---:|---:|
| 图片/转写页 | 3/3 | 5/5 | 6/6 |
| 样品 | 4 | 4 | 4 |
| 事件修订 | 34 | 58 | 58 |
| 提案 | 3 | 5 | 5 |

S2→S3 的样品卡内容、字段与当前状态完全相同，P06事件数为0；两个主要临时结果缺口关闭0/2。58条既有事件的引用均可解析，卡片与研究上下文也一致，但这只证明**现有知识可追溯且视图一致**，不证明六页知识完整。证据：[state-proof.json](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/evaluation/state-proof.json)、[metrics.json](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/evaluation/metrics.json)。

## OCR、整理与运行失败分开归因

六图得到104个视觉转写块。评测者回看了六张实际可见图；所核查关键数字、涂改、批注和表格没有发现足以解释 P06 整理失败的 OCR 漏读。存在 P01/PO1、IR/1R 等字形混淆，P06有一个额外 unclear 块；未据此虚报整页 CER/WER，这两个指标仍为 null。

P06转写实际已包含低压更正、湿循环表、B2新结果、库存和预约更新，因此主要断点发生在转写之后：

1. **收尾检查点溢出。** 独立审阅用真实生产函数、本地保存轨迹和共享数组关系重建：P04最终检查点1,909,000字节，P05为2,473,242字节，均超过1,800,000上限。错误发生在 Core complete 之前；worker停止心跳，租约恢复后再次尝试同一收尾，形成循环。字节数是本地重建值，不伪称捕获了失败HTTP payload。原根即使声明 completed，也因真实部分子任务被约束为 partial。
2. **模型输出及最终答复预算。** P04/P05的部分子输出截断，S2有一次 missing_status_declaration 收尾，收尾声明自身又因输出达到上限没有执行。不能把它的参数当正式答复。
3. **P06提案结构失败后未交付。** S3先因第10条缺 canonical subject_sample_labels 被拒，随后因状态更新标签为空再次失败，最终根超时。两次均没有采纳，所以不存在P06知识/状态更新成功的回执。

诊断与取消证据：[P04](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/P04-runtime-diagnosis.json)、[P05](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/P05-runtime-diagnosis.json)、[P04取消](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/operations/P04-cancellation.json)、[P05取消](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/operations/P05-cancellation.json)。

## 独立外部 AI 接续：另列，不加100分

一个不继承开发历史的新 AI 没有看图片、gold、被测答复或数据库，只获授权测试 reader 和四种只读工具。在 S2 后完成一次接续；S3 后只收到“出现新记录，请重新获取上下文并更新判断”，再实际重新读取。

- S2：12次真实请求，9成功、3次 schema 错误均保留。能发现A1/B1、读取当前知识并追查来源；区分干态比较、报告的 IAST 与湿气尚无结果。原值73.92与22.4换算为3.30的算术成立。错误请求同时给 revision 和 include_history，未将其记作成功读取。
- S3：6次真实成功读取。A1/B1事件数仍22/25，指纹与S2相同；湿气计划仍pending。读者没有虚构新湿气结果，并明确读取范围和 knowledge_complete=null，没有断言整个项目任何位置都无新记录。

这项结果证明它能消费已保存的S2知识，也暴露了P06未归卡造成的接续断点；不能表述为“新AI成功消费六页最终更新”。本次四工具读者未发现未关联的新页，不能靠给它隐藏图片或事后补答案来修饰本轮结果。

原始接续：[S2答复](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/external-reader/S2-answer.md)、[S2轨迹](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/external-reader/S2-trace.json)、[S3答复](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/external-reader/S3-answer.md)、[S3轨迹](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/external-reader/S3-trace.json)。

## 保留物与接续重点

工作目录 `D:\Chem\ChemClaw-Lab-S10-Repair\work\image-v3-2026-10-25` 保存每张图和阶段的SQLite一致性快照、脱敏原始轨迹、OCR、评分、冻结源码与失败现场。DB和reader私有配置仅留本地，不进入分享包。最终语义评分和数值/状态证据由独立评测Agent已落盘；两项最终评阅子任务遇到账户用量上限，主Agent依据已保存数据完成本报告和确定性复核，没有声称未完成的额外独立审查通过，也没有再调用被测模型。

主服务4174/8876均HTTP200，主库六业务表逐行哈希未变，隔离任务无活动run token，隔离服务已停；未执行实际扣库、采购、预约、发消息或仪器操作。[最终完整性](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/public/final-integrity.json)、[评分/冻结/外部请求结构复核](D:/Chem/ChemClaw-Lab-S10-Repair/work/image-v3-2026-10-25/evaluation/final-integrity-review.json)。

后续优先离线复现并修复通用收尾快照去重/引用化及超限终态处理，避免无限租约恢复；限制重复全量读回造成的上下文膨胀；复现无样品元数据与状态更新标签的合法表达，不能靠虚构主体绕过校验。保持 LLM 决定语义、本地验证结构与权限。若复测，应另建修复后运行并保留本次55.5及失败轨迹；同一已见案例不再作为新盲测，不覆盖原成绩。
