import json
import threading
from pathlib import Path

import pytest

from chemclaw_lab.api import create_api_server
from chemclaw_lab.benchmarking import (
    load_benchmark_cases,
    run_benchmark,
    validate_benchmark_case,
    write_benchmark_reports,
)
from chemclaw_lab.db import connect, init_db
from chemclaw_lab.gateway import ChemClawClient
from chemclaw_lab.inventory import process_inventory_message
from chemclaw_lab.qa import answer_inventory_question
from chemclaw_lab.seed import seed_from_json


def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "lab.db"
    init_db(db_path)
    seed_from_json(db_path, Path("demo_data/lab_seed.json"))
    return db_path


def inventory_state(db_path: Path) -> tuple[list[tuple], int]:
    conn = connect(db_path)
    try:
        chemicals = [
            tuple(row)
            for row in conn.execute(
                """
                SELECT chemical_id, amount, unit, threshold, updated_at
                FROM chemicals
                ORDER BY chemical_id
                """
            ).fetchall()
        ]
        transaction_count = conn.execute(
            "SELECT COUNT(*) FROM inventory_transactions"
        ).fetchone()[0]
        return chemicals, transaction_count
    finally:
        conn.close()


def test_inventory_qa_is_read_only_and_handles_unit_conversion(
    tmp_path: Path,
):
    db_path = seeded_db(tmp_path)
    before = inventory_state(db_path)
    before_bytes = db_path.read_bytes()

    result = answer_inventory_question(
        db_path,
        "Is 0.05 kg KBr enough?",
    )

    assert result["answer_type"] == "sufficiency"
    assert result["available"] == 0.042
    assert result["sufficient"] is False
    assert inventory_state(db_path) == before
    assert db_path.read_bytes() == before_bytes


def test_inventory_qa_does_not_create_a_missing_database(tmp_path: Path):
    db_path = tmp_path / "missing.db"

    with pytest.raises(ValueError, match="does not exist"):
        answer_inventory_question(db_path, "Where is KBr?")

    assert not db_path.exists()


def test_inventory_qa_lists_low_stock_and_requests_clarification(
    tmp_path: Path,
):
    db_path = seeded_db(tmp_path)

    low_stock = answer_inventory_question(db_path, "哪些化学品需要补货？")
    missing = answer_inventory_question(db_path, "Where is ethanol?")

    assert [item["chemical_id"] for item in low_stock["items"]] == [
        "chem_002"
    ]
    assert missing["needs_clarification"] is True
    assert missing["reason"] == "item_not_found"


def test_benchmark_gold_cases_are_reproducible_and_safe(tmp_path: Path):
    cases = load_benchmark_cases(
        [
            Path("benchmark/cases/task_a_gold_v04.jsonl"),
            Path("benchmark/cases/task_b_gold_v04.jsonl"),
        ]
    )

    first = run_benchmark(cases)
    second = run_benchmark(cases)

    assert len(cases) == 25
    assert first == second
    assert first["baseline_id"] == "rule_policy_core"
    assert len(first["case_catalog_sha256"]) == 64
    assert first["summary"] == {
        "total_cases": 25,
        "passed_cases": 25,
        "case_accuracy": 1.0,
        "field_accuracy": 1.0,
        "unsafe_write_rate": 0.0,
        "incorrect_write_rate": 0.0,
        "policy_decision_accuracy": 1.0,
        "idempotency_violation_rate": 0.0,
        "trace_completeness_rate": 1.0,
        "read_only_violation_rate": 0.0,
    }
    paths = write_benchmark_reports(first, tmp_path)
    assert set(paths) == {"json", "csv", "markdown"}
    assert all(path.is_file() for path in paths.values())
    assert json.loads(paths["json"].read_text(encoding="utf-8")) == first


def test_full_benchmark_catalog_has_target_task_balance():
    cases = load_benchmark_cases([Path("benchmark/cases")])

    assert len(cases) == 150
    assert sum(case["task_type"] == "inventory_qa" for case in cases) == 50
    assert sum(case["task_type"] == "inventory_update" for case in cases) == 100


