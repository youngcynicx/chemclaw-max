"""R07/F08: citation opens, field match and source support are independent."""

from __future__ import annotations

from chemclaw_lab import answers, db


def _answer(lab, question="S-9 用量？"):
    member = lab.context("hum_claim", memberships={"P1": "member"})
    conn = db.connect(lab.db_path)
    try:
        return answers.build_answer(conn, member, question)
    finally:
        conn.close()


def _setup(lab, text: str, amount: float, *, provenance=None, revision=1,
           supersedes=None, event_id=None):
    lab.execute(
        "INSERT OR IGNORE INTO samples (sample_id, project_id, label) "
        "VALUES ('smp_claim', 'P1', 'S-9')"
    )
    page = lab.add_page("P1")
    transcript = lab.add_transcript(page["page_id"], [text])
    refs = [
        {
            "page_id": page["page_id"],
            "transcript_revision": 1,
            "block_id": transcript["block_ids"][0],
        }
    ]
    event_id = lab.add_event(
        project_id="P1",
        sample_id="smp_claim",
        amount=amount,
        unit="g",
        raw_text=text,
        evidence_refs=refs,
        provenance=provenance,
        revision=revision,
        event_id=event_id,
        supersedes_revision_id=supersedes,
    )
    return event_id, transcript["block_ids"][0]


def test_supported_claim_is_answered_with_three_checks(lab):
    _setup(lab, "实际取用 0.5 g S-9", 0.5)
    answer = _answer(lab)
    assert answer["status"] == "answered"
    claim = next(
        c for c in answer["claims"]
        if c["field_pointer"] == "fields.amount"
    )
    assert claim["verify"]["ref_opens"] is True
    assert claim["verify"]["field_matches"] is True
    assert claim["verify"]["source_supports"] is True
    assert claim["verify"]["support_kind"] == "source_text"


def test_source_that_does_not_support_value_becomes_needs_review(lab):
    _setup(lab, "实际取用 9 g S-9", 0.5)
    answer = _answer(lab)
    assert answer["status"] == "needs_review"
    claim = next(
        c for c in answer["claims"]
        if c["field_pointer"] == "fields.amount"
    )
    assert claim["verify"]["ref_opens"] is True
    assert claim["verify"]["field_matches"] is True
    assert claim["verify"]["source_supports"] is False
    assert claim["verify"]["value_matches"] is False
    assert any(g["kind"] == "claim_unsupported" for g in answer["gaps"])
    assert "待核对" in answer["rendered_text"]
    assert not any(g["kind"] == "insufficient_records" for g in answer["gaps"])


def test_missing_citation_fails_ref_open(lab):
    lab.execute(
        "INSERT INTO samples (sample_id, project_id, label) "
        "VALUES ('smp_claim', 'P1', 'S-9')"
    )
    lab.add_page("P1")
    event_id = lab.add_event(
        project_id="P1",
        sample_id="smp_claim",
        amount=0.5,
        unit="g",
        raw_text="实际取用 0.5 g S-9",
        evidence_refs=[
            {"page_id": "pg_missing", "transcript_revision": 1, "block_id": "nope"}
        ],
    )
    answer = _answer(lab)
    claim = next(
        c for c in answer["claims"] if c["field_pointer"] == "fields.amount"
    )
    assert claim["object_id"] == event_id
    assert claim["verify"]["ref_opens"] is False
    assert claim["verify"]["source_supports"] is False
    assert answer["status"] == "needs_review"


def test_human_correction_statement_supports_new_value(lab):
    event_id, block_id = _setup(lab, "实际取用 5 g S-9", 5.0)
    # Revision 2 is a human correction; the old photo still says 5 g, but the
    # correction statement explicitly says 0.2 g.
    lab.add_event(
        project_id="P1",
        sample_id="smp_claim",
        amount=0.2,
        unit="g",
        raw_text="实际取用 5 g S-9",
        evidence_refs=[
            {
                "page_id": lab.rows(
                    "SELECT json_extract(field_json, '$.page_id') AS p "
                    "FROM event_revisions WHERE entity_id = ? AND revision = 1",
                    (event_id,),
                )[0]["p"],
                "transcript_revision": 1,
                "block_id": block_id,
            }
        ],
        provenance={
            "correction_of": 1,
            "reason": "天平读数看错，实际 0.2 g",
            "correction_source": {"kind": "human_report", "operator": "hum_claim"},
        },
        revision=2,
        event_id=event_id,
        supersedes_revision_id=None,
    )
    answer = _answer(lab)
    claim = next(
        c for c in answer["claims"]
        if c["field_pointer"] == "fields.amount"
    )
    assert claim["value"] == 0.2
    assert claim["verify"]["source_supports"] is True
    assert claim["verify"]["support_kind"] == "human_report"
    assert answer["status"] == "answered"
