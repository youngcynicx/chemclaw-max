# ChemClaw 英文 MOF 六图测试（2026-10-29，已完成）

用户本轮要求已完成。不要继续旧runner、上传、屏障、补库或重评分；等待新指示。原2026-10-27及其他历史轮不变。

报告 `docs/ENGLISH_MOF_SUITE_2026-10-29_cn.md`；工作根 `work/english-suite-2026-10-29`，运行在其regression，独立评测在evaluation。桌面报告 `ChemClaw_英文MOF六图测试报告_2026-10-29.md` 与主报告字节一致，SHA256 ccac5497e1aa80fb42edbe97ea530cb96cb3906c88bb60192e6c8e9b90ed02af。桌面证据包 `ChemClaw_英文MOF六图测试证据_2026-10-29.zip`：66文件、197250字节、SHA256 1d64b16350ee799f9f8e76116a2f0053ef85e3bb73ce00c0ab39274255eeb640。receipt regression/package-receipt.json，实际密钥扫描0、CRC/解包字节通过；不含DB/配置/密钥/原gold/私有来源/原始checkpoint/隐藏推理。少量评分引用需本地私有快照，不是全私有状态独立复现环境。

四题预冻结AI评分95.7/100，精确2297/24。L1=49/2=24.50；L2=70/3≈23.33；H1=74/3≈24.67；H2=557/24≈23.21，各25。包内无明确分值权重，独立Agent在见真实输出前冻结原子评分；不是人类盲评、语言/架构因果或泛化证据。原冻结清单可信SHA256 d965f27bc68c508213bba6884ffe13401b56fb4eb814de1fc00cc1e7838c54cb。评测者读取后题前封存前题，不回填。算术/哈希/原文/快照435/435通过，没有根语义重评或向应用反馈。公开scorecard.json/report.md/numeric-scorecard.json与各题score/evidence文件均封存。

十任务6partial/4completed。P01 973.92s、P02 960.67、P03 935.69、P04 978.92、P05 937.56、P06 1101.25，均run_timeout；L1 377.88、L2 135.30、H1 141.66、H2 83.95，均completed。单任务端到端含OCR/启动，900秒是Agent树原绝对共享期限。

六图原件与OCR全部保存，6active transcript/193块、OCR succeeded6/6；结构化current仅P01。最终2sample、12个不同事件（10样品事件+2来源注记，各revision1）、3提案共12项全applied、待review提案0。两卡7/6条含3共享，不能加成13不同样品事件；1个不同P01科研计划在两视图仍pending。P02–P06只有持久转录，无相应proposal/current事件链接，第三样品尚无实体。P06到H2样品/事件/提案/转录/块五层规范hash相同；高答题分未补全知识。来源引用有效、人类/AI读视图一致。见ingestion-state-review.*、persistent_knowledge_audit.json。

输入ZIP SHA256 91d68d46b7e5acbc46fc7cdac5a35165140110f1abf1ee4c7a39b01b9ccd5fc8。只喂6原始英文JPEG P01–P06逐张，然后同会话原文L1/L2/H1/H2；标准转录/gold/图像生成提示留评测侧。独立项目 simulated_mof_english_20261029，DB regression/run/evaluation.db。不要执行prepare_run.py、launch_once.ps1、run_english_suite.py，它们拒绝既存run，不能用于恢复终态。

text/OCR均glm-5.3-flash、zai/max、16384输出、128000上下文；每任务树共享900s绝对deadline，OCR240另列、observer1500。五角色启用，实际10orchestrator+4b_capture，6OCR另列，无b_query/planner/inventory运行。产品145文件、runner、提示协议、输入固定；无付费预检、终态重投、模型补跑或业务预置。应用产品没有本轮新修复。正式runner SHA fb10644794bad0e3007fddf45de01cb963f9ba77ff51e4e5822e33823bedf0e0。

provider安全合并41意图/传输尝试（6OCR+35Pi），41终态观测含5aborted、0retry/0quota。36有usage，total_tokens已知小计1225981，另5条缺usage；完整tokens/cost均null。cache/reasoning分别为input/output子集，不重复加。6length均16384output，reasoning占90.58%–99.95%，仅配置预算压力观测，不划定模型能力/提示/编排因果。54工具尝试、2错误、0pending、0精确参数回执复用。独立Codex评测推理不在GLM统计内。

审计：harness14/14、guard离线10/10、进程加强mock7/7；14Agent/10trace实际预算7/7；评分冻结/原文435/435。最终完整性29/30：唯一fail main_health_200，主4174/8876均URLError。运行前未采健康基线，停止时间/原因未知；未为改结论而重启。主六业务表、私有模型配置哈希未变、active0。隔离58277/58278已关闭，已捕获实际Python/Node和历史父PID可能孤儿进程清理检查通过，activejobs/runs/tokens0。进程核查为时点检查非完整生命周期遥测。manifest completed/servers_stopped及freeze flags全true，completed表示流程完成而非十任务全成功。原29/30保留。

原进行中交接备份为work根handoff-before-completion.md，RUN_STATUS头部亦完成标记。评测Agent /root/english_suite_evaluator已完成；工程Agent /root/english_harness_review已完成最后公开交付复核16/16，delivery-audit.json/md已保存，未改已封存报告/包。审计器schema修正仅把agent_runs成功终态认作succeeded并加强孤儿Node覆盖，auditor-maintenance.md留证，未改产品/数据/分数。

用户边界继续保持：LLM主导、多Agent、薄代码、快速闭环/论文，不做direct/single/multi，不化学硬编码补答案，可能模型回答残差先保留。后续若用户要求修复，优先另建候选核对逐页整理预算分配/最小知识写入闭环，再冻结新验证；不要改本轮参数/终态/分数或手动补库提分。
