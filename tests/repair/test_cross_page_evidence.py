"""Current notebook knowledge joins exact same-project sources without guessing."""
from copy import deepcopy
import json

import pytest

from chemclaw_lab import db, experiments
from chemclaw_lab.capabilities import CapabilityError


@pytest.fixture()
def pages(lab):
    actor = lab.context("cross_page_writer", memberships={"P1": "member", "P2": "member"})
    viewer = lab.context("cross_page_reader", kind="service", memberships={"P1": "viewer"})
    anchor, support, foreign = lab.add_page("P1"), lab.add_page("P1"), lab.add_page("P2")
    first = lab.add_transcript(anchor["page_id"], ["A and B share an operation."])
    old = lab.add_transcript(support["page_id"], ["Old support text."])
    current = lab.add_transcript(support["page_id"], ["Shared operation details; A also has an individual observation."])
    other = lab.add_transcript(foreign["page_id"], ["Foreign project text."])
    def ref(page, transcript, revision):
        return {"page_id": page["page_id"], "transcript_revision": revision, "block_id": transcript["block_ids"][0]}
    anchor_ref, support_ref = ref(anchor, first, 1), ref(support, current, 2)
    items = [
        {"item_key": "joint", "event_kind": "operation", "raw_text": "A and B share an operation.",
         "fields": {"subject_sample_labels": ["A", "B"], "condition": {"normalized": 75, "unit": "C"}},
         "evidence_refs": [anchor_ref, support_ref]},
        {"item_key": "support_only", "event_kind": "observation", "raw_text": "A individual observation.",
         "fields": {"subject_sample_labels": ["A"], "observation": "individual"}, "evidence_refs": [support_ref]},
    ]
    params = {"project_id": "P1", "page_id": anchor["page_id"], "transcript_revision": 1,
              "profile_id": "chemistry_notebook", "items": items, "operation_id": "cross-pages"}
    return {"lab": lab, "actor": actor, "viewer": viewer, "anchor": anchor, "support": support,
            "first": first, "old": old, "current": current, "foreign": foreign, "other": other,
            "params": params, "ref": ref}


def create(data, params=None, operation="cross-pages"):
    params = {**(params or data["params"]), "operation_id": operation}
    return data["lab"].invoke(data["actor"], "memory.proposal.create", params, operation_id=operation)["result"]


def counts(lab):
    return {table: lab.scalar("SELECT count(*) FROM " + table) for table in
            ("samples", "memory_proposals", "event_revisions", "event_confirmations", "inventory_transactions")}


def test_shared_sources_with_distinct_versions_and_support_only_item(pages):
    data = pages
    result = create(data)
    proposal = result["proposal"]
    assert proposal["status"] == "applied" and len(result["knowledge"]["adopted"]) == 2
    assert data["lab"].scalar("SELECT count(*) FROM event_confirmations") == 0
    viewer = data["viewer"]
    for label, expected in (("A", 2), ("B", 1)):
        context = data["lab"].invoke(viewer, "query.sample.context", {"project_id": "P1", "sample_label": label})["result"]
        assert context["status"] == "complete" and len(context["events"]) == expected
        joint = next(event for event in context["events"] if event["event_kind"] == "operation")
        assert joint["evidence_refs"] == data["params"]["items"][0]["evidence_refs"]
        assert joint["page_id"] == data["anchor"]["page_id"]
        assert joint["provenance"]["source_transcript_revision"] == 1
        assert not joint["source_stale"]
        assert {(unit["page_id"], unit["transcript_revision"]) for unit in context["evidence"]} == {
            (data["anchor"]["page_id"], 1), (data["support"]["page_id"], 2)}


@pytest.mark.parametrize("problem", ["no_anchor", "foreign", "wrong_page", "missing_block", "old_block",
    "old_revision", "future_revision", "missing_revision", "boolean_revision", "missing_active",
    "wrong_active_run", "failed_active_run", "foreign_object", "empty_item_refs", "non_list_refs", "non_object_ref",
    "list_block", "object_block", "blank_block"])
def test_invalid_support_is_rejected_atomically(pages, problem):
    data, lab = pages, pages["lab"]
    params = deepcopy(data["params"])
    ref = params["items"][0]["evidence_refs"][1]
    expected = "invalid_evidence"
    if problem == "no_anchor":
        params["items"][0]["evidence_refs"] = [ref]
    elif problem == "foreign":
        ref.update(data["ref"](data["foreign"], data["other"], 1))
        expected = "forbidden_scope"
    elif problem == "wrong_page":
        ref["page_id"] = data["anchor"]["page_id"]
    elif problem == "missing_block":
        ref["block_id"] = "nonexistent-block"
    elif problem in ("list_block", "object_block", "blank_block"):
        ref["block_id"] = {"list_block": [], "object_block": {}, "blank_block": "  "}[problem]
    elif problem == "old_block":
        ref["block_id"] = data["old"]["block_ids"][0]
    elif problem == "old_revision":
        ref["transcript_revision"] = 1
    elif problem == "future_revision":
        ref["transcript_revision"] = 3
    elif problem == "missing_revision":
        del ref["transcript_revision"]
    elif problem == "boolean_revision":
        ref["transcript_revision"] = True
    elif problem == "missing_active":
        lab.execute("DELETE FROM page_transcripts WHERE page_id=?", (data["support"]["page_id"],))
    elif problem == "wrong_active_run":
        lab.execute("UPDATE page_transcripts SET active_run_id=? WHERE page_id=?", (data["first"]["run_id"], data["support"]["page_id"]))
    elif problem == "failed_active_run":
        lab.execute("UPDATE transcript_runs SET status='failed' WHERE run_id=?", (data["current"]["run_id"],))
    elif problem == "foreign_object":
        lab.execute("UPDATE evidence_objects SET project_id='P2' WHERE evidence_id=(SELECT evidence_id FROM evidence_pages WHERE page_id=?)", (data["support"]["page_id"],))
        expected = "forbidden_scope"
    elif problem == "empty_item_refs":
        params["items"][1]["evidence_refs"] = []
    elif problem == "non_list_refs":
        params["items"][1]["evidence_refs"] = {"block_id": "invalid-shape"}
    elif problem == "non_object_ref":
        params["items"][1]["evidence_refs"] = ["invalid-shape"]
    before = counts(lab)
    with pytest.raises(CapabilityError) as error:
        create(data, params)
    assert error.value.code == expected
    assert counts(lab) == before


