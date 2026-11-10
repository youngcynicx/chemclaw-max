"""Live-model compatibility: legacy top-level item fields are shape-normalized."""

from __future__ import annotations

from copy import deepcopy

import pytest

from chemclaw_lab import db, experiments
from chemclaw_lab.sample_cards import enrich_samples


def _proposal_items(lab, page, transcript, items):
    lab.execute(
        "INSERT INTO samples (sample_id, project_id, label) "
        "VALUES ('smp_norm', 'P1', 'SAMPLE-002')"
    )
    conn = db.connect(lab.db_path)
    try:
        created = experiments.create_proposal(
            conn,
            project_id="P1",
            page_id=page["page_id"],
            transcript_revision=1,
            profile_id="chemistry_notebook",
            profile_version="1.0",
            items=items,
            created_by="hum_norm",
            operation_id="op-norm-create",
            acceptance_mode="review",  # This legacy test covers review -> inventory apply.
        )
        conn.commit()
        applied = experiments.apply_proposal(
            conn,
            proposal_id=created["proposal"]["proposal_id"],
            principal_id="hum_norm",
            operation_id="op-norm-apply",
        )
        conn.commit()
    finally:
        conn.close()
    return created, applied


def test_top_level_amount_unit_and_sample_are_lifted_into_fields(lab):
    page = lab.add_page("P1")
    transcript = lab.add_transcript(page["page_id"], ["取用氯化钠 8 g 加入 SAMPLE-002 缓冲液。"])
    block = transcript["block_ids"][0]
    created, applied = _proposal_items(lab, page, transcript, [
        {
            "item_key": "item-1",
            "event_kind": "operation",
            "raw_text": "取用氯化钠 8 g 加入 SAMPLE-002 缓冲液。",
            # Deliberately the legacy/model shape: values at item top level.
            "amount": {"normalized": 8, "unit": "g"},
            "sample_label": {"normalized": "SAMPLE-002"},
            "evidence_refs": [
                {"page_id": page["page_id"], "transcript_revision": 1, "block_id": block}
            ],
            "needs_confirmation": False,
        }
    ])
    items = created["proposal"]["items"]
    assert items[0]["fields"]["amount"]["normalized"] == 8
    assert items[0]["fields"]["sample_label"]["normalized"] == "SAMPLE-002"
    assert applied["applied"][0]["sample_id"] == "smp_norm"
    outbox = lab.rows(
        "SELECT message_type, payload_json FROM outbox WHERE message_type = 'inventory_proposal'"
    )
    assert len(outbox) == 1
    assert '"amount": 8' in outbox[0]["payload_json"]
    assert '"unit": "g"' in outbox[0]["payload_json"]


def test_duration_is_not_sent_to_inventory(lab):
    page = lab.add_page("P1")
    transcript = lab.add_transcript(page["page_id"], ["磁力搅拌 30 分钟。"])
    block = transcript["block_ids"][0]
    _created, applied = _proposal_items(lab, page, transcript, [
        {
            "item_key": "item-duration",
            "event_kind": "operation",
            "raw_text": "磁力搅拌 30 分钟。",
            "amount": {"normalized": 30, "unit": "分钟"},
            "sample_label": {"normalized": "SAMPLE-002"},
            "evidence_refs": [
                {"page_id": page["page_id"], "transcript_revision": 1, "block_id": block}
            ],
            "needs_confirmation": False,
        }
    ])
    assert applied["applied"]
    assert lab.scalar(
        "SELECT COUNT(*) FROM outbox WHERE message_type = 'inventory_proposal'"
    ) == 0


def _adopt_label_array(lab, array_key, *, nested=None):
    author = lab.context("shape_author", memberships={"P1": "member"})
    page = lab.add_page("P1")
    transcript = lab.add_transcript(page["page_id"], ["A, B and C share this explicitly attributed observation."])
    item = {"item_key": "shared", "event_kind": "observation", "raw_text": "shared observation",
        array_key: ["A", {"normalized": "B"}, {"normalized": "C"}],
        "fields": {} if nested is None else {array_key: nested},
        "evidence_refs": [{"page_id": page["page_id"], "transcript_revision": 1,
                           "block_id": transcript["block_ids"][0]}]}
    raw = deepcopy(item)
    conn = db.connect(lab.db_path)
    try:
        created = experiments.create_proposal(conn, project_id="P1", page_id=page["page_id"],
            transcript_revision=1, profile_id="chemistry_notebook", profile_version="1.0",
            items=[item], created_by=author.principal_id, operation_id="array-shape")
        conn.commit()
    finally:
        conn.close()
    assert item == raw, "shape compatibility must not mutate the original model item"
    return author, created, raw


@pytest.mark.parametrize("array_key", ["related_sample_labels", "sample_labels", "subject_sample_labels"])
def test_top_level_explicit_sample_arrays_link_one_event_to_each_sample_and_context(lab, array_key):
    author, created, raw = _adopt_label_array(lab, array_key)
    stored = created["proposal"]["items"][0]
    assert stored["fields"][array_key] == raw[array_key]
    assert stored[array_key] == raw[array_key]  # Preserve the auditable legacy item too.
    assert lab.scalar("SELECT count(*) FROM samples") == 3
    assert lab.scalar("SELECT count(*) FROM event_revisions") == 1
    assert lab.scalar("SELECT count(*) FROM event_confirmations") == 0
    assert lab.scalar("SELECT count(*) FROM inventory_transactions") == 0
    conn = db.connect(lab.db_path)
    try:
        cards = enrich_samples(conn, [dict(row) for row in conn.execute("SELECT * FROM samples")], ["P1"])
    finally:
        conn.close()
    assert not cards["unassigned_records"]
    assert {card["label"] for card in cards["samples"]} == {"A", "B", "C"}
    assert all(len(card["records"]) == 1 for card in cards["samples"])
    for label in ("A", "B", "C"):
        context = lab.invoke(author, "query.sample.context", {"project_id": "P1", "sample_label": label},
                             operation_id="array-read")["result"]
        assert [event["event_id"] for event in context["events"]] == [stored["event_id"]]
        assert len(context["events"][0]["sample_ids"]) == 3
        assert context["quality"]["unassigned_records_on_linked_pages"] == 0


@pytest.mark.parametrize("array_key", ["related_sample_labels", "sample_labels", "subject_sample_labels"])
@pytest.mark.parametrize("nested", [["Nested-A", {"normalized": "Nested-B"}], []])
def test_nested_sample_arrays_take_precedence_even_when_explicitly_empty(lab, array_key, nested):
    _, created, raw = _adopt_label_array(lab, array_key, nested=nested)
    stored = created["proposal"]["items"][0]
    assert stored["fields"][array_key] == nested
    assert stored[array_key] == raw[array_key]
    assert {row["label"] for row in lab.rows("SELECT label FROM samples")} == (
        {"Nested-A", "Nested-B"} if nested else set())


@pytest.mark.parametrize("array_key", ["related_sample_labels", "sample_labels", "subject_sample_labels"])
def test_invalid_top_level_sample_array_uses_existing_identity_validation(array_key):
    normalized = experiments._normalize_proposal_items([{array_key: "not-an-array", "fields": {}}])[0]
    with pytest.raises(experiments.MemoryError, match="must be an array"):
        experiments._explicit_sample_labels(normalized)
