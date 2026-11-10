"""R10/F13: booking overlap is compared on UTC instants, not text."""

from __future__ import annotations

import pytest

from chemclaw_lab.capabilities import CapabilityError


def _resource(lab):
    owner = lab.context("hum_book", memberships={"P1": "owner"})
    return lab.invoke(
        owner,
        "resource.register",
        {"project_id": "P1", "name": "NMR", "kind": "instrument", "capacity": 1},
        operation_id="op-res",
    )["result"]["resource_id"], owner


def _propose(lab, owner, rid, start, end, op):
    return lab.invoke(
        owner,
        "resource.booking.propose",
        {
            "resource_id": rid,
            "project_id": "P1",
            "slot_start": start,
            "slot_end": end,
        },
        operation_id=op,
    )["result"]["booking_id"]


def _confirm(lab, owner, booking_id, op):
    return lab.invoke(
        owner,
        "resource.booking.confirm",
        {"booking_id": booking_id},
        operation_id=op,
    )


def test_equivalent_timezones_conflict(lab):
    rid, owner = _resource(lab)
    first = _propose(
        lab, owner, rid, "2026-10-14T09:00:00+08:00", "2026-10-14T10:00:00+08:00",
        "op-p1",
    )
    _confirm(lab, owner, first, "op-c1")
    second = _propose(
        lab, owner, rid, "2026-10-14T01:00:00+00:00", "2026-10-14T02:00:00+00:00",
        "op-p2",
    )
    with pytest.raises(CapabilityError) as excinfo:
        _confirm(lab, owner, second, "op-c2")
    assert excinfo.value.code == "conflict_detected"


def test_adjacent_booking_is_allowed(lab):
    rid, owner = _resource(lab)
    first = _propose(
        lab, owner, rid, "2026-10-14T09:00:00+08:00", "2026-10-14T10:00:00+08:00",
        "op-p3",
    )
    _confirm(lab, owner, first, "op-c3")
    adjacent = _propose(
        lab, owner, rid, "2026-10-14T10:00:00+08:00", "2026-10-14T11:00:00+08:00",
        "op-p4",
    )
    result = _confirm(lab, owner, adjacent, "op-c4")
    assert result["result"]["status"] == "confirmed"


def test_naive_timestamp_is_rejected(lab):
    rid, owner = _resource(lab)
    with pytest.raises(CapabilityError) as excinfo:
        _propose(
            lab, owner, rid, "2026-10-14T09:00:00", "2026-10-14T10:00:00",
            "op-p5",
        )
    assert excinfo.value.code == "timezone_required"


def test_capacity_pool_is_not_silently_ignored(lab):
    owner = lab.context("hum_book", memberships={"P1": "owner"})
    with pytest.raises(CapabilityError) as excinfo:
        lab.invoke(
            owner,
            "resource.register",
            {"project_id": "P1", "name": "Hood", "kind": "equipment", "capacity": 2},
            operation_id="op-res-cap",
        )
    assert excinfo.value.code == "not_supported"
