# Schema Specification (Condensed)

This file summarizes the operational schema for ChemClaw-Lab.

## Core entities

- `users`
- `projects`
- `samples`
- `chemicals`
- `consumables`
- `inventory_transactions`
- `experiment_events`
- `attachments`
- `instruments`
- `bookings`
- `purchase_requests`
- `message_logs`
- `event_proposals`
- `audit_logs`
- `gateway_identities`
- `import_batches`

The list above describes the target domain. In v0.3, `samples`, `consumables`,
`attachments`, and `purchase_requests` are still planned.

## Key principles

### Event-first
Messages must first become structured events.

### Raw evidence preserved
Messages and attachments remain stored as evidence.

### State is derived from approved events
Inventory and booking state should not be directly overwritten from casual chat.

In v0.3, `chemicals.amount` is a materialized projection. Each change is linked to a
persisted event and inventory transaction.

## Core tables

### `users`
Lab members and roles.

### `projects`
Research projects and subprojects.

### `samples`
Lab-generated samples linked to projects.

### `chemicals`
Chemical inventory master records.

Suggested fields:
- names
- aliases
- CAS
- formula
- purity
- supplier
- location
- current amount
- threshold

### `consumables`
Non-chemical expendables such as separators, gloves, filters, vials.

### `inventory_transactions`
All stock-changing actions.

Important fields:
- item type
- item id
- transaction type
- amount delta
- unit
- project link
- confidence
- confirmation status

### `event_proposals`

Stores the complete schema-validated proposal, lifecycle status, policy version,
confirmation identity, and superseded-event link.

Lifecycle states:

- `proposed`
- `pending_confirmation`
- `approved`
- `rejected`
- `applied`
- `superseded`

### `experiment_events`
Structured experiment memory records.

Typical event families:
- sample_preparation
- reaction
- characterization
- electrochem_test
- observation
- result
- issue
- failure
- decision
- next_step

### `bookings`
Internal instrument reservations.

### `audit_logs`
Trace of important actions and approval decisions.

### `gateway_identities`

Maps `(source, external_user_id)` to a known ChemClaw `user_id`. This keeps platform
identity concerns at the gateway boundary while preserving actor attribution.

### `import_batches`

Records the source path, SHA-256 content hash, import mode, actor, row count, and
generated event count for each Excel import. The unique content hash prevents replaying
the same workbook.

## Implemented inventory-event payload

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
  "confidence": 0.92,
  "requires_confirmation": false,
  "raw_text": "I used 5 g KBr today",
  "project_id": "proj_001",
  "user_id": "user_001"
}
```

`event_id`, lifecycle status, schema version, policy version, and confirmation metadata
are stored in `event_proposals` around this immutable payload.

## Main event families

### Inventory events
- use
- add
- adjust
- opening_balance
- discard
- expire

### Experiment events
- observation
- characterization
- issue
- result
- next_step

### Planning / booking events
- booking_request
- conflict_detection
- planning_result

## Implementation note

The executable SQLite definition is in `src/chemclaw_lab/schema.py`. The canonical
inventory-event JSON Schema is in
`src/chemclaw_lab/schemas/inventory_event.schema.json`.

The v0.4 benchmark case contract is intentionally file-based rather than a new
operational table. Its canonical JSON Schema is in
`src/chemclaw_lab/schemas/benchmark_case.schema.json`. Benchmark runs create disposable
SQLite databases and never write evaluation state into the production database.
