# ChemClaw 接续交接（2026-10-25）

本文件优先于前日交接的“下一步”。用户最新方向：**继续开发和测试，先不考虑 direct/single/multi 对比**。保持 LLM 主导、多 Agent 协作、本地薄代码，以快速闭环和论文发表为优先。人的 UI 暂不改，不引入固定化学语义分类器、答案分支或新的统一审批前置。

项目：`D:\Chem\ChemClaw-Lab-S10-Repair`，无 Git；运行环境 Windows/PowerShell，Python `D:\Chem\ChemClaw-Lab-S10-Repair\.venv\Scripts\python.exe`。中文命令输出设 `$env:PYTHONIOENCODING='utf-8'`。Node：`C:\Users\yu\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe`。

完整报告：[RESEARCH_CLOSURE_2026-10-25_cn.md](D:/Chem/ChemClaw-Lab-S10-Repair/docs/RESEARCH_CLOSURE_2026-10-25_cn.md)。工作入口：`D:\Chem\ChemClaw-Lab-S10-Repair\work\research-closure-2026-10-25`。

## 已完成：不重复修复

1. **完成协议**：正常结束自然正文仍有效；缺 `agent.respond` 本身不是失败。durable 自然有效答复若真实回执 partial 且缺显式状态，使用现有预算内只读＋respond 收尾；默认修复预算1，原可配置2保留。已 settled 恢复不重放模型/工具/写入，保留真实 partial 和重算 missing 诊断。Core pending 与致命子任务约束不变。本轮真实模型没有触发兜底收尾，契约由测试夹具验证。
2. **连接队列**：`ChemClawHTTPServer.request_queue_size=128`。人工20ms accept压力下24并发真实认证读取，旧5成功19拒绝，新24成功0拒绝；无压力两种均90/90。只能证明队列容量缺陷，历史9次fetch failed的确切根因仍未知。
3. **通用提示**：共同未知保留真实主体和原因；核对包括completed在内的全部相关状态；方法/条件写fields；来源覆盖验收；补全替代保留其余字段和时间精度，新有效项不继承旧superseded。最后又加版本标志与跨事件生命周期的区别，仅完成契约审阅，尚无该最后一段的真实模型行为验证。
4. **回归**：相关Python175通过（82.68秒），Node全组119通过（2.019455秒），无真实模型调用。独立审阅发现的P3诊断丢失已修复并复核。不要因结束交接再跑全套。
5. **真实接续修复**：从旧模拟快照四次串行隔离开发接续，32.50/183.81/155.86/184.69秒；前两轮有漏修、第三轮有替代信息问题、第四轮修复并独立读回通过。全部失败和原状态保留，无新比较/分数。后三轮实际仅root＋b_capture，无b_query；独立验收由另一个Codex Agent完成。总provider调用/token不可观测仍null，不拿最后completion usage当总量。后两轮给了具体事件/来源/缺陷，是定向开发反馈，不是盲测泛化。

## 最终模拟知识分支与验收

最终可消费隔离库：`D:\Chem\ChemClaw-Lab-S10-Repair\work\research-closure-2026-10-25\curation-final\lab\after-curation.db`。配套读回、results及reader在同目录；reader含密钥，禁止公开或打印。

独立验收：[final-review.md](D:/Chem/ChemClaw-Lab-S10-Repair/work/research-closure-2026-10-25/curation-final/review/final-review.md)。全链实证：[actual-readback-proof.json](D:/Chem/ChemClaw-Lab-S10-Repair/work/research-closure-2026-10-25/curation-final/review/actual-readback-proof.json)。29个事件、10个提案、2个样品，无库存交易/人工确认；A卡21/B卡20事件，卡/context一致。

- A石榴-07当前测量 `evt_c3a0aea3f4755d5e90abcc7ae8b7a3ef@1`：86Ω/□、四探针、23℃、45%、原单次语境；date_context分钟11:05、2026-10-21、timezone:null，occurred_at=2026-10-21T11:05，来源43页t0001/t0003。旧9b950…/3f8f…实际superseded。
- B岩鹭-12当前 `evt_06e4925cdef75a80862630d934b4dec3@1`：112Ω/□、四探针、24℃、47%、单次保持。A质量13.4mg及旧12.4订正链、B14.8mg保持。
- 双方固含量/产率未知 `evt_293be11d8e4056628cfb318726e5fe77@1`，双方面积/膜厚未知 `evt_b235a9e14fad586f86e27e03da12a54a@1`，都有真实主体、null/unknown及具体原因，不再只有聊天说明。
- B旧排队 `evt_eb4c04d93cec526da862b693469f5a70@1` 实际superseded，basis fa154…@1；真实执行计划22a75…仍completed。历史basis自身superseded按当前投影契约允许，不据此自动撤销断言。
- 新有效历史说明 `evt_c9056912a2375a808d60cc99a3aeec4d@1` 是双方observation、自身completed，但`tape_peel_test.performed=false/status=cancelled`，并非做过剥离。旧d835…双方、28ade…/218e…的A状态实际superseded；真实剥离计划仍cancelled，原basis不变。

