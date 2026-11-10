"""S02: durable task/interaction/message contracts.

Implements plan §8.7/§8.8 contracts that later stages (S04/S05/S06) build on:
- Task status semantics with validated transitions and a monotonic revision
  (``expected_task_revision`` checks on resume).
- AgentRun rows with worker lease + ``claim_epoch`` fencing (worker integration
  happens in S06; here only the durable state and transitions).
- StepCheckpoint rows: input version + output refs + operation_id, so recovery
  can reuse existing proposals instead of re-running side effects.
- PendingInteractions: the question is stored atomically together with the
  ``waiting_input`` status; the answering path re-validates identity and
  revision, and never mints approval credentials.
- Durable inbound (transport_event_id dedupe), outbox, and deliveries as three
  SEPARATE idempotency layers (business idempotency lives in
  ``operation_results``; enqueue success is not business ``applied``).
- Typed messages (command/domain_event/task_result) with schema validation and
  causation/correlation ids.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .validation import (
    SchemaViolation,
    load_named_schema,
    validate_payload_against_schema,
)


TASK_SCHEMA_NAME = "task_v1.json"
TYPED_MESSAGE_SCHEMA_NAME = "typed_message_v1.json"

TASK_STATUSES = ("queued", "running", "waiting_input", "succeeded", "failed", "cancelled")
TASK_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"running", "waiting_input", "cancelled"},
    "running": {"waiting_input", "succeeded", "failed", "cancelled"},
    "waiting_input": {"running", "cancelled"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}
TASK_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}

AGENT_RUN_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
INBOUND_STATUSES = ("received", "queued", "processed", "failed", "duplicate")
OUTBOX_STATUSES = ("pending", "sent", "failed")
DELIVERY_STATUSES = ("queued", "delivered", "failed")


class TaskError(Exception):
    """Domain error with a stable error code for the capability envelope."""

    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def create_task(
    conn: sqlite3.Connection,
    *,
    task_type: str,
    principal_id: str,
    project_id: str | None = None,
    title: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    task_id = task_id or _new_id("task")
    conn.execute(
        """
        INSERT INTO tasks (task_id, task_type, title, principal_id, project_id, status, revision)
        VALUES (?, ?, ?, ?, ?, 'queued', 0)
        """,
        (task_id, task_type, title, principal_id, project_id),
    )
    return get_task(conn, task_id)


def get_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise TaskError("invalid_state", f"Unknown task {task_id!r}.", http_status=404)
    return _task_payload(row)


def transition_task(
    conn: sqlite3.Connection,
    task_id: str,
    new_status: str,
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Apply a validated status transition and bump the task revision.

    The caller owns the surrounding transaction so the status change can be
    atomic with checkpoints, interactions, or outbox enqueues.
    """
    if new_status not in TASK_STATUSES:
        raise TaskError("schema_violation", f"Unknown task status {new_status!r}.")
    row = conn.execute(
        "SELECT status, revision FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row is None:
        raise TaskError("invalid_state", f"Unknown task {task_id!r}.", http_status=404)
    if expected_revision is not None and row["revision"] != expected_revision:
        raise TaskError(
            "revision_conflict",
            f"Task revision moved: expected {expected_revision}, current {row['revision']}.",
            http_status=409,
        )
    if row["status"] in TASK_TERMINAL_STATUSES:
        raise TaskError(
            "invalid_state",
            f"Task {task_id!r} is terminal ({row['status']}).",
            http_status=409,
        )
    if new_status not in TASK_TRANSITIONS[row["status"]]:
        raise TaskError(
            "invalid_state",
            f"Transition {row['status']} -> {new_status} is not allowed.",
            http_status=409,
        )
    conn.execute(
        """
        UPDATE tasks
        SET status = ?, revision = revision + 1, updated_at = CURRENT_TIMESTAMP
        WHERE task_id = ?
        """,
        (new_status, task_id),
    )
    return get_task(conn, task_id)


def create_agent_run(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    agent_role: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    task = get_task(conn, task_id)
    if task["status"] not in ("queued", "running", "waiting_input"):
        raise TaskError(
            "invalid_state",
            f"Cannot start a run for a task in status {task['status']!r}.",
        )
    run_id = run_id or _new_id("run")
    conn.execute(
        """
        INSERT INTO agent_runs (run_id, task_id, agent_role, status, claim_epoch)
        VALUES (?, ?, ?, 'queued', 0)
        """,
        (run_id, task_id, agent_role),
    )
    return _run_payload(
        conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    )


def claim_agent_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    worker_id: str,
    lease_seconds: int = 600,
) -> dict[str, Any]:
    """Claim a queued run and bump the fencing epoch.

    A stale worker (older claim_epoch) must never advance business state; the
    epoch is the fencing token it must present on later writes (enforced by
    callers in S06).
    """
    cursor = conn.execute(
        """
        UPDATE agent_runs
        SET status = 'running', worker_id = ?,
            claim_epoch = claim_epoch + 1,
            lease_expires_at = datetime('now', ?),
            heartbeat_at = CURRENT_TIMESTAMP,
            started_at = COALESCE(started_at, CURRENT_TIMESTAMP)
        WHERE run_id = ?
          AND (
                status = 'queued'
                OR (status = 'running'
                    AND (lease_expires_at IS NULL
                         OR lease_expires_at <= datetime('now')))
              )
        """,
        (worker_id, f"+{int(lease_seconds)} seconds", run_id),
    )
    row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise TaskError("invalid_state", f"Unknown run {run_id!r}.", http_status=404)
    if cursor.rowcount != 1:
        raise TaskError(
            "run_lease_active",
            f"Run {run_id!r} is still held by another worker "
            f"({row['worker_id']!r}); only an expired lease can be reclaimed.",
            http_status=409,
        )
    return _run_payload(row)


def finish_agent_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    claim_epoch: int | None = None,
) -> dict[str, Any]:
    if status not in ("succeeded", "failed", "cancelled"):
        raise TaskError("schema_violation", f"Invalid terminal run status {status!r}.")
    row = conn.execute(
        "SELECT claim_epoch FROM agent_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise TaskError("invalid_state", f"Unknown run {run_id!r}.", http_status=404)
    if claim_epoch is not None and claim_epoch != row["claim_epoch"]:
        raise TaskError(
            "revision_conflict",
            "Stale worker: claim_epoch does not match the current lease.",
            http_status=409,
        )
    if claim_epoch is None:
        raise TaskError(
            "stale_worker",
            "A terminal run write requires the current claim_epoch.",
            http_status=409,
        )
    cursor = conn.execute(
        """
        UPDATE agent_runs
        SET status = ?, result_json = ?, finished_at = CURRENT_TIMESTAMP,
            lease_expires_at = NULL
        WHERE run_id = ? AND status = 'running' AND claim_epoch = ?
        """,
        (
            status,
            json.dumps(result or {}, sort_keys=True, ensure_ascii=False),
            run_id,
            claim_epoch,
        ),
    )
    if cursor.rowcount != 1:
        current = conn.execute(
            "SELECT status, claim_epoch FROM agent_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        raise TaskError(
            "revision_conflict",
            "Stale worker: claim_epoch does not match the current lease "
            f"(current status={current['status']!r}, epoch={current['claim_epoch']}).",
            http_status=409,
        )
    return _run_payload(
        conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    )


def record_checkpoint(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    step_name: str,
    input_version: str,
    output_refs: list[dict[str, Any]] | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Persist a step checkpoint (input version + output refs + operation_id).

    Recovery replays pure computation but reuses recorded proposals via
    operation_id, and never re-applies business writes on its own.
    """
    checkpoint_id = _new_id("ckp")
    conn.execute(
        """
        INSERT INTO task_checkpoints
          (checkpoint_id, task_id, step_name, input_version, output_refs_json, operation_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            checkpoint_id,
            task_id,
            step_name,
            input_version,
            json.dumps(output_refs or [], sort_keys=True, ensure_ascii=False),
            operation_id,
        ),
    )
    return dict(
        conn.execute(
            "SELECT * FROM task_checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
        ).fetchone()
    )


def latest_checkpoint(conn: sqlite3.Connection, task_id: str, step_name: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM task_checkpoints
        WHERE task_id = ? AND step_name = ?
        ORDER BY rowid DESC LIMIT 1
        """,
        (task_id, step_name),
    ).fetchone()
    return dict(row) if row is not None else None


def open_pending_interaction(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    question: str,
    context_refs: list[dict[str, Any]] | None = None,
    interaction_id: str | None = None,
) -> dict[str, Any]:
    """Store the clarification question atomically with ``waiting_input``.

    Caller must hold the transaction; both writes commit together or not at all
    (plan P0: the worker is released after this returns).
    """
    transition_task(conn, task_id, "waiting_input")
    interaction_id = interaction_id or _new_id("int")
    conn.execute(
        """
        INSERT INTO pending_interactions
          (interaction_id, task_id, question, context_refs_json, status)
        VALUES (?, ?, ?, ?, 'open')
        """,
        (
            interaction_id,
            task_id,
            question,
            json.dumps(context_refs or [], sort_keys=True, ensure_ascii=False),
        ),
    )
    return dict(
        conn.execute(
            "SELECT * FROM pending_interactions WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()
    )


def answer_pending_interaction(
    conn: sqlite3.Connection,
    *,
    interaction_id: str,
    answer: dict[str, Any],
    principal_id: str,
    expected_task_revision: int,
) -> dict[str, Any]:
    """Authenticated resume path for a waiting task.

    Re-validates that the interaction is still open and that the caller's
    revision matches the task. Answers are attached as user input; approval
    credentials for business proposals are NEVER derived from this payload
    (that requires the separate confirmation capability).
    """
    row = conn.execute(
        "SELECT * FROM pending_interactions WHERE interaction_id = ?",
        (interaction_id,),
    ).fetchone()
    if row is None:
        raise TaskError("invalid_state", f"Unknown interaction {interaction_id!r}.", http_status=404)
    if row["status"] != "open":
        raise TaskError(
            "invalid_state",
            f"Interaction {interaction_id!r} already {row['status']}.",
            http_status=409,
        )
    task = get_task(conn, row["task_id"])
    if task["principal_id"] != principal_id:
        raise TaskError("forbidden_scope", "Only the task owner may answer.", http_status=403)
    transition_task(
        conn,
        task["task_id"],
        "running",
        expected_revision=expected_task_revision,
    )
    conn.execute(
        """
        UPDATE pending_interactions
        SET status = 'answered', answer_json = ?, answered_at = CURRENT_TIMESTAMP
        WHERE interaction_id = ?
        """,
        (json.dumps(answer, sort_keys=True, ensure_ascii=False), interaction_id),
    )
    return {
        "interaction": dict(
            conn.execute(
                "SELECT * FROM pending_interactions WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
        ),
        "task": get_task(conn, task["task_id"]),
    }


def list_open_interactions(conn: sqlite3.Connection, *, task_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM pending_interactions WHERE status = 'open'"
    params: tuple[Any, ...] = ()
    if task_id is not None:
        sql += " AND task_id = ?"
        params = (task_id,)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def list_tasks_for_principal(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    project_ids: list[str],
) -> list[dict[str, Any]]:
    """Tasks visible to a principal: owned by them or inside their projects."""
    clauses = ["principal_id = ?"]
    params: list[Any] = [principal_id]
    if project_ids:
        placeholders = ",".join("?" for _ in project_ids)
        clauses.append(f"project_id IN ({placeholders})")
        params.extend(project_ids)
    sql = f"SELECT * FROM tasks WHERE {' OR '.join(clauses)} ORDER BY rowid DESC LIMIT 200"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def enqueue_inbound(
    conn: sqlite3.Connection,
    *,
    source: str,
    transport_event_id: str,
    payload: dict[str, Any],
    principal_id: str | None = None,
    external_thread: str | None = None,
) -> dict[str, Any]:
    """Persist a raw inbound event exactly once (durable ingress layer).

    Enqueue success means "queued", never business-applied. A repeated
    transport_event_id is recorded as ``duplicate`` and returns the original
    row so callers can ACK safely.
    """
    existing = conn.execute(
        "SELECT * FROM inbound_events WHERE source = ? AND transport_event_id = ?",
        (source, transport_event_id),
    ).fetchone()
    if existing is not None:
        payload_row = dict(existing)
        payload_row["dedupe"] = "duplicate"
        return payload_row
    inbound_id = _new_id("inb")
    conn.execute(
        """
        INSERT INTO inbound_events
          (inbound_id, source, transport_event_id, external_thread, principal_id,
           payload_json, status)
        VALUES (?, ?, ?, ?, ?, ?, 'queued')
        """,
        (
            inbound_id,
            source,
            transport_event_id,
            external_thread,
            principal_id,
            json.dumps(payload, sort_keys=True, ensure_ascii=False),
        ),
    )
    row = conn.execute(
        "SELECT * FROM inbound_events WHERE inbound_id = ?", (inbound_id,)
    ).fetchone()
    payload_row = dict(row)
    payload_row["dedupe"] = "queued"
    return payload_row


def mark_inbound_processed(conn: sqlite3.Connection, inbound_id: str, *, failed: bool = False) -> None:
    status = "failed" if failed else "processed"
    conn.execute(
        """
        UPDATE inbound_events
        SET status = ?, processed_at = CURRENT_TIMESTAMP
        WHERE inbound_id = ? AND status IN ('queued', 'failed')
        """,
        (status, inbound_id),
    )


def enqueue_outbox(
    conn: sqlite3.Connection,
    *,
    aggregate_type: str,
    aggregate_id: str,
    message_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Atomic outbox enqueue (caller commits it with the business write)."""
    outbox_id = _new_id("out")
    conn.execute(
        """
        INSERT INTO outbox
          (outbox_id, aggregate_type, aggregate_id, message_type, payload_json, status)
        VALUES (?, ?, ?, ?, ?, 'pending')
        """,
        (
            outbox_id,
            aggregate_type,
            aggregate_id,
            message_type,
            json.dumps(payload, sort_keys=True, ensure_ascii=False),
        ),
    )
    return dict(conn.execute("SELECT * FROM outbox WHERE outbox_id = ?", (outbox_id,)).fetchone())


DELIVERY_STATES = (
    "queued", "sending", "accepted", "delivered", "retry_wait",
    "delivery_unknown", "failed_permanent", "held", "cancelled",
)


def create_delivery(
    conn: sqlite3.Connection,
    *,
    outbox_id: str,
    channel: str,
    target: str,
    delivery_id: str | None = None,
    channel_account_id: str | None = None,
    platform_thread_id: str | None = None,
    state: str = "queued",
) -> dict[str, Any]:
    row = conn.execute("SELECT outbox_id FROM outbox WHERE outbox_id = ?", (outbox_id,)).fetchone()
    if row is None:
        raise TaskError("invalid_state", f"Unknown outbox entry {outbox_id!r}.", http_status=404)
    if state not in DELIVERY_STATES:
        raise TaskError("schema_violation", f"Invalid delivery state {state!r}.")
    delivery_id = delivery_id or _new_id("dlv")
    conn.execute(
        """
        INSERT INTO deliveries (delivery_id, logical_delivery_id, outbox_id, channel,
                                target, status, state, channel_account_id,
                                platform_thread_id)
        VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)
        """,
        (
            delivery_id, delivery_id, outbox_id, channel, target, state,
            channel_account_id, platform_thread_id,
        ),
    )
    return dict(
        conn.execute("SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)).fetchone()
    )


def mark_delivery(
    conn: sqlite3.Connection,
    delivery_id: str,
    *,
    status: str,
    transport_event_id: str | None = None,
    state: str | None = None,
    error_code: str | None = None,
    last_error: str | None = None,
    next_retry_at: str | None = None,
    increment_attempt: bool = True,
) -> dict[str, Any]:
    """Legacy-compatible delivery update.

    ``status`` stays one of the v11 values for compatibility while ``state``
    carries the explicit transport state. Mapping:
      delivered/accepted -> status delivered
      retry_wait/unknown/failed_permanent/held -> status failed
      queued/sending -> status queued
    """
    if status not in ("queued", "delivered", "failed"):
        raise TaskError("schema_violation", f"Invalid delivery status {status!r}.")
    state = state or {
        "queued": "queued",
        "delivered": "delivered",
        "failed": "failed_permanent",
    }[status]
    if state not in DELIVERY_STATES:
        raise TaskError("schema_violation", f"Invalid delivery state {state!r}.")
    legacy_status = (
        "delivered" if state in ("accepted", "delivered")
        else "queued" if state in ("queued", "sending")
        else "failed"
    )
    assignments = [
        "status = ?",
        "state = ?",
        "transport_event_id = COALESCE(?, transport_event_id)",
    ]
    values: list[Any] = [legacy_status, state, transport_event_id]
    if state in ("accepted", "delivered"):
        assignments.append("delivered_at = CURRENT_TIMESTAMP")
    if state == "accepted":
        assignments.append("accepted_at = CURRENT_TIMESTAMP")
    if error_code is not None:
        assignments.append("error_code = ?")
        values.append(error_code)
    if last_error is not None:
        assignments.append("last_error = ?")
        values.append(last_error[:400])
    if next_retry_at is not None:
        assignments.append("next_retry_at = ?")
        values.append(next_retry_at)
    if increment_attempt:
        assignments.append("attempt_count = attempt_count + 1")
        assignments.append("last_attempt_at = CURRENT_TIMESTAMP")
    values.append(delivery_id)
    conn.execute(
        f"UPDATE deliveries SET {', '.join(assignments)} WHERE delivery_id = ?",
        values,
    )
    return dict(
        conn.execute("SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)).fetchone()
    )


def list_pending_outbox(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM outbox WHERE status = 'pending' ORDER BY rowid LIMIT ?",
            (limit,),
        ).fetchall()
    ]


def record_typed_message(
    conn: sqlite3.Connection,
    *,
    message_kind: str,
    message_type: str,
    payload: dict[str, Any],
    schema_version: str = "typed_message_v1",
    causation_id: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Validate and persist a typed message (command/domain_event/task_result)."""
    envelope = {
        "message_kind": message_kind,
        "message_type": message_type,
        "schema_version": schema_version,
        "payload": payload,
        "causation_id": causation_id,
        "correlation_id": correlation_id,
    }
    try:
        validate_payload_against_schema(envelope, load_named_schema(TYPED_MESSAGE_SCHEMA_NAME))
    except SchemaViolation as exc:
        raise TaskError("schema_violation", str(exc)) from exc
    message_id = _new_id("tmsg")
    conn.execute(
        """
        INSERT INTO typed_messages
          (message_id, message_kind, message_type, payload_json, schema_version,
           causation_id, correlation_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            message_kind,
            message_type,
            json.dumps(payload, sort_keys=True, ensure_ascii=False),
            schema_version,
            causation_id,
            correlation_id,
        ),
    )
    return {"message_id": message_id, **envelope}


def task_to_contract(task: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "task_id": task["task_id"],
        "task_type": task["task_type"],
        "title": task.get("title"),
        "principal_id": task["principal_id"],
        "project_id": task.get("project_id"),
        "status": task["status"],
        "revision": task["revision"],
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }
    validate_payload_against_schema(payload, load_named_schema(TASK_SCHEMA_NAME))
    return payload


def _task_payload(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _run_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    if payload.get("result_json"):
        payload["result"] = json.loads(payload["result_json"])
    return payload


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
