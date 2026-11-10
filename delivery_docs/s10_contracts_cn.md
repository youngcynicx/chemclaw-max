# S10 契约演进说明（消息通道集成：注册、绑定、摄取、投递、QQ 适配器）

S10（计划 §16）新增 schema v11 三张表、5 个 owner 能力、平台签名 webhook 端点、
通道适配器层与投递 worker 的通道扩展参数。全部为**纯增量**：既有能力的角色、
幂等与 scope 语义零改动（`channel.*` 一律 owner-only，member/viewer 面不变）。

## 1. channel_accounts：凭据只存引用，永不存明文（§16.1）

**表**：`channel_accounts`（channel_account_id PK, platform∈{webhook_test, qq_bot,
wecom_bot, local}, display_name, credential_ref, verify_secret_ref,
allow_group_threads 默认 0, enabled 默认 1, created_by）。

**契约**：
- `credential_ref`/`verify_secret_ref` 是**环境变量或 secret-store 的名字**
  （如 `CHEMCLAW_QQ_APP_ID`/`CHEMCLAW_QQ`），能力层与适配器运行时才解析——
  数据库里没有任何平台密钥明文（`channel.account.register` 入参即收引用名）；
- `channel.account.register` 仅 owner；disabled 账户的 webhook 摄取直接
  `invalid_state`/409 拒绝（`ingest_channel_event` 首检）。

## 2. channel_identities：显式绑定 + 吊销重映射（§16.3）

**表**：`channel_identities`（binding_id PK, (channel_account_id, platform_user_id)
UNIQUE, principal_id FK→principals, display_name, bound_by, bound_at,
revoked_at 可空）。

**语义**：
- 平台作者 → principal 的映射**只能由管理员显式绑定**，绑定时目标 principal
  必须真实存在（不存在 → `invalid_state`/404）；入参用
  `target_principal_id`（`principal_id` 是 v2 禁传字段
  `FORBIDDEN_V2_FIELDS = (confirmed_by, user_id, principal_id)`，必须来自
  认证主体——管理能力改用显式目标字段名，禁传检查本身零放宽）；
- 同一 (account, platform_user) 二次 bind → `conflict_detected`/409（幂等
  不适用：绑定是管理事实不是业务事务，重复绑定必为操作错误）；
- **吊销即重映射失效**：`channel.identity.revoke` 置 revoked_at 后，同
  platform_user 的新消息不再解析到旧 principal（查询按 revoked_at IS NULL
  过滤）——吊销不是删除，审计链保留全历史；
- **群聊永不自动授权**（§16.3）：allow_group_threads=0 的账户，群线程消息
  落 ledger 状态 `unmapped`，不产生任何任务/实验事实；allow_group_threads=1
  也只允许线程级映射（identity 显式绑定到线程发言者），群成员资格本身
  不授予项目访问。

## 3. channel_messages：原件先落账本，红投零重复（§16.6/§16.7）

**表**：`channel_messages`（channel_message_id PK, (channel_account_id,
platform_message_id) UNIQUE, kind∈{message, edited, recalled, receipt},
status∈{received, deduplicated, unmapped, rejected, processed, recalled},
raw_json, attachments_json, recall_of_message_id, edits_original_id,
inbound_id, error_code, received_at, processed_at）。

**摄取契约**（`channels.ingest_channel_event`，同事务完成）：
1. **先落原件再处理**：raw_json（safe 形式：附件 bytes 摘要为 `data_bytes`
   大小，原件在磁盘 sha256 命名文件）与原文、几何、元数据在同一事务写入，
   后续任何处理失败都不丢外部事件；
2. **跨重投幂等**：UNIQUE(account, platform_message_id) 兜底 + 先查后写；
   红投直接 ACK 返回原行（`dedupe=duplicate`），**不产生第二条事实**；
   同一外部事件在两个 channel account 下 namespace 分离（§16.7）；
3. **附件原件**：base64 解码后写入 `<storage_root>/channel_attachments/
   <account>/<sha256>.bin` + meta（name/mime/bytes），红投不重写文件、
   不产生重复行（`test_attachment_original_saved_with_sha256_and_redelivery_
   writes_no_dupes`）；
4. **撤回/编辑是追加不是改写**（§16.4）：recall/edited 事件新行 +
   recall_of_message_id/edits_original_id 指回原件，状态机单向，
   已审计业务历史永不重写；
5. **receipt 反查**：kind=receipt 且携带 receipt_for=transport_event_id →
   在 deliveries 里按 (transport_event_id, channel_account_id) 反查投递并
   回填状态（delivered/failed），返回映射结果供调用方审计；
