"""S09 C-domain: resources & bookings (plan §15).

Scheduling facts are kept strictly separate from experiment facts:

- a *plan* (``planned``) and its candidate slots are proposals that never
  block a resource;
- a *confirmed booking* is created inside the same transaction as the
  conflict re-check (§15 C: 冲突和事务复查);
- bookings mint NO experiment events and NO inventory messages — a booking
  can never pose as an experiment result.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

BOOKING_STATUSES = ("planned", "confirmed", "cancelled", "completed")


class ResourceError(Exception):
    """Domain error mapped to a stable capability code by the handler."""

    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _parse_iso(value: str, field: str) -> datetime:
    """Parse an ISO-8601 timestamp and REQUIRE an explicit timezone.

    Comparing naive and offset-aware datetimes is the F13 defect: 09:00
    +08:00 and 01:00Z are the same instant but sort differently as text.
    All booking math is done on aware UTC instants; the original text is
    preserved for display.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResourceError(
            "schema_violation", f"params.{field} must be an ISO-8601 timestamp."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResourceError(
            "timezone_required",
            f"params.{field} must include an explicit UTC offset (e.g. +08:00 or Z).",
        )
    return parsed


def _utc_micros(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000)


def register_resource(
    conn: sqlite3.Connection,
    *,
    name: str,
    kind: str,
    project_id: str | None,
    capacity: int = 1,
    created_by: str | None = None,
) -> dict[str, Any]:
    if kind not in ("instrument", "room", "equipment", "service"):
        raise ResourceError("schema_violation", f"Unknown resource kind {kind!r}.")
    if capacity < 1:
        raise ResourceError("schema_violation", "params.capacity must be >= 1.")
    if capacity != 1:
        # The conflict engine implements single-capacity resources only. Do
        # not accept a field we would silently ignore (§R10).
        raise ResourceError(
            "not_supported",
            "capacity pools are not implemented; register capacity=1 resources only.",
            http_status=501,
        )
    resource_id = f"res_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO resources (resource_id, project_id, name, kind, capacity, "
        "status, created_by) VALUES (?, ?, ?, ?, ?, 'active', ?)",
        (resource_id, project_id, name, kind, capacity, created_by),
    )
    return {"resource_id": resource_id, "name": name, "kind": kind,
            "project_id": project_id, "capacity": capacity, "status": "active"}


def list_resources(
    conn: sqlite3.Connection,
    *,
    project_ids: list[str] | None = None,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    if project_ids is not None:
        placeholders = ",".join("?" for _ in project_ids)
        # Shared infrastructure (project_id IS NULL) is visible to everyone.
        if placeholders:
            clauses.append(f"(project_id IS NULL OR project_id IN ({placeholders}))")
        else:
            clauses.append("project_id IS NULL")
        values.extend(project_ids)
    if kind is not None:
        clauses.append("kind = ?")
        values.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM resources {where} ORDER BY name", values
    ).fetchall()
    return [dict(r) for r in rows]


def get_resource(conn: sqlite3.Connection, resource_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM resources WHERE resource_id = ?", (resource_id,)
    ).fetchone()
    if row is None:
        raise ResourceError("not_found", f"Unknown resource {resource_id!r}.", http_status=404)
    return dict(row)


