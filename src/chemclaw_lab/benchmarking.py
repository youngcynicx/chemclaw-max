from __future__ import annotations

import csv
import hashlib
import json
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .db import connect, init_db
from .inventory import process_inventory_message
from .qa import answer_inventory_question
from .seed import seed_from_json


BENCHMARK_VERSION = "0.4.0"
BASELINE_ID = "rule_policy_core"
TASK_A = "inventory_qa"
TASK_B = "inventory_update"
SUPPORTED_TASKS = {TASK_A, TASK_B}


def load_benchmark_cases(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for source in _expand_case_paths(paths):
        for line_number, line in enumerate(
            source.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{source}:{line_number}: invalid JSON: {exc.msg}"
                ) from exc
            validate_benchmark_case(case)
            case_id = case["case_id"]
            if case_id in seen_ids:
                raise ValueError(f"Duplicate benchmark case_id: {case_id}")
            seen_ids.add(case_id)
            cases.append(case)
    if not cases:
        raise ValueError("No benchmark cases found")
    return sorted(cases, key=lambda item: item["case_id"])


def validate_benchmark_case(case: dict[str, Any]) -> None:
    if not isinstance(case, dict):
        raise ValueError("Benchmark case must be an object")
    required = {"case_id", "task_type", "fixture", "input", "expected", "tags"}
    missing = required - set(case)
    if missing:
        raise ValueError(f"Benchmark case is missing fields: {sorted(missing)}")
    unknown = set(case) - (required | {"annotation_notes"})
    if unknown:
        raise ValueError(f"Benchmark case has unsupported fields: {sorted(unknown)}")
    if not isinstance(case["case_id"], str) or not case["case_id"].strip():
        raise ValueError("case_id must be a non-empty string")
    if case["task_type"] not in SUPPORTED_TASKS:
        raise ValueError(f"Unsupported task_type: {case['task_type']}")
    if not isinstance(case["fixture"], str) or not case["fixture"].strip():
        raise ValueError("fixture must be a non-empty path")
    if not isinstance(case["input"], dict):
        raise ValueError("input must be an object")
    if (
        not isinstance(case["input"].get("text"), str)
        or not case["input"]["text"].strip()
    ):
        raise ValueError("input.text must be a non-empty string")
    unknown_input = set(case["input"]) - {
        "text",
        "user_id",
        "project_id",
        "repeat_count",
        "replay_text",
    }
    if unknown_input:
        raise ValueError(
            f"input has unsupported fields: {sorted(unknown_input)}"
        )
    repeat_count = case["input"].get("repeat_count")
    if repeat_count is not None and (
        not isinstance(repeat_count, int)
        or isinstance(repeat_count, bool)
        or repeat_count < 1
    ):
        raise ValueError("input.repeat_count must be a positive integer")
    for field in ["user_id", "project_id", "replay_text"]:
        value = case["input"].get(field)
        if value is not None and (
            not isinstance(value, str) or not value.strip()
        ):
            raise ValueError(f"input.{field} must be a non-empty string")
    if not isinstance(case["expected"], dict):
        raise ValueError("expected must be an object")
    expected = case["expected"]
    if not expected:
        raise ValueError("expected must not be empty")
    if case["task_type"] == TASK_A and "answer_type" not in expected:
        raise ValueError("inventory_qa expected must include answer_type")
    if case["task_type"] == TASK_B:
        if "write_count" not in expected:
            raise ValueError("inventory_update expected must include write_count")
        if "error_type" in expected:
            if expected["write_count"] != 0:
                raise ValueError("error cases must expect write_count 0")
        else:
            required_update = {
                "event",
                "policy_decision",
                "status",
                "final_amount",
            }
            missing_update = required_update - set(expected)
            if missing_update:
                raise ValueError(
                    "inventory_update expected is missing fields: "
                    f"{sorted(missing_update)}"
                )
            if not isinstance(expected["event"], dict):
                raise ValueError("inventory_update expected.event must be an object")
            material_fields = {
                "item_id",
                "amount",
                "unit",
                "transaction_type",
            }
            missing_material = material_fields - set(expected["event"])
            if missing_material:
                raise ValueError(
                    "inventory_update expected.event is missing fields: "
                    f"{sorted(missing_material)}"
                )
    if not isinstance(case["tags"], list) or not all(
        isinstance(tag, str) and tag for tag in case["tags"]
    ):
        raise ValueError("tags must be a list of non-empty strings")
    if "annotation_notes" in case and not isinstance(
        case["annotation_notes"],
        str,
    ):
        raise ValueError("annotation_notes must be a string")


def run_benchmark(
    cases: Iterable[dict[str, Any]],
    *,
    repository_root: str | Path = ".",
) -> dict[str, Any]:
    root = Path(repository_root).resolve()
    materialized = list(cases)
    if not materialized:
        raise ValueError("No benchmark cases found")
    seen_ids: set[str] = set()
    for case in materialized:
        validate_benchmark_case(case)
        case_id = case["case_id"]
        if case_id in seen_ids:
            raise ValueError(f"Duplicate benchmark case_id: {case_id}")
        seen_ids.add(case_id)
    ordered_cases = sorted(materialized, key=lambda item: item["case_id"])
    case_results = [
        _run_case(case, repository_root=root)
        for case in ordered_cases
    ]
    return _aggregate_results(
        case_results,
        case_catalog_sha256=_case_catalog_hash(ordered_cases),
    )


def write_benchmark_reports(
    report: dict[str, Any],
    output_dir: str | Path,
    *,
    stem: str = "v0.4_baseline",
) -> dict[str, Path]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / f"{stem}.json"
    csv_path = destination / f"{stem}.csv"
    markdown_path = destination / f"{stem}.md"
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "case_id",
                "task_type",
                "passed",
                "field_accuracy",
                "policy_correct",
                "unsafe_write",
                "incorrect_write",
                "trace_complete",
                "tags",
            ],
        )
        writer.writeheader()
        for case in report["cases"]:
            writer.writerow(
                {
                    "case_id": case["case_id"],
                    "task_type": case["task_type"],
                    "passed": case["passed"],
                    "field_accuracy": case["field_accuracy"],
                    "policy_correct": case.get("policy_correct"),
                    "unsafe_write": case.get("unsafe_write"),
                    "incorrect_write": case.get("incorrect_write"),
                    "trace_complete": case.get("trace_complete"),
                    "tags": "|".join(case["tags"]),
                }
            )
    markdown_path.write_text(_markdown_report(report), encoding="utf-8")
    return {
        "json": json_path,
        "csv": csv_path,
        "markdown": markdown_path,
    }


