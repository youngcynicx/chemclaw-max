# ChemClaw 交接（2026-10-27，本轮已完成）

源码根 `D:/Chem/ChemClaw-Lab-S10-Repair`，无 Git。Python `.venv/Scripts/python.exe`；Node `C:/Users/yu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe`。当前任务已经完成；不要继续旧日志中的上传、屏障或恢复命令，不要自动重跑、补知识或改分。

用户约束保持：LLM 主导、多 Agent、薄本地代码、快速闭环/论文；不做 direct/single/multi 对比；系统鲁棒性修复，不用化学硬编码补模型答案。用户最近的“继续”已完成到报告、评分、完整性审计和桌面证据包。

## 交付与主要结果

主报告 `docs/RESEARCH_LOOP_REPAIR_2026-10-26_cn.md`。新工作目录 `work/research-loop-2026-10-26`，实际回归在其 `regression` 子目录。独立评分 `regression/evaluation/report.md`、`scorecard.json`、`numeric-scorecard.json`、`public-27-table.md`。

27 项未舍入 **76.75，ROUND_HALF_UP 显示76.8/100**。正式 S3 JSON 数值25/28、明确单位22/28、条件13/28、数值+单位+条件同时完整10/28；正文与JSON共享语境下22/28。实际current持久状态 **0/28**：全库0sample、0experiment_event、0proposal。不能把评分或正式JSON数值正确当知识入库成功。

九任务8partial/1completed。P01 12859.54秒含宿主离线，P02 914.80、P03 916.97、S1 903.09、P04 920.03、P05 911.42、S2 901.39、P06 923.80，均run_timeout；S3 explicit agent.respond completed288.94秒。S1/S2只有超时正式答复，不能用S3补前阶段分。6图各唯一上传/OCR，6page/6transcript、19run/9job，真实角色9orchestrator+10b_capture；OCR vision_ocr另列，无实际b_query/库存/planner运行。

S3真实正文19474 UTF8字节，SHA `5bf3afe962e9b7d52ee7eaa347fafb87f4d37c558a6de526a5354712e982ac29`。内嵌未围栏JSON13904字节，SHA `41045ddf8bcf60845ffa354c78a194630e542aee1bd467a923b875b1906ba862`。独立评测者与根代理严格提取逐字节一致、无重复键或NaN/Infinity。`delivered-outputs/S3-machine-handoff.json`是原正文子串，没有新字段或后端知识写入。原export-proof漏检只因支持围栏/全正文JSON，原证明保留，正确结论在export-proof-addendum.json，不能误报JSON缺失。

桌面 `C:/Users/yu/Desktop/Chemclaw 论文 (1)/ChemClaw_研究闭环修复_六图回归报告_2026-10-27.md`。
同目录证据 ZIP `ChemClaw_研究闭环修复_六图回归证据_2026-10-27.zip`，**59文件，217897字节，SHA256 `2cac53fadffb19e275ff3e9a4af168a02554b0d2c00ee5125348032f5857d5ca`**。白名单打包与解包字节校验通过，已知实际密钥扫描0命中；不含DB、私有配置、原始gold/空评分模板或原始checkpoint。评分引用的部分私有证明仍在仓库，包不是脱离仓库的全部私有证据公开复现包。receipt为`regression/package-receipt.json`，不要修改包内报告或覆盖旧包。

## 已实施修复和验证

`runtime/pi/task-continuation.mjs`与runtime：每个Agent运行最多一次截断任务续做，和只读completion repair分开；先真实成功读回再允许原权限写入/委派；原绝对deadline和共享工具余额持久化、恢复不重开预算。相同完整参数成功写入复用原回执；同ID改参拒绝；unknown写入效果停止待核查，包括旧completed journal。不是语义去重/exactly-once保证。

`evidence.pages.list`复用已有接口，补稳定分页/客观source_state，orchestrator/b_capture/b_query开放；统计复用完整来源授权，不泄露私有嵌套事件。原件、OCR、链接数不等于知识完整，knowledge_complete仍null。没有新增上下文压缩模块或化学解析规则。

`provider-observability.mjs`、models/frontend：SDK及OCR请求意图/实际HTTP尝试/重试/usage/时长/错误安全账本；不记正文、图像、keys或思考内容。1113和普通429区分，额度耗尽停任务树。`benchmark_phase_guard.py`与新harness持久游标，终态不重投、不重复OCR；外部屏障无一小时误超时。恢复额度需人类明确确认，不自动付费probe。

