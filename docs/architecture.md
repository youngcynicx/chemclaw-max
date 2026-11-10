# Architecture

## High-level components

ChemClaw-Lab contains five major logical components:

1. **Messaging-facing layer**
2. **Orchestrator**
3. **Specialist agents**
4. **Deterministic Policy / Confirmation layer**
5. **Dual-memory backend**

## 1. Messaging-facing layer

Receives:
- text messages
- images
- screenshots
- file descriptions
- mixed chat inputs

Typical channels:
- WeChat
- WhatsApp

Its job is to preserve the original evidence and normalize it for downstream processing.

## 2. Orchestrator

The Orchestrator:
- detects intent
- decomposes mixed requests
- routes subtasks
- merges outputs
- enforces event-first logic

It should never directly mutate operational state.

## 3. Specialist agents

### Alice — Inventory & Consumables Manager
Focus:
- stock lookup
- location lookup
- usage / addition parsing
- restock suggestions

### Agent B — Experiment Memory Manager
Focus:
- project attribution
- sample attribution
- event extraction
- follow-up question generation
- weekly summary generation

### Agent C — Planning & Booking Manager
Focus:
- short-horizon plan generation
- dependency checking
- inventory-aware feasibility
- booking conflict detection
- alternative slot suggestion

## 4. Deterministic Policy / Confirmation layer

Reviews risky or uncertain actions before they are applied.

Decision labels:
- `approve`
- `pending_confirmation`
- `reject`

The v0.4 policy is local and deterministic. An LLM or gateway can propose an action but
cannot bypass this layer.

## 5. Dual-memory backend

### Structured memory
Examples:
- users
- projects
- samples
- chemicals
- consumables
- inventory transactions
- experiment events
- bookings

### Unstructured memory
Examples:
- original messages
- image references
- screenshot descriptions
- file descriptions
- generated reports

## Event-first workflow

Raw message -> persisted event proposal -> policy / confirmation -> guarded transaction -> state projection

## v0.4 implementation boundary

The current runnable vertical slice implements Alice inventory updates, read-only
Inventory QA, a local HTTP boundary, identity mapping, Excel import/projection, and an
isolated benchmark runner. The Orchestrator, Agent B, and Agent C remain specifications.
Gateway runtimes such as OpenClaw, WebChat, QQ, or WeChat are adapters around the core
rather than core dependencies.

```text
chat/runtime adapter
  -> HTTP API + identity mapping
  -> message/event/policy/audit pipeline
  -> SQLite source of truth
  -> generated Excel projection
  -> isolated benchmark execution and deterministic reports
```

## Why this matters

This architecture makes it possible to:
- preserve evidence
- reduce unsupported writes
- coordinate across workflows
- benchmark the system