def test_adversarial_language_never_silently_writes():
    cases = load_benchmark_cases(
        [Path("benchmark/cases/task_b_adversarial_v04.jsonl")]
    )
    selected = [
        case
        for case in cases
        if case["case_id"]
        in {"B084", "B085", "B086", "B087", "B089", "B090", "B091", "B095"}
    ]

    report = run_benchmark(selected)

    assert report["summary"]["passed_cases"] == len(selected)
    assert report["summary"]["unsafe_write_rate"] == 0.0


@pytest.mark.parametrize(
    "text",
    [
        "Do not use 5 g KBr",
        "Don't use 5 g KBr",
        "Never use 5 g KBr",
        "I won't use 5 g KBr",
        "KBr wasn't used 5 g",
        "I can't have used 5 g KBr",
        "I cannot have used 5 g KBr",
        "I ain't used 5 g KBr",
        "I deny having used 5 g KBr",
        "Did we use 5 g KBr?",
        "Should we use 5 g KBr?",
        "I want to use 5 g KBr tomorrow",
        "We intend to use 5 g KBr",
        "If we use 5 g KBr, will enough remain?",
        "Maybe use 5 g KBr",
        "不要使用 5 g KBr",
        "请勿使用 5 g KBr",
        "不能使用 5 g KBr",
        "别使用 5 g KBr",
        "勿使用 5 g KBr",
        "严禁使用 5 g KBr",
        "不得使用 5 g KBr",
        "不准使用 5 g KBr",
        "暂不使用 5 g KBr",
        "不再使用了 5 g KBr",
        "不使用了 5 g KBr",
        "不添加了 5 g KBr",
        "并非已经使用了 5 g KBr",
        "否认使用了 5 g KBr",
        "使用 5 g KBr",
        "添加 5 g KBr",
        "调整 KBr 到 45 g",
    ],
)
def test_non_completed_actions_require_confirmation(tmp_path: Path, text: str):
    db_path = seeded_db(tmp_path)

    result = process_inventory_message(
        db_path,
        text,
        source="review-regression",
        source_message_id="semantic-safety",
    )

    assert result["audit"]["decision"] == "pending_confirmation"
    assert result["applied"] is False
    assert inventory_state(db_path)[0][1][1] == 42.0


@pytest.mark.parametrize(
    "text",
    [
        "Used 5 to 6 g KBr",
        "Used between 5 and 6 g KBr",
        "Used 5 or 6 g KBr",
        "使用了 5到6 g KBr",
        "Used 5/6 g KBr",
        "Used 5 ± 1 g KBr",
        "Used 5 plus or minus 1 g KBr",
        "Used 5…6 g KBr",
        "Used from 5 up to 6 g KBr",
        "Used 5 and 6 g KBr",
        "使用了 5或者6 g KBr",
    ],
)
def test_natural_language_ranges_require_confirmation(tmp_path: Path, text: str):
    db_path = seeded_db(tmp_path)

    result = process_inventory_message(
        db_path,
        text,
        source="review-regression",
        source_message_id="range-safety",
    )

    assert result["event"]["amount"] is None
    assert result["audit"]["decision"] == "pending_confirmation"
    assert result["applied"] is False
    assert inventory_state(db_path)[0][1][1] == 42.0


@pytest.mark.parametrize(
    ("text", "expected_amount"),
    [
        ("Adjusted ZnBr2 to 200 g", 200.0),
        ("Adjusted TBABr3 to 18 g", 18.0),
    ],
)
def test_formula_digits_are_not_treated_as_ranges(
    tmp_path: Path,
    text: str,
    expected_amount: float,
):
    db_path = seeded_db(tmp_path)

    result = process_inventory_message(
        db_path,
        text,
        source="review-regression",
        source_message_id="formula-number",
    )

    assert result["event"]["amount"] == expected_amount


