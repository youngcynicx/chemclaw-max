# S10 验收报告：消息通道集成（§16）

日期：2026-10-14 ｜ 执行：Alice
范围：§16 通道账户/身份绑定/统一摄取账本/平台 webhook/通道投递 + 内容闸门/QQ 适配器。
新 schema **v11**（三张新表 channel_accounts / channel_identities / channel_messages，纯增量）。

## 1. 确定性测试（tests/test_s10_channels.py，15/15 绿）

| §16 验收点 | 测试 | 结果 |
|---|---|---|
| 凭据只存引用 + 角色防护 | `test_channel_register_rejects_plaintext_secret_bad_platform_and_non_owner`：明文 secret → schema_violation；未知 platform → schema_violation；member 注册 → forbidden_capability | PASS |
| 绑定必须真实主体 + 吊销重映射 | `test_identity_bind_requires_real_principal_and_revocation_remaps_to_unmapped`：绑定到不存在 principal → invalid_state/404；bind prin_lab 成功；revoke 后同作者新消息落 unmapped（审计链保留） | PASS |
| 跨重投幂等 | `test_inbound_dedupe_is_idempotent_on_redelivery`：同 (account, platform_message_id) 红投 → dedupe=duplicate，返回原行，事实计数零变化 | PASS |
| 账户命名空间分离 | `test_same_platform_message_id_across_accounts_is_namespaced`：同外部消息 ID 两个账户各自成行，互不吞并 | PASS |
| 未绑定作者归档不处理 | `test_unmapped_sender_is_archived_and_never_processed`：无绑定作者 → status=unmapped，inbound 零产生 | PASS |
| 群聊永不自动授权 | `test_group_thread_rejected_by_default_and_never_grants_scope`：默认拒绝群线程；群成员资格不授予任何项目访问 | PASS |
| 撤回保历史 | `test_recall_keeps_history_and_opens_review_task`：recall 追加新行指回原件，原行原样保留 | PASS |
| 编辑追加不改写 | `test_edit_appends_new_row_and_never_overwrites_original`：edited 新行 + edits_original_id，原件文本不变 | PASS |
| 附件原件 + 红投零重复 | `test_attachment_original_saved_with_sha256_and_redelivery_writes_no_dupes`：原件 sha256 命名落盘 + meta；红投不重写文件、不产生重复行 | PASS |
| receipt 反查回填 | `test_receipt_maps_to_delivery_by_transport_event_id`：outbox→delivery（transport_event_id 落库）→ receipt 摄取反查并回填 delivered | PASS |
| 回复原线程 + 通道列 | `test_channel_delivery_targets_original_thread_with_account_columns`：target_from_payload 覆盖目标为 reply_to_thread；delivery 行带 channel_account_id/platform_thread_id | PASS |
| 内容闸门永久 hold | `test_content_review_holds_reply_permanently_until_human_resolves`：注入指令文本 → held → failed + next_retry_at=NULL，无自动重试 | PASS |
| 故障恢复 exactly-once | `test_outage_recovery_no_loss_no_double_apply`：两次注入 drop → 平台侧恰 1 次发送、恰 1 条 delivered 行、审计链 3 行；本地知识库零波动 | PASS |
| 平台 webhook 签名 + 红投幂等 | `test_webhook_endpoint_signature_and_idempotent_redelivery`：错签名 401；未知账户 404；正确 HMAC → 202/200；重投零重复 | PASS |
| QQBot dry-run + 缺凭据永久失败 | `test_qqbot_dry_run_records_without_network_and_missing_creds_fail_permanently`：dry_run 零网络记录 qqdry-*；缺凭据 transient=False 永久失败不盲重试 | PASS |

## 2. 全量回归与基准

- **268 tests 全绿**（253 基线 + 15 新增，零回归）
- **基准 140/150 = 93.333% 精确复现**（case_accuracy 0.933333，unsafe/incorrect
  write 0%，idempotency violation 0%，trace completeness 100%，报告落
  `outputs/s10-benchmark/s10_regress.*`）
- 存量契约对齐（非放宽）：user_version 10→11、能力 60→65（全 owner）；
  三处既有断言锚点更新（test_cli_smoke / test_s02_contracts /
  test_s08_recovery 的 `== 10` → `== 11`，语义不变）

## 3. 本轮修复的实现缺口

1. **worker 复用 queued 投递时 `target` 未定义**（S06 dispatch 预建 queued
   delivery 走 else 分支，S10 重构把 target 解析移进 if 分支导致
   UnboundLocalError → 三个 S06 投递测试回归）——else 分支补齐同款解析
   （target_from_payload 语义对复用路径同样生效）；
2. **raw_json 序列化崩溃**：附件 base64 已解码为 bytes 后 json.dumps 抛
   TypeError——摄取账本改存 safe 形式（bytes → data_bytes 长度摘要），
   原件本体在磁盘 sha256 文件；
3. **receipt 反查缺列**：映射 SELECT 未取 transport_event_id → 补列；
4. **bind 参数名撞禁传字段**：`principal_id` 在 FORBIDDEN_V2_FIELDS
   （必须来自认证主体）→ 管理绑定改显式 `target_principal_id`，
   禁传检查零放宽；
5. **QQBotAdapter 延迟 import 缺口**：`_credentials`/`_access_token` 路径
   的 DeliverySendError NameError → 统一 `_delivery_send_error` 延迟构造。

## 4. 交付物

| 交付 | 位置 |
|---|---|
| schema v11（channel_accounts / channel_identities / channel_messages） | `src/chemclaw_lab/schema.py` `S10_SCHEMA_SQL` |
| 通道域模块 | `src/chemclaw_lab/channels.py`（摄取/绑定/receipt/内容闸门/适配器） |
| 5 个 owner 能力 | `capabilities.py`（registry + ROLE_CAPABILITIES + handlers） |
| 平台 webhook 端点 | `api.py` `POST /v2/channels/{account}/webhook`（HMAC 签名认证） |
| worker 通道扩展 | `deliveries.py`（target_from_payload / create_delivery_kwargs / content_gate，默认参数下 S06 语义不变） |
| 测试矩阵 | `tests/test_s10_channels.py`（15 用例） |
| 基准报告 | `outputs/s10-benchmark/s10_regress.{json,csv,md}` |

## 5. 遗留与边界

- **wecom_bot 平台**：schema 枚举与账户注册就绪，真实适配器未实现
  （S10 范围只含 QQ 适配器 + 本地 webhook 适配器；wecom 需要真实企微凭据做
  live 才能定契约）；
- **live 验收**：通道域的 live（QQ 真实机器人收发）依赖真实 QQ 机器人凭据
  （`CHEMCLAW_QQ_APP_ID`/`CHEMCLAW_QQ`），本轮未提供——dry-run 与离线
  webhook 链路已全绿，真实凭据到位后可跑 §16 live 矩阵；
- 遗留项延续：真实 50 页语料 / 3 份研究文档 / 用户侧人工试点（S03 起未变）。

## 6. 结论

S10 五条验收线（§16.1 凭据引用化、§16.3 显式绑定、§16.4 追加不改写、
§16.6 先落账本、§16.7 跨重投幂等 + §16.5 人工闸门）全部由确定性测试钉死；
268 全绿 + 基准 93.333% 零回归。代码仓库与备份归档见
`backups/repo_backup_post_s10.zip`。