Node **189/189**，frontend **76/76**。Core初次561pass/36fail，36在改前57源码隔离副本中逐项同签名复现。随后只修5个旧Python测试夹具/契约（显式review、真实页/块引用、默认采纳/不可信几何的更强断言），最终完整 **597/597，0fail/error/skip**，原失败与5个原测试备份保留。未改默认采纳及来源拒绝37项另跑通过。5测试文件不在145产品冻结范围内；全产品145hash始终一致，S1并行测试不能作受控性能比较。详情`legacy-test-maintenance/`、`offline-verification.json`、`product-changes.patch`、`final-independent-review.json`。

根独立复核`regression/score-validation.json`：27权重/因子/算术、28计数、证据文件和JSON字节一致，无重新语义打分。最终完整性`regression/integrity-audit.json/.md` **27/27通过**：3/5/6页阶段、原输入ZIP、原/恢复runner及145冻结、快照hash、OCR无重复、进程/监听清理及active job/run/token0。审计脚本在子Agent使用限额报错前已完整落盘，最终由根执行并通过，不因该旧Agent状态误判任务未完成。

## 运行边界、中断和用量

项目`simulated_image_v3_researchloop_20261026`，隔离DB `regression/run/evaluation.db`；text/OCR均glm-5.3-flash、zai/max、16384输出/128000上下文，每任务Agent树900共享，OCR240另列，无付费合成预检。P01–P03/S1、P04–P05/S2、fresh reader、P06/S3、同reader更新。产品、提示、输入、预算和旧终态运行中未改。

原进程在P01消失，原因未知。标准同job/session/root claim只按原过期deadline收尾，epoch2→3一次；独立恢复审计15/15确认0新provider、0新OCR、六来源知识表逐行hash和childCP原始字节不变、root消息/回执/预算不变。P01原deadline1793024238012、剩余工具57未重置。恢复前后证据在rootwork/operations/host-recovery-1，备份和新resume runner在regression/operations/host-recovery-1；这些备份含私有数据，不公开打包。

S2屏障跨约9.7小时离线等待后继续，7终态任务/前5图不重投，浏览器会话免费刷新后仅上传P06。最终`manifest.status=completed`只表示流程完成，非9任务全成功；servers_stopped和所有freeze字段true，隔离50200/50201及实际Python/Node均已退出。不要重新执行run_research_loop.py或resume_after_host.py。

合并安全观测`regression/provider-observations.json`：**50唯一意图/尝试，49终态、1未收尾，6OCR+44Pi**；40条有usage，已知total_tokens小计949825，不是全量；完整tokens/cost均null。原P01未收尾provider意图不证明远端接受或计费，不能删掉或算0。原评测者turn明细49漏P01原OCR，`evaluation/provider-observability-addendum.md/json`保留并补充scope，未覆盖原报告。36工具尝试、2工具错误、0pending journal、0完全参数回执复用；工具数不是模型调用数。13条length响应均16384输出，reasoning占99.15%–99.94%；这是当前输出预算表现，不推断模型总体能力，也没有以增加本地业务规则补分。

## Fresh AI 交接和主服务

`/root/fresh_research_reader` fresh fork-none，初始只给公开research-task/工具contract/helper，五只读工具；未提供图/gold/DB/旧结果。S2真实7API调用读5页；同reader通用刷新后S3真实3API调用，读新P06、旧5页版本未变。initial-answer.md、update-answer.md、S2/S3 trace和boundary.json封存，旧答案/trace未改。独立评审确认更正/湿结果/保持率及权衡更新，来源块与冻结revision吻合；另列加0分。样品目录始终为空，不能称结构化样品知识复用成功。读API内部0模型调用不等于外部AI推理免费。

主4174/8876健康200，新代码/三个来源cap已加载；私有配置`D:/Chem/ChemClaw-Lab-S10-Repair-test/ui-config.json`未改、启动无付费probe，六业务表逐行hash仍与原准备一致、active0。主界面模型配置沿原设置，不能把隔离max模型设置当成已写入主配置。`main-after-final.json`和`main-final-health.json`留证；main-deployment/before-private.db含私有数据不公开。

当前无本轮必做工作。后续可依用户新指示先核对输出预算配置、再冻结新候选/未见案例；不得自动调本轮参数、补入库、重评分或把本开发案例写成盲测/真实湿实验/泛化或显著性结论。历史900轮70.3报告和旧桌面包保持原样。
