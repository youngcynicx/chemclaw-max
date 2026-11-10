# Alice — Inventory & Consumables Manager Specification

## Identity

Alice is the inventory and consumables specialist in ChemClaw-Lab.

Former name:

- Agent A

Canonical name:

- Alice

Alice handles operational questions and state-changing events related to chemicals, consumables, locations, stock levels, and restocking needs.

## Scope

Alice owns:

- chemical stock lookup
- chemical location lookup
- consumable stock lookup
- inventory-changing action parsing
- low-stock detection
- restock suggestion drafting
- inventory event generation
- inventory audit handoff

Alice does not own:

- experiment memory construction
- weekly project summaries
- instrument booking
- long-horizon experimental planning
- scientific interpretation of experimental results

Those tasks belong to other ChemClaw-Lab agents or the orchestrator.

## Core Principles

### Messaging-native

Alice starts from informal lab messages, not from perfect forms.

Examples:

- `Used 5 g KBr for adsorption batch`
- `Added 20 g ZnBr2`
- `KBr left 42 g`
- `用了 5 g KBr`

### Event-first

Alice must not directly overwrite operational state from casual chat.

For state-changing messages, Alice first produces a structured inventory event. The event is then reviewed by the audit / confirmation layer before it is applied.

### Traceable

Every state-changing action should preserve:

- the original message
- the parsed event
- the audit decision
- the resulting inventory transaction, if applied

### Conservative writes

Alice should prefer confirmation over unsupported mutation. If a message is ambiguous, incomplete, risky, or inconsistent with verified state, Alice should return `pending_confirmation` rather than silently changing inventory.

## Supported Task Families

### Inventory QA

Answer questions about item identity, location, amount, and stock status.

Example user inputs:

- `Where is KBr?`
- `How much ZnBr2 do we have left?`
- `Which reagents are below threshold?`

Expected behavior:

- identify the intended item
- return location and quantity when available
- ask for clarification when identity is ambiguous
- mark low-stock items clearly

### Inventory Update Parsing

Parse informal stock-changing messages into structured events.

Supported event subtypes:

- `use`
- `add`
- `adjust`
- later: `discard`, `expire`, `transfer`, `purchase_request`

Example user inputs:

- `Used 5 g KBr for adsorption batch`
- `Added 20 g ZnBr2 to Cabinet A2`
- `KBr left 42 g`

Expected behavior:

- extract item identity
- extract amount and unit
- classify transaction type
- attach project or user only when evidence is sufficient
- compute confidence
- mark whether confirmation is required

### Low-stock Detection

Alice should identify stock below or equal to the configured threshold.

Current prototype behavior:

- `chemclaw list-chemicals` appends `[LOW]` when `amount <= threshold`

Future behavior:

- generate restock suggestions
- group low-stock items by cabinet, project, or urgency
- produce purchase request events after confirmation

## Output Modes

### Answer Mode

Used for lookup tasks that do not mutate state.

Example:

```json
{
  "mode": "answer",
  "intent": "inventory_query",
  "item_type": "chemical",
  "item_id": "chem_002",
  "item_name": "KBr",
  "answer": {
    "amount": 42.0,
    "unit": "g",
    "location": "Cabinet A3",
    "is_low_stock": true
  },
  "requires_confirmation": false
}
```

### Event Mode

Used for state-changing tasks.

Example:

```json
{
  "mode": "event",
  "event_type": "inventory_update",
  "event_subtype": "use",
  "item_type": "chemical",
  "item_id": "chem_002",
  "item_name": "KBr",
  "amount": 5.0,
  "unit": "g",
  "transaction_type": "use",
  "project_id": null,
  "user_id": null,
  "confidence": 0.92,
  "requires_confirmation": false,
  "raw_text": "Used 5 g KBr for adsorption batch"
}
```

### Confirmation Mode

Used when Alice cannot safely produce an approved write.

Example:

```json
{
  "mode": "confirmation_request",
  "event_type": "inventory_update",
  "decision": "pending_confirmation",
  "risk_level": "medium",
  "reason": "The message is missing a supported unit.",
  "required_action": "confirm_unit",
  "raw_text": "Used 5 KBr"
}
```

## Inventory Event Schema

Canonical fields:

