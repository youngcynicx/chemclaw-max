# ChemLabOps-Bench v0.4 Cases

The v0.4 inventory benchmark contains 150 independently executable cases:

- 50 Task A inventory-question cases;
- 100 Task B inventory-update cases.

Each JSONL record identifies its fixture, input, expected output, evaluation tags, and
annotation rationale. Every case runs in a newly initialized temporary SQLite database,
so order and previous cases cannot affect the result.

## Dataset layers

`task_a_gold_v04.jsonl` and `task_b_gold_v04.jsonl` are the 25-case regression set.
They must remain at 100% and run in CI.

The other files form the challenge set. They include unsupported capabilities and
adversarial language so the benchmark continues to distinguish releases rather than
only measuring cases already handled by the current parser.

## Coverage

- English, Chinese, and mixed-language inputs
- amount, location, availability, low-stock, and sufficiency questions
- use, add, adjust, discard, and expire reports
- mass/volume compatibility and unit conversion
- policy thresholds, hazards, negative-stock prevention, and invalid amounts
- ambiguity, missing fields, aliases, word numbers, and numeric notation
- actor/project attribution
- duplicate delivery and payload conflicts
- negation, future intent, questions, conditions, ranges, inequalities, and injection

The canonical executable case schema is
`src/chemclaw_lab/schemas/benchmark_case.schema.json`.
It rejects empty expectations and requires task-specific result fields.
