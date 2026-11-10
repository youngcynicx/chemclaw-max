# Repository Structure

```text
ChemClaw-Lab/
├── .github/workflows/       # CI
├── agents/                  # role and behavior specifications
├── benchmark/               # benchmark framing
│   ├── cases/               # executable Task A/B JSONL cases
│   └── reports/             # canonical baseline summaries
├── demo_data/               # synthetic lab state
├── docs/                    # architecture and release guidance
├── src/chemclaw_lab/        # runnable core
│   └── schemas/             # canonical executable event schemas
└── tests/                   # smoke, policy, lifecycle, API, and Excel tests
```

## Runtime modules

- `api.py` - local HTTP boundary and authentication
- `gateway.py` - external identity mapping and reusable HTTP client
- `excel.py` - atomic inventory import and generated workbook projection
- `qa.py` - read-only natural-language inventory questions
- `benchmarking.py` - isolated case runner, metrics, and report generation
- `inventory.py` - event workflow, persistence, confirmation, and transaction execution
- `models.py` - immutable event and audit models
- `policy.py` - deterministic inventory policy
- `units.py` - unit normalization and conversion
- `validation.py` - packaged JSON Schema execution
- `db.py` / `schema.py` - SQLite initialization and compatible migrations
- `cli.py` - standalone interface used by tests, administrators, and local demos

Messaging runtimes and chat platforms remain thin adapters around the HTTP/core
contract.
