"""Default knowledge adoption never impersonates approval or touches inventory."""
import json
from dataclasses import replace

import pytest

from chemclaw_lab import db, experiments, profiles
from chemclaw_lab.capabilities import CapabilityError


@pytest.fixture()
def knowledge(lab):
    context = lab.context("hum_knowledge", memberships={"P1": "member", "P2": "member"})
    page = lab.add_page("P1")
    transcript = lab.add_transcript(page["page_id"], ["A used 1 g; B shares the measurement", "A planned a test; temperature unknown"])
    return lab, context, page, transcript


def make_item(knowledge, key="a", label="A", **extra):
    _, _, page, transcript = knowledge
    fields = {"sample_label": {"normalized": label}} if label is not None else {}
    return {"item_key": key, "event_kind": "operation", "raw_text": "A used 1 g; B shares the measurement",
            "fields": fields, "evidence_refs": [{"page_id": page["page_id"], "transcript_revision": 1, "block_id": transcript["block_ids"][0]}], **extra}


def create(knowledge, items=None, operation_id="create-knowledge", **kwargs):
    lab, context, page, _ = knowledge
    return lab.invoke(context, "memory.proposal.create", {"project_id": "P1", "page_id": page["page_id"], "transcript_revision": 1,
        "profile_id": "chemistry_notebook", "items": items or [make_item(knowledge)], "operation_id": operation_id, **kwargs}, operation_id=operation_id)


def event(knowledge, proposal):
    lab, context, _, _ = knowledge
    return lab.invoke(context, "memory.event.read", {"event_id": proposal["items"][0]["event_id"]}, operation_id="read-"+str(lab.scalar("SELECT count(*) FROM event_revisions")))["result"]


def test_default_adopts_unknown_knowledge_without_human_confirmations(knowledge):
    item = make_item(knowledge, unknown=True, unknowns=["temperature not measured"], needs_confirmation=True,
                     occurred_at="2026-10-12", fields={"sample_label": {"normalized": "A"}, "amount": {"normalized": 1, "unit": "g"}})
    result = create(knowledge, [item])
    proposal = result["result"]["proposal"]
    lab = knowledge[0]
    assert proposal["status"] == "applied" and proposal["acceptance_mode"] == "default"
    assert proposal["items"][0]["needs_confirmation"] is False
    assert proposal["items"][0]["event_revision"] == 1
    current = event(knowledge, proposal)
    assert current["field_json"]["unknown"] is True
    assert current["field_json"]["unknowns"] == ["temperature not measured"]
    assert current["field_json"]["occurred_at"] == "2026-10-12"
    assert lab.scalar("SELECT event_time_start FROM event_revisions") == "2026-10-12"
    assert json.loads(current["provenance_json"])["acceptance"] == "default"
    assert json.loads(current["provenance_json"])["confirmed_by"] is None
    assert lab.scalar("SELECT count(*) FROM event_confirmations") == 0
    assert lab.scalar("SELECT count(*) FROM outbox WHERE message_type='inventory_proposal'") == 0
    assert lab.scalar("SELECT count(*) FROM samples WHERE project_id='P1' AND label='A'") == 1
    assert {ref["kind"] for ref in result["object_refs"]} == {"memory_proposal", "experiment_event"}


def test_shared_sample_labels_link_one_fact_to_every_explicit_sample(knowledge):
    fields = {"sample_label": {"normalized": "A"}, "related_sample_labels": [{"normalized": "B"}], "sample_labels": ["A", "C"]}
    proposal = create(knowledge, [make_item(knowledge, fields=fields)])["result"]["proposal"]
    current = event(knowledge, proposal)["field_json"]
    assert len(current["sample_ids"]) == 3
    assert current["sample_id"] in current["sample_ids"]
    assert knowledge[0].scalar("SELECT count(*) FROM event_revisions") == 1
    assert knowledge[0].scalar("SELECT count(*) FROM samples") == 3