最后根模型状态原样为 **needs_review**：误把旧事件 `is_current=true` 与 `supersedes_revision_id=null` 当作生命周期未更新。实际旧事件版本仍当前，而样品维度lifecycle已经superseded，取代真实生效；proposal applied/default，没有待审批写入。不要为这个误报开人工审批、再造提案或改Core。一次子invalid_state_subject草稿失败后在原轮修正成功，保留错误成本。最终新增通用提示正是澄清这两个维度。

各轮原turns、来源库和运行时134项产品映射不变；最终后只改common末段说明。保留精确最终运行旧common及最终135文件快照，见 `D:\Chem\ChemClaw-Lab-S10-Repair\work\research-closure-2026-10-25\source-preservation.json`。旧比较/分数、旧ZIP和原始结果不改。

## 常用服务与安全操作

UI/Core：`http://127.0.0.1:4174` / `http://127.0.0.1:8876`，最后均HTTP200。主库 `D:\Chem\ChemClaw-Lab-S10-Repair-test\ui-demo.db`，私有配置同目录 `ui-config.json` 含真实模型密钥，只程序读取、不输出。API修复已重启部署；runtime与提示新任务加载。所有本轮隔离服务已停，不能直接复用历史endpoint或PID。

主库六业务表从本轮开始到结束逐行哈希一致：12/617/20/27/6/5（samples/event_revisions/memory_proposals/evidence_pages/chemicals/inventory_transactions）；无queued/running/cancelling任务。证据 `D:\Chem\ChemClaw-Lab-S10-Repair\work\research-closure-2026-10-25\main-final.json`。所有模拟修复都在隔离副本，未向人的主库导入。未来重启先核对实时监听PID/命令、确切库/配置和活动任务，后台使用隐藏窗口。

恢复最终模拟分支时用现有snapshot服务/reader工具和全新生命周期文件；不要读出reader密钥，不要复用旧stop文件，不要用主库承载模拟接续。具体已验证恢复方式见前日交接的工具章节。不要公开整个work，DB和私有reader留本地；公开论文材料用经过明确allowlist的产物。

## 下一步

先读本报告和最终独立验收，确认服务即可。指定缺口已闭合，不重跑原四次修复或旧组比较。

优先在**新的自然研究任务**上冻结当前版本，做一次非比较的独立读者接续：只给任务与现有只读工具，不提供缺陷答案，检查它是否会自主选择样品生命周期投影、区分版本与状态、保持有来源未知与全部测量信息。保存引用、真实错误及消耗未知，不预设多Agent优势。如果要做真实研究员纵向案例，需要真实记录与任务来源；当前六页全部文字模拟，不得写成真实实验/OCR或人类盲评。

新窗口可使用：

> 请阅读 D:\Chem\ChemClaw-Lab-S10-Repair\HANDOFF_2026-10-25_cn.md，按最新交接继续开发和测试 ChemClaw。保持 LLM 主导、多 Agent 协作和本地薄代码；先不考虑 direct/single/multi 对比，不重复已完成修复，以快速闭环和论文发表为优先。

## 最新追加：v3六图真实上传评测（优先于上文下一步）

用户随后要求用桌面 `ChemClaw_模拟手写记录_v3_2026-10-24` 的图像输入包逐张上传，参考评测者包测评。现已完成一次保留运行，没有产品/提示修改或图像补跑。完整报告：[IMAGE_V3_EVALUATION_2026-10-25_cn.md](D:/Chem/ChemClaw-Lab-S10-Repair/docs/IMAGE_V3_EVALUATION_2026-10-25_cn.md)。

