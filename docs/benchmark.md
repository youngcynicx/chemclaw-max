# ChemLabOps-Bench

ChemLabOps-Bench is the reproducible evaluation suite for messaging-native laboratory
operation assistants.

## v0.4 implemented scope

The first executable release focuses on:

- Task A: read-only inventory questions;
- Task B: inventory-update parsing, policy, writes, idempotency, and traceability.

The catalog contains 150 cases: 50 Task A and 100 Task B. Every case initializes a
fresh SQLite database from a declared fixture, so execution order cannot affect scores.

## Case contract

Each JSONL case contains:

- `case_id`
- `task_type`
- `fixture`
- `input`
- `expected`
- `tags`
- `annotation_notes`

The executable schema is
`src/chemclaw_lab/schemas/benchmark_case.schema.json`.

Task A expectations must include `answer_type`. Task B expectations must include
`write_count` and either a zero-write error contract or the material event fields,
policy decision, status, and final inventory amount. Empty or safety-incomplete
expectations are rejected rather than scored as correct.

## Dataset layers

The 25-case gold set runs in CI and must remain at 100%.

The challenge set intentionally includes current capability gaps and adversarial
language. It must not be weakened merely to produce a perfect score.

## Metrics

- case accuracy
- expected-field accuracy
- policy-decision accuracy
- unsafe-write rate
- incorrect-write rate
- idempotency-violation rate
- trace-completeness rate
- Task A read-only violation rate
- per-task and per-tag accuracy

An unsafe write is either a transaction produced when the gold contract requires the
operation to be held, or an applied transaction whose material item, amount, unit,
transaction type, write count, or final inventory value differs from gold. The
incorrect-write rate reports the latter category separately. Both targets are zero.

## v0.4 rule + policy baseline

| Metric | Value |
| --- | ---: |
| Total cases | 150 |
| Case accuracy | 93.3% |
| Field accuracy | 95.3% |
| Policy-decision accuracy | 95.9% |
| Unsafe-write rate | 0.0% |
| Incorrect-write rate | 0.0% |
| Idempotency violation rate | 0.0% |
| Trace completeness rate | 100.0% |
| Task A read-only violation rate | 0.0% |

The remaining failures measure aliases, word numbers, discard/expire events, project
extraction, informal low-stock language, and implicit sufficiency questions. Negation,
plans, questions, conditional actions, ranges, inequalities, and unsupported multi-value
numeric forms no longer write silently.

## Reproduction

Run the full suite:

```bash
chemclaw benchmark-run benchmark/cases \
  --output-dir outputs/v0.4-benchmark \
  --stem v0.4_rule_policy_baseline
```

Run the gold regression set:

```bash
chemclaw benchmark-run \
  benchmark/cases/task_a_gold_v04.jsonl \
  benchmark/cases/task_b_gold_v04.jsonl \
  --require-all
```

Reports are deterministic and available as JSON, CSV, and Markdown.
Each report records the baseline identifier and SHA-256 hash of its canonical case
catalog.

The benchmark cases and demo fixture are included in the wheel. An installed
`chemclaw benchmark-run` command discovers them automatically when it is run outside
the source repository.

## Future task families

- Task C: experiment-log structuring
- Task D: evidence-grounded weekly summaries
- Task E: planning and booking

These will reuse the v0.4 case isolation, reporting, and safety-metric infrastructure.
