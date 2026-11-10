from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import replace
from pathlib import Path
from sqlite3 import Row
from typing import Any

from .db import connect
from .models import AuditDecision, InventoryEvent
from .policy import POLICY_VERSION, evaluate_inventory_event
from .units import UNIT_ALIASES, convert_amount, normalize_unit
from .validation import INVENTORY_EVENT_SCHEMA_VERSION, validate_inventory_event


_UNIT_TOKEN_PATTERN = "|".join(
    re.escape(token) for token in sorted(UNIT_ALIASES, key=len, reverse=True)
)
_NUMERIC_TOKEN_PATTERN = (
    r"[+-]?(?:"
    r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"|\d+,\d+"
    r"|\.\d+"
    r")(?:e[+-]?\d+)?"
)
_STANDALONE_NUMERIC_PATTERN = re.compile(
    rf"(?<![a-z0-9_.,+\-]){_NUMERIC_TOKEN_PATTERN}(?![a-z0-9_.,])",
    re.IGNORECASE,
)
_ALLOWED_CORRECTIONS = {
    "item_id",
    "amount",
    "unit",
    "transaction_type",
    "project_id",
    "user_id",
}


def process_inventory_message(
    db_path: str | Path,
    raw_text: str,
    *,
    source: str = "cli",
    source_message_id: str | None = None,
    user_id: str | None = None,
    project_id: str | None = None,
    apply_approved: bool = True,
) -> dict[str, Any]:
    """Parse, audit, persist, and optionally apply one inventory message."""
    if not raw_text.strip():
        raise ValueError("raw_text must not be empty")

    conn = connect(db_path)
    try:
        result = process_inventory_message_conn(
            conn,
            raw_text,
            source=source,
            source_message_id=source_message_id,
            user_id=user_id,
            project_id=project_id,
            apply_approved=apply_approved,
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def process_inventory_message_conn(
    conn: sqlite3.Connection,
    raw_text: str,
    *,
    source: str = "cli",
    source_message_id: str | None = None,
    user_id: str | None = None,
    project_id: str | None = None,
    apply_approved: bool = True,
) -> dict[str, Any]:
    """Same contract as ``process_inventory_message`` on a caller-owned
    connection and transaction (S02 capability entry composes this with
    authorization, expected-revision checks, and idempotency in ONE commit)."""
    if not raw_text.strip():
        raise ValueError("raw_text must not be empty")

    message_id = _new_id("msg")
    if source_message_id is not None:
        existing = _message_by_source_identity(conn, source, source_message_id)
        if existing is not None:
            return _result_from_message(conn, existing["message_id"], idempotent_replay=True)

    try:
        conn.execute(
            """
            INSERT INTO message_logs
              (message_id, source, source_message_id, raw_text, parsed_intent)
            VALUES (?, ?, ?, ?, ?)
            """,
            (message_id, source, source_message_id, raw_text, "inventory_update"),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = _message_by_source_identity(conn, source, source_message_id)
        if existing is not None:
            return _result_from_message(conn, existing["message_id"], idempotent_replay=True)
        raise

    chemicals = _fetch_chemicals(conn)
    parsed_event = parse_inventory_event(raw_text, chemicals)
    _validate_optional_identity(conn, user_id, project_id)
    proposed_event = replace(
        parsed_event,
        user_id=user_id,
        project_id=project_id,
    )
    event_id = persist_inventory_event(
        conn,
        message_id,
        proposed_event,
        apply_approved=apply_approved,
    )
    return _result_from_event(conn, event_id)


def persist_inventory_event(
    conn: sqlite3.Connection,
    message_id: str,
    proposed_event: InventoryEvent,
    *,
    apply_approved: bool = True,
    user_confirmed: bool = False,
    supersedes_event_id: str | None = None,
    confirmed_by: str | None = None,
    event_id: str | None = None,
) -> str:
    """Validate, audit, persist, and optionally apply a structured proposal.

    The caller owns the surrounding database transaction. Gateways normally use
    ``process_inventory_message``; administrative importers can use this function
    to preserve the same event/policy/write invariants for structured input.
    ``event_id`` lets the cross-domain subtask path reuse the source event id so
    the B-side transaction stays queryable by it (S07 §13.6).
    """
    chemical = _chemical_by_id(_fetch_chemicals(conn), proposed_event.item_id)
    audit = evaluate_inventory_event(
        proposed_event,
        chemical,
        user_confirmed=user_confirmed,
    )
    event = replace(
        proposed_event,
        requires_confirmation=audit.decision == "pending_confirmation",
    )
    validate_inventory_event(event)

    event_id = event_id or _new_id("evt")
    _insert_event_proposal(
        conn,
        event_id,
        message_id,
        event,
        _status_for_decision(audit.decision),
        supersedes_event_id=supersedes_event_id,
        confirmed_by=confirmed_by,
    )
    _insert_audit(conn, message_id, event_id, event.event_type, audit)
    if apply_approved and audit.decision == "approve":
        apply_inventory_event(
            conn,
            message_id,
            event_id,
            event,
            confirmation_status="confirmed" if user_confirmed else "approved",
        )
    return event_id


def resolve_inventory_event(
    db_path: str | Path,
    event_id: str,
    *,
    approve: bool,
    corrections: dict[str, Any] | None = None,
    confirmed_by: str = "cli_user",
) -> dict[str, Any]:
    """Confirm, correct, or reject a pending/approved proposal.

    Approval creates a new immutable event revision. The original proposal is
    marked as superseded, which keeps the complete confirmation evidence chain.
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        original = _event_row(conn, event_id)
        if original is None:
            raise ValueError(f"Unknown event_id: {event_id}")

        if original["status"] == "applied":
            result = _result_from_event(conn, event_id)
            result["confirmation_replay"] = True
            return result
        if original["status"] == "superseded":
            revision = conn.execute(
                """
                SELECT event_id
                FROM event_proposals
                WHERE supersedes_event_id = ?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (event_id,),
            ).fetchone()
            if revision is None:
                raise ValueError(f"Event {event_id} has no persisted revision")
            result = _result_from_event(conn, revision["event_id"])
            result["confirmation_replay"] = True
            result["supersedes_event_id"] = event_id
            return result
        if original["status"] == "rejected":
            result = _result_from_event(conn, event_id)
            result["confirmation_replay"] = True
            return result

        if not approve:
            if original["status"] != "rejected":
                conn.execute(
                    """
                    UPDATE event_proposals
                    SET status = 'rejected', confirmed_by = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE event_id = ?
                    """,
                    (confirmed_by, event_id),
                )
                rejection = AuditDecision(
                    "reject",
                    "low",
                    "User rejected the proposed inventory event.",
                    "no_action",
                )
                _insert_audit(
                    conn,
                    original["message_id"],
                    event_id,
                    original["event_type"],
                    rejection,
                )
                conn.commit()
            return _result_from_event(conn, event_id)

        payload = json.loads(original["payload_json"])
        event = _event_with_corrections(
            conn,
            InventoryEvent.from_payload(payload),
            corrections or {},
        )
        chemical = _chemical_by_id(_fetch_chemicals(conn), event.item_id)
        audit = evaluate_inventory_event(event, chemical, user_confirmed=True)
        revised_event = replace(
            event,
            requires_confirmation=audit.decision == "pending_confirmation",
        )
        validate_inventory_event(revised_event)

        revised_event_id = _new_id("evt")
        conn.execute(
            """
            UPDATE event_proposals
            SET status = 'superseded', confirmed_by = ?, updated_at = CURRENT_TIMESTAMP
            WHERE event_id = ?
            """,
            (confirmed_by, event_id),
        )
        _insert_event_proposal(
            conn,
            revised_event_id,
            original["message_id"],
            revised_event,
            _status_for_decision(audit.decision),
            supersedes_event_id=event_id,
            confirmed_by=confirmed_by,
        )
        _insert_audit(
            conn,
            original["message_id"],
            revised_event_id,
            revised_event.event_type,
            audit,
        )
        if audit.decision == "approve":
            apply_inventory_event(
                conn,
                original["message_id"],
                revised_event_id,
                revised_event,
                confirmation_status="confirmed",
            )
        conn.commit()
        result = _result_from_event(conn, revised_event_id)
        result["supersedes_event_id"] = event_id
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def parse_inventory_event(raw_text: str, chemicals: list[Row]) -> InventoryEvent:
    transaction_type = _detect_transaction_type(raw_text)
    amount, unit = _extract_amount_and_unit(raw_text)
    ambiguous_quantity = _has_ambiguous_quantity(raw_text)
    if ambiguous_quantity:
        amount = None
        unit = unit or _extract_unit(raw_text)
    matches = _match_chemicals(raw_text, chemicals)
    semantic_confirmation = _message_requires_confirmation(raw_text)

    item = matches[0] if len(matches) == 1 else None
    confidence = 0.92
    if transaction_type == "unknown":
        confidence -= 0.35
    if amount is None:
        confidence -= 0.25
    if unit is None:
        confidence -= 0.2
    if len(matches) != 1:
        confidence -= 0.3

    return InventoryEvent(
        event_type="inventory_update",
        event_subtype=transaction_type,
        item_type="chemical",
        item_id=item["chemical_id"] if item is not None else None,
        item_name=item["name"] if item is not None else None,
        amount=amount,
        unit=unit,
        transaction_type=transaction_type,
        confidence=round(max(confidence, 0.0), 2),
        requires_confirmation=(
            semantic_confirmation
            or ambiguous_quantity
            or transaction_type == "unknown"
            or amount is None
            or unit is None
            or len(matches) != 1
        ),
        raw_text=raw_text,
    )


def audit_inventory_event(event: InventoryEvent, chemicals: list[Row]) -> AuditDecision:
    """Compatibility wrapper around the independent v0.2 policy engine."""
    return evaluate_inventory_event(event, _chemical_by_id(chemicals, event.item_id))


def apply_inventory_event(
    conn: sqlite3.Connection,
    message_id: str,
    event_id: str,
    event: InventoryEvent,
    *,
    confirmation_status: str = "approved",
) -> str:
    existing = conn.execute(
        "SELECT transaction_id FROM inventory_transactions WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if existing is not None:
        return existing["transaction_id"]

    chemical = conn.execute(
        "SELECT amount, unit FROM chemicals WHERE chemical_id = ?",
        (event.item_id,),
    ).fetchone()
    if chemical is None or event.amount is None or event.unit is None:
        raise ValueError("Cannot apply incomplete inventory event")

    current_amount = float(chemical["amount"])
    normalized_amount = convert_amount(event.amount, event.unit, chemical["unit"])
    if event.transaction_type == "use":
        amount_delta = -normalized_amount
    elif event.transaction_type == "add":
        amount_delta = normalized_amount
    elif event.transaction_type == "adjust":
        amount_delta = normalized_amount - current_amount
    elif event.transaction_type == "opening_balance":
        if abs(current_amount) > 1e-12:
            raise ValueError("Opening balance requires zero current stock")
        amount_delta = normalized_amount
    else:
        raise ValueError(f"Unsupported transaction type: {event.transaction_type}")

    if current_amount + amount_delta < 0:
        raise ValueError("Inventory transaction would make stock negative")

    transaction_id = _new_id("txn")
    conn.execute(
        """
        INSERT INTO inventory_transactions
          (transaction_id, message_id, event_id, chemical_id, user_id, project_id,
           transaction_type, amount_delta, unit, confidence, confirmation_status, raw_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            transaction_id,
            message_id,
            event_id,
            event.item_id,
            event.user_id,
            event.project_id,
            event.transaction_type,
            amount_delta,
            chemical["unit"],
            event.confidence,
            confirmation_status,
            event.raw_text,
        ),
    )
    # S07 §13.3: the balance carries its own monotonic revision, bumped in the
    # same transaction as the delta. updated_at is metadata, never a version.
    updated = conn.execute(
        """
        UPDATE chemicals
        SET amount = amount + ?, updated_at = CURRENT_TIMESTAMP,
            revision = revision + 1
        WHERE chemical_id = ? AND amount + ? >= 0
        """,
        (amount_delta, event.item_id, amount_delta),
    )
    if updated.rowcount != 1:
        raise ValueError("Inventory changed concurrently or would become negative")
    conn.execute(
        """
        UPDATE event_proposals
        SET status = 'applied', updated_at = CURRENT_TIMESTAMP
        WHERE event_id = ?
        """,
        (event_id,),
    )
    return transaction_id


def list_inventory_events(
    db_path: str | Path,
    *,
    status: str | None = None,
) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        sql = """
            SELECT event_id, message_id, event_type, event_subtype, status,
                   supersedes_event_id, confirmed_by, created_at, updated_at
            FROM event_proposals
        """
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY rowid DESC"
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def result_to_json(result: dict[str, Any] | list[dict[str, Any]]) -> str:
    return json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False)


def _event_with_corrections(
    conn: sqlite3.Connection,
    event: InventoryEvent,
    corrections: dict[str, Any],
) -> InventoryEvent:
    unknown = set(corrections) - _ALLOWED_CORRECTIONS
    if unknown:
        raise ValueError(f"Unsupported event corrections: {sorted(unknown)}")

    values = event.to_payload()
    values.update({key: value for key, value in corrections.items() if value is not None})
    if "unit" in corrections:
        values["unit"] = normalize_unit(corrections["unit"])
    if "amount" in corrections and corrections["amount"] is not None:
        values["amount"] = float(corrections["amount"])
    if "transaction_type" in corrections:
        values["event_subtype"] = corrections["transaction_type"]
    if "item_id" in corrections:
        chemical = conn.execute(
            "SELECT name FROM chemicals WHERE chemical_id = ?",
            (corrections["item_id"],),
        ).fetchone()
        values["item_name"] = chemical["name"] if chemical is not None else None
    if values.get("project_id") is not None:
        project = conn.execute(
            "SELECT 1 FROM projects WHERE project_id = ?",
            (values["project_id"],),
        ).fetchone()
        if project is None:
            raise ValueError(f'Unknown project_id: {values["project_id"]}')
    if values.get("user_id") is not None:
        user = conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?",
            (values["user_id"],),
        ).fetchone()
        if user is None:
            raise ValueError(f'Unknown user_id: {values["user_id"]}')
    return InventoryEvent.from_payload(values)


def _validate_optional_identity(
    conn: sqlite3.Connection,
    user_id: str | None,
    project_id: str | None,
) -> None:
    if user_id is not None:
        user = conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if user is None:
            raise ValueError(f"Unknown user_id: {user_id}")
    if project_id is not None:
        project = conn.execute(
            "SELECT 1 FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if project is None:
            raise ValueError(f"Unknown project_id: {project_id}")


def _insert_event_proposal(
    conn: sqlite3.Connection,
    event_id: str,
    message_id: str,
    event: InventoryEvent,
    status: str,
    *,
    supersedes_event_id: str | None = None,
    confirmed_by: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO event_proposals
          (event_id, message_id, schema_version, event_type, event_subtype,
           payload_json, status, policy_version, supersedes_event_id, confirmed_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            message_id,
            INVENTORY_EVENT_SCHEMA_VERSION,
            event.event_type,
            event.event_subtype,
            json.dumps(event.to_payload(), sort_keys=True, ensure_ascii=False),
            status,
            POLICY_VERSION,
            supersedes_event_id,
            confirmed_by,
        ),
    )


def _insert_audit(
    conn: sqlite3.Connection,
    message_id: str,
    event_id: str,
    event_type: str,
    audit: AuditDecision,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_logs
          (audit_id, message_id, event_id, event_type, decision,
           risk_level, reason, required_action)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _new_id("aud"),
            message_id,
            event_id,
            event_type,
            audit.decision,
            audit.risk_level,
            audit.reason,
            audit.required_action,
        ),
    )


def _result_from_message(
    conn: sqlite3.Connection,
    message_id: str,
    *,
    idempotent_replay: bool,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT event_id
        FROM event_proposals
        WHERE message_id = ?
        ORDER BY rowid DESC
        LIMIT 1
        """,
        (message_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Message {message_id} has no persisted event")
    return _result_from_event(conn, row["event_id"], idempotent_replay=idempotent_replay)


def _result_from_event(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    idempotent_replay: bool = False,
) -> dict[str, Any]:
    row = _event_row(conn, event_id)
    if row is None:
        raise ValueError(f"Unknown event_id: {event_id}")
    audit = conn.execute(
        """
        SELECT decision, risk_level, reason, required_action
        FROM audit_logs
        WHERE event_id = ?
        ORDER BY rowid DESC
        LIMIT 1
        """,
        (event_id,),
    ).fetchone()
    transaction = conn.execute(
        "SELECT transaction_id FROM inventory_transactions WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    return {
        "message_id": row["message_id"],
        "event_id": event_id,
        "event": json.loads(row["payload_json"]),
        "status": row["status"],
        "audit": dict(audit) if audit is not None else None,
        "applied": transaction is not None,
        "transaction_id": transaction["transaction_id"] if transaction is not None else None,
        "idempotent_replay": idempotent_replay,
    }


def _event_row(conn: sqlite3.Connection, event_id: str) -> Row | None:
    return conn.execute(
        "SELECT * FROM event_proposals WHERE event_id = ?",
        (event_id,),
    ).fetchone()


def _message_by_source_identity(
    conn: sqlite3.Connection,
    source: str,
    source_message_id: str | None,
) -> Row | None:
    if source_message_id is None:
        return None
    return conn.execute(
        """
        SELECT message_id
        FROM message_logs
        WHERE source = ? AND source_message_id = ?
        """,
        (source, source_message_id),
    ).fetchone()


def _fetch_chemicals(conn: sqlite3.Connection) -> list[Row]:
    return conn.execute(
        """
        SELECT chemical_id, name, formula, amount, unit, threshold,
               hazard_level, max_auto_delta, revision
        FROM chemicals
        ORDER BY length(name) DESC
        """
    ).fetchall()


def _status_for_decision(decision: str) -> str:
    return {
        "approve": "approved",
        "pending_confirmation": "pending_confirmation",
        "reject": "rejected",
    }[decision]


def _detect_transaction_type(raw_text: str) -> str:
    text = raw_text.lower()
    if re.search(r"\b(used|use|consumed|spent)\b|用了|使用|消耗", text):
        return "use"
    if re.search(r"\b(added|add|received|restocked)\b|添加|加入|补充|新增", text):
        return "add"
    if re.search(r"\b(left|remaining|remains|set|adjusted)\b|剩余|还剩|调整", text):
        return "adjust"
    return "unknown"


def _message_requires_confirmation(raw_text: str) -> bool:
    text = raw_text.casefold().replace("’", "'")
    chinese_action = r"(?:使用|消耗|添加|加入|补充|新增|调整)"
    patterns = [
        r"[?？]",
        r"\b(?:not|never|without)\b",
        r"\b[a-z]+n't\b|\bcannot\b",
        (
            r"\b(?:deny|denied|denies|doubt|doubted|doubts|unclear|unsure|"
            r"uncertain|dispute|disputed)\b"
        ),
        (
            r"\b(?:plan(?:ning)? to|going to|want to|intend to|expect to|"
            r"hope to|will|tomorrow|later|next week|maybe|perhaps|possibly|"
            r"might)\b"
        ),
        (
            r"\b(?:i|we|you|they|he|she)\s+(?:may|might)\s+"
            r"(?:have\s+)?(?:use|used|consume|consumed|add|added|adjust|adjusted)\b"
        ),
        r"\b(?:if|unless|in case|provided that)\b",
        r"^\s*(?:did|do|does|have|has|can|could|may|might|should|would|will)\b",
        r"\b(?:use|consume|spend|add|receive|restock|adjust|set)\b",
        rf"(?:不|没|未|别|勿|禁|无).{{0,4}}{chinese_action}",
        (
            r"没有使用|没用|未使用|还没|不要|请勿|不能|不可|禁止|计划|准备|"
            r"打算|将要|想要|希望|预计|可能|也许|如果|若|假如|万一|明天|"
            r"稍后|能否|是否|有没有|别|勿|严禁|不得|不准|暂不|不会|"
            r"无需|无须|拒绝|否认|并非|避免|吗\s*$"
        ),
    ]
    if any(re.search(pattern, text) for pattern in patterns):
        return True
    if not re.search(chinese_action, text):
        return False
    completed = (
        re.search(rf"(?:已|已经|刚|刚刚|刚才)\s*{chinese_action}", text)
        or re.search(rf"{chinese_action}\s*了", text)
        or re.search(rf"{chinese_action}.{{0,3}}(?:完成|完毕)", text)
    )
    return completed is None


def _has_ambiguous_quantity(raw_text: str) -> bool:
    text = raw_text.casefold()
    return bool(
        len(_STANDALONE_NUMERIC_PATTERN.findall(text)) > 1
        or re.search(r"\bplus\s+or\s+minus\b|[±/…]", text)
        or re.search(
            r"(?<![a-z0-9.])\d+(?:\.\d+)?\s*[-–—~～]\s*\d",
            text,
        )
        or re.search(
            r"(?<![a-z0-9.])\d+(?:\.\d+)?\s*(?:to|through|or)\s*\d",
            text,
        )
        or re.search(
            r"\bbetween\s+\d+(?:\.\d+)?\s+and\s+\d",
            text,
        )
        or re.search(r"\b(less than|more than|at least|at most)\b", text)
        or re.search(r"[<>≤≥]\s*\d", text)
        or re.search(
            r"(?<![a-z0-9.])\d+(?:\.\d+)?\s*(?:到|至|或)\s*\d",
            text,
        )
        or re.search(r"(少于|多于|超过|至少|至多)\s*\d", text)
    )


def _extract_amount_and_unit(raw_text: str) -> tuple[float | None, str | None]:
    with_unit = re.search(
        rf"(?<![a-z0-9_.,+\-])(?P<amount>{_NUMERIC_TOKEN_PATTERN})"
        rf"(?![a-z0-9_.,])\s*(?P<unit>{_UNIT_TOKEN_PATTERN})(?![a-z])",
        raw_text,
        re.IGNORECASE,
    )
    if with_unit is not None:
        amount = _parse_numeric_token(with_unit.group("amount"))
        return amount, normalize_unit(with_unit.group("unit"))

    standalone = re.search(
        rf"(?<![a-z0-9_.,+\-])(?P<amount>{_NUMERIC_TOKEN_PATTERN})"
        r"(?![a-z0-9_.,])",
        raw_text,
        re.IGNORECASE,
    )
    if standalone is None:
        return None, _extract_unit(raw_text)
    return _parse_numeric_token(standalone.group("amount")), None


def _extract_unit(raw_text: str) -> str | None:
    match = re.search(
        rf"(?<![a-z])(?P<unit>{_UNIT_TOKEN_PATTERN})(?![a-z])",
        raw_text,
        re.IGNORECASE,
    )
    return normalize_unit(match.group("unit")) if match is not None else None


def _parse_numeric_token(raw_value: str) -> float:
    value = raw_value.casefold()
    exponent = ""
    exponent_match = re.search(r"e[+-]?\d+$", value)
    if exponent_match is not None:
        exponent = exponent_match.group(0)
        value = value[: exponent_match.start()]
    if "," in value and "." in value:
        if not re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+\.\d+", value):
            raise ValueError(f"Unsupported numeric format: {raw_value}")
        value = value.replace(",", "")
    elif "," in value:
        if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+", value):
            value = value.replace(",", "")
        elif value.count(",") == 1:
            value = value.replace(",", ".")
        else:
            raise ValueError(f"Unsupported numeric format: {raw_value}")
    return float(f"{value}{exponent}")


def _match_chemicals(raw_text: str, chemicals: list[Row]) -> list[Row]:
    text = raw_text.lower()
    matches = []
    for chemical in chemicals:
        names = [chemical["name"], chemical["formula"]]
        if any(
            name
            and re.search(
                rf"(?<![a-z0-9]){re.escape(name.lower())}(?![a-z0-9])",
                text,
            )
            for name in names
        ):
            matches.append(chemical)
    return matches


def _chemical_by_id(chemicals: list[Row], chemical_id: str | None) -> Row | None:
    if chemical_id is None:
        return None
    for chemical in chemicals:
        if chemical["chemical_id"] == chemical_id:
            return chemical
    return None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# S07: cross-domain inventory subtasks (plan §13)
# ---------------------------------------------------------------------------
#
# B-domain memory events reach the inventory domain as persisted subtasks,
# never as LLM chat. The pipeline is deterministic end to end:
#
#   memory proposal apply (experiments.py)
#     -> outbox message `inventory_proposal` (only event_kind=operation with
#        an amount; plans/computed never enqueue, historical imports are
#        rejected at ingest with a traceable row)
#     -> ingest_inventory_proposal: identity + dedup + stock-revision snapshot
#     -> execute_inventory_subtask (alice_inventory): structured event through
#        the same audit/persist/apply path as every other inventory write
#     -> adjustment proposals for corrected usages (§13.8)
#
# "Record saved, inventory pending" (§13.7) is a normal state: the B-domain
# event is never rolled back because the inventory step failed.

_SUBTASK_SOURCES = frozenset({"cross_domain_subtask"})


def _event_head(conn: sqlite3.Connection, event_id: str) -> Row | None:
    return conn.execute(
        """
        SELECT * FROM event_revisions
        WHERE entity_type = 'experiment_event' AND entity_id = ?
        ORDER BY revision DESC LIMIT 1
        """,
        (event_id,),
    ).fetchone()


def _subtask_row(conn: sqlite3.Connection, subtask_id: str) -> Row:
    row = conn.execute(
        "SELECT * FROM inventory_subtasks WHERE subtask_id = ?",
        (subtask_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown inventory subtask: {subtask_id}")
    return row


def _subtask_result(row: Row, *, replay: bool = False) -> dict[str, Any]:
    return {
        "subtask_id": row["subtask_id"],
        "source_event_id": row["source_event_id"],
        "evidence_id": row["evidence_id"],
        "parent_task_id": row["parent_task_id"],
        "chemical_id": row["chemical_id"],
        "transaction_type": row["transaction_type"],
        "amount": row["amount"],
        "unit": row["unit"],
        "amount_delta": row["amount_delta"],
        "status": row["status"],
        "expected_revision": row["expected_revision"],
        "inventory_transaction_id": row["inventory_transaction_id"],
        "event_id": row["event_id"],
        "trace_id": row["trace_id"],
        "error_code": row["error_code"],
        "attempts": row["attempts"],
        "idempotent_replay": replay,
    }


def _chemical_candidates(raw_text: str, chemicals: list[Row]) -> list[Row]:
    return _match_chemicals(raw_text or "", chemicals)


def _historical_import(head: Row) -> bool:
    fields = json.loads(head["field_json"] or "{}")
    return bool(fields.get("historical_import"))


def _occurred_at_start(head: Row) -> str | None:
    return head["event_time_start"]


def _evidence_ref_id(head: Row) -> str | None:
    """Best evidence pointer from the B-side event revision (S07 §13.6)."""
    try:
        refs = json.loads(head["evidence_refs_json"] or "[]") or []
    except (TypeError, json.JSONDecodeError):
        refs = []
    first = refs[0] if refs else {}
    return first.get("evidence_id") or first.get("page_id") or first.get("block_id")


def ingest_inventory_proposal(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Consume one ``inventory_proposal`` outbox message into a subtask row.

    Deterministic by design (plan §13.3-13.5): chemical identity comes from
    catalog matching over the raw text, units must be dimension-compatible
    with the stock unit (no volume<->mass derivation without an authorized
    condition), and historical imports never touch current stock. Ambiguity
    becomes ``clarification`` — never a guessed write. Re-ingesting the same
    message replays the existing subtask (§13.4 idempotency).
    """
    event_id = payload.get("event_id")
    if not event_id:
        raise ValueError("inventory_proposal payload requires event_id")
    amount = payload.get("amount")
    unit = payload.get("unit")
    raw_text = payload.get("raw_text") or ""
    if amount is None:
        raise ValueError(f"inventory_proposal {event_id}: missing amount")

    head = _event_head(conn, event_id)
    if head is None:
        raise ValueError(f"Unknown experiment event: {event_id}")
    fields = json.loads(head["field_json"] or "{}")
    if fields.get("event_kind") != "operation":
        raise ValueError(
            f"Event {event_id} is {fields.get('event_kind')!r}; only operations reach inventory"
        )

    # historical import: default is facts-only bookkeeping (§13.3). We keep a
    # traceable rejected row instead of silently skipping.
    if _historical_import(head):
        subtask_id = _new_id("sub")
        conn.execute(
            """
            INSERT INTO inventory_subtasks
              (subtask_id, parent_task_id, project_id, source_event_id,
               source_event_revision, evidence_id, chemical_id, transaction_type,
               amount, unit, amount_delta, dedup_key, status, expected_revision,
               trace_id, error_code)
            VALUES (?, ?, ?, ?, ?, ?, NULL, 'use', ?, ?, 0.0, ?, 'rejected',
                    NULL, ?, 'historical_import')
            """,
            (
                subtask_id,
                payload.get("task_id"),
                json.loads(head["field_json"] or "{}").get("project_id"),
                event_id,
                head["revision"],
                _evidence_ref_id(head),
                float(amount),
                normalize_unit(unit) or unit or "unknown",
                f"consumption:{event_id}:historical",
                trace_id,
            ),
        )
        return _subtask_result(
            conn.execute(
                "SELECT * FROM inventory_subtasks WHERE subtask_id = ?", (subtask_id,)
            ).fetchone()
        )

    chemicals = _fetch_chemicals(conn)
    matches = _chemical_candidates(raw_text, chemicals)
    if len(matches) != 1:
        # Ambiguous or unknown identity: keep the operation pending with the
        # candidate list attached; the human resolves it via catalog (§13.2).
        subtask_id = _new_id("sub")
        dedup_key = f"consumption:{event_id}:ambiguous" if not matches else f"consumption:{event_id}:ambiguous"
        conn.execute(
            """
            INSERT INTO inventory_subtasks
              (subtask_id, parent_task_id, project_id, source_event_id,
               source_event_revision, evidence_id, chemical_id, transaction_type,
               amount, unit, amount_delta, dedup_key, status, expected_revision,
               trace_id, error_code, last_error)
            VALUES (?, ?, ?, ?, ?, ?, NULL, 'use', ?, ?, 0.0, ?,
                    'clarification', NULL, ?, 'chemical_identity',
                    ?)
            """,
            (
                subtask_id,
                payload.get("task_id"),
                json.loads(head["field_json"] or "{}").get("project_id"),
                event_id,
                head["revision"],
                _evidence_ref_id(head),
                float(amount),
                normalize_unit(unit) or unit or "unknown",
                dedup_key,
                trace_id,
                json.dumps(
                    {
                        "candidates": [
                            {"chemical_id": c["chemical_id"], "name": c["name"]}
                            for c in matches
                        ],
                        "matched": len(matches),
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        return _subtask_result(
            conn.execute(
                "SELECT * FROM inventory_subtasks WHERE subtask_id = ?", (subtask_id,)
            ).fetchone()
        )

    chemical = matches[0]
    dedup_key = f"consumption:{event_id}:{chemical['chemical_id']}"
    existing = conn.execute(
        "SELECT * FROM inventory_subtasks WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    if existing is not None:
        # Same source event + same chemical reported again (duplicate photo,
        # duplicate message): exactly one consumption (§13.4).
        return _subtask_result(existing, replay=True)

    stock_unit = chemical["unit"]
    # Missing/unknown dimension: "unknown" never converts, so the subtask
    # lands in unit_conversion_needed clarification instead of crashing the
    # bridge (S08 live finding: models may omit fields.unit).
    normalized = normalize_unit(unit) or unit or "unknown"
    try:
        stock_amount = convert_amount(float(amount), normalized, stock_unit)
    except (ValueError, TypeError):
        # Dimension mismatch (e.g. mL for a solid stocked in g): no automatic
        # volume<->mass derivation without an authorized condition (§13.5).
        subtask_id = _new_id("sub")
        conn.execute(
            """
            INSERT INTO inventory_subtasks
              (subtask_id, parent_task_id, project_id, source_event_id,
               source_event_revision, evidence_id, chemical_id, transaction_type,
               amount, unit, amount_delta, dedup_key, status, expected_revision,
               trace_id, error_code, last_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'use', ?, ?, 0.0, ?, 'clarification',
                    NULL, ?, 'unit_conversion_needed', ?)
            """,
            (
                subtask_id,
                payload.get("task_id"),
                json.loads(head["field_json"] or "{}").get("project_id"),
                event_id,
                head["revision"],
                _evidence_ref_id(head),
                chemical["chemical_id"],
                float(amount),
                normalized,
                dedup_key,
                trace_id,
                json.dumps(
                    {"stock_unit": stock_unit, "event_unit": normalized},
                    ensure_ascii=False,
                ),
            ),
        )
        return _subtask_result(
            conn.execute(
                "SELECT * FROM inventory_subtasks WHERE subtask_id = ?", (subtask_id,)
            ).fetchone()
        )

    delta = -stock_amount  # operations that reach inventory are usages
    status = "pending"
    error_code = None
    # Same operation reported through two channels (photo page + chat message)
    # produces two events with identical facts (§13.4): identical chemical,
    # identical amount and unit, and the SAME non-null occurred_at. When the
    # event time is unknown the operation cannot be compared, so it stays a
    # normal pending subtask instead of a guessed duplicate.
    this_time = _occurred_at_start(head)
    duplicate = None
    if this_time is not None:
        duplicate = conn.execute(
            """
            SELECT s.subtask_id FROM inventory_subtasks s
            JOIN event_revisions h
              ON h.entity_type = 'experiment_event'
             AND h.entity_id = s.source_event_id
             AND h.revision = s.source_event_revision
            WHERE s.chemical_id = ? AND s.amount = ? AND s.unit = ?
              AND s.transaction_type = 'use'
              AND s.status IN ('pending', 'applied')
              AND s.source_event_id != ?
              AND h.event_time_start IS NOT NULL
              AND h.event_time_start = ?
            LIMIT 1
            """,
            (chemical["chemical_id"], float(amount), normalized, event_id, this_time),
        ).fetchone()
    if duplicate is not None:
        status = "duplicate_suspect"
        error_code = f"duplicate_of:{duplicate['subtask_id']}"

    subtask_id = _new_id("sub")
    conn.execute(
        """
        INSERT INTO inventory_subtasks
          (subtask_id, parent_task_id, project_id, source_event_id,
           source_event_revision, evidence_id, chemical_id, transaction_type,
           amount, unit, amount_delta, dedup_key, status, expected_revision,
           trace_id, error_code)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'use', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            subtask_id,
            payload.get("task_id"),
            payload.get("project_id")
            or json.loads(head["field_json"] or "{}").get("project_id"),
            event_id,
            head["revision"],
            _evidence_ref_id(head),
            chemical["chemical_id"],
            float(amount),
            normalized,
            delta,
            dedup_key,
            status,
            chemical["revision"],
            trace_id,
            error_code,
        ),
    )
    return _subtask_result(
        conn.execute(
            "SELECT * FROM inventory_subtasks WHERE subtask_id = ?", (subtask_id,)
        ).fetchone()
    )


def process_inventory_inbound(conn: sqlite3.Connection) -> dict[str, Any]:
    """Ingest every sent ``inventory_proposal`` outbox message not yet consumed.

    Idempotent: re-running replays existing subtasks via the dedup key. This
    is the cross-domain bridge worker; deliveries already proved transport.
    """
    rows = conn.execute(
        """
        SELECT outbox_id, payload_json, trace_id FROM outbox
        WHERE message_type = 'inventory_proposal' AND status = 'sent'
        ORDER BY created_at, outbox_id
        """
    ).fetchall()
    processed: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        payload.setdefault("outbox_id", row["outbox_id"])
        payload.setdefault("task_id", payload.get("task_id"))
        processed.append(
            ingest_inventory_proposal(conn, payload, trace_id=row["trace_id"])
        )
    return {"scanned": len(rows), "subtasks": processed}


def resolve_inventory_subtask(
    conn: sqlite3.Connection,
    subtask_id: str,
    *,
    chemical_id: str | None = None,
    unit: str | None = None,
    amount: float | None = None,
    confirm_duplicate: bool = False,
    confirmed_by: str | None = None,
) -> dict[str, Any]:
    """Human resolution for clarification / duplicate_suspect subtasks.

    Identity or unit fixes come from the catalog and the user's own answer,
    never from a model guess. A duplicate suspect may only be released after
    an explicit confirmation; rejecting it cancels the subtask.
    """
    row = _subtask_row(conn, subtask_id)
    updates: list[str] = []
    values: list[Any] = []
    if row["status"] == "duplicate_suspect":
        if confirm_duplicate:
            updates.append("status = 'pending'")
            updates.append("error_code = NULL")
        else:
            updates.append("status = 'cancelled'")
            updates.append("error_code = 'duplicate_rejected'")
    elif row["status"] == "clarification":
        if chemical_id is not None:
            chemical = conn.execute(
                "SELECT chemical_id, unit, revision FROM chemicals WHERE chemical_id = ?",
                (chemical_id,),
            ).fetchone()
            if chemical is None:
                raise ValueError(f"Unknown chemical_id: {chemical_id}")
            resolved_unit = unit or row["unit"]
            resolved_amount = float(amount if amount is not None else row["amount"])
            try:
                delta = -convert_amount(resolved_amount, resolved_unit, chemical["unit"])
            except (ValueError, TypeError):
                raise ValueError(
                    f"Unit {resolved_unit!r} is not dimension-compatible with "
                    f"stock unit {chemical['unit']!r}; provide an authorized conversion"
                )
            updates += [
                "chemical_id = ?",
                "unit = ?",
                "amount = ?",
                "amount_delta = ?",
                "expected_revision = ?",
                "status = 'pending'",
                "error_code = NULL",
                "dedup_key = ?",
            ]
            values += [
                chemical_id,
                normalize_unit(resolved_unit) or resolved_unit,
                resolved_amount,
                delta,
                chemical["revision"],
                f"consumption:{row['source_event_id']}:{chemical_id}",
            ]
        elif unit is not None and row["chemical_id"] != "unresolved":
            chemical = conn.execute(
                "SELECT unit, revision FROM chemicals WHERE chemical_id = ?",
                (row["chemical_id"],),
            ).fetchone()
            resolved_amount = float(amount if amount is not None else row["amount"])
            try:
                delta = -convert_amount(resolved_amount, unit, chemical["unit"])
            except (ValueError, TypeError):
                raise ValueError(
                    f"Unit {unit!r} is not dimension-compatible with stock unit "
                    f"{chemical['unit']!r}; provide an authorized conversion"
                )
            updates += [
                "unit = ?",
                "amount = ?",
                "amount_delta = ?",
                "expected_revision = ?",
                "status = 'pending'",
                "error_code = NULL",
            ]
            values += [
                normalize_unit(unit) or unit,
                resolved_amount,
                delta,
                chemical["revision"],
            ]
        else:
            raise ValueError(
                "clarification subtask needs chemical_id (identity) or unit/amount"
            )
    else:
        raise ValueError(
            f"Subtask {subtask_id} is {row['status']!r}; only clarification or "
            "duplicate_suspect rows can be resolved"
        )
    if not updates:
        return _subtask_result(row)
    values.append(subtask_id)
    conn.execute(
        f"""
        UPDATE inventory_subtasks
        SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP
        WHERE subtask_id = ?
        """,
        values,
    )
    return _subtask_result(_subtask_row(conn, subtask_id))


def execute_inventory_subtask(
    conn: sqlite3.Connection,
    subtask_id: str,
    *,
    principal_id: str | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Apply one subtask as an inventory transaction (§13.3, §13.6).

    The subtask's ``expected_revision`` (snapshotted at ingest, or supplied by
    the caller) must still match the stock revision inside the same
    transaction; a stale worker is rejected instead of silently writing. The
    structured event goes through the same audit/persist/apply path as every
    other inventory write, so auto-apply thresholds still gate it. Applied
    execution replays the recorded transaction (idempotent).
    """
    row = _subtask_row(conn, subtask_id)
    if row["status"] == "applied":
        return _subtask_result(row, replay=True)
    if row["status"] not in ("pending", "failed", "submitted"):
        raise ValueError(
            f"Subtask {subtask_id} is {row['status']!r} and cannot be executed"
        )

    chemical = conn.execute(
        "SELECT chemical_id, name, unit, amount, revision FROM chemicals WHERE chemical_id = ?",
        (row["chemical_id"],),
    ).fetchone()
    if chemical is None:
        raise ValueError(f"Unknown chemical: {row['chemical_id']}")
    wanted = expected_revision if expected_revision is not None else row["expected_revision"]
    if wanted is not None and int(wanted) != int(chemical["revision"]):
        conn.execute(
            """
            UPDATE inventory_subtasks
            SET status = 'failed', error_code = 'stale_revision',
                last_error = ?,
                expected_revision = ?,
                attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP
            WHERE subtask_id = ?
            """,
            (
                f"stock revision moved {wanted} -> {chemical['revision']}",
                chemical["revision"],
                subtask_id,
            ),
        )
        return _subtask_result(_subtask_row(conn, subtask_id))

    event = InventoryEvent(
        event_type="inventory_update",
        event_subtype=row["transaction_type"],
        item_type="chemical",
        item_id=chemical["chemical_id"],
        item_name=chemical["name"],
        amount=abs(float(row["amount_delta"])) if row["amount_delta"] < 0 else float(row["amount_delta"]),
        unit=chemical["unit"],
        transaction_type=row["transaction_type"],
        confidence=1.0,
        requires_confirmation=False,
        raw_text=row["last_error"] or f"cross-domain subtask {subtask_id}",
        project_id=row["project_id"],
        user_id=None,
    )
    message_id = f"subtask:{subtask_id}"
    try:
        audit = evaluate_inventory_event(event, chemical, user_confirmed=False)
    except Exception:  # pragma: no cover - policy raises only on bad input
        audit = None
    if audit is not None and audit.decision == "reject":
        conn.execute(
            """
            UPDATE inventory_subtasks
            SET status = 'rejected', error_code = 'policy_reject', last_error = ?,
                attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP
            WHERE subtask_id = ?
            """,
            (audit.reason, subtask_id),
        )
        return _subtask_result(_subtask_row(conn, subtask_id))

    conn.execute(
        """
        INSERT INTO message_logs (message_id, source, source_message_id, raw_text)
        VALUES (?, 'cross_domain_subtask', ?, ?)
        ON CONFLICT(message_id) DO NOTHING
        """,
        (message_id, subtask_id, event.raw_text),
    )
    try:
        # Reuse the source event id so the B-side transaction is queryable by
        # the A-side event identity (S07 §13.6: event_id ↔ source_event_id).
        inventory_event_id = persist_inventory_event(
            conn, message_id, event, event_id=row["source_event_id"]
        )
    except ValueError as exc:
        # Stock went negative or the write raced: keep the record, mark the
        # subtask failed and retryable (§13.7) — never delete the B-side fact.
        conn.execute(
            """
            UPDATE inventory_subtasks
            SET status = 'failed', error_code = 'apply_failed', last_error = ?,
                attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP
            WHERE subtask_id = ?
            """,
            (str(exc), subtask_id),
        )
        return _subtask_result(_subtask_row(conn, subtask_id))

    txn = conn.execute(
        """
        SELECT transaction_id FROM inventory_transactions WHERE event_id = ?
        """,
        (inventory_event_id,),
    ).fetchone()
    if txn is None:
        # Policy asked for confirmation (risk threshold): keep the subtask in
        # clarification with the pending event linked for human resolution.
        conn.execute(
            """
            UPDATE inventory_subtasks
            SET status = 'clarification', error_code = 'policy_pending',
                last_error = ?, event_id = ?, attempts = attempts + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE subtask_id = ?
            """,
            ("inventory event awaits confirmation", inventory_event_id, subtask_id),
        )
        return _subtask_result(_subtask_row(conn, subtask_id))

    conn.execute(
        """
        UPDATE inventory_subtasks
        SET status = 'applied', error_code = NULL, last_error = NULL,
            inventory_transaction_id = ?, event_id = ?,
            attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP
        WHERE subtask_id = ?
        """,
        (txn["transaction_id"], inventory_event_id, subtask_id),
    )
    return _subtask_result(_subtask_row(conn, subtask_id))


def list_inventory_subtasks(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    project_id: str | None = None,
    source_event_id: str | None = None,
    project_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Subtask status listing for mixed requests (§13.6) and retry queues.

    ``project_ids`` is the caller's visibility scope (S08 §14: a listing
    without an explicit project filter must never leak other projects'
    subtasks — callers pass the principal's memberships here).
    """
    clauses: list[str] = []
    values: list[Any] = []
    if status is not None:
        clauses.append("status = ?")
        values.append(status)
    if project_id is not None:
        clauses.append("project_id = ?")
        values.append(project_id)
    if project_ids is not None:
        placeholders = ",".join("?" for _ in project_ids)
        if placeholders:
            clauses.append(f"project_id IN ({placeholders})")
        else:
            clauses.append("0 = 1")  # no visible projects -> no rows
        values.extend(project_ids)
    if source_event_id is not None:
        clauses.append("source_event_id = ?")
        values.append(source_event_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM inventory_subtasks {where} ORDER BY created_at, subtask_id",
        values,
    ).fetchall()
    return [_subtask_result(row) for row in rows]


def propose_inventory_adjustment(
    conn: sqlite3.Connection,
    source_event_id: str,
    *,
    corrected_amount: float,
    corrected_unit: str,
    reason: str,
    created_by: str | None = None,
) -> dict[str, Any]:
    """A corrected usage creates a linked stock adjustment proposal (§13.8).

    Applied history is never rewritten in place and never double-deducted:
    the proposal records the delta between the original transaction and the
    corrected usage, and stays pending until a human confirms it.
    """
    txn = conn.execute(
        """
        SELECT * FROM inventory_transactions WHERE event_id = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (source_event_id,),
    ).fetchone()
    if txn is None:
        raise ValueError(
            f"Event {source_event_id} never produced an inventory transaction"
        )
    chemical = conn.execute(
        "SELECT unit FROM chemicals WHERE chemical_id = ?", (txn["chemical_id"],)
    ).fetchone()
    if chemical is None:
        raise ValueError(f"Unknown chemical: {txn['chemical_id']}")
    try:
        corrected_delta = -convert_amount(
            float(corrected_amount), corrected_unit, chemical["unit"]
        )
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"Unit {corrected_unit!r} is not dimension-compatible with stock unit "
            f"{chemical['unit']!r}; provide an authorized conversion"
        ) from exc
    adjustment_delta = corrected_delta - float(txn["amount_delta"])

    existing = conn.execute(
        """
        SELECT * FROM inventory_adjustment_proposals
        WHERE source_event_id = ? AND status = 'pending_confirmation'
        """,
        (source_event_id,),
    ).fetchone()
    if existing is not None:
        return _adjustment_result(existing, replay=True)

    adjustment_id = _new_id("adj")
    conn.execute(
        """
        INSERT INTO inventory_adjustment_proposals
          (adjustment_id, project_id, source_event_id, original_transaction_id,
           chemical_id, amount_delta, unit, reason, status, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending_confirmation', ?)
        """,
        (
            adjustment_id,
            txn["project_id"],
            source_event_id,
            txn["transaction_id"],
            txn["chemical_id"],
            adjustment_delta,
            chemical["unit"],
            reason,
            created_by,
        ),
    )
    return _adjustment_result(
        conn.execute(
            "SELECT * FROM inventory_adjustment_proposals WHERE adjustment_id = ?",
            (adjustment_id,),
        ).fetchone()
    )


def _adjustment_result(row: Row, *, replay: bool = False) -> dict[str, Any]:
    return {
        "adjustment_id": row["adjustment_id"],
        "source_event_id": row["source_event_id"],
        "original_transaction_id": row["original_transaction_id"],
        "chemical_id": row["chemical_id"],
        "amount_delta": row["amount_delta"],
        "unit": row["unit"],
        "reason": row["reason"],
        "status": row["status"],
        "inventory_transaction_id": row["inventory_transaction_id"],
        "idempotent_replay": replay,
    }


def apply_inventory_adjustment(
    conn: sqlite3.Connection,
    adjustment_id: str,
    *,
    principal_id: str | None = None,
    expected_revision: int | None = None,
    reject: bool = False,
) -> dict[str, Any]:
    """Confirm (or reject) an adjustment proposal and apply the delta (§13.8).

    The stock revision is verified in the same transaction as the write; the
    proposal is stamped ``stale`` when the revision moved instead of applying
    a silent correction.
    """
    row = conn.execute(
        "SELECT * FROM inventory_adjustment_proposals WHERE adjustment_id = ?",
        (adjustment_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown adjustment proposal: {adjustment_id}")
    if row["status"] == "applied":
        return _adjustment_result(row, replay=True)
    if row["status"] != "pending_confirmation":
        raise ValueError(f"Adjustment {adjustment_id} is {row['status']!r}")
    if reject:
        conn.execute(
            """
            UPDATE inventory_adjustment_proposals
            SET status = 'rejected', updated_at = CURRENT_TIMESTAMP
            WHERE adjustment_id = ?
            """,
            (adjustment_id,),
        )
        return _adjustment_result(
            conn.execute(
                "SELECT * FROM inventory_adjustment_proposals WHERE adjustment_id = ?",
                (adjustment_id,),
            ).fetchone()
        )

    chemical = conn.execute(
        "SELECT chemical_id, name, unit, amount, revision FROM chemicals WHERE chemical_id = ?",
        (row["chemical_id"],),
    ).fetchone()
    if chemical is None:
        raise ValueError(f"Unknown chemical: {row['chemical_id']}")
    if expected_revision is not None and int(expected_revision) != int(chemical["revision"]):
        conn.execute(
            """
            UPDATE inventory_adjustment_proposals
            SET status = 'stale', updated_at = CURRENT_TIMESTAMP
            WHERE adjustment_id = ?
            """,
            (adjustment_id,),
        )
        return _adjustment_result(
            conn.execute(
                "SELECT * FROM inventory_adjustment_proposals WHERE adjustment_id = ?",
                (adjustment_id,),
            ).fetchone()
        )

    # The adjust event carries the corrected *balance*; the delta is applied
    # against the current stock inside apply_inventory_event.
    target_amount = float(chemical["amount"]) + float(row["amount_delta"])
    event = InventoryEvent(
        event_type="inventory_update",
        event_subtype="adjust",
        item_type="chemical",
        item_id=chemical["chemical_id"],
        item_name=chemical["name"],
        amount=target_amount,
        unit=chemical["unit"],
        transaction_type="adjust",
        confidence=1.0,
        requires_confirmation=False,
        raw_text=f"adjustment {adjustment_id}: {row['reason']}",
        project_id=row["project_id"],
        user_id=None,
    )
    message_id = f"adjustment:{adjustment_id}"
    conn.execute(
        """
        INSERT INTO message_logs (message_id, source, source_message_id, raw_text)
        VALUES (?, 'cross_domain_adjustment', ?, ?)
        ON CONFLICT(message_id) DO NOTHING
        """,
        (message_id, adjustment_id, event.raw_text),
    )
    inventory_event_id = persist_inventory_event(conn, message_id, event)
    txn = conn.execute(
        "SELECT transaction_id FROM inventory_transactions WHERE event_id = ?",
        (inventory_event_id,),
    ).fetchone()
    conn.execute(
        """
        UPDATE inventory_adjustment_proposals
        SET status = 'applied', inventory_transaction_id = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE adjustment_id = ?
        """,
        (txn["transaction_id"] if txn is not None else None, adjustment_id),
    )
    return _adjustment_result(
        conn.execute(
            "SELECT * FROM inventory_adjustment_proposals WHERE adjustment_id = ?",
            (adjustment_id,),
        ).fetchone()
    )