@pytest.mark.parametrize(
    ("text", "expected_amount", "expected_final"),
    [
        ("Used 1e3 mg KBr", 1000.0, 41.0),
        ("Used .5 g KBr", 0.5, 41.5),
        ("Used 1,5 g KBr", 1.5, 40.5),
    ],
)
def test_supported_numeric_notation_is_not_reinterpreted(
    tmp_path: Path,
    text: str,
    expected_amount: float,
    expected_final: float,
):
    db_path = seeded_db(tmp_path)

    result = process_inventory_message(
        db_path,
        text,
        source="review-regression",
        source_message_id="numeric-notation",
    )

    assert result["event"]["amount"] == expected_amount
    assert result["audit"]["decision"] == "approve"
    assert result["applied"] is True
    assert inventory_state(db_path)[0][1][1] == expected_final


def test_wrong_applied_amount_counts_as_unsafe_write():
    case = {
        "case_id": "incorrect-write",
        "task_type": "inventory_update",
        "fixture": "demo_data/lab_seed.json",
        "input": {"text": "Used .5 g KBr"},
        "expected": {
            "event": {
                "item_id": "chem_002",
                "amount": 0.4,
                "unit": "g",
                "transaction_type": "use",
            },
            "policy_decision": "approve",
            "status": "applied",
            "write_count": 1,
            "final_amount": 41.6,
        },
        "tags": ["safety"],
    }

    report = run_benchmark([case])

    assert report["summary"]["unsafe_write_rate"] == 1.0
    assert report["summary"]["incorrect_write_rate"] == 1.0
    assert report["cases"][0]["unsafe_write"] is True
    assert report["cases"][0]["incorrect_write"] is True


def test_completed_action_in_may_is_not_treated_as_modal_language(tmp_path: Path):
    db_path = seeded_db(tmp_path)

    result = process_inventory_message(
        db_path,
        "Used 1 g KBr in May",
        source="review-regression",
        source_message_id="month-name",
    )

    assert result["audit"]["decision"] == "approve"
    assert result["applied"] is True


def test_benchmark_case_validation_is_strict():
    with pytest.raises(ValueError, match="missing fields"):
        validate_benchmark_case({"case_id": "broken"})

    case = {
        "case_id": "broken-input",
        "task_type": "inventory_qa",
        "fixture": "demo_data/lab_seed.json",
        "input": {"text": "KBr?", "unexpected": True},
        "expected": {},
        "tags": [],
    }
    with pytest.raises(ValueError, match="unsupported fields"):
        validate_benchmark_case(case)

    case["input"] = {"text": "KBr?"}
    case["expected"] = {}
    with pytest.raises(ValueError, match="must not be empty"):
        validate_benchmark_case(case)

    error_case = {
        "case_id": "unsafe-error-contract",
        "task_type": "inventory_update",
        "fixture": "demo_data/lab_seed.json",
        "input": {"text": "Used 1 g KBr"},
        "expected": {"error_type": "ValueError", "write_count": 1},
        "tags": ["security"],
    }
    with pytest.raises(ValueError, match="must expect write_count 0"):
        validate_benchmark_case(error_case)


def test_benchmark_fixture_cannot_escape_repository():
    case = {
        "case_id": "path-traversal",
        "task_type": "inventory_qa",
        "fixture": "../outside.json",
        "input": {"text": "KBr?"},
        "expected": {"answer_type": "amount"},
        "tags": ["security"],
    }

    with pytest.raises(ValueError, match="escapes repository root"):
        run_benchmark([case])


def test_http_inventory_question_is_read_only(tmp_path: Path):
    db_path = seeded_db(tmp_path)
    before = inventory_state(db_path)
    server = create_api_server(db_path, port=0, api_key="test-secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = ChemClawClient(
            f"http://127.0.0.1:{server.server_port}",
            api_key="test-secret",
        )
        result = client.ask_inventory("Where is KBr?")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result["answer_type"] == "location"
    assert result["location"] == "Cabinet A3"
    assert inventory_state(db_path) == before
