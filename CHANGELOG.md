# Changelog

## 0.4.1 - S10 repair round (schema v12)

### Fixed

- operation receipts are scoped by initiating principal + immutable project context
- cross-project corrections and report sources are refused; reports re-authorize every source at read time
- run tokens narrow (never widen) the principal's project scope; every tool is scope-checked
- approvals are human-only; agent execution of adjustments requires a consumed, content-bound grant
- live-run claims require an expired lease; terminal writes are epoch-conditional
- units are decoded from all published shapes; mass/volume/amount are never mixed in a sum
- claims now carry independent ref/field/source-support checks and degrade to needs_review/partial
- formal knowledge time is apply time, and full-text search selects the snapshot-era transcript run
- HTTP and capability attachments share one strict decoder; originals are written atomically
- production channels require signature verification; edit/recall must come from the original author
- delivery states distinguish held/failed_permanent/delivery_unknown; unknown outcomes are never auto-resent
- booking overlap uses UTC instants and requires explicit timezones
- Node discovery and smoke assertions are cross-device; schema version is 12

### Added

- `approval_grants` and `delivery_attempts` tables, plus additive v12 columns
- `chemclaw worker` for durable inbound -> Core -> outbox processing
- `tests/repair/` regressions for F01-F14 and a local L1 worker E2E
- `query.samples.list` read capability for the unified frontend
- `frontend/` workbench with a local bridge, extended read routes
  (samples/timeline/event/page/answer/resources/bookings/providers) and
  28 bridge tests
- homepage unified task session: natural-language classification + drag/drop
  uploads -> `assistant.task.process` -> b_query / alice_inventory /
  b_capture / planner, with cited answers, pending proposals and controlled
  low-risk inventory application
- settings model window: OpenAI-compatible base URL/model/key, real connection
  test, per-browser-session memory only; model handles intent parsing and reply
  wording while Core remains the business authority; deterministic fallback
- broad research-file allowlist (images, PDF/Office, CSV/JSON, CIF/PDB,
  FASTA/FASTQ/VCF, mzML/JCAMP/FCS/NetCDF/HDF5, archives and more); 20 MB per
  file, 64 MB capability body; originals preserved for non-text types
- fixed JPG uploads: Core JSON body limit raised for capability invocation,
  JPEG MIME detection/extension fallback, and idempotent preview records
- JPEG/TIFF/BMP/WebP/GIF previews via optional Pillow; PNG remains zero-dependency
- vision-model OCR: settings have a separate vision model; images are OCR'd,
  imported as immutable transcript runs and turned into pending proposals;
  `evidence.page.original` + `evidence.transcript.import` capabilities added
- evidence review workspace: recent pages with thumbnails, transcript blocks and
  proposal items; edit, confirm, mark unknown, apply, re-OCR; new
  `evidence.pages.list` / `evidence.page.preview` / `memory.proposal.item.update`
  and bridge review routes
- one-click “confirm all and save”; `auto_confirm_safe` auto-confirms only
  quantitative items with a valid unit and a unique sample link; missing units,
  ambiguous samples and explicit unknown still require human review
- LLM-guided candidate extraction: noisy OCR lines are merged by the text model
  into a small set of candidate facts; UI/screenshot text with no lab facts
  produces no proposal instead of dozens of confirmations
- `scripts/frontend_core_smoke.py`, `scripts/frontend_browser_smoke.py`,
  `scripts/seed_frontend_demo.py` and `scripts/bootstrap_frontend_identity.py`

### Verified

- 337 tests passed (268 original + 69 repair)
- benchmark 140/150 = 93.333%, unsafe/incorrect/idempotency violations 0%
- Pi mock smoke and `live_test_s06/s08/s09 --mock` passed with bundled Node
- real `glm-5.3-flash` live tests s06/s08/s09 passed (capture/confirm/query,
  cross-domain delivery faults, crash/lease recovery, model outage retry,
  planner conflict, MCP 67 tools/idempotent replay)
- frontend bridge integration: 12 routes ok, read-only counts unchanged
- frontend bridge tests: 28 passed
- Chrome headless + CDP actually connected the page to Core and rendered inventory

### Not verified

- real paper-photo OCR/original-byte UI, real QQ/WeCom accounts, and a single
  resident worker driving a real model end-to-end; see `docs/repair/G2_G4_status_cn.md`

## 0.4.0 - ChemLabOps-Bench v1

### Added

- 150 independently executable Task A/B benchmark cases
- strict zero-dependency benchmark case validation and path confinement
- isolated per-case SQLite execution and deterministic result normalization
- JSON, CSV, and Markdown benchmark reports
- read-only Inventory QA for amount, location, availability, low-stock, and
  sufficiency questions
- `ask-inventory` and `benchmark-run` CLI commands
- authenticated HTTP inventory-question route and gateway client method
- CI-enforced 25-case gold regression set
- adversarial coverage for negation, future intent, questions, conditions,
  ranges, inequalities, idempotency, attribution, and injection

### Changed

- uncertain or non-completed action language is now held for confirmation
- ambiguous ranges and inequalities no longer produce silent inventory writes
- scientific notation, leading decimals, and locale decimal commas are normalized safely
- Inventory QA opens SQLite in read-only mode and never initializes or migrates it
- benchmark cases require task-specific, non-empty expected-result contracts
- unsafe-write scoring includes applied transactions with incorrect material fields
- project status and roadmap now report benchmarked rather than demo-only behavior

### Baseline

- 93.3% case accuracy and 95.3% field accuracy over 150 cases
- 95.9% policy-decision accuracy
- 0% unsafe-write, incorrect-write, and idempotency-violation rates
- 100% trace completeness and Task A read-only compliance

## 0.3.0 - Messaging + Excel

### Added

- zero-dependency local HTTP API for health, inventory messages, confirmation,
  inventory queries, identity mapping, and Excel export
- authenticated remote binding with API keys read from the environment
- gateway identity mapping from external users to ChemClaw lab users
- standard-library gateway client for OpenClaw, WebChat, QQ, and integration tests
- atomic `.xlsx` inventory import with content-hash idempotency
- generated Excel projection with inventory, transactions, events, and usage notes
- opening-balance events for initial inventory migration
- explicit user and project attribution at the message boundary
- v0.3 database migration, integration tests, and Python 3.10 compatibility checks

### Changed

- SQLite remains the operational source of truth; Excel is an explicit import/export
  boundary rather than live state
- administrative inventory imports now pass amount changes through the same
  event, policy, audit, and transaction pipeline

## 0.2.0 — Reliable Alice

### Added

- persistent inventory event proposals with explicit lifecycle states
- immutable confirmation revisions and rejection flow
- gateway message idempotency via `(source, source_message_id)`
- independent deterministic inventory policy engine
- magnitude, hazard, unit compatibility, and negative-stock controls
- mass and volume unit normalization/conversion, including common Chinese units
- executable, packaged JSON Schema for inventory events
- schema version tracking and compatible migration from the v0.1 database
- `list-events` and `confirm-event` CLI commands
- GitHub Actions test matrix

### Changed

- inventory events now persist before any operational write
- audit records and inventory transactions link to their event proposal
- repeated demo seeding no longer resets live inventory state
- OpenClaw is treated as an optional gateway/runtime adapter rather than a core dependency