def _run_case(
    case: dict[str, Any],
    *,
    repository_root: Path,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="chemclaw-bench-") as tmp_dir:
        db_path = Path(tmp_dir) / "case.db"
        fixture = (repository_root / case["fixture"]).resolve()
        if not fixture.is_relative_to(repository_root):
            raise ValueError(
                f'{case["case_id"]}: fixture escapes repository root'
            )
        if not fixture.is_file():
            raise ValueError(f'{case["case_id"]}: fixture not found: {fixture}')
        init_db(db_path)
        seed_from_json(db_path, fixture)
        before = _state_snapshot(db_path)
        if case["task_type"] == TASK_A:
            actual = answer_inventory_question(db_path, case["input"]["text"])
            after = _state_snapshot(db_path)
            read_only = before == after
            comparisons = _compare_expected(case["expected"], actual)
            return _case_result(
                case,
                actual,
                comparisons,
                read_only=read_only,
            )
        return _run_task_b(case, db_path, before)


def _run_task_b(
    case: dict[str, Any],
    db_path: Path,
    before: dict[str, Any],
) -> dict[str, Any]:
    input_data = case["input"]
    repeat_count = int(input_data.get("repeat_count", 1))
    if repeat_count < 1:
        raise ValueError(f'{case["case_id"]}: repeat_count must be positive')
    results: list[dict[str, Any]] = []
    try:
        for attempt in range(repeat_count):
            text = (
                input_data.get("replay_text", input_data["text"])
                if attempt
                else input_data["text"]
            )
            results.append(
                process_inventory_message(
                    db_path,
                    text,
                    source="benchmark",
                    source_message_id=case["case_id"],
                    user_id=input_data.get("user_id"),
                    project_id=input_data.get("project_id"),
                )
            )
    except Exception as exc:
        after = _state_snapshot(db_path)
        write_should_be_blocked = case["expected"].get("write_count") == 0
        actual = {
            "error_type": type(exc).__name__,
            "error": str(exc),
            "write_count": (
                after["transaction_count"] - before["transaction_count"]
            ),
        }
        comparisons = _compare_expected(case["expected"], actual)
        return _case_result(
            case,
            actual,
            comparisons,
            unsafe_write=(
                write_should_be_blocked and actual["write_count"] > 0
            ),
            incorrect_write=False,
            write_should_be_blocked=write_should_be_blocked,
        )
    first = results[0]
    last = results[-1]
    after = _state_snapshot(db_path)
    item_id = first["event"].get("item_id")
    final_amount = (
        after["chemicals"].get(item_id, {}).get("amount")
        if item_id is not None
        else None
    )
    actual = {
        "event": {
            key: first["event"].get(key)
            for key in [
                "event_type",
                "event_subtype",
                "item_id",
                "item_name",
                "amount",
                "unit",
                "transaction_type",
                "project_id",
                "user_id",
                "requires_confirmation",
            ]
        },
        "policy_decision": first["audit"]["decision"],
        "status": first["status"],
        "applied": first["applied"],
        "write_count": after["transaction_count"] - before["transaction_count"],
        "final_amount": final_amount,
        "idempotent_replay": bool(last["idempotent_replay"]),
        "trace_complete": _trace_complete(
            db_path,
            case["case_id"],
            applied=first["applied"],
        ),
    }
    comparisons = _compare_expected(case["expected"], actual)
    write_should_be_blocked = (
        case["expected"].get("policy_decision") != "approve"
        or case["expected"].get("write_count") == 0
    )
    critical_write_fields = {
        "event.amount",
        "event.unit",
        "event.item_id",
        "event.transaction_type",
        "final_amount",
        "write_count",
    }
    incorrect_write = actual["write_count"] > 0 and any(
        field in critical_write_fields and not matched
        for field, matched in comparisons.items()
    )
    unsafe_write = (
        write_should_be_blocked and actual["write_count"] > 0
    ) or incorrect_write
    idempotency_violation = repeat_count > 1 and (
        actual["write_count"] > 1 or not actual["idempotent_replay"]
    )
    properties: dict[str, Any] = {
        "policy_correct": (
            actual["policy_decision"]
            == case["expected"].get("policy_decision")
        ),
        "unsafe_write": unsafe_write,
        "incorrect_write": incorrect_write,
        "write_should_be_blocked": write_should_be_blocked,
        "trace_complete": actual["trace_complete"],
    }
    if repeat_count > 1:
        properties["idempotency_violation"] = idempotency_violation
    return _case_result(
        case,
        actual,
        comparisons,
        **properties,
    )


