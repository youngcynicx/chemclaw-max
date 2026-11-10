"""S09: external result submission (plan §15).

External work products are persisted with their full provenance — input
source, tool/method/version, artifacts. A ``computation`` result is a
computation: it NEVER enters experiment events, memory proposals, or the
inventory bridge. The strict separation lives in its own table.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

SOURCE_KINDS = ("computation", "literature", "external_lab")

# Fields that make a wet-lab claim. Their presence on a computation result is
# a contract violation, not a style issue.
_WET_LAB_ONLY_FIELDS = (
    "yield_percent", "mp_celsius", "bp_celsius", "observed_mass",
    "observed_volume", "operator", "instrument_id",
)


class ExternalResultError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def submit_external_result(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    source_kind: str,
    title: str,
    summary: str | None = None,
    tool_name: str | None = None,
    tool_version: str | None = None,
    method: str | None = None,
    inputs: dict[str, Any] | None = None,
    input_source: str | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    task_id: str | None = None,
    submitted_by: str = "",
) -> dict[str, Any]:
    if source_kind not in SOURCE_KINDS:
        raise ExternalResultError(
            "schema_violation",
            f"source_kind must be one of {SOURCE_KINDS!r}.",
        )
    wet_fields = [k for k in (inputs or {}) if k in _WET_LAB_ONLY_FIELDS]
    if source_kind == "computation" and wet_fields:
        raise ExternalResultError(
            "schema_violation",
            f"A computation result cannot carry wet-lab fields {wet_fields!r}.",
        )
    provenance = {
        "tool_name": tool_name,
        "tool_version": tool_version,
        "method": method,
        "input_source": input_source,
        "artifacts": artifacts or [],
    }
    if source_kind == "computation" and not tool_name:
        raise ExternalResultError(
            "missing_provenance",
            "Computation results must record the producing tool.",
        )
    canonical = json.dumps(
        {"inputs": inputs or {}, "provenance": provenance},
        ensure_ascii=False, sort_keys=True,
    )
    inputs_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    result_id = f"ext_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO external_results (result_id, project_id, source_kind, title, "
        "summary, tool_name, tool_version, method, inputs_hash, artifact_refs, "
        "task_id, submitted_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            result_id, project_id, source_kind, title, summary,
            tool_name, tool_version, method, inputs_hash,
            json.dumps(
                {**provenance, "inputs": inputs or {}}, ensure_ascii=False
            ),
            task_id, submitted_by,
        ),
    )
    return {
        "result_id": result_id,
        "source_kind": source_kind,
        "inputs_hash": inputs_hash,
        "usage_semantics": "computation" if source_kind == "computation"
        else "external_reference",
    }


def list_external_results(
    conn: sqlite3.Connection,
    *,
    project_ids: list[str] | None = None,
    source_kind: str | None = None,
    result_id: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    if result_id is not None:
        clauses.append("result_id = ?")
        values.append(result_id)
    if project_ids is not None:
        placeholders = ",".join("?" for _ in project_ids)
        if placeholders:
            clauses.append(f"project_id IN ({placeholders})")
        else:
            clauses.append("0 = 1")
        values.extend(project_ids)
    if source_kind is not None:
        clauses.append("source_kind = ?")
        values.append(source_kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM external_results {where} ORDER BY created_at", values
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        if item.get("artifact_refs"):
            item["artifact_refs"] = json.loads(item.pop("artifact_refs"))
        result.append(item)
    if result_id is not None and not result:
        raise ExternalResultError(
            "not_found", f"Unknown external result {result_id!r}.", http_status=404)
    return result
