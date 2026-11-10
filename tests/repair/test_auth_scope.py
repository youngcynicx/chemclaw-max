"""R01/F01-F03: authority, object ownership and cache/replay isolation."""

from __future__ import annotations

import pytest

from chemclaw_lab import agent_gateway, agents, auth, db, tasks
from chemclaw_lab.capabilities import CapabilityError


def test_operation_receipt_is_principal_scoped(lab):
    alice = lab.context("hum_a", memberships={"P1": "member"})
    bob = lab.context("hum_b", memberships={"P1": "member"})

    first = lab.invoke(
        alice,
        "inventory.message.process",
        {"project_id": "P1", "raw_text": "Used 5 g of Sodium chloride"},
        operation_id="shared-op",
    )
    assert first["idempotent_replay"] is False
    assert lab.stock() == (95.0, 1)

    # Same namespace + operation id + identical business content, DIFFERENT
    # subject. B must never receive A's envelope/identity.
    second = lab.invoke(
        bob,
        "inventory.message.process",
        {"project_id": "P1", "raw_text": "Used 5 g of Sodium chloride"},
        operation_id="shared-op",
    )
    assert second["idempotent_replay"] is False
    assert second["principal_id"] == "hum_b"
    assert second["object_refs"] != first["object_refs"]
    assert lab.stock() == (90.0, 2)
    assert lab.scalar("SELECT COUNT(*) FROM operation_results") == 2


def test_operation_receipt_not_replayed_after_membership_revocation(lab):
    member = lab.context("hum_revoke", memberships={"P1": "member"})
    first = lab.invoke(
        member,
        "inventory.message.process",
        {"project_id": "P1", "raw_text": "Used 1 g of Sodium chloride"},
        operation_id="op-revoke",
    )
    assert first["action_status"] == "applied"

    conn = db.connect(lab.db_path)
    try:
        conn.execute("DELETE FROM memberships WHERE principal_id = 'hum_revoke'")
        conn.commit()
    finally:
        conn.close()
    revoked = auth.load_principal_context(
        db.connect(lab.db_path), "hum_revoke"
    )
    with pytest.raises(CapabilityError) as excinfo:
        lab.invoke(
            revoked,
            "inventory.message.process",
            {"project_id": "P1", "raw_text": "Used 1 g of Sodium chloride"},
            operation_id="op-revoke",
        )
    assert excinfo.value.code == "forbidden_capability"


def test_correction_cannot_cross_project_or_rewrite_project(lab):
    page = lab.add_page("P2")
    event_id = lab.add_event(
        project_id="P2",
        sample_id=None,
        amount=5.0,
        unit="g",
        evidence_refs=[{"page_id": page["page_id"], "transcript_revision": 1,
                        "block_id": "b1"}],
    )
    p1_member = lab.context("hum_p1", memberships={"P1": "member"})
    with pytest.raises(CapabilityError) as excinfo:
        lab.invoke(
            p1_member,
            "memory.event.correct",
            {
                "event_id": event_id,
                "expected_revision": 1,
                "changes": {"fields": {"amount": {"normalized": 0.5}}},
                "reason": "cross-project attempt",
            },
            operation_id="op-cross-correct",
        )
    assert excinfo.value.code == "forbidden_scope"
    assert lab.scalar(
        "SELECT COUNT(*) FROM event_revisions WHERE entity_id = ?", (event_id,)
    ) == 1

    p2_member = lab.context("hum_p2", memberships={"P2": "member"})
    with pytest.raises(CapabilityError) as excinfo:
        lab.invoke(
            p2_member,
            "memory.event.correct",
            {
                "event_id": event_id,
                "expected_revision": 1,
                "changes": {"project_id": "P1"},
                "reason": "reassign project",
            },
            operation_id="op-reassign",
        )
    assert excinfo.value.code == "invalid_correction"
    assert lab.scalar(
        "SELECT COUNT(*) FROM event_revisions WHERE entity_id = ?", (event_id,)
    ) == 1


def test_run_token_scope_is_enforced_for_every_tool(lab, monkeypatch, tmp_path):
    owner = lab.context("hum_owner", memberships={"P1": "owner", "P2": "owner"})
    page_p2 = lab.add_page("P2")
    event_p2 = lab.add_event(project_id="P2", amount=1.0, unit="g",
                             evidence_refs=[])

    conn = db.connect(lab.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        agents.load_registry(conn, str((__import__("pathlib").Path(__file__).resolve().parents[2] / "runtime" / "agents")))
        agents.set_manifest_enabled(conn, agent_id="b_query", version="1.0.0", enabled=True)
        dispatched = agents.dispatch(
            conn, intent="query_answer", context=owner, project_id="P1"
        )
        conn.commit()
    finally:
        conn.close()

    conn = db.connect(lab.db_path)
    try:
        token_row = agents.resolve_run_token(conn, dispatched["run_token"])
        with pytest.raises(agents.AgentHostError) as excinfo:
            agent_gateway.invoke_agent_tool(
                lab.db_path, conn, token_row,
                capability_id="query.events.search",
                params={"project_ids": ["P2"]},
            )
        assert excinfo.value.code == "forbidden_scope"

        # Omitting an explicit filter must derive only the token's P1 scope.
        result = agent_gateway.invoke_agent_tool(
            lab.db_path, conn, token_row,
            capability_id="query.events.search", params={},
        )
        assert result["result"]["scope"] == ["P1"]

        with pytest.raises(agents.AgentHostError) as excinfo:
            agent_gateway.invoke_agent_tool(
                lab.db_path, conn, token_row,
                capability_id="memory.event.read",
                params={"event_id": event_p2},
            )
        assert excinfo.value.code == "forbidden_scope"
    finally:
        conn.close()


def test_report_refuses_cross_project_sources(lab):
    page1 = lab.add_page("P1")
    event1 = lab.add_event(
        project_id="P1", amount=0.5, unit="g",
        evidence_refs=[{"page_id": page1["page_id"], "transcript_revision": 1,
                        "block_id": "b1"}],
    )
    page2 = lab.add_page("P2")
    event2 = lab.add_event(
        project_id="P2", amount=9.0, unit="g",
        evidence_refs=[{"page_id": page2["page_id"], "transcript_revision": 1,
                        "block_id": "b2"}],
    )
    member = lab.context("hum_rep", memberships={"P1": "member"})
    answer = {
        "snapshot": {"id": "s1"},
        "claims": [
            {"claim_id": "c1", "kind": "reported_fact", "object_type": "experiment_event",
             "object_id": event1, "revision": 1, "value": 0.5, "unit": "g",
             "statement": "P1 fact", "evidence_refs": []},
            {"claim_id": "c2", "kind": "reported_fact", "object_type": "experiment_event",
             "object_id": event2, "revision": 1, "value": 9.0, "unit": "g",
             "statement": "P2 secret", "evidence_refs": []},
        ],
        "gaps": [],
    }
    with pytest.raises(CapabilityError) as excinfo:
        lab.invoke(
            member,
            "query.report.save",
            {"project_id": "P1", "question": "mix", "answer": answer},
            operation_id="op-mix-report",
        )
    assert excinfo.value.code == "forbidden_scope"
    assert lab.scalar("SELECT COUNT(*) FROM query_reports") == 0