def _case_result(
    case: dict[str, Any],
    actual: dict[str, Any],
    comparisons: dict[str, bool],
    **properties: Any,
) -> dict[str, Any]:
    field_accuracy = (
        sum(comparisons.values()) / len(comparisons) if comparisons else 0.0
    )
    passed = bool(comparisons) and all(comparisons.values()) and all(
        value is not False
        for key, value in properties.items()
        if key in {"read_only", "trace_complete"}
    )
    if properties.get("unsafe_write") or properties.get("idempotency_violation"):
        passed = False
    return {
        "case_id": case["case_id"],
        "task_type": case["task_type"],
        "tags": sorted(case["tags"]),
        "passed": passed,
        "field_accuracy": round(field_accuracy, 6),
        "field_results": comparisons,
        "actual": actual,
        **properties,
    }


def _compare_expected(
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    prefix: str = "",
) -> dict[str, bool]:
    comparisons: dict[str, bool] = {}
    for key, expected_value in expected.items():
        field = f"{prefix}.{key}" if prefix else key
        if key.endswith("_contains"):
            actual_key = key.removesuffix("_contains")
            actual_value = actual.get(actual_key)
            comparisons[field] = (
                isinstance(expected_value, str)
                and expected_value in str(actual_value)
            )
            continue
        actual_value = actual.get(key)
        if isinstance(expected_value, dict):
            comparisons.update(
                _compare_expected(
                    expected_value,
                    actual_value if isinstance(actual_value, dict) else {},
                    prefix=field,
                )
            )
        else:
            comparisons[field] = _values_equal(expected_value, actual_value)
    return comparisons


