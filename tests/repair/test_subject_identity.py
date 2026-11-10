"""Canonical fact subjects are distinct from related or mentioned samples."""
import pytest

from chemclaw_lab.capabilities import CapabilityError


@pytest.fixture
def subjects(lab):
    actor = lab.context("subject_author", memberships={"P1": "member"})
    lab.execute("INSERT INTO samples(sample_id,project_id,label) VALUES('sample-A','P1','A'),('sample-B','P1','B')")
    page = lab.add_page("P1")
    transcript = lab.add_transcript(page["page_id"], ["A and B are mentioned; the observation belongs to B."])
    def create(fields, **extra):
        return lab.invoke(actor, "memory.proposal.create", {"project_id": "P1", "page_id": page["page_id"],
            "transcript_revision": 1, "profile_id": "chemistry_notebook", "operation_id": "subject-create",
            "items": [{"item_key": "value", "event_kind": "observation", "raw_text": "B: 2.1 units; A is related.",
                "fields": {"measurement_value": {"normalized": 2.1, "unit": "units"}, **fields},
                "evidence_refs": [{"page_id": page["page_id"], "transcript_revision": 1,
                                   "block_id": transcript["block_ids"][0]}], **extra}]},
            operation_id="subject-create")["result"]["proposal"]["items"][0]["event_id"]
    def read(event):
        return lab.invoke(actor, "memory.event.read", {"event_id": event}, operation_id="subject-read")["result"]
    return lab, actor, create, read


def test_canonical_single_subject_wins_over_all_related_and_mentioned_labels(subjects):
    lab, actor, create, read = subjects
    event = create({"subject_sample_labels": ["B"], "sample_label": {"normalized": "B"},
        "related_sample_labels": [{"normalized": "A"}], "sample_labels": ["A"],
        "mentioned_sample_labels": ["A", "mentioned-only-C"]})
    record = read(event)["field_json"]
    assert record["sample_ids"] == ["sample-B"]
    assert record["fields"]["measurement_value"]["normalized"] == 2.1
    assert record["fields"]["related_sample_labels"] == [{"normalized": "A"}]
    assert lab.scalar("SELECT count(*) FROM samples") == 2
    for label, count in (("A", 0), ("B", 1)):
        context = lab.invoke(actor, "query.sample.context", {"project_id": "P1", "sample_label": label},
                             operation_id="subject-context")["result"]
        assert len(context["events"]) == count


def test_canonical_shared_subjects_keep_both_explicit_links(subjects):
    _, _, create, read = subjects
    event = create({"subject_sample_labels": ["A", {"normalized": "B"}], "sample_label": "B"})
    assert set(read(event)["field_json"]["sample_ids"]) == {"sample-A", "sample-B"}


def test_empty_canonical_does_not_fall_back_even_to_an_explicit_sample_match(subjects):
    _, _, create, read = subjects
    event = create({"subject_sample_labels": [], "sample_label": "B", "related_sample_labels": ["A"]},
        sample_match={"action": "link", "candidates": [{"sample_id": "sample-B"}]})
    assert read(event)["field_json"]["sample_ids"] == []


def test_uncertain_canonical_subject_does_not_fall_back_to_legacy_identity(subjects):
    _, _, create, read = subjects
    event = create({"subject_sample_labels": [{"normalized": "A", "uncertain": True}], "sample_label": "B"})
    assert read(event)["field_json"]["sample_ids"] == []


def test_legacy_union_is_preserved_only_when_canonical_subjects_are_absent(subjects):
    _, _, create, read = subjects
    event = create({"sample_label": "B", "related_sample_labels": ["A"]})
    assert set(read(event)["field_json"]["sample_ids"]) == {"sample-A", "sample-B"}


def test_canonical_subject_correction_reassigns_then_can_explicitly_detach(subjects):
    lab, actor, create, read = subjects
    event = create({"sample_label": "B", "related_sample_labels": ["A"]})
    lab.invoke(actor, "memory.event.correct", {"event_id": event, "expected_revision": 1,
        "changes": {"fields": {"subject_sample_labels": ["B"]}}, "reason": "Only B owns this observation"},
        operation_id="subject-correction")
    assert read(event)["field_json"]["sample_ids"] == ["sample-B"]
    lab.invoke(actor, "memory.event.correct", {"event_id": event, "expected_revision": 2,
        "changes": {"fields": {"sample_label": "A"}}, "reason": "Preserve canonical subject despite legacy metadata"},
        operation_id="legacy-metadata")
    assert read(event)["field_json"]["sample_ids"] == ["sample-B"]
    lab.invoke(actor, "memory.event.correct", {"event_id": event, "expected_revision": 3,
        "changes": {"fields": {"subject_sample_labels": []}}, "reason": "Attribution unknown"},
        operation_id="detach")
    current = read(event)
    assert current["field_json"]["sample_ids"] == []
    assert current["field_json"]["fields"]["measurement_value"]["normalized"] == 2.1
    assert lab.scalar("SELECT count(*) FROM event_revisions WHERE entity_id=?", (event,)) == 4


def test_malformed_canonical_is_rejected_instead_of_falling_back(subjects):
    _, _, create, _ = subjects
    with pytest.raises(CapabilityError) as error:
        create({"subject_sample_labels": "B", "sample_label": "B"})
    assert error.value.code == "invalid_items"