def test_unique_alias_reuses_catalogue_and_duplicate_alias_stays_unassigned(knowledge):
    lab = knowledge[0]
    lab.execute("INSERT INTO samples(sample_id,project_id,label,aliases_json) VALUES('existing-a','P1','Canonical','[\"A\"]')")
    first = create(knowledge)["result"]["proposal"]
    assert event(knowledge, first)["field_json"]["sample_ids"] == ["existing-a"]
    lab.execute("INSERT INTO samples(sample_id,project_id,label,aliases_json) VALUES('other-a','P1','Other','[\"A\"]')")
    second = create(knowledge, operation_id="ambiguous")["result"]["proposal"]
    current = event(knowledge, second)["field_json"]
    assert current["sample_ids"] == []
    assert current["sample_resolution"]["issues"][0]["reason"] == "ambiguous_sample_label"
    assert second["status"] == "applied"  # Knowledge is saved, identity remains unknown.


def test_uncertain_identity_does_not_create_sample_but_keeps_knowledge(knowledge):
    proposal = create(knowledge, [make_item(knowledge, fields={"sample_label": {"normalized": "maybe-A", "uncertain": True}})])["result"]["proposal"]
    assert event(knowledge, proposal)["field_json"]["sample_ids"] == []
    assert knowledge[0].scalar("SELECT count(*) FROM samples") == 0


def test_replay_and_legacy_apply_do_not_duplicate_or_send_inventory(knowledge):
    lab, context, _, _ = knowledge
    result = create(knowledge)
    proposal = result["result"]["proposal"]
    assert create(knowledge)["idempotent_replay"] is True
    applied = lab.invoke(context, "memory.proposal.apply", {"proposal_id": proposal["proposal_id"], "operation_id": "legacy-apply"}, operation_id="legacy-apply")
    assert applied["result"]["already_applied"] is True
    assert lab.scalar("SELECT count(*) FROM event_revisions") == 1
    assert lab.scalar("SELECT count(*) FROM samples") == 1
    assert lab.scalar("SELECT count(*) FROM outbox WHERE message_type='inventory_proposal'") == 0


def test_explicit_review_preserves_legacy_confirmation_path(knowledge):
    lab, context, _, _ = knowledge
    proposal = create(knowledge, acceptance_mode="review")["result"]["proposal"]
    assert proposal["status"] == "awaiting_confirmation"
    assert not proposal["knowledge_events"]
    lab.invoke(context, "memory.proposal.confirm_items", {"proposal_id": proposal["proposal_id"], "expected_proposal_revision": 0,
        "item_keys": ["a", "sample:create:a"], "decision": "confirmed"}, operation_id="explicit-confirm")
    done = lab.invoke(context, "memory.proposal.apply", {"proposal_id": proposal["proposal_id"], "operation_id": "explicit-apply"}, operation_id="explicit-apply")
    assert done["result"]["applied"]
    assert lab.scalar("SELECT count(*) FROM event_confirmations") == 2


def test_other_profile_stays_review_only(knowledge, monkeypatch):
    other = replace(profiles.CHEMISTRY_NOTEBOOK_V1, profile_id="other_notes")
    monkeypatch.setitem(profiles._REGISTRY, (other.profile_id, other.version), other)
    proposal = create(knowledge, profile_id="other_notes")["result"]["proposal"]
    assert proposal["status"] == "awaiting_confirmation"
    assert knowledge[0].scalar("SELECT count(*) FROM event_revisions") == 0


def test_cross_project_page_is_rejected_before_adoption(knowledge):
    lab = knowledge[0]
    outside = lab.add_page("P2")
    with pytest.raises(CapabilityError) as error:
        create(knowledge, page_id=outside["page_id"])
    assert error.value.code == "forbidden_scope"
    assert lab.scalar("SELECT count(*) FROM event_revisions") == 0
    assert lab.scalar("SELECT count(*) FROM samples") == 0