def test_support_update_marks_both_views_stale_and_keeps_exact_old_blocks(pages):
    data, lab = pages, pages["lab"]
    create(data)
    lab.add_transcript(data["support"]["page_id"], ["Replacement support text."])
    cards = lab.invoke(data["viewer"], "query.samples.list", {"project_ids": ["P1"]})["result"]["samples"]
    for card in cards:
        context = lab.invoke(data["viewer"], "query.sample.context", {"project_id": "P1", "sample_id": card["sample_id"]})["result"]
        assert context["status"] == "complete"
        human = {event["event_id"]: event for event in card["records"]}
        for event in context["events"]:
            assert event["source_stale"] and human[event["event_id"]]["source_stale"]
            assert event["fields"] == human[event["event_id"]]["fields"]
            assert event["evidence_refs"] == human[event["event_id"]]["evidence_refs"]
        old = next(unit for unit in context["evidence"] if unit["page_id"] == data["support"]["page_id"])
        assert old["transcript_revision"] == 2 and old["active_revision"] == 3 and old["source_stale"]
        assert old["block_id"] == data["current"]["block_ids"][0]
        assert old["blocks"][0]["text"].startswith("Shared operation details")


def test_adoption_rechecks_support_after_a_proposal_was_saved(pages):
    data, lab = pages, pages["lab"]
    params = deepcopy(data["params"])
    # Retain a real legacy review proposal, then supply cross-page sources as
    # an already persisted pre-adoption extraction. Adoption must revalidate.
    params["acceptance_mode"] = "review"
    original_items = deepcopy(params["items"])
    params["items"] = [params["items"][0]]
    params["items"][0]["evidence_refs"] = [params["items"][0]["evidence_refs"][0]]
    proposal = create(data, params)["proposal"]
    lab.execute("UPDATE memory_proposals SET items_json=? WHERE proposal_id=?", (json.dumps(original_items), proposal["proposal_id"]))
    lab.add_transcript(data["support"]["page_id"], ["Support changed after extraction."])
    before = counts(lab)
    connection = db.connect(lab.db_path)
    try:
        with pytest.raises(experiments.MemoryError) as error:
            experiments.accept_knowledge_proposal(connection, project_id="P1", proposal_id=proposal["proposal_id"], principal_id=data["actor"].principal_id)
        assert error.value.code == "invalid_evidence"
        connection.rollback()
    finally:
        connection.close()
    assert counts(lab) == before


def test_legacy_review_stays_single_page_and_historical_creation_is_unchanged(pages):
    data, lab = pages, pages["lab"]
    params = deepcopy(data["params"])
    params["acceptance_mode"] = "review"
    with pytest.raises(CapabilityError) as error:
        create(data, params)
    assert error.value.code == "invalid_evidence"
    params["items"] = [params["items"][0]]
    params["items"][0]["evidence_refs"] = [params["items"][0]["evidence_refs"][0]]
    lab.add_transcript(data["anchor"]["page_id"], ["New anchor text."])
    proposal = create(data, params)["proposal"]
    assert proposal["status"] == "awaiting_confirmation"
    assert lab.scalar("SELECT count(*) FROM event_revisions") == 0


def test_single_page_compatibility_and_replay_do_not_overwrite_correction(pages):
    data, lab = pages, pages["lab"]
    params = deepcopy(data["params"])
    params["items"] = [params["items"][0]]
    params["items"][0]["evidence_refs"] = [params["items"][0]["evidence_refs"][0]]
    first = create(data, params)
    event_id = first["knowledge"]["adopted"][0]["event_id"]
    lab.invoke(data["actor"], "memory.event.correct", {"event_id": event_id, "expected_revision": 1,
        "changes": {"fields": {"condition": {"normalized": 76, "unit": "C"}}}, "reason": "Human correction"}, operation_id="correct-single")
    create(data, params)
    read = lab.invoke(data["viewer"], "memory.event.read", {"event_id": event_id})["result"]
    assert read["revision"] == 2 and read["field_json"]["fields"]["condition"]["normalized"] == 76