def _values_equal(expected: Any, actual: Any) -> bool:
    if (
        isinstance(expected, (int, float))
        and not isinstance(expected, bool)
        and isinstance(actual, (int, float))
        and not isinstance(actual, bool)
    ):
        return abs(float(expected) - float(actual)) <= 1e-9
    return expected == actual


def _state_snapshot(db_path: Path) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        chemicals = {
            row["chemical_id"]: {
                "amount": float(row["amount"]),
                "unit": row["unit"],
                "location": row["location"],
                "threshold": float(row["threshold"] or 0),
            }
            for row in conn.execute(
                """
                SELECT chemical_id, amount, unit, location, threshold
                FROM chemicals
                ORDER BY chemical_id
                """
            ).fetchall()
        }
        return {
            "chemicals": chemicals,
            "transaction_count": conn.execute(
                "SELECT COUNT(*) FROM inventory_transactions"
            ).fetchone()[0],
            "message_count": conn.execute(
                "SELECT COUNT(*) FROM message_logs"
            ).fetchone()[0],
            "event_count": conn.execute(
                "SELECT COUNT(*) FROM event_proposals"
            ).fetchone()[0],
            "audit_count": conn.execute(
                "SELECT COUNT(*) FROM audit_logs"
            ).fetchone()[0],
        }
    finally:
        conn.close()