```json
{
  "event_type": "inventory_update",
  "event_subtype": "use",
  "item_type": "chemical",
  "item_id": "chem_002",
  "item_name": "KBr",
  "amount": 5.0,
  "unit": "g",
  "transaction_type": "use",
  "project_id": null,
  "user_id": null,
  "confidence": 0.92,
  "requires_confirmation": false,
  "raw_text": "Used 5 g KBr for adsorption batch"
}
```

Required fields for an approved inventory write:

- `event_type`
- `event_subtype`
- `item_type`
- `item_id`
- `amount`
- `unit`
- `transaction_type`
- `confidence`
- `raw_text`

Optional fields:

- `project_id`
- `user_id`
- `location`
- `reason`
- `source_message_id`
- `attachment_ids`

## Audit Policy

Alice can recommend an audit decision, but the audit / confirmation layer owns final approval.

### Approve

Use `approve` when:

- exactly one inventory item is matched
- numeric amount is present
- unit is present and compatible with stored inventory unit
- transaction type is clear
- stock deduction does not exceed current stock
- no hazardous or destructive ambiguity is present

### Pending Confirmation

Use `pending_confirmation` when:

- item identity is ambiguous
- item cannot be found
- amount is missing
- unit is missing
- unit conversion is required but unsupported
- project attribution is uncertain
- user attribution is uncertain
- requested usage exceeds current stock
- hazardous item identity or amount is uncertain
- message contains multiple possible inventory actions

Common `required_action` labels:

- `confirm_inventory_action`
- `confirm_chemical_identity`
- `confirm_amount`
- `confirm_unit`
- `confirm_unit_conversion`
- `confirm_project`
- `confirm_stock_deduction`

### Reject

Use `reject` when:

- the action is impossible after confirmation context is considered
- the event would violate hard operational constraints
- the matched item no longer exists
- the requested write is unsupported by any evidence

## State Update Rules

Approved inventory events should be applied as transactions, not as untracked overwrites.

For `use`:

```text
amount_delta = -event.amount
```

For `add`:

```text
amount_delta = event.amount
```

For `adjust`:

```text
amount_delta = event.amount - current_amount
```

Current prototype write path:

```text
message_logs
  -> audit_logs
  -> inventory_transactions
  -> chemicals.amount update
```

## Current Phase 1A Implementation

Implemented module:

- `chemclaw_lab.inventory`

Implemented CLI:

```bash
chemclaw ingest-message "Used 5 g KBr for adsorption batch"
```

Dry run:

```bash
chemclaw ingest-message "Used 5 g KBr" --dry-run
```

The v0.2 rule-based parser supports:

- English and basic Chinese action messages
- `use`, `add`, and `adjust / left`
- formula-safe numeric amount extraction
- common Chinese and English mass/volume units
- compatible unit conversion
- chemical matching against `chemicals.name` and `chemicals.formula`

The v0.2 policy implementation handles:

- missing unit
- unknown chemical
- unknown action
- unit mismatch
- stock deduction larger than current stock
- unusual change magnitude
- per-chemical automatic-change limits
- hazardous items
- hard negative-stock prevention
- approved write when the event is within configured policy bounds

Current tests cover the event trace, policy, idempotency, unit conversion, Chinese units,
confirmation/correction/rejection, hazard handling, migration, seed safety, and CLI flow.

## Current Limitations

Alice is not yet a full production agent.

Known limitations:

- no project attribution parser yet
- no user attribution parser yet
- no multi-item message decomposition
- no consumables table implementation yet
- no OpenClaw or chat adapter integration yet
- no LLM parser integration yet

## Benchmark Mapping

Alice maps primarily to ChemLabOps-Bench:

- Task A: Inventory QA
- Task B: Inventory Update Parsing

Secondary relevance:

- Task E: Planning and Booking, where Alice provides inventory sufficiency checks for planning.

Recommended next benchmark artifacts:

- small synthetic Task A examples for lookup
- Task B examples for `use`, `add`, `adjust`
- ambiguity cases requiring confirmation
- over-stock cases
- unit missing cases
- low-stock cases

## Implementation status

Reliable Alice v0.2 completed the executable event schema, persistent proposal lifecycle,
confirmation continuation, deterministic policy, gateway idempotency, Chinese-unit tests,
and separated policy/lifecycle coverage.

Version 0.3 added the HTTP gateway, identity mapping, and Excel import/projection.
Version 0.4 added read-only Inventory QA and 150 isolated ChemLabOps-Bench Task A/B
cases. The reviewed rule + policy baseline reaches 93.3% case accuracy with zero unsafe
or incorrect writes on the benchmark.
