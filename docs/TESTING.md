# Tests and retained evaluations

## Offline engineering tests

Use Python 3.10+ and Node 24 for the complete test suite (`node:sqlite` is imported by some tests). This snapshot was checked with Python 3.12 and Node 24. Create/activate a Python virtual environment, then:

```sh
python -m pip install -e . -r requirements-dev.txt
cd runtime/pi
npm ci
cd ../..
python scripts/run_offline_tests.py
```

The runner uses the active Python interpreter and `CHEMCLAW_NODE_EXE` or Node on `PATH`. It writes individual logs and `results.json` to ignored `test-results/`. Fixtures use temporary databases, local HTTP servers and controlled model responses; these are engineering tests, not live chemistry or OCR evaluations. No production database or credentials are needed.

Individual suites:

```sh
python -m pytest -q tests
python -m pytest -q frontend/tests
cd runtime/pi
npm test
cd ../..
node frontend/tests/test_jobs_ui.mjs
node frontend/tests/test_markdown_settings.mjs
```

The publication-time results are in `evidence/repository-validation-2026-11-10/`. Historical logs retain their original dates; their counts must not be added to this run.

## Retained model evaluations

| Evidence | What it establishes | Boundaries |
| --- | --- | --- |
| `evidence/fast-closure-2026-10-29/` | One-page reused OCR development experiment, candidate: 9 saved/read-back events and six predeclared fact groups checked | No new OCR; one candidate run; candidate was slower than original low; not unseen evaluation |
| `evidence/english-suite-2026-10-30/` | Six original synthetic images processed with fresh OCR; 72 events, four question answers, 93.67/100 answer score | Four pages completed, two partial; false correction histories and checkpoint decode failure retained; non-blind development regression |

`benchmark/materials/mof-handwritten-en/` contains synthetic notebook images, original PDF, prompts and grading material. Keep grading answers outside the tested agent's input. Images were used in previous development; they are not an unseen test set.

The original runners and evidence may refer to local paths from the historical workstation. They are retained as provenance, not guaranteed one-command replicas. Live runs require user-supplied model credentials, an explicitly configured isolated project and a fresh output directory. They may incur charges and are not started by the offline test runner. Raw databases, reader credentials, raw checkpoints and private model configuration are intentionally not distributed.

For exact evidence and limitations, start with [the one-page report](reports/ChemClaw_快速整理优化与小例验证_2026-10-29.md) and [the latest six-image report](reports/ChemClaw_最新版英文六图测试报告_2026-10-30.md).
