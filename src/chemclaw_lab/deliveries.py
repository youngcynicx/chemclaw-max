"""S06: outbox -> delivery worker with a fault-injectable local adapter.

Generated results and their delivery are decoupled (plan §12.16): business
writes enqueue an outbox message in the SAME transaction; the delivery worker
later turns each pending outbox message into delivery attempts through a
pluggable adapter. Retry consumes the EXISTING result — it never re-triggers
the agent or re-applies a business operation, and it never mints a new message
id (plan §12.13).

``FaultInjectionAdapter`` is the local sender used by S08's failure matrix;
S10 will only add a real platform adapter behind the same interface.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import tasks
from .agents import AgentHostError


class DeliveryAdapter(Protocol):
    """A sender returns a transport event id or raises DeliverySendError."""

    name: str

    def send(self, *, message_type: str, payload: dict[str, Any], target: str) -> str: ...


class DeliverySendError(Exception):
    def __init__(
        self,
        message: str,
        *,
        transient: bool = True,
        uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.transient = transient
        # ``uncertain`` means the request may have reached the platform (for
        # example a timeout after the bytes were written). Such a send can
        # never be retried automatically: the result is delivery_unknown.
        self.uncertain = uncertain


@dataclass
class EchoAdapter:
    """Records sends in memory; always succeeds. Default local adapter."""

    name: str = "echo"
    sent: list[dict[str, Any]] = field(default_factory=list)
    _counter: int = 0

    def send(self, *, message_type: str, payload: dict[str, Any], target: str) -> str:
        self._counter += 1
        transport_event_id = f"echo-{self._counter:06d}"
        self.sent.append(
            {"transport_event_id": transport_event_id, "message_type": message_type,
             "target": target, "payload": payload}
        )
        return transport_event_id


@dataclass
class FaultInjectionAdapter:
    """Local sender with scripted faults for recovery tests.

    ``drop_first_n``: the first N sends per outbox id raise a transient error
    (message lost in transit); ``timeout_first_n``: raise a timeout-shaped
    error AFTER recording the send (receipt lost — the message DID go out).
    """

    name: str = "fault_local"
    inner: EchoAdapter = field(default_factory=EchoAdapter)
    drop_first_n: int = 0
    timeout_first_n: int = 0
    attempts: dict[str, int] = field(default_factory=dict)

    def send(self, *, message_type: str, payload: dict[str, Any], target: str) -> str:
        outbox_key = str(payload.get("outbox_id") or payload.get("trace_id") or message_type)
        count = self.attempts.get(outbox_key, 0) + 1
        self.attempts[outbox_key] = count
        if count <= self.drop_first_n:
            raise DeliverySendError(f"injected drop for {outbox_key} (attempt {count})")
        transport_event_id = self.inner.send(
            message_type=message_type, payload=payload, target=target
        )
        if count <= self.timeout_first_n:
            # Receipt lost: the send happened, the caller never learned the id.
            raise DeliverySendError(
                f"injected timeout after send for {outbox_key} (attempt {count})",
                transient=True,
                uncertain=True,
            )
        return transport_event_id


def _delivery_state(delivery: dict[str, Any]) -> str:
    """Read the explicit state, tolerating pre-v12 rows (status -> state)."""
    state = delivery.get("state")
    if state:
        return str(state)
    if delivery.get("status") == "delivered":
        return "delivered"
    if delivery.get("status") == "failed":
        return "retry_wait" if delivery.get("next_retry_at") else "failed_permanent"
    return "queued"


def _latest_delivery(conn: sqlite3.Connection, outbox_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM deliveries WHERE outbox_id = ? ORDER BY rowid DESC LIMIT 1",
        (outbox_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _next_attempt_no(conn: sqlite3.Connection, delivery_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS n FROM delivery_attempts "
        "WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()
    return int(row["n"])


def _start_attempt(
    conn: sqlite3.Connection,
    delivery_id: str,
    *,
    attempt_no: int,
    state: str = "sending",
) -> str:
    attempt_id = f"att_{uuid.uuid4().hex[:16]}"
    conn.execute(
        """
        INSERT INTO delivery_attempts
          (attempt_id, delivery_id, attempt_no, state, error_code, error_message)
        VALUES (?, ?, ?, ?, NULL, NULL)
        """,
        (attempt_id, delivery_id, attempt_no, state),
    )
    return attempt_id


def _finish_attempt(
    conn: sqlite3.Connection,
    attempt_id: str,
    *,
    state: str,
    transport_event_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE delivery_attempts
        SET state = ?, finished_at = CURRENT_TIMESTAMP,
            transport_event_id = COALESCE(?, transport_event_id),
            error_code = ?, error_message = ?
        WHERE attempt_id = ?
        """,
        (state, transport_event_id, error_code, (error_message or "")[:400], attempt_id),
    )