def _trace_complete(
    db_path: Path,
    source_message_id: str,
    *,
    applied: bool,
) -> bool:
    conn = connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT m.message_id, e.event_id, a.audit_id, t.transaction_id
            FROM message_logs m
            JOIN event_proposals e ON e.message_id = m.message_id
            JOIN audit_logs a
              ON a.message_id = m.message_id AND a.event_id = e.event_id
            LEFT JOIN inventory_transactions t
              ON t.message_id = m.message_id AND t.event_id = e.event_id
            WHERE m.source = 'benchmark' AND m.source_message_id = ?
            """,
            (source_message_id,),
        ).fetchone()
        if row is None:
            return False
        return (row["transaction_id"] is not None) == applied
    finally:
        conn.close()


def _aggregate_results(
    case_results: list[dict[str, Any]],
    *,
    case_catalog_sha256: str,
) -> dict[str, Any]:
    task_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    tag_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in case_results:
        task_groups[result["task_type"]].append(result)
        for tag in result["tags"]:
            tag_groups[tag].append(result)
    total = len(case_results)
    passed = sum(result["passed"] for result in case_results)
    return {
        "benchmark_version": BENCHMARK_VERSION,
        "baseline_id": BASELINE_ID,
        "case_catalog_sha256": case_catalog_sha256,
        "summary": {
            "total_cases": total,
            "passed_cases": passed,
            "case_accuracy": round(passed / total, 6),
            "field_accuracy": _average(
                result["field_accuracy"] for result in case_results
            ),
            "unsafe_write_rate": _rate(
                [
                    item
                    for item in case_results
                    if "unsafe_write" in item
                ],
                lambda item: bool(item.get("unsafe_write")),
            ),
            "incorrect_write_rate": _rate(
                [
                    item
                    for item in case_results
                    if "incorrect_write" in item
                ],
                lambda item: bool(item.get("incorrect_write")),
            ),
            "policy_decision_accuracy": _rate(
                [
                    item
                    for item in case_results
                    if "policy_correct" in item
                ],
                lambda item: bool(item["policy_correct"]),
            ),
            "idempotency_violation_rate": _rate(
                [
                    item
                    for item in case_results
                    if "idempotency_violation" in item
                ],
                lambda item: bool(item["idempotency_violation"]),
            ),
            "trace_completeness_rate": _rate(
                [item for item in case_results if "trace_complete" in item],
                lambda item: bool(item["trace_complete"]),
            ),
            "read_only_violation_rate": _rate(
                [item for item in case_results if "read_only" in item],
                lambda item: not bool(item["read_only"]),
            ),
        },
        "tasks": {
            task: _group_metrics(results)
            for task, results in sorted(task_groups.items())
        },
        "tags": {
            tag: _group_metrics(results)
            for tag, results in sorted(tag_groups.items())
        },
        "cases": case_results,
    }


def _group_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total_cases": len(results),
        "passed_cases": sum(result["passed"] for result in results),
        "case_accuracy": _rate(results, lambda item: item["passed"]),
        "field_accuracy": _average(
            result["field_accuracy"] for result in results
        ),
    }


def _case_catalog_hash(cases: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        cases,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _average(values: Iterable[float]) -> float:
    materialized = list(values)
    if not materialized:
        return 0.0
    return round(sum(materialized) / len(materialized), 6)


def _rate(
    values: Iterable[dict[str, Any]],
    predicate: Any,
) -> float:
    materialized = list(values)
    if not materialized:
        return 0.0
    return round(
        sum(bool(predicate(value)) for value in materialized)
        / len(materialized),
        6,
    )


def _expand_case_paths(paths: Iterable[str | Path]) -> list[Path]:
    expanded: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            expanded.extend(sorted(path.glob("*.jsonl")))
        elif path.is_file():
            expanded.append(path)
        else:
            raise ValueError(f"Benchmark case path not found: {path}")
    return expanded


def _markdown_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# ChemLabOps-Bench v0.4 Baseline",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Total cases | {summary['total_cases']} |",
        f"| Passed cases | {summary['passed_cases']} |",
        f"| Case accuracy | {summary['case_accuracy']:.3f} |",
        f"| Field accuracy | {summary['field_accuracy']:.3f} |",
        f"| Unsafe-write rate | {summary['unsafe_write_rate']:.3f} |",
        (
            "| Incorrect-write rate | "
            f"{summary['incorrect_write_rate']:.3f} |"
        ),
        (
            "| Policy-decision accuracy | "
            f"{summary['policy_decision_accuracy']:.3f} |"
        ),
        (
            "| Idempotency violation rate | "
            f"{summary['idempotency_violation_rate']:.3f} |"
        ),
        (
            "| Trace completeness rate | "
            f"{summary['trace_completeness_rate']:.3f} |"
        ),
        (
            "| Read-only violation rate | "
            f"{summary['read_only_violation_rate']:.3f} |"
        ),
        "",
        "## Tasks",
        "",
        "| Task | Cases | Accuracy | Field accuracy |",
        "| --- | ---: | ---: | ---: |",
    ]
    for task, metrics in report["tasks"].items():
        lines.append(
            f"| {task} | {metrics['total_cases']} | "
            f"{metrics['case_accuracy']:.3f} | "
            f"{metrics['field_accuracy']:.3f} |"
        )
    failures = [case for case in report["cases"] if not case["passed"]]
    lines.extend(["", "## Failed cases", ""])
    if failures:
        lines.extend(f"- `{case['case_id']}`" for case in failures)
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"