def _overlapping_confirmed(
    conn: sqlite3.Connection, resource_id: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Half-open overlap on UTC instants: [start, end).

    ``existing_start < new_end AND existing_end > new_start`` — adjacent
    bookings do not conflict. Rows without a trustworthy UTC instant are
    surfaced as legacy ambiguities instead of being silently ignored.
    """
    legacy = conn.execute(
        """
        SELECT booking_id, project_id, slot_start, slot_end, purpose_note
        FROM resource_bookings
        WHERE resource_id = ? AND status = 'confirmed'
          AND (slot_start_utc IS NULL OR slot_end_utc IS NULL)
        """,
        (resource_id,),
    ).fetchall()
    if legacy:
        raise ResourceError(
            "legacy_time_ambiguous",
            f"Resource {resource_id!r} has {len(legacy)} confirmed booking(s) "
            "without a trustworthy UTC instant; migrate or resolve them before "
            "any new conflict decision.",
            http_status=409,
        )
    rows = conn.execute(
        """
        SELECT booking_id, project_id, slot_start, slot_end, purpose_note
        FROM resource_bookings
        WHERE resource_id = ? AND status = 'confirmed'
          AND slot_start_utc < ? AND slot_end_utc > ?
        """,
        (resource_id, _utc_micros(end), _utc_micros(start)),
    ).fetchall()
    return [dict(r) for r in rows]


def find_candidate_slots(
    conn: sqlite3.Connection,
    *,
    resource_id: str,
    window_start: str,
    window_end: str,
    duration_minutes: int = 60,
) -> list[dict[str, Any]]:
    """Candidate free slots inside the window (§15 C: 候选时段独立返回)."""
    resource = get_resource(conn, resource_id)
    if resource["status"] != "active":
        raise ResourceError("invalid_state", f"Resource {resource_id!r} is not active.")
    win_start = _parse_iso(window_start, "window_start")
    win_end = _parse_iso(window_end, "window_end")
    if win_end <= win_start:
        raise ResourceError("schema_violation", "window_end must be after window_start.")
    busy = _overlapping_confirmed(conn, resource_id, win_start, win_end)
    busy_windows = sorted(
        (_parse_iso(b["slot_start"], "slot_start"), _parse_iso(b["slot_end"], "slot_end"))
        for b in busy
    )
    duration = timedelta(minutes=duration_minutes)
    candidates: list[dict[str, Any]] = []
    cursor = win_start
    step = timedelta(minutes=30)
    while cursor + duration <= win_end:
        slot_end = cursor + duration
        clash = any(s < slot_end and e > cursor for s, e in busy_windows)
        if not clash:
            candidates.append({
                "slot_start": cursor.isoformat(),
                "slot_end": slot_end.isoformat(),
            })
        cursor = cursor + step
    return candidates


def propose_booking(
    conn: sqlite3.Connection,
    *,
    resource_id: str,
    project_id: str,
    slot_start: str,
    slot_end: str,
    purpose_note: str | None = None,
    plan_summary: str | None = None,
    task_id: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """A plan: never blocks the slot, never conflicts (§15 C)."""
    get_resource(conn, resource_id)  # existence + active check
    start = _parse_iso(slot_start, "slot_start")
    end = _parse_iso(slot_end, "slot_end")
    if end <= start:
        raise ResourceError("schema_violation", "slot_end must be after slot_start.")
    # Conflict AWARENESS, not enforcement: a plan against a busy slot is
    # allowed but flagged, so the human sees the clash before confirming.
    conflicts = _overlapping_confirmed(conn, resource_id, start, end)
    booking_id = f"bkg_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO resource_bookings (booking_id, resource_id, project_id, task_id, "
        "status, slot_start, slot_end, slot_start_utc, slot_end_utc, "
        "time_normalization, purpose_note, plan_summary, created_by) "
        "VALUES (?, ?, ?, ?, 'planned', ?, ?, ?, ?, 'utc', ?, ?, ?)",
        (booking_id, resource_id, project_id, task_id,
         start.isoformat(), end.isoformat(),
         _utc_micros(start), _utc_micros(end),
         purpose_note, plan_summary, created_by),
    )
    return {
        "booking_id": booking_id,
        "status": "planned",
        "resource_id": resource_id,
        "slot_start": start.isoformat(),
        "slot_end": end.isoformat(),
        "conflicts": conflicts,
        "conflict_detected": bool(conflicts),
    }


def confirm_booking(
    conn: sqlite3.Connection,
    *,
    booking_id: str,
    confirmed_by: str,
) -> dict[str, Any]:
    """Formal booking: conflict re-check and the write happen in ONE
    transaction (§15 C: 冲突和事务复查). Caller owns BEGIN IMMEDIATE."""
    row = conn.execute(
        "SELECT * FROM resource_bookings WHERE booking_id = ?", (booking_id,)
    ).fetchone()
    if row is None:
        raise ResourceError("not_found", f"Unknown booking {booking_id!r}.", http_status=404)
    if row["status"] == "confirmed":
        return {"booking_id": booking_id, "status": "confirmed",
                "idempotent_replay": True, "conflict_detected": False}
    if row["status"] not in ("planned",):
        raise ResourceError(
            "invalid_state",
            f"Booking {booking_id!r} is {row['status']!r} and cannot be confirmed.",
        )
    start = _parse_iso(row["slot_start"], "slot_start")
    end = _parse_iso(row["slot_end"], "slot_end")
    conflicts = _overlapping_confirmed(conn, row["resource_id"], start, end)
    if conflicts:
        raise ResourceError(
            "conflict_detected",
            f"Resource {row['resource_id']} already has {len(conflicts)} confirmed "
            "booking(s) overlapping this slot.",
            http_status=409,
        )
    conn.execute(
        "UPDATE resource_bookings SET status = 'confirmed', confirmed_by = ?, "
        "updated_at = CURRENT_TIMESTAMP WHERE booking_id = ?",
        (confirmed_by, booking_id),
    )
    return {"booking_id": booking_id, "status": "confirmed",
            "conflict_detected": False, "rechecked_conflicts": []}


def cancel_booking(conn: sqlite3.Connection, *, booking_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT status FROM resource_bookings WHERE booking_id = ?", (booking_id,)
    ).fetchone()
    if row is None:
        raise ResourceError("not_found", f"Unknown booking {booking_id!r}.", http_status=404)
    if row["status"] == "cancelled":
        return {"booking_id": booking_id, "status": "cancelled", "idempotent_replay": True}
    if row["status"] == "completed":
        raise ResourceError("invalid_state", "A completed booking cannot be cancelled.")
    conn.execute(
        "UPDATE resource_bookings SET status = 'cancelled', "
        "updated_at = CURRENT_TIMESTAMP WHERE booking_id = ?",
        (booking_id,),
    )
    return {"booking_id": booking_id, "status": "cancelled"}


def list_bookings(
    conn: sqlite3.Connection,
    *,
    project_ids: list[str] | None = None,
    resource_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    if project_ids is not None:
        placeholders = ",".join("?" for _ in project_ids)
        if placeholders:
            clauses.append(f"project_id IN ({placeholders})")
        else:
            clauses.append("0 = 1")
        values.extend(project_ids)
    if resource_id is not None:
        clauses.append("resource_id = ?")
        values.append(resource_id)
    if status is not None:
        clauses.append("status = ?")
        values.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM resource_bookings {where} ORDER BY slot_start", values
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        if item.get("candidates_json"):
            item["candidates"] = json.loads(item.pop("candidates_json"))
        else:
            item.pop("candidates_json", None)
        result.append(item)
    return result


def record_candidates(
    conn: sqlite3.Connection, *, booking_id: str, candidates: list[dict[str, Any]]
) -> None:
    conn.execute(
        "UPDATE resource_bookings SET candidates_json = ?, "
        "updated_at = CURRENT_TIMESTAMP WHERE booking_id = ?",
        (json.dumps(candidates, ensure_ascii=False), booking_id),
    )
