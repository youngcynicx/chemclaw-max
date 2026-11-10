# Roadmap

## Phase 0 - Project framing (completed)

- problem, architecture, schema, benchmark, and agent specifications

## Phase 1A - Minimal inventory loop (completed)

- SQLite demo environment
- rule-based inventory parser
- basic audit and transaction write
- CLI smoke tests

## Phase 1B - Reliable Alice / v0.2 (completed)

- persistent event proposals
- deterministic policy engine
- idempotent gateway contract
- confirmation/correction/rejection lifecycle
- executable event schema
- unit conversion and safety invariants
- compatible database migration
- expanded tests and CI

## Phase 2 - Messaging + Excel / v0.3 (completed)

- zero-dependency local HTTP API
- reusable gateway client and identity mapping
- explicit user/project attribution
- atomic Excel inventory import
- generated Excel projection
- authenticated remote binding

## Phase 3 - Benchmark v1 / v0.4 (completed)

- 150 Task A/B synthetic and adversarial samples
- rule + policy core baseline
- parsing, policy, unsafe-write, incorrect-write, idempotency, and traceability metrics
- reproducible JSON, CSV, and Markdown reports
- CI gold regression set

## Phase 4 - Experiment memory / v0.5 (next)

- Agent B event extraction
- attachment references and explicit vision triggers
- evidence-grounded weekly summaries

## Phase 5 - Planning and booking

- dependency and inventory checks
- booking conflict detection
- valid alternative slot generation

## Phase 6 - Research and open-source release

- experiments and case studies
- paper draft and figures
- tagged starter release
