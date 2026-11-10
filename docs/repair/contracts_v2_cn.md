# Repair contracts v2（实际实现，schema v12）

本文件冻结本轮修复实际采用的契约。字段名沿用现有代码，语义按审查指南 R01–R10 收紧。

## 1. 执行主体与范围

- 人类 HTTP/API 调用：`PrincipalContext(principal_id, kind, memberships)` 由已认证凭据构造。
- Agent run：`principal_from_token()` 返回**委托人当前 memberships ∩ token 冻结 project_ids**。空交集 = 空范围，绝不解释为全部。
- 所有工具统一检查 `project_id` / `project_ids`；Core handler 再按对象归属复查。
- 审批类能力要求 `context.kind ∈ {human, user}`；`agent`/`service` 返回 `human_approval_required`。

## 2. 操作回执与幂等

存储键由服务端派生：

```text
opscope = sha256(
  principal_id
  + client_namespace
  + sorted(project context from params)
)
operation_results key = (opscope, operation_id)
```

- 同 key 同业务内容：返回原 envelope + `idempotent_replay=true`；
- 同 key 不同内容：`idempotency_conflict`；
- 回执读取前重新检查当前 principal 与项目权限；
- 库存消息另用稳定业务键
  `op:<principal>:<project>:<operation_id>` 写入 `message_logs.source_message_id`，
  用于“业务已提交、回执未落库”窗口的重放；
- 新的真实取用必须使用新的 operation_id，不能仅凭相同参数吞掉。

## 3. 批准与执行

```text
approval_grants
  approval_id / proposal_type / proposal_id / project_id
  allowed_action / content_digest
  approved_by / approval_source(human|policy)
  status(active|consumed|expired|revoked)
  expires_at / consumed_by_operation_id
```

- `inventory.approval.grant` 只能由人类调用；
- Agent 执行 `inventory.adjustment.apply` 时必须提交 `approval_id`；
- Grant 校验提案类型、ID、项目、动作、有效期与**服务端重算的内容摘要**；
- Grant 在与业务写入相同的事务中消费；内容变化返回 `approval_content_changed`；
- 人类直接执行调整仍兼容，但审计记录 `approval_source=human`。

## 4. 量值

- `Quantity` 解码兼容：`amount.normalized + amount.unit`、`fields.unit={raw,normalized}`、旧标量；
- 冲突/缺失单位不猜，返回缺单位记录；
- 支持量纲：mass (µg/mg/g/kg)、volume (µL/mL/L)、amount_of_substance (mmol/mol)；
- 合计分组键：`sample_id + material identity + dimension + usage=实际取用`；
- 计划/配方/测量/否定/计算值不进入实际消耗合计；
- 每条派生 claim 保存 `inputs`、`conversion_inputs`、`conversion_rule`、`unit_rule_version=1`。

## 5. 知识时间

- `proposal_created_at` ≠ 正式知识时间；
- `apply_proposal` 写入 `knowledge_as_of = CURRENT_TIMESTAMP`（同事务）；
- `event_time_start` 仍是来源报告的发生时间；
- `knowledge_as_of` 查询按时间点选择当时有效的 revision；
- 全文检索先选该 page 在 `as_of` 当时最近的 succeeded run，再取其 blocks；
- 旧 run/旧块保留可读，不会贴在当前 revision 上。

## 6. Claim 验证

每条 claim 分开保存：

```text
verify.ref_opens       引用块存在且可打开
verify.field_matches   claim 与结构化字段一致
verify.source_supports 绑定来源确实支持该值/单位/状态
verify.support_kind    source_text | human_report | derived_conversion | ...
verify.value_matches   仅当 ref_opens 且 source_supports 为 true
```

- 结构化字段复制只证明 `field_matches`；
- 文本中出现相同数字不足以证明支持；
- 人工更正陈述可作为 `human_report` 支持，但必须明确包含更正值；
- 失败 claim 进入 `needs_review/partial`，渲染标记“待核对”。

## 7. 附件与通道

- HTTP 与 capability 都调用 `channels.normalize_attachments()`；
- 显式区分 `data`/`data_b64` 与 `platform_reference`；
- 空字节、非法 base64、超限、MIME 冲突均拒绝；
- 原件写入同文件系统暂存区，fsync 后原子 rename，sha256 命名；
- 生产平台（`qq_bot`/`wecom_bot`）缺 `verify_secret_ref` 直接拒绝；
- edit/recall 必须由原作者发出，否则 `author_mismatch`，victim 原文不改。

## 8. 投递状态

一条 outbox = 一条逻辑 delivery；每次真实网络发送 = `delivery_attempts` 一行。

```text
queued -> sending -> delivered | accepted
                  -> retry_wait -> sending
                  -> delivery_unknown
                  -> failed_permanent
queued -> held
```

- `sending` 在网络调用前提交；
- 发送后超时/连接中断/崩溃恢复 -> `delivery_unknown`，禁止自动重发；
- `held` / `failed_permanent` 是显式终态；
- receipt 按平台消息 ID 反查；迟到失败 receipt 不覆盖 delivered，转 `receipt_conflict`；
- `accepted` ≠ 用户已读；无平台送达回执时不得声称 delivered。

## 9. 预约时间

- 输入必须带时区（`+08:00` 或 `Z`），否则 `timezone_required`；
- 存储同时保留原始文本与 UTC 微秒 instant；
- 半开区间：`existing_start_utc < new_end_utc AND existing_end_utc > new_start_utc`；
- 相邻允许，等价时区冲突；
- 无 UTC 的旧 confirmed 预约会返回 `legacy_time_ambiguous`，不猜；
- `capacity != 1` 明确 `not_supported`。

## 10. 统一任务会话

`assistant.task.process` 是网页首页唯一受控写入入口。默认使用确定性规则；
浏览器会话可显式配置 OpenAI-compatible 真实模型，模型只负责意图解析和回复措辞，
业务执行/权限/来源校验仍由 Core 完成。模型配置只存本地桥内存，附件字节不发送给模型；
模型失败时回退到 Core 文本并加 `model_reply_failed`。真实模型 Pi 运行仍是独立验收脚本。

```text
project_id, text, attachments[{name,mime,data_b64}], session_id?
  -> 意图分类
  -> query_answer  -> query.answer.build
  -> record_inventory -> inventory.message.process（仍受 policy）
  -> capture_page -> evidence.document.create/page.ingest
                    -> text_input transcribe -> memory.proposal.create
  -> plan_experiment -> resource.list + resource.booking.status（只读）
  -> clarify
```

- 需要 member/owner；不允许 viewer 通过任务框写业务；
- 附件最多 5 个、每个解码后 ≤8 MB；严格 base64；
- 文本页自动转写并生成 `awaiting_confirmation` 提案；图片/PDF 只保存原件，
  返回 `ocr_not_available`，不伪造识别；
- 预约请求不直接创建/确认预约；
- user/reply 以 `typed_messages` 持久化，`correlation_id=session_id`；
- 返回 `intent / agent / status / text / data / object_refs`，前端按状态展示。

## 11. 兼容与迁移

- schema v11 -> v12 为加法迁移，不重写业务事实；
- 旧 delivery 的 `status` 保留，`state` 由迁移按 `status/next_retry_at` 回填；
- 旧报告补 `source_manifest_json` / `source_project_ids_json`，读取时重授权；
- 旧预约只有带时区值才迁移 UTC，naive 值标 `legacy_unknown`；
- 旧 operation 回执不参与新 key 复用；新 key 由 principal + 项目上下文派生。