- 工作目录 `D:\Chem\ChemClaw-Lab-S10-Repair\work\image-v3-2026-10-25`；新空项目 `simulated_image_v3_20261025`，同新会话，生产前端jobs/真实OCR/durable Pi，P01/P02/P03→S1、P04/P05→S2、P06→S3。gold/作者转写不入被测系统，六图hash四方匹配。
- 内容评分 **55.5/100**（27项weight×0/.5/1），按材料实际状态/阶段输出口径，**不是端到端成功**。S1 completed；P04/P05因收尾循环分别正常cancel一次；S2 partial；P06上传completed但无知识写入；S3两次提案校验失败后超时partial，无正式人类摘要/JSON。9任务2217.31秒，额外外部AI屏障210.09/146.09秒；模型调用/token/cost全量仍null。
- S1/S2/S3事件34/58/58、提案3/5/5，四样品ID不变；S2/S3卡片、字段和状态完全相同，P06事件0。28数值目标15正确、12缺失、1旧值；既有58事件引用可解析不等于知识完整。独立评分/数值/状态/冻结证据在evaluation目录；不要把失败proposal或未执行respond参数算成功交付。
- **新通用运行缺陷已证实，未修复**：P04/P05实际生产函数重建settled checkpoint为1,909,000/2,473,242字节，超过1,800,000硬限，在Core complete前失败，worker停heartbeat后租约恢复循环。原job各1条、各标准取消一次；未手造finish、改数据或重传。独立诊断P04/P05-runtime-diagnosis.json与operations取消前DB/回执保留。
- 新fork-none外部AI只获四只读工具：S2 12请求9成功3schema错误；S3仅generic更新通知后6成功请求，A1/B1 fingerprint与22/25事件均未变，计划仍pending。它没有看到P06事实，不虚构更新，结果另列不加100；不得称最终六页交接成功。external-reader目录有完整答复/轨迹。
- 评测者回看实际六PNG，关键OCR内容可见，P06未落盘不能归咎关键数字没转写。CER/WER未测仍null。产品134文件、原图、运行脚本/公开任务前后冻结不变，主库六业务表哈希不变，两常用端口200，隔离服务和token已停。DB/reader-private.json不公开。
- 评分结构数据已由独立Agent保存；最后两个评阅子任务触发账户用量上限，主Agent用已有评分/实证完成报告和数学/冻结/外部请求复核，无额外模型补跑，不声称未完成的额外独立终稿已通过。

下一开发重点应改为：先用本轮保存的真实轨迹**离线**复现/修通用checkpoint超限与租约循环，再处理上下文膨胀及P06无样品元数据/状态更新标签失败。不要先重跑六图或直接抬高所有预算，也不要加入CC26/化学关键词答案分支。修复后若再测，另列开发回归，保留本55.5与失败成本，不作新盲测或direct/single/multi比较。


## 最新追加：系统鲁棒性已修复并部署（优先于上文未修复提示）

用户要求只修系统鲁棒性，可能涉及 LLM 能力的问题暂缓。本轮已完成，不要再按上一节的“checkpoint超限未修复”重复开发。

- 完整报告 `docs/RUNTIME_ROBUSTNESS_2026-10-25_cn.md`；工作目录 `work/runtime-robustness-2026-10-25`，桌面另有 `ChemClaw_系统鲁棒性修复_2026-10-25.md`。
- 保留1,800,000字节checkpoint上限；大快照采用可逆JSON引用去重，先脱敏、内容仍接受Core原校验。小v1/v2/v3兼容；解码校验run ID、引用与展开边界。模型/提示/语义/预算均不变。
- 大恢复请求走Python→Node本地gzip，保留2,000,000字节线上限制，解压与总恢复展开最多8MiB；修UTF-8跨HTTP分块问题。
- 确定性checkpoint_size_limit/checkpoint_invalid以及runtime_request_size_limit不再假装临时故障。整树停止、聚合回执且最多64KiB摘要，真实已执行回执可保留partial；动态坏child恢复无法被父模型提升completed。临时存储/网络故障仍保留lease恢复，不重放不确定写入。
- typed不可重试结果若被Core明确400/schema_violation拒绝，原fence只再提交一次固定小体日志定位，保留partial；503/timeout/409/普通结果不走该降级。取消和旧epoch仍拒绝。
- Node全组135通过（2.350秒），Python相关68项+74子用例通过（22.46秒），独立审查已核对。无ChemClaw真实模型调用。
- P04/P05原取消前DB只读actual createRuntime恢复通过，均partial，模型/业务工具/新委派0；最终cp无损降到601,574/724,280字节，完整恢复请求gzip338,866/532,267字节，worker完成结果907,322/1,186,908字节且Core safe_json接受。不是六图补跑，未改变旧取消任务、55.5或原失败成本。旧恢复循环诊断补充：后续恢复还可能先撞2MB请求入口，不能把每次都归为最终cp超限。
- 主服务已重启部署；4174/8876均200，Core自动连接与模型配置正常，六业务表哈希不变、12/617/20/27/6/5，无活动任务。当前listener PID131944仅快照。启动入口暂为工作目录launch_main.py，调用原run_frontend_demo.py且仅跳过本次启动模型探测，vision标记not_tested；真实UI探测函数已恢复。以后仍核对实时PID/命令/db，不直接复用PID。
- 已保存12个改动文件after快照、可用before快照及preservation.json；正式评测报告、已发布evaluation记录与原ZIP已逐字节/哈希核对不变。一个迟到评阅Agent写入旧evaluation/report.md，写前未核对旧内容，无法保证该非正式补记旧版不变；已另存late-evaluation-review.md并记录，不当新评测，正式评分/报告不受影响。

下一步按用户约束暂不处理输出截断、P06知识遗漏、提案主体/状态标签等模型相关问题；不做direct/single/multi比较，不重跑六图来抹去旧失败。若用户之后授权新评测，另建冻结版本、另列开发回归结果。