6. **未绑定作者**：platform_user_id 无活跃绑定 → 行落 `unmapped` 状态，
   不生成 inbound_event，不触发任何领域处理。

## 4. 平台 webhook 端点：平台签名认证，与 API key 隔离（§16.2）

**端点**：`POST /v2/channels/{channel_account_id}/webhook`。

**认证契约**：平台调用方**没有我们的 API key**——此端点只认通道账户的
HMAC 签名（`X-ChemClaw-Signature`，密钥取 verify_secret_ref 指向的环境
变量，`sha256 HMAC(raw_body)` 常数时间比较）。因此：
- 无签名/错签名 → `401 unauthorized`（不泄露账户是否存在：先验签名再查
  账户，错误信息同文案）；
- 未知 account_id → 404；
- 签名通过后走统一 envelope：**202 Accepted**（事件已入 ledger，后续处理
  异步/由 worker 跟进）或 **200**（同步处理完成的查询型事件）；
- 载荷大小限制 MAX_WEBHOOK_BODY_BYTES，防恶意大包。

## 5. 出站投递：worker 通道参数 + 内容闸门（§16.5）

**worker 扩展**（`deliveries.process_outbox_once` 新增可选参数，默认值下
S06 语义逐字节不变）：
- `target_from_payload`：payload 键名 → 出站目标覆盖默认 aggregate_id
  （回复原平台线程用 `reply_to_thread`）；
- `create_delivery_kwargs`：通道列（channel_account_id / platform_thread_id）
  注入投递行；
- `content_gate`：callable(payload) → {"held": bool, "reason": …}，
  **send 之前**执行；held → 投递 `failed` + `next_retry_at=NULL`
  （**永久 hold，无自动重试**），消息必须由人工处置——闸门永不静默丢弃
  也不无限重试。默认闸门
  `channels.payload_content_gate(payload)`（对 outgoing text 跑
  `review_outbound_content` 正则组：注入指令/危险指令模式 → held）；
- `channels.review_outbound_content(text)` 供适配器直连使用（QQBotAdapter
  真实发送路径 send 前强制过闸）。

**重试语义零回归**：transient 失败按 backoff 重投；每次重试保留独立
failed 行作为审计链，平台侧 exactly-once（`test_outage_recovery_...`
断言 sent==1 + delivered 行==1 + 审计链 3 行）。

## 6. QQBotAdapter：dry-run 隔离 + 凭据缺失永久失败（§16.8）

- `dry_run=True`（默认）：**零网络**——send 只记录内存 sent 列表并返回
  `qqdry-NNNNNN`；凭据不需要存在；
- `dry_run=False`：凭据从 `{CREDENTIAL_REF}_APP_ID`/`{CREDENTIAL_REF}`
  解析；缺失 → `DeliverySendError(transient=False)` **永久失败**——
  无凭据盲重试被明确禁止，等人工配好凭据再投；
- 真实路径：token 申请 → `POST /v2/users/@me/messages`，HTTP 5xx/429 →
  transient，4xx → permanent，URLError → transient；发送前强制
  content review（held → permanent 失败）；
- 适配器实现 `DeliveryAdapter` Protocol（S06 契约），EchoAdapter/
  FaultInjectionAdapter/WebhookTestAdapter/QQBotAdapter 同一接口，
  S08 故障矩阵语义原样覆盖通道适配器。

## 7. 存量契约对齐（非放宽）

| 项 | 变化 | 说明 |
|---|---|---|
| user_version | 10 → **11** | 三张 channel 表纯增量迁移 |
| 能力总数 | 60 → **65** | +5 个 channel.*（全 owner） |
| viewer/member 能力集 | 不变 | channel 面不进 member/viewer |
| FORBIDDEN_V2_FIELDS | 不变 | bind 用 target_principal_id 显式目标字段 |
| deliveries 行语义 | 不变 | 每次投递尝试一行（审计链），通道列可空 |
| outbox/幂等三层 | 不变 | 通道回复沿用 S02-S06 语义 |

## 8. 测试矩阵（tests/test_s10_channels.py，15 用例）

注册/禁用摄取拒绝 → 绑定真实 principal + 吊销重映射 → 群聊不自动授权 →
附件 sha256 原件 + 红投零重复 → 原件先落账本 → receipt 反查回填 →
撤回追加不改写 → 回复投递 target_from_payload → 内容闸门永久 hold →
故障注入 exactly-once + 审计链 → 重启无丢失 → QQBot dry-run 零网络 +
缺凭据永久失败 → webhook 端点 401/404/202 签名链 → ledger 审计读。