def process_outbox_once(
    conn: sqlite3.Connection,
    *,
    adapter: DeliveryAdapter,
    limit: int = 20,
    retry_backoff_s: int = 60,
    target_from_payload: str | None = None,
    create_delivery_kwargs: dict[str, Any] | None = None,
    content_gate: Any = None,
    sending_lease_s: int = 300,
    adapter_factory: Any = None,
) -> dict[str, Any]:
    """One worker pass: pending outbox -> ONE logical delivery -> attempts.

    Transport semantics (§R09/F12):
    - one stable delivery row per outbox; every real network call appends a
      ``delivery_attempts`` row;
    - ``sending`` is committed BEFORE the network call; a process that dies
      after that point leaves a recoverable row, never a silent resend;
    - a send whose outcome is unknown becomes ``delivery_unknown`` and is
      never retried automatically;
    - permanent failures and content holds are terminal states, not
      "retry with next_retry_at = NULL";
    - ``accepted``/``delivered`` never re-run B/Alice business logic.
    """
    processed = {
        "attempted": 0,
        "delivered": 0,
        "failed": 0,
        "skipped_retrying": 0,
        "skipped_terminal": 0,
        "unknown": 0,
        "held": 0,
    }
    pending = tasks.list_pending_outbox(conn, limit=limit)
    for outbox in pending:
        payload = json.loads(outbox["payload_json"])
        payload["outbox_id"] = outbox["outbox_id"]
        target = str(outbox["aggregate_id"])
        if target_from_payload and payload.get(target_from_payload):
            target = str(payload[target_from_payload])

        delivery = _latest_delivery(conn, outbox["outbox_id"])

        # Resolve the transport adapter per logical delivery. A single global
        # adapter must never send every account's pending messages (§R09).
        # The delivery row's stored account wins over mutable payload data.
        effective_adapter = adapter
        stored_account_id = (delivery or {}).get("channel_account_id")
        routing_account_id = stored_account_id or payload.get("channel_account_id")
        if adapter_factory is not None and routing_account_id:
            effective_adapter = adapter_factory(routing_account_id)

        if delivery is None:
            delivery_kwargs: dict[str, Any] = dict(create_delivery_kwargs or {})
            if delivery_kwargs.get("channel_account_id") is None:
                delivery_kwargs.setdefault(
                    "channel_account_id", payload.get("channel_account_id")
                )
            if delivery_kwargs.get("platform_thread_id") is None:
                delivery_kwargs.setdefault(
                    "platform_thread_id",
                    payload.get("reply_to_thread") or payload.get("platform_thread_id"),
                )
            delivery_kwargs = {
                key: value for key, value in delivery_kwargs.items() if value is not None
            }
            delivery = tasks.create_delivery(
                conn,
                outbox_id=outbox["outbox_id"],
                channel=effective_adapter.name,
                target=target,
                **delivery_kwargs,
            )
            conn.execute(
                "UPDATE deliveries SET trace_id = ? WHERE delivery_id = ?",
                (outbox.get("trace_id"), delivery["delivery_id"]),
            )
            conn.commit()
        else:
            # The target/account/thread are part of the logical delivery and
            # must not be recomputed from a later global default.
            target = str(delivery.get("target") or target)

        delivery_id = delivery["delivery_id"]
        state = _delivery_state(delivery)

        if state in (
            "delivered", "accepted", "delivery_unknown",
            "failed_permanent", "held", "cancelled",
        ):
            processed["skipped_terminal"] += 1
            continue
        if state == "sending":
            # Crash while a send was in flight. We cannot prove it was not
            # sent, so after the lease we record unknown — never resend.
            row = conn.execute(
                """
                SELECT 1 FROM deliveries
                WHERE delivery_id = ? AND last_attempt_at IS NOT NULL
                  AND datetime(last_attempt_at, ?) > datetime('now')
                """,
                (delivery_id, f"+{int(sending_lease_s)} seconds"),
            ).fetchone()
            if row is not None:
                processed["skipped_terminal"] += 1
                continue
            tasks.mark_delivery(
                conn, delivery_id, status="failed", state="delivery_unknown",
                error_code="sending_interrupted",
                last_error="sending state interrupted; outcome unknown, no auto-resend",
            )
            conn.commit()
            processed["unknown"] += 1
            continue
        if state == "retry_wait" and delivery.get("next_retry_at"):
            due = conn.execute(
                "SELECT 1 WHERE datetime('now') >= ?",
                (delivery["next_retry_at"],),
            ).fetchone()
            if due is None:
                processed["skipped_retrying"] += 1
                continue

        processed["attempted"] += 1
        attempt_no = _next_attempt_no(conn, delivery_id)

        if content_gate is not None:
            gate = content_gate(payload)
            if gate and gate.get("held"):
                attempt_id = _start_attempt(
                    conn, delivery_id, attempt_no=attempt_no, state="held"
                )
                _finish_attempt(
                    conn, attempt_id, state="held",
                    error_code="content_review_held",
                    error_message=f"content review held: {gate.get('reason')}",
                )
                tasks.mark_delivery(
                    conn, delivery_id, status="failed", state="held",
                    error_code="content_review_held",
                    last_error=f"content review held: {gate.get('reason')}",
                )
                conn.commit()
                processed["failed"] += 1
                processed["held"] += 1
                continue

        # Persist the attempt BEFORE the network call. If the process dies
        # here, recovery sees a sending row and conservatively stays unknown.
        attempt_id = _start_attempt(conn, delivery_id, attempt_no=attempt_no)
        tasks.mark_delivery(
            conn, delivery_id, status="failed", state="sending",
            error_code=None, increment_attempt=False,
        )
        conn.commit()

        try:
            transport_event_id = effective_adapter.send(
                message_type=outbox["message_type"], payload=payload,
                target=target,
            )
        except DeliverySendError as exc:
            uncertain = bool(getattr(exc, "uncertain", False))
            if uncertain:
                new_state = "delivery_unknown"
                attempt_state = "unknown"
                error_code = "send_outcome_unknown"
                next_retry = None
                processed["unknown"] += 1
            elif exc.transient:
                new_state = "retry_wait"
                attempt_state = "retryable_failure"
                error_code = "transient_send_failure"
                next_retry = f"+{int(retry_backoff_s)} seconds"
            else:
                new_state = "failed_permanent"
                attempt_state = "failed_permanent"
                error_code = "permanent_send_failure"
                next_retry = None
            _finish_attempt(
                conn, attempt_id, state=attempt_state,
                error_code=error_code, error_message=str(exc),
            )
            tasks.mark_delivery(
                conn, delivery_id, status="failed", state=new_state,
                error_code=error_code, last_error=str(exc),
            )
            if next_retry is not None:
                conn.execute(
                    "UPDATE deliveries SET next_retry_at = datetime('now', ?) "
                    "WHERE delivery_id = ?",
                    (next_retry, delivery_id),
                )
            conn.commit()
            processed["failed"] += 1
            continue
        except Exception as exc:  # adapter bug: retryable, never crash the pass
            _finish_attempt(
                conn, attempt_id, state="retryable_failure",
                error_code="adapter_error",
                error_message=f"{type(exc).__name__}: {exc}",
            )
            tasks.mark_delivery(
                conn, delivery_id, status="failed", state="retry_wait",
                error_code="adapter_error",
                last_error=f"{type(exc).__name__}: {exc}",
            )
            conn.execute(
                "UPDATE deliveries SET next_retry_at = datetime('now', ?) "
                "WHERE delivery_id = ?",
                (f"+{int(retry_backoff_s)} seconds", delivery_id),
            )
            conn.commit()
            processed["failed"] += 1
            continue

        if not transport_event_id:
            # No real platform id: accepted, but no delivery proof. This is
            # explicitly NOT delivered (§R09).
            _finish_attempt(conn, attempt_id, state="accepted")
            tasks.mark_delivery(
                conn, delivery_id, status="delivered", state="accepted",
                error_code="no_transport_receipt",
                last_error="platform accepted without a transport id",
            )
        else:
            _finish_attempt(
                conn, attempt_id, state="delivered",
                transport_event_id=transport_event_id,
            )
            tasks.mark_delivery(
                conn, delivery_id, status="delivered", state="delivered",
                transport_event_id=transport_event_id,
            )
        conn.execute(
            "UPDATE outbox SET status = 'sent', sent_at = CURRENT_TIMESTAMP "
            "WHERE outbox_id = ? AND status = 'pending'",
            (outbox["outbox_id"],),
        )
        conn.commit()
        processed["delivered"] += 1
    return processed


def delivery_trace(conn: sqlite3.Connection, *, trace_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT d.delivery_id, d.outbox_id, d.channel, d.target, d.status,
               d.transport_event_id, d.attempt_count, d.last_error, d.trace_id
        FROM deliveries d WHERE d.trace_id = ? ORDER BY d.rowid
        """,
        (trace_id,),
    ).fetchall()
    return [dict(row) for row in rows]
