"""R02/F05: a committed business result survives a missing receipt window."""

from __future__ import annotations

import json
import uuid

from chemclaw_lab import capabilities, db


def test_inventory_retry_after_lost_receipt_does_not_double_charge(lab):
    human = lab.context("hum_atomic", memberships={"P1": "member"})
    params = {"project_id": "P1", "raw_text": "Used 5 g of Sodium chloride"}
    first = lab.invoke(
        human, "inventory.message.process", params, operation_id="op-crash-window"
    )
    assert first["action_status"] == "applied"
    assert lab.stock() == (95.0, 1)

    # Simulate the real F05 window: the handler committed, then the process
    # died before operation_results was written.
    conn = db.connect(lab.db_path)
    try:
        conn.execute(
            "DELETE FROM operation_results WHERE operation_id = ?",
            ("op-crash-window",),
        )
        conn.commit()
    finally:
        conn.close()

    replay = lab.invoke(
        human, "inventory.message.process", params, operation_id="op-crash-window"
    )
    # No stored receipt -> the handler runs again, but stable business
    # idempotency (message_logs.source_message_id = op:<id>) returns the SAME
    # event/transaction instead of applying a second delta.
    assert replay["result"]["event_id"] == first["result"]["event_id"]
    assert replay["result"].get("idempotent_replay") is True
    assert lab.stock() == (95.0, 1)
    assert lab.scalar("SELECT COUNT(*) FROM inventory_transactions") == 1
    assert lab.scalar("SELECT COUNT(*) FROM message_logs") == 1


def test_new_logical_operation_is_a_second_real_usage(lab):
    human = lab.context("hum_real", memberships={"P1": "member"})
    params = {"project_id": "P1", "raw_text": "Used 5 g of Sodium chloride"}
    lab.invoke(human, "inventory.message.process", params, operation_id="op-first")
    second = lab.invoke(
        human, "inventory.message.process", params, operation_id="op-second"
    )
    assert second["idempotent_replay"] is False
    assert lab.stock() == (90.0, 2)
    assert lab.scalar("SELECT COUNT(*) FROM inventory_transactions") == 2


def test_proposal_apply_replays_after_lost_receipt(lab):
    human = lab.context("hum_prop", memberships={"P1": "member"})
    page = lab.add_page("P1")
    transcript = lab.add_transcript(page["page_id"], ["Sample S-1 0.5 g"])
    from chemclaw_lab import experiments

    conn = db.connect(lab.db_path)
    try:
        conn.execute(
            "INSERT INTO samples (sample_id, project_id, label) VALUES ('smp_1', 'P1', 'S-1')"
        )
        conn.commit()
        proposal = experiments.create_proposal(
            conn,
            project_id="P1",
            page_id=page["page_id"],
            transcript_revision=1,
            profile_id="chemistry_notebook",
            profile_version="1.0",
            items=[
                {
                    "item_key": "k1",
                    "event_kind": "operation",
                    "raw_text": "Sample S-1 0.5 g",
                    "fields": {
                        "amount": {"normalized": 0.5, "unit": "g", "raw": "0.5"},
                        "sample_label": {"normalized": "S-1"},
                    },
                    "sample_match": {"action": "link",
                                     "candidates": [{"sample_id": "smp_1"}]},
                    "evidence_refs": [
                        {"page_id": page["page_id"], "transcript_revision": 1,
                         "block_id": transcript["block_ids"][0]}
                    ],
                    "needs_confirmation": False,
                }
            ],
            created_by="hum_prop",
            operation_id="prop-create",
        )
        conn.commit()
    finally:
        conn.close()
    proposal_id = proposal["proposal"]["proposal_id"]
    first = lab.invoke(
        human,
        "memory.proposal.apply",
        {"proposal_id": proposal_id, "operation_id": "apply-1"},
        operation_id="apply-1",
    )
    assert first["action_status"] == "applied"
    conn = db.connect(lab.db_path)
    try:
        conn.execute("DELETE FROM operation_results WHERE operation_id = 'apply-1'")
        conn.commit()
    finally:
        conn.close()
    replay = lab.invoke(
        human,
        "memory.proposal.apply",
        {"proposal_id": proposal_id, "operation_id": "apply-1"},
        operation_id="apply-1",
    )
    assert replay["result"]["already_applied"] is True
    assert lab.scalar(
        "SELECT COUNT(*) FROM event_revisions WHERE entity_type = 'experiment_event'"
    ) == 1
