"""Explicit research state writes and projections; no temporal/text inference."""
from copy import deepcopy
import json

import pytest

from chemclaw_lab import db, experiments, knowledge_state
from chemclaw_lab.capabilities import CapabilityError


def _write(lab, actor, fields, *, kind="observation", project="P1", operation="new"):
    page = lab.add_page(project)
    transcript = lab.add_transcript(page["page_id"], ["Synthetic cited record for structural state tests."])
    result = lab.invoke(actor, "memory.proposal.create", {"project_id": project,
        "page_id": page["page_id"], "transcript_revision": 1, "profile_id": "chemistry_notebook",
        "operation_id": operation, "items": [{"item_key": "fact", "event_kind": kind,
            "raw_text": "Synthetic cited record for structural state tests.", "fields": fields,
            "evidence_refs": [{"page_id": page["page_id"], "transcript_revision": 1,
                               "block_id": transcript["block_ids"][0]}]}]}, operation_id=operation)
    return result["result"]["proposal"]["items"][0]["event_id"]


def _record(lab, event_id, revision=None):
    conn = db.connect(lab.db_path)
    try:
        raw = experiments.read_event(conn, event_id) if revision is None else next(
            record for record in experiments.read_event_history(conn, event_id)["revisions"] if record["revision"] == revision)
    finally:
        conn.close()
    return {**raw["field_json"], "event_id": event_id, "revision": raw["revision"],
        "knowledge_as_of": raw["knowledge_as_of"], "evidence_refs": raw["evidence_refs"]}


def _update(event_id, labels, status="completed", revision=1, **extra):
    return {"target_event_id": event_id, "target_revision": revision, "status": status,
            "subject_sample_labels": labels, "reason": "Explicitly reported in cited record", **extra}


@pytest.fixture
def state_lab(lab):
    actor = lab.context("state_author", memberships={"P1": "member", "P2": "member"})
    fields = {"sample_labels": ["A", "B"], "state": {"status": "pending"},
              "planned_temperature": {"normalized": 120, "unit": "C"}}
    plan = _write(lab, actor, fields, kind="plan", operation="plan")
    ids = {row["label"]: row["sample_id"] for row in lab.rows("SELECT label,sample_id FROM samples")}
    return lab, actor, plan, ids


def test_shared_plan_subjects_update_independently_and_canonical_ids_are_recomputed(state_lab):
    lab, actor, plan, ids = state_lab
    update = _update(plan, ["A"], subject_sample_ids=[ids["B"]])
    fields = {"sample_labels": ["A"], "measured_temperature": {"normalized": 117, "unit": "C"},
              "state_updates": [update]}
    original = deepcopy(fields)
    done = _write(lab, actor, fields, operation="done-A")
    assert fields == original
    stored = _record(lab, done)
    assert stored["fields"]["state_updates"][0]["subject_sample_ids"] == [ids["A"]]
    result = knowledge_state.project_states([_record(lab, plan), stored])
    assert {(s["sample_id"], s["status"]) for s in result["states"]} == {(ids["A"], "completed"), (ids["B"], "pending")}
    assert next(s for s in result["states"] if s["sample_id"] == ids["A"])["basis"]["event_id"] == done
    assert _record(lab, plan)["fields"]["planned_temperature"]["normalized"] == 120
    assert stored["fields"]["measured_temperature"]["normalized"] == 117
    assert not knowledge_state.project_states([_record(lab, plan), stored], ids["B"])["edges"]
    assert lab.scalar("SELECT count(*) FROM inventory_transactions") == 0
    assert lab.scalar("SELECT count(*) FROM event_confirmations") == 0


@pytest.mark.parametrize("mutation,code", [
    ({"target_event_id": "not-a-real-id"}, "state_target_not_found"),
    ({"target_revision": 2}, "state_target_changed"),
    ({"target_revision": True}, "invalid_state_fields"),
    ({"subject_sample_labels": []}, "invalid_state_fields"),
    ({"subject_sample_labels": ["B"]}, "invalid_state_subject"),
    ({"subject_sample_labels": ["missing"]}, "state_subject_unresolved"),
    ({"status": {"completed": True}}, "invalid_state_fields"),
    ({"target_item_key": "local-plan"}, "invalid_state_fields"),
])
def test_invalid_reference_or_subject_is_rejected_without_saving_fact(state_lab, mutation, code):
    lab, actor, plan, _ = state_lab
    count = lab.scalar("SELECT count(*) FROM event_revisions")
    with pytest.raises(CapabilityError) as error:
        _write(lab, actor, {"sample_labels": ["A"], "state_updates": [{**_update(plan, ["A"]), **mutation}]}, operation="invalid")
    assert error.value.code == code
    assert lab.scalar("SELECT count(*) FROM event_revisions") == count


