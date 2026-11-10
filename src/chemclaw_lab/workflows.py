"""S06: fixed workflow templates with step checkpoints.

The first version ships FOUR frozen templates (plan 搂12.12): paper-page
capture, knowledge query, event correction, and inventory recording. Dynamic
DAG orchestration is intentionally deferred (ADR): an orchestrator routes an
intent directly to its agent; the workflow is a fixed, versioned step list.

Recovery semantics (plan 搂12.13):
- pure computation steps are re-runnable;
- steps that committed a business operation record their ``operation_id`` in
  a checkpoint; a resumed run with the SAME input_version reuses the receipt
  from ``operation_results`` via ``workflow_reusable_output`` and never
  re-applies;
- advancing the workflow uses an epoch-conditional UPDATE, so a stale worker
  cannot move the state machine (fencing, plan 搂12.14);
- a workflow waiting for a human does NOT hold the run lease (the task is
  ``waiting_input`` and the run is finished/cancelled before the wait).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

# Imported lazily to avoid a circular import (agents -> workflows -> agents).


def _agent_host_error(*args: Any, **kwargs: Any):
    from .agents import AgentHostError

    return AgentHostError(*args, **kwargs)

WORKFLOW_SCHEMA_VERSION = "workflow_v1"

# Each step: name, capability that implements it (informational 鈥?the worker
# calls it through the gateway), reuse policy, and whether it mutates.
WORKFLOW_TEMPLATES: dict[str, dict[str, Any]] = {
    "capture_page": {
        "version": "capture_page_v1",
        "steps": [
            {"name": "ingest_page", "capability": "evidence.page.ingest", "mutates": True, "reuse": "by_operation_id"},
            {"name": "transcribe", "capability": "evidence.transcribe.run", "mutates": True, "reuse": "rerun"},
            {"name": "propose", "capability": "memory.proposal.create", "mutates": True, "reuse": "by_operation_id"},
            {"name": "human_confirm", "capability": "memory.proposal.confirm_items", "mutates": True, "reuse": "never"},
            {"name": "apply", "capability": "memory.proposal.apply", "mutates": True, "reuse": "by_operation_id"},
        ],
    },
    "query_answer": {
        "version": "query_answer_v1",
        "steps": [
            {"name": "search", "capability": "query.events.search", "mutates": False, "reuse": "rerun"},
            {"name": "build_answer", "capability": "query.answer.build", "mutates": False, "reuse": "rerun"},
            {"name": "render", "capability": "query.answer.render", "mutates": False, "reuse": "rerun"},
        ],
    },
    "correct_event": {
        "version": "correct_event_v1",
        "steps": [
            {"name": "correct", "capability": "memory.event.correct", "mutates": True, "reuse": "by_operation_id"},
            {"name": "rebuild_artifacts", "capability": "memory.index.rebuild", "mutates": True, "reuse": "rerun"},
        ],
    },
    "record_inventory": {
        "version": "record_inventory_v1",
        "steps": [
            {"name": "parse_message", "capability": "inventory.message.process", "mutates": True, "reuse": "by_operation_id"},
            {"name": "human_resolve", "capability": "inventory.event.resolve", "mutates": True, "reuse": "never"},
        ],
    },
    # S07 §13: B-side applied usage events reach the inventory domain as
    # persisted subtasks and are executed under stock-revision fencing.
    "cross_domain_consumption": {
        "version": "cross_domain_consumption_v1",
        "steps": [
            {"name": "ingest_inbound", "capability": "inventory.inbound.process", "mutates": True, "reuse": "by_operation_id"},
            {"name": "execute_subtask", "capability": "inventory.subtask.execute", "mutates": True, "reuse": "by_operation_id"},
        ],
    },
}


def start_workflow(
    conn: sqlite3.Connection,
    *,
    workflow_name: str,
    task_id: str,
    agent_id: str | None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    template = WORKFLOW_TEMPLATES.get(workflow_name)
    if template is None:
        raise _agent_host_error(
            "unsupported_intent",
            f"Unknown workflow {workflow_name!r}; available: {sorted(WORKFLOW_TEMPLATES)}.",
            http_status=404,
        )
    workflow_run_id = f"wf_{uuid.uuid4().hex[:12]}"
    conn.execute(
        """
        INSERT INTO workflow_runs
          (workflow_run_id, workflow_name, workflow_version, task_id, agent_id,
           current_step, steps_json, status, claim_epoch, trace_id)
        VALUES (?, ?, ?, ?, ?, NULL, ?, 'running', 0, ?)
        """,
        (
            workflow_run_id,
            workflow_name,
            template["version"],
            task_id,
            agent_id,
            json.dumps(template["steps"], sort_keys=True, ensure_ascii=False),
            trace_id,
        ),
    )
    return get_workflow(conn, workflow_run_id)


def advance_workflow(
    conn: sqlite3.Connection,
    *,
    workflow_run_id: str,
    claim_epoch: int,
    next_step: str | None,
    status: str | None = None,
    output_refs: list[dict[str, Any]] | None = None,
    operation_id: str | None = None,
    step_name: str | None = None,
    input_version: str | None = None,
) -> dict[str, Any]:
    """Epoch-conditional checkpoint + advance (fencing, plan 搂12.14).

    The caller passes the epoch it holds; if the workflow moved on (a stale
    worker or a recovery pass), the UPDATE matches nothing and the call fails
    with ``stale_worker`` instead of corrupting the state machine.
    """
    row = conn.execute(
        "SELECT claim_epoch, status FROM workflow_runs WHERE workflow_run_id = ?",
        (workflow_run_id,),
    ).fetchone()
    if row is None:
        raise _agent_host_error("invalid_state", f"Unknown workflow run {workflow_run_id!r}.", http_status=404)
    if row["claim_epoch"] != claim_epoch:
        raise _agent_host_error(
            "stale_worker",
            f"Workflow advance rejected: caller epoch {claim_epoch} != current {row['claim_epoch']}.",
            http_status=409,
        )
    if step_name and input_version:
        from . import tasks as tasks_mod

        tasks_mod.record_checkpoint(
            conn,
            task_id=_task_id_of(conn, workflow_run_id),
            step_name=step_name,
            input_version=input_version,
            output_refs=output_refs or [],
            operation_id=operation_id,
        )
    if status is not None and status not in ("running", "waiting_input", "succeeded", "failed", "cancelled"):
        raise _agent_host_error("schema_violation", f"Invalid workflow status {status!r}.")
    cursor = conn.execute(
        """
        UPDATE workflow_runs
        SET current_step = ?, status = COALESCE(?, status), claim_epoch = claim_epoch + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE workflow_run_id = ? AND claim_epoch = ?
        """,
        (next_step, status, workflow_run_id, claim_epoch),
    )
    if cursor.rowcount != 1:
        raise _agent_host_error(
            "stale_worker", "Workflow advance lost the epoch race.", http_status=409
        )
    return get_workflow(conn, workflow_run_id)


def workflow_reusable_output(
    conn: sqlite3.Connection,
    *,
    workflow_run_id: str,
    step_name: str,
    input_version: str,
) -> dict[str, Any] | None:
    """A committed step with unchanged input: reuse its receipt by reference."""
    from . import agents as agents_mod

    row = conn.execute(
        "SELECT task_id FROM workflow_runs WHERE workflow_run_id = ?", (workflow_run_id,)
    ).fetchone()
    if row is None:
        raise _agent_host_error("invalid_state", f"Unknown workflow run {workflow_run_id!r}.", http_status=404)
    return agents_mod.reuse_operation_result(
        conn, task_id=row["task_id"], step_name=step_name, input_version=input_version
    )


def get_workflow(conn: sqlite3.Connection, workflow_run_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM workflow_runs WHERE workflow_run_id = ?", (workflow_run_id,)
    ).fetchone()
    if row is None:
        raise _agent_host_error("invalid_state", f"Unknown workflow run {workflow_run_id!r}.", http_status=404)
    payload = dict(row)
    payload["steps"] = json.loads(payload.pop("steps_json") or "[]")
    return payload


def _task_id_of(conn: sqlite3.Connection, workflow_run_id: str) -> str:
    row = conn.execute(
        "SELECT task_id FROM workflow_runs WHERE workflow_run_id = ?", (workflow_run_id,)
    ).fetchone()
    if row is None:
        raise _agent_host_error("invalid_state", f"Unknown workflow run {workflow_run_id!r}.", http_status=404)
    return row["task_id"]

