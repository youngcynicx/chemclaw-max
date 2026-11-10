# 事实主体与显式当前状态修复

日期：2026-10-24。承接首轮外部AI交接先导发现的数值误归属与计划状态问题。

## 事实主体与提及

新Agent条目显式填写`fields.subject_sample_labels`，作为唯一事实主体数组；读取增加`subject_sample_ids`明确这一含义。`sample_label`、`related_sample_labels`保留旧调用兼容，有canonical主体时不再覆盖它；空数组明确保留未归属，不走旧单样品匹配兜底。仅作比较、背景、引用或“另一对象无更正”说明的标签放在`fields.mentioned_sample_labels`，不参与样品关联。Pi工具schema要求新条目包含fields和canonical主体，Core旧HTTP调用继续兼容。

不同主体的专属数值由LLM拆项。共同备注不复制某个主体专属数值，而引用已有数值事件；真正共同的操作、条件和计划仍可保存一个共享事件。本地不按化学字段名、数字或关键词猜归属。

common、capture、orchestrator、query四份提示同时要求：写前确定主体，读回后逐项核对真实sample_ids、数值/状态与来源；工具成功和数量正确不能替代语义验收。

结构化交付进一步要求给出已报告测量的真实`fields`路径、该路径的保存值、单位、主体、事件版本和来源。`raw_text`含值或`state=completed`不能证明结果已结构化。缺失时由LLM补齐；有纠错权限则修改版本，只有新增提案权限则保存有来源的完整补充项，用显式`superseded`连接旧不完整记录。补充项表示知识补全，不表示又做了一次实验。本地没有添加化学字段名白名单或数值提取器。

## 计划及可演进状态

初始状态使用开放实验字段内的通用结构：

```json
{"state": {"status": "pending"}}
```

可用状态为`pending`、`in_progress`、`completed`、`cancelled`、`superseded`、`unknown`。这些状态由LLM依据原文声明，本地不把任意旧status字符串或日期自动解释成已完成。

后续完成记录在自己的fields中明确写状态变化。以下ID是文档占位符，实际必须使用已经保存并读回的真实事件及版本：

```json
{
  "subject_sample_labels": ["样品乙"],
  "sample_label": {"normalized": "样品乙", "uncertain": false},
  "mentioned_sample_labels": [{"normalized": "样品甲", "uncertain": false}],
  "state_updates": [
    {
      "target_event_id": "evt_原计划的真实ID",
      "target_revision": 1,
      "status": "completed",
      "subject_sample_labels": ["样品乙"],
      "reason": "本条来源明确报告乙完成原计划"
    },
    {
      "target_event_id": "evt_原排队记录的真实ID",
      "target_revision": 1,
      "status": "superseded",
      "subject_sample_labels": ["样品乙"],
      "reason": "乙的未测排队状态已被本次完成记录取代"
    }
  ]
}
```

本地校验目标同项目、引用版本与明确主体，保存解析后的`subject_sample_ids`；更新主体必须是新事实和目标事实共同明确的主体。共享计划仅完成一个主体时不会关闭另一主体。历史原件、原计划字段和旧排队记录保留。

## 机器读取

样品上下文、人的样品卡数据投影、样品时间线复用同一状态投影函数：

- 顶层`current_state`数组：每个目标事件、每个样品的当前状态。
- 事件`lifecycle_state`：该样品视角的状态；共享计划可在两张卡分别为completed与pending。
- `basis`：具体更新事件、版本、知识时间与证据引用。
- 上下文`relations`内`kind=state_update`：显式状态边及实际是否应用。
- 问题记录在`quality.issues`或`state_issues`，无显式状态的旧计划返回unknown。

只需当前状态时，调用`query.sample.context`并指定`include=["current_state"]`。该段也遵循预算与遗漏计数，完整历史仍可按事件版本读取。as-of只使用相应知识快照中的记录，不让未来完成记录进入旧状态。

状态是附在新证据上的断言；同一知识时刻的相互矛盾断言保留为unknown及具体依据。普通读取不启动内部模型，不修改历史，也不迁移旧业务数据。

## 验证与部署

最终Python相关回归139项通过（61.30秒）；Pi/Node此前107项通过，之后无JavaScript改动。覆盖部分完成、旧待测取代、跨主体误更新拒绝、canonical主体优先级、提及不归卡、历史版本、预算和多接口一致性。Pi新写入必须包含fields及canonical主体，Core旧调用兼容也有测试。

常用工作台4174/8876已加载最新Core，两端HTTP200；重启前无活动任务，六张主要业务表逐行摘要一致，模拟验收没有写入主库。最终验证记录在`verification/main-final-before.json`和`main-final-after.json`，最终源码/提示哈希在`verification/final-source.json`。不迁移或自动推断旧记录状态；缺少显式状态关系的历史计划继续返回unknown。

真实模型验证根目录为`work/handoff-subject-state-fix-2026-10-24/`，使用首轮相同的四份原文/任务输入，所有失败和原始分数保留：

1. 第一版单/多Agent各三阶段共六次运行完成，但单Agent仍用旧related标签误挂数值，多Agent漏取代一条旧排队状态。这两项失败推动canonical主体契约和全部待办枚举要求，不能记为验收通过。
2. `canonical-final-replay/`仅从上述两条件各自append快照，新增两次纠错阶段模型运行，前四次历史输入/结果复用。两条件的主体和计划状态检查均通过；多Agent的A/B专属测量值完整且分别归属，旧B排队记录被取代。单Agent的B主体已正确、计划状态已正确，但B测量结果只写在raw_text，缺结构化值。详见`targeted-summary.json`。
3. 单Agent另做一次通用结构化审计（52.47秒），模型未写入，仍错误判为完整。`audit-structural-readback.json`如实记失败；原turns未改。两卡与context的事件版本和当前状态一致。审计提示没有给样品标签、具体数值、评分规则或开发报告。
4. 在通用提示加入真实字段路径/读回值验收后，再做一次独立记录的通用补缺续验（70.28秒）。LLM自行识别缺值，新增一条有来源的B专属完整测量，将两条不完整旧记录显式标为superseded。保存后直接核对：A=2.40与B=2.10 mmol/g分别只属于A/B；共享计划两侧completed、旧B排队superseded；人工质量纠错38 mg和原版本保留；卡片/context的事件、字段、来源、状态完全一致。十项实际读回检查全部通过，见`fields-proof-summary.json`，完整结果见`fields-proof-readback.json`。没有给模型具体样品或数值答案，没有上传新附件，原turns未改；新增两次审计单独保存在audits[]，不能算作纠错阶段一次即成功。

这些运行是同一案例的定向缺陷验证，不能当作新独立基准或成功率提升。第二版运行期间提示/schema曾发生变化，`source-hash-postcheck.json`已记录，不能用于严格比较单/多Agent耗时；保留的四次纠错阶段加一次字段补全proposal.create真实实参均离线通过最终Pi schema（`final-pi-schema-replay-check.json`）。最后字段补全期间最终源码/提示哈希保持一致（`fields-proof-source-postcheck.json`）。`verification/code-freeze.json`只对应较早第一版，不代表最终版本。

临时案例服务在最终读回后停止；最后单Agent快照为`canonical-final-replay/single/fields-proof-v2.db`，前一次失败审计快照另留`audit-v1.db`，多Agent快照为`canonical-final-replay/multi/correction.db`。库和reader配置仅供本地恢复，不放公开论文材料包。人的界面本轮未改。