def test_backfill_is_project_scoped_and_preserves_later_corrections(knowledge):
    lab, context, _, _ = knowledge
    legacy = create(knowledge, acceptance_mode="review")["result"]["proposal"]
    old_items = [{k: v for k, v in item.items() if k != "acceptance"} for item in legacy["items"]]
    lab.execute("UPDATE memory_proposals SET items_json=? WHERE proposal_id=?", (json.dumps(old_items), legacy["proposal_id"]))
    conn = db.connect(lab.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        first = experiments.backfill_knowledge_proposals(conn, project_id="P1", principal_id=context.principal_id)
        conn.commit()
        assert first["new_events"] == 1
        proposal = experiments.get_proposal(conn, legacy["proposal_id"])
    finally:
        conn.close()
    event_id = proposal["items"][0]["event_id"]
    lab.invoke(context, "memory.event.correct", {"event_id": event_id, "expected_revision": 1,
        "changes": {"fields": {"sample_label": {"normalized": "B"}}, "unknowns": ["user questions original label"]},
        "reason": "organize existing record", "correction_source": {"source_kind": "llm_organization"}}, operation_id="correct-knowledge")
    conn = db.connect(lab.db_path)
    try:
        second = experiments.backfill_knowledge_proposals(conn, project_id="P1", principal_id=context.principal_id)
        assert second["new_events"] == 0
        head = experiments.read_event(conn, event_id)
        assert head["revision"] == 2
        assert head["field_json"]["fields"]["sample_label"]["normalized"] == "B"
        assert json.loads(head["provenance_json"])["acceptance"] == "default"
        assert json.loads(head["provenance_json"])["source_kind"] == "llm_organization"
        conn.rollback()
    finally:
        conn.close()
    assert lab.scalar("SELECT count(*) FROM event_confirmations") == 0


def test_corrections_reassign_cards_but_plain_edits_keep_links(knowledge):
    lab, context, _, _ = knowledge
    proposal = create(knowledge)["result"]["proposal"]
    event_id = proposal["items"][0]["event_id"]
    first = event(knowledge, proposal)
    lab.invoke(context, "memory.event.correct", {"event_id": event_id, "expected_revision": 1,
        "changes": {"raw_text": "corrected text", "fields": {"disputed": True, "human_question": {"text": "check this", "status": "open"}}}, "reason": "question"}, operation_id="question-1")
    current = event(knowledge, proposal)
    assert current["field_json"]["sample_ids"] == first["field_json"]["sample_ids"]
    lab.invoke(context, "memory.event.correct", {"event_id": event_id, "expected_revision": 2,
        "changes": {"fields": {"sample_label": {"normalized": "B"}, "related_sample_labels": [{"normalized": "C"}]}, "occurred_at": "2026-10-13"}, "reason": "correct subjects"}, operation_id="question-2")
    current = event(knowledge, proposal)
    assert len(current["field_json"]["sample_ids"]) == 2
    assert first["field_json"]["sample_id"] not in current["field_json"]["sample_ids"]
    assert lab.scalar("SELECT event_time_start FROM event_revisions ORDER BY revision DESC LIMIT 1") == "2026-10-13"
    assert lab.scalar("SELECT count(*) FROM event_revisions") == 3
    assert lab.scalar("SELECT count(*) FROM outbox WHERE message_type='inventory_proposal'") == 0


@pytest.mark.parametrize("revision", [1, 0])
def test_cross_project_sample_correction_is_rejected_even_when_stale(knowledge, revision):
    lab, context, _, _ = knowledge
    proposal = create(knowledge)["result"]["proposal"]
    lab.execute("INSERT INTO samples(sample_id,project_id,label) VALUES('foreign-sample','P2','B')")
    with pytest.raises(CapabilityError) as error:
        lab.invoke(context, "memory.event.correct", {"event_id": proposal["items"][0]["event_id"], "expected_revision": revision,
            "changes": {"sample_id": "foreign-sample"}, "reason": "wrong project"}, operation_id="foreign-correction")
    assert error.value.code in ("forbidden_scope", "schema_violation")
    assert lab.scalar("SELECT count(*) FROM event_revisions") == 1


def test_outdated_source_cannot_be_adopted(knowledge):
    lab, _, page, _ = knowledge
    legacy = create(knowledge, acceptance_mode="review")["result"]["proposal"]
    old_items = [{k: v for k, v in item.items() if k != "acceptance"} for item in legacy["items"]]
    lab.execute("UPDATE memory_proposals SET items_json=? WHERE proposal_id=?", (json.dumps(old_items), legacy["proposal_id"]))
    lab.add_transcript(page["page_id"], ["source corrected"])
    conn = db.connect(lab.db_path)
    try:
        outcome = experiments.backfill_knowledge_proposals(conn, project_id="P1", principal_id="hum_knowledge")
        assert outcome["skipped"][0]["reason"] == "source_changed"
        assert outcome["new_events"] == 0
        conn.rollback()
    finally:
        conn.close()