def test_cross_project_target_and_ambiguous_alias_are_rejected(state_lab):
    lab, actor, plan, ids = state_lab
    foreign = _write(lab, actor, {"sample_labels": ["A"]}, project="P2", operation="foreign")
    with pytest.raises(CapabilityError) as error:
        _write(lab, actor, {"sample_labels": ["A"], "state_updates": [_update(foreign, ["A"])]}, operation="cross")
    assert error.value.code == "forbidden_scope"
    for sid in ids.values():
        lab.execute("UPDATE samples SET aliases_json='[\"shared-alias\"]' WHERE sample_id=?", (sid,))
    with pytest.raises(CapabilityError) as error:
        _write(lab, actor, {"sample_labels": ["A"], "state_updates": [_update(plan, ["shared-alias"])]}, operation="ambiguous")
    assert error.value.code == "state_subject_unresolved"


def test_later_target_revision_invalidates_old_relation_but_unrelated_source_edit_still_succeeds(state_lab):
    lab, actor, plan, ids = state_lab
    done = _write(lab, actor, {"sample_labels": ["A"], "state_updates": [_update(plan, ["A"])]}, operation="done")
    lab.invoke(actor, "memory.event.correct", {"event_id": plan, "expected_revision": 1,
        "changes": {"fields": {"planned_temperature": {"normalized": 130, "unit": "C"}}},
        "reason": "Plan revision"}, operation_id="revise-plan")
    lab.invoke(actor, "memory.event.correct", {"event_id": done, "expected_revision": 1,
        "changes": {"fields": {"product_mass": {"normalized": 22, "unit": "mg"}}},
        "reason": "Independent mass correction"}, operation_id="mass")
    result = knowledge_state.project_states([_record(lab, plan), _record(lab, done)], ids["A"])
    assert result["states"][0]["status"] == "pending"
    assert result["issues"][0]["kind"] == "state_target_revision_mismatch"
    assert result["edges"][0]["applied"] is False
    with pytest.raises(CapabilityError) as error:
        lab.invoke(actor, "memory.event.correct", {"event_id": done, "expected_revision": 2,
            "changes": {"fields": {"state_updates": [_update(plan, ["A"])]}}, "reason": "Reuse stale relation"},
            operation_id="stale-relation")
    assert error.value.code == "state_target_changed"


def test_snapshot_selected_source_revisions_preserve_old_state_and_allow_explicit_retraction(state_lab):
    lab, actor, plan, ids = state_lab
    done = _write(lab, actor, {"sample_labels": ["A"], "state_updates": [_update(plan, ["A"])]}, operation="done")
    old_plan, old_done = _record(lab, plan), _record(lab, done)
    assert knowledge_state.project_states([old_plan], ids["A"])["states"][0]["status"] == "pending"
    assert knowledge_state.project_states([old_plan, old_done], ids["A"])["states"][0]["status"] == "completed"
    lab.invoke(actor, "memory.event.correct", {"event_id": done, "expected_revision": 1,
        "changes": {"fields": {"state_updates": []}}, "reason": "Withdraw unsupported completion assertion"}, operation_id="withdraw")
    assert knowledge_state.project_states([old_plan, _record(lab, done)], ids["A"])["states"][0]["status"] == "pending"
    assert knowledge_state.project_states([old_plan, old_done], ids["A"])["states"][0]["status"] == "completed"


def test_supersession_is_explicit_and_retains_old_fact(state_lab):
    lab, actor, plan, ids = state_lab
    queued = _write(lab, actor, {"sample_labels": ["A"], "state": {"status": "pending"}}, kind="plan", operation="queued")
    actual = _write(lab, actor, {"sample_labels": ["A"], "state_updates": [
        _update(plan, ["A"], "completed"), _update(queued, ["A"], "superseded")]}, operation="actual")
    result = knowledge_state.project_states([_record(lab, plan), _record(lab, queued), _record(lab, actual)], ids["A"])
    assert {item["event_id"]: item["status"] for item in result["states"]} == {plan: "completed", queued: "superseded"}
    assert lab.scalar("SELECT count(*) FROM event_revisions") == 3


def test_projection_never_infers_from_dates_or_legacy_strings_and_reports_same_time_conflict():
    plan = {"event_id": "plan", "revision": 1, "event_kind": "plan", "sample_ids": ["A", "B"],
        "knowledge_as_of": "2020-01-01 00:00:00", "occurred_at": "1900-01-01", "fields": {"status": "completed"}}
    def claim(event_id, status, time="2026-10-24T08:00:00+08:00"):
        return {"event_id": event_id, "revision": 1, "sample_ids": ["A"], "knowledge_as_of": time,
            "fields": {"state_updates": [{**_update("plan", ["label-A"], status), "subject_sample_ids": ["A"]}]}}
    assert all(item["status"] == "unknown" and item["explicit"] is False
               for item in knowledge_state.project_states([plan])["states"])
    result = knowledge_state.project_states([plan, claim("done", "completed"), claim("cancel", "cancelled")], "A")
    assert result["states"][0]["status"] == "unknown" and result["states"][0]["basis"] is None
    assert result["issues"][0]["kind"] == "conflicting_state_updates"
    later = knowledge_state.project_states([plan, claim("done", "completed"), claim("cancel", "cancelled"),
        claim("new", "in_progress", "2026-10-24T00:01:00Z")], "A")
    assert later["states"][0]["status"] == "in_progress"
    assert later["states"][0]["basis"]["event_id"] == "new"
