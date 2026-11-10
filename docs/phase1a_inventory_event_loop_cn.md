# Phase 1A：库存事件最小闭环说明

> 历史说明：本阶段已完成，并在 v0.2 Reliable Alice 中升级为持久化事件、独立
> Policy、幂等消息和可恢复确认流程。当前行为以
> [`v0.2_reliable_alice.md`](v0.2_reliable_alice.md) 为准。

## 本次更新目标

本次更新把 ChemClaw-Lab 从“可初始化和查看 demo 数据”的状态，推进到第一个可运行的事件闭环：

```text
实验室消息
  -> 库存事件解析
  -> audit / confirmation 判断
  -> 通过后写入交易记录并更新库存
```

这个闭环对应 Alice（Inventory & Consumables Manager）的最小可运行版本，重点体现项目的核心原则：

- messaging-native：从自然语言实验室消息开始
- event-first：消息先被解析成结构化库存事件
- traceable：保留原始消息、audit 决策和库存交易记录
- safe writes：不确定或高风险操作不会静默写库

Alice 的完整规格见：[`agents/alice_inventory_spec.md`](../agents/alice_inventory_spec.md)。

## 新增能力

### 1. 库存消息解析

新增 `chemclaw_lab.inventory` 模块，用于解析简单库存消息，例如：

```bash
chemclaw ingest-message "Used 5 g KBr for adsorption batch"
```

当前支持的最小动作类型包括：

- `use`：消耗库存，例如 `Used 5 g KBr`
- `add`：增加库存，例如 `Added 20 g ZnBr2`
- `adjust`：调整库存，例如 `KBr left 42 g`

当前实现是 rule-based parser，目的是先跑通系统闭环。后续可以替换或扩展为 LLM / OpenClaw agent parser。

### 2. Audit 决策

解析后的库存事件会经过 audit 规则判断。

会直接通过的情况：

- 能明确匹配到一个化学品
- 有数值和单位
- 单位与库存记录一致
- 消耗量不超过当前库存

会进入 `pending_confirmation` 的情况：

- 找不到化学品
- 缺少单位
- 动作类型不明确
- 消耗量超过当前库存
- 单位与库存记录不一致

这使系统不会因为一句含糊聊天消息就直接修改库存。

### 3. 状态写入

当 audit 决策为 `approve` 时，系统会：

1. 写入 `message_logs`
2. 写入 `audit_logs`
3. 写入 `inventory_transactions`
4. 更新 `chemicals.amount`

例如 demo 数据中 KBr 初始为 `42.0 g`，执行：

```bash
chemclaw ingest-message "Used 5 g KBr for adsorption batch"
```

之后 KBr 会变为：

```text
37.0 g
```

如果消息是：

```bash
chemclaw ingest-message "Used 500 g KBr"
```

系统会返回 `pending_confirmation`，不会直接扣库存。

### 4. CLI 更新

新增命令：

```bash
chemclaw ingest-message "Used 5 g KBr for adsorption batch"
```

支持 dry-run：

```bash
chemclaw ingest-message "Used 5 g KBr" --dry-run
```

`status` 命令现在也会显示：

- `message_logs`
- `audit_logs`

## 数据库变化

新增表：

- `message_logs`：保存原始消息和初步意图
- `audit_logs`：保存 audit 决策、风险等级和需要确认的动作

扩展表：

- `inventory_transactions`
  - `message_id`
  - `confidence`
  - `confirmation_status`

同时 `init_db` 增加了轻量迁移逻辑，方便旧版本本地 demo DB 继续使用。

## 测试覆盖

本次新增测试覆盖：

- seed smoke test
- 正常库存消耗并更新库存
- 缺单位时不写库
- 未知化学品时不写库
- 超库存消耗时不写库
- 旧版 `inventory_transactions` 表迁移

验证命令：

```bash
pytest -q
```

当前结果：

```text
6 passed
```

## 当前边界

这一版仍然是最小原型，不是完整 Alice。

已实现：

- rule-based message parsing
- basic audit decision
- approved inventory write
- CLI demo loop

尚未实现：

- 中文库存消息的系统化测试
- 项目 / 用户归属解析
- 真实 LLM agent parser
- 多化学品复合消息拆分
- 单位换算
- confirmation 后续交互
- OpenClaw / chat adapter 集成

## 下一步建议

建议下一步进入 Phase 1B：

1. 建立 Task B（Inventory Update Parsing）的 benchmark v0 样例。
2. 将 parser 输出固定为可验证 JSON schema。
3. 增加中文和中英混合消息测试。
4. 支持 project/user attribution。
5. 设计 confirmation flow，让 `pending_confirmation` 可以被用户确认后继续写入。
