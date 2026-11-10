"""R03/F04: humans approve; agents execute with a consumed grant."""

from __future__ import annotations

import json
import uuid

import pytest

from chemclaw_lab import db
from chemclaw_lab.capabilities import CapabilityError


def test_agent_cannot_resolve_a_pending_inventory_event(lab):
    human = lab.context("hum_approver", memberships={"P1": "member"})
    pending = lab.invoke(
        human,
        "inventory.message.process",
        {"project_id": "P1", "raw_text": "I plan to use 5 g of Sodium chloride"},
        operation_id="op-pending",
    )
    assert pending["action_status"] == "clarification_needed"
    event_id = pending["result"]["event_id"]
    assert lab.stock() == (100.0, 0)

    agent = lab.context("agent_alice", kind="agent", memberships={"P1": "member"})
    with pytest.raises(CapabilityError) as excinfo:
        lab.invoke(
            agent,
            "inventory.event.resolve",
            {"event_id": event_id, "approve": True},
            operation_id="op-agent-resolve",
        )
    assert excinfo.value.code == "human_approval_required"
    assert lab.stock() == (100.0, 0)

    # A real human confirmation still works through the same capability.
    resolved = lab.invoke(
        human,
        "inventory.event.resolve",
        {"event_id": event_id, "approve": True},
        operation_id="op-human-resolve",
    )
    assert resolved["action_status"] == "applied"
    assert lab.stock() == (95.0, 1)


def test_agent_cannot_confirm_memory_proposal_items(lab):
    conn = db.connect(lab.db_path)
    try:
        proposal_id = f"prop_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO memory_proposals "
            "(proposal_id, project_id, profile_id, profile_version, items_json, "
            "revision, status, created_by) VALUES (?, 'P1', 'p', '1', ?, 0, 'proposed', 'x')",
            (proposal_id, json.dumps([{"item_key": "i1", "fields": {}}])),
        )
        conn.commit()
    finally:
        conn.close()
    agent = lab.context("agent_b", kind="agent", memberships={"P1": "member"})
    with pytest.raises(CapabilityError) as excinfo:
        lab.invoke(
            agent,
            "memory.proposal.confirm_items",
            {
                "proposal_id": proposal_id,
                "item_keys": ["i1"],
                "decision": "confirmed",
                "expected_proposal_revision": 0,
            },
            operation_id="op-agent-confirm",
        )
    assert excinfo.value.code == "human_approval_required"
    assert lab.scalar(
        "SELECT COUNT(*) FROM event_confirmations WHERE proposal_id = ?",
        (proposal_id,),
    ) == 0


def test_adjustment_grant_is_human_issued_consumed_once_and_content_bound(lab):
    human = lab.context("hum_grant", memberships={"P1": "member"})
    applied = lab.invoke(
        human,
        "inventory.message.process",
        {"project_id": "P1", "raw_text": "Used 5 g of Sodium chloride"},
        operation_id="op-use",
    )
    assert applied["action_status"] == "applied"
    txn_id = applied["result"]["transaction_id"]
    event_id = applied["result"]["event_id"]

    conn = db.connect(lab.db_path)
    try:
        adjustment_id = f"adj_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO inventory_adjustment_proposals "
            "(adjustment_id, project_id, source_event_id, original_transaction_id, "
            "chemical_id, amount_delta, unit, reason, status, created_by) "
            "VALUES (?, 'P1', ?, ?, 'chm_nacl', -2.0, 'g', 'extra 2 g now confirmed', "
            "'pending_confirmation', 'hum_grant')",
            (adjustment_id, event_id, txn_id),
        )
        conn.commit()
    finally:
        conn.close()

    grant = lab.invoke(
        human,
        "inventory.approval.grant",
        {
            "proposal_type": "inventory_adjustment",
            "proposal_id": adjustment_id,
            "allowed_action": "apply",
        },
        operation_id="op-grant",
    )
    approval_id = grant["result"]["approval_id"]

    agent = lab.context("agent_executor", kind="agent", memberships={"P1": "member"})
    executed = lab.invoke(
        agent,
        "inventory.adjustment.apply",
        {
            "adjustment_id": adjustment_id,
            "expected_revision": 1,
            "approval_id": approval_id,
        },
        operation_id="op-agent-apply",
    )
    assert executed["action_status"] == "applied"
    assert lab.stock() == (93.0, 2)

    # Same grant with a NEW logical operation is rejected: it was consumed.
    with pytest.raises(CapabilityError) as excinfo:
        lab.invoke(
            agent,
            "inventory.adjustment.apply",
            {
                "adjustment_id": adjustment_id,
                "expected_revision": 2,
                "approval_id": approval_id,
            },
            operation_id="op-agent-replay",
        )
    assert excinfo.value.code in ("approval_consumed", "invalid_state")
    assert lab.stock() == (93.0, 2)


def test_grant_invalidates_when_proposal_content_changes(lab):
    human = lab.context("hum_grant2", memberships={"P1": "member"})
    applied = lab.invoke(
        human,
        "inventory.message.process",
        {"project_id": "P1", "raw_text": "Used 1 g of Sodium chloride"},
        operation_id="op-use2",
    )
    txn_id = applied["result"]["transaction_id"]
    event_id = applied["result"]["event_id"]
    conn = db.connect(lab.db_path)
    try:
        adjustment_id = f"adj_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO inventory_adjustment_proposals "
            "(adjustment_id, project_id, source_event_id, original_transaction_id, "
            "chemical_id, amount_delta, unit, reason, status, created_by) "
            "VALUES (?, 'P1', ?, ?, 'chm_nacl', -2.0, 'g', 'initial', "
            "'pending_confirmation', 'hum_grant2')",
            (adjustment_id, event_id, txn_id),
        )
        conn.commit()
    finally:
        conn.close()
    approval_id = lab.invoke(
        human,
        "inventory.approval.grant",
        {"proposal_type": "inventory_adjustment", "proposal_id": adjustment_id,
         "allowed_action": "apply"},
        operation_id="op-grant2",
    )["result"]["approval_id"]

    # The proposal changes after approval (new revision of its content).
    conn = db.connect(lab.db_path)
    try:
        conn.execute(
            "UPDATE inventory_adjustment_proposals SET amount_delta = -9.0 "
            "WHERE adjustment_id = ?",
            (adjustment_id,),
        )
        conn.commit()
    finally:
        conn.close()

    agent = lab.context("agent_executor2", kind="agent", memberships={"P1": "member"})
    with pytest.raises(CapabilityError) as excinfo:
        lab.invoke(
            agent,
            "inventory.adjustment.apply",
            {"adjustment_id": adjustment_id, "expected_revision": 1,
             "approval_id": approval_id},
            operation_id="op-agent-apply2",
        )
    assert excinfo.value.code == "approval_content_changed"
    assert lab.stock() == (99.0, 1)
