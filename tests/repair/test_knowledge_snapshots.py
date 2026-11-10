"""R06/F09: proposal time is not knowledge time; transcripts are versioned."""

from __future__ import annotations

from chemclaw_lab import db, experiments, query


def test_applied_event_gets_knowledge_time_at_apply_not_proposal_creation(lab):
    page = lab.add_page("P1")
    transcript = lab.add_transcript(page["page_id"], ["Sample S-1 0.5 g"])
    conn = db.connect(lab.db_path)
    try:
        conn.execute(
            "INSERT INTO samples (sample_id, project_id, label) VALUES ('smp_1', 'P1', 'S-1')"
        )
        conn.commit()
        created = experiments.create_proposal(
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
                    "fields": {"amount": {"normalized": 0.5, "unit": "g"}},
                    "sample_match": {"action": "link",
                                     "candidates": [{"sample_id": "smp_1"}]},
                    "needs_confirmation": False,
                    "evidence_refs": [
                        {"page_id": page["page_id"], "transcript_revision": 1,
                         "block_id": transcript["block_ids"][0]}
                    ],
                }
            ],
            created_by="hum_snap",
            operation_id="op-snap-create",
        )
        conn.commit()
        # The proposal is old; the formal knowledge event is created only now.
        conn.execute(
            "UPDATE memory_proposals SET created_at = '2020-01-01 00:00:00' "
            "WHERE proposal_id = ?",
            (created["proposal"]["proposal_id"],),
        )
        conn.commit()
        experiments.apply_proposal(
            conn,
            proposal_id=created["proposal"]["proposal_id"],
            principal_id="hum_snap",
            operation_id="op-snap-apply",
        )
        conn.commit()
        row = conn.execute(
            "SELECT knowledge_as_of FROM event_revisions "
            "WHERE entity_type = 'experiment_event'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["knowledge_as_of"] != "2020-01-01 00:00:00"
    assert row["knowledge_as_of"] > "2020-01-01 00:00:00"


def test_fulltext_as_of_does_not_return_future_transcript(lab):
    page = lab.add_page("P1")
    lab.add_transcript(
        page["page_id"], ["future secret block"], created_at="2030-01-01 00:00:00"
    )
    member = lab.context("hum_hist", memberships={"P1": "member"})
    conn = db.connect(lab.db_path)
    try:
        past = query.search(
            conn, member, text="future secret", knowledge_as_of="2020-01-01 00:00:00"
        )
        current = query.search(conn, member, text="future secret")
    finally:
        conn.close()
    assert past["candidates"] == []
    assert any(c["object_type"] == "transcript_block" for c in current["candidates"])


def test_transcript_revision_snapshot_picks_the_then_current_run(lab):
    page = lab.add_page("P1")
    old = lab.add_transcript(
        page["page_id"], ["old 5 g"], created_at="2026-01-01 00:00:00"
    )
    lab.add_transcript(
        page["page_id"], ["new 0.5 g"], created_at="2026-02-01 00:00:00"
    )
    member = lab.context("hum_hist2", memberships={"P1": "member"})
    conn = db.connect(lab.db_path)
    try:
        past = query.search(
            conn, member, text="g", knowledge_as_of="2026-01-15 00:00:00"
        )
        current = query.search(conn, member, text="g")
    finally:
        conn.close()
    past_texts = [c["chunk"] for c in past["candidates"]
                  if c["object_type"] == "transcript_block"]
    current_texts = [c["chunk"] for c in current["candidates"]
                     if c["object_type"] == "transcript_block"]
    assert "old 5 g" in past_texts and "new 0.5 g" not in past_texts
    assert "new 0.5 g" in current_texts and "old 5 g" not in current_texts
