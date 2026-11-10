# ChemClaw 鲁棒性最小修复交接（2026-10-29，已完成）

用户要求逐项修复五个问题，已完成代码、故障测试、独立审查及主服务部署。报告docs/ROBUSTNESS_MINIMAL_REPAIR_2026-10-29_cn.md，工作根work/robustness-minimal-2026-10-29。无Git，145份原产品范围source-before备份，修复后152份；最终source hash与patch在verification.json、product-changes-final.patch。

主服务4174/8876正在运行，最终wrapper PID127460、实际Python131740，身份/端口由审查Agent核查；启动收据main-process-final.json。读取源码后启动了最后版本，不能执行restart_main.ps1或launch_main*.py重复启动。主私有模型配置保持原样；本轮没有外部GLM实测或启动付费probe。匿名Core桥401为预期鉴权，合法浏览器会话Core桥200，静态页/Core health200，served JS与最终文件一致。主六业务表/config和六份English sealed artifact hash全部未变，active0，见verification.json。

已修：900秒新任务默认120秒收尾预留（工作期780秒），动态预算提示与软边界停止新推理/写/委派，但不打断已发Core写入；硬/软期限与工具余额持久化，恢复不重开预算，legacy hard-only不追溯改变。角色提示精简并先小批create→readback→progress。失败/超时改为真实执行回执摘要，不伪造化学答案或自动completed。capture_progress默认32KiB，有truncated/total/omitted，knowledge_complete:null，旧版本与旧游标不当新证明。界面显式continue新job，原job不变、同原job仅一幂等子job，复用OCR、重新鉴权并读版本/事件；取消/未知/裁剪检查点需核查。免费health helper记录真实前后基线，不自动启动或调用模型。

审查补强：未知子树效果统一任务树围栏，父不能继续模型或写；每笔未知保留pending，支持通用业务工具及legacy settled，hard过期也优先needs_review。进度投影大项目不会让保存结果超过Core元数据上限；完整journal不裁。Core结果allowlist新增capture_progress/runtime_budget_hint，有界finiteJSON检查，progress版本1且completeness必须null。没有新表、关键词分流或化学硬编码。

全部验证：Node237/237，Core632/632，前端Python94/94，前端两份JS文件（14UI行为+12Markdown/settings断言）通过。含真实临时Core→Node SDK→本地SSE受控provider→CP→下一显式续作。独立审查final-review16/16、addendum4/4；24项针对测试+2guard单元通过，和全套重叠不要求和。真实GLM首批写入和六图知识覆盖改善仍未验证，不可把本轮离线/受控验证当新化学评分。

保留旧English测试95.7、6partial/4completed、P01-only current知识和29/30原审计，全不改、不补库、不重跑旧终态。后续用户若要模型验证，另建新隔离候选，原输入可作已知开发回归但不得称未见盲测；文本/OCR使用用户要求的glm-5.3-flash zai/max与900任务树时限，单列OCR240，记录新版本/预算/输入协议与前后健康。不要执行旧English runner，也不要将已过期旧job当成新900秒恢复。

独立审查Agent曾额度停止，root接手完成bounded与安全边界，随后恢复独立复核全部关闭；当前无必做工作。工作内main-deployment与main-deployment-final含私有DB备份，不公开打包；前端旧UI测试逻辑重建副本非原始字节，note明确。源/测试变更均在报告记录，无人为化学答案补丁。
