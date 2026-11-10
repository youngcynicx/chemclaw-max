# 修复后最终态的独立外部 AI 交接

2026-10-24。这次接续针对“主体归属、当前状态及结构化字段修复后，外部强 AI 能否直接消费研究记忆”，不重复旧首轮模型实验，不修改旧 16/16 或 24 点分数。

## 结果

一个不继承开发历史的独立 Codex 子 Agent，通过四个只读工具完成 CCX-A / CCX-B 的研究交接。9 次工具调用和 9 次 Core 请求全部成功；普通读取没有启动 ChemClaw 内部模型。其 39 项可机读字段声明、9 项逐主体状态及 basis，经保留回执逐项核查全部一致，并解析至 3 页精确转写版本和原文块。另一个独立审阅 Agent 未发现主要结论的实质错误。

它正确消费了各自专属吸附值 2.40 / 2.10 mmol/g、A 质量纠错 36→38 mg、共享计划两侧 completed、B 旧排队及不完整测量条目的 superseded；没有将这些历史条目恢复成待办。它保留产率、比表面积、重复性和误差缺口，区分授权范围的完整检索与尚未评估的语义完整性。

这说明当前隔离最终态可以支持一次有来源、可机读的外部接续；不证明泛化能力或多 Agent 整体优越。该最终态包含此前单 Agent 两次额外审计及补缺成本，不能记为原纠错阶段一次成功。

## 实际边界

- 从 `canonical-final-replay/single/fields-proof-v2.db` 只读备份到新案例库；原冻结快照哈希未变。人的主库没有用于这次模拟交接。
- 新 Agent 只获得 `external-run/system.md`、`task.md`、`tools.json` 和自主请求返回的完整工具结果；没有开发历史、源码、数据库、评分、参考答案或 reader 凭据。父 Agent 仅执行其请求并返回结果路径。
- 调用顺序由研究 Agent 选择：目录 1 次、context 2 次、精确原文 3 次、精确事件版本 3 次。任务要求研究交接和字段/状态依据，不含具体数值答案。
- 完整 trace、原始答复与机读声明均保留。完成后没有补工具或改答复。原始答复中的“人工对照”来自 AI 自述，应理解为 Agent 对照；没有人类盲评证据。
- 332.57 秒是 prepare 到 finish 的墙钟时间，包含父子调度、等待和机读文件撰写，不是纯推理耗时。外部完整模型调用数、token usage 均为 null，不写成零。
- 四工具约束由任务说明、运行器白名单和 Core 权限共同维护。本次是协作 Agent 的隔离历史/输入验证，不是对恶意进程的操作系统沙箱评测。

## 复核材料

目录：`work/postfix-independent-2026-10-24/`。

| 材料 | 内容 |
|---|---|
| `external-run/task.md`、`system.md`、`tools.json` | 实际任务及四工具契约 |
| `external-run/request-01.json` … `request-09.json` | 研究 Agent 的实际参数 |
| `external-run/trace.jsonl`、`tool-results/` | 完整请求、成功/失败和结果；本次无失败 |
| `external-run/final.md`、`claims.json` | 原始独立交接与机读字段/状态声明 |
| `external-run/summary.json` | 调用次数、观测边界及墙钟时间 |
| `claims-verification.json`、`verify_claims.py` | 声明与保留工具结果的机械匹配，非语义分数 |
| `independent-audit.json` | 独立 Agent 的语义与引用审阅，非人类盲评 |
| `view-comparison.json` | 两样品共15次事件出现：卡片/context字段、引用与状态一致 |
| `prior-final-source-check.json` | 18个交接关键文件与此前最终版本一致 |

核查没有修改产品代码或提示。外部运行期间记录的产品哈希未变。临时案例服务已通过独立 stop 文件关闭；常用 4174/8876 工作台保持运行。

数据库、reader JSON、`private-launch.json` 仅用于本地恢复，不纳入公开材料包。
