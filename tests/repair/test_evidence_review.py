"""Evidence workspace: list pages, previews and edit candidate facts."""

from __future__ import annotations

import io
import json

import pytest

from chemclaw_lab import db, ingest


def _jpeg() -> bytes:
    PIL = pytest.importorskip("PIL.Image")
    image = PIL.new("RGB", (800, 600), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def _seed_page_and_proposal(lab):
    conn = db.connect(lab.db_path)
    try:
        doc = ingest.create_document(
            conn, project_id="P1", notebook_code="NB-REVIEW", title="review"
        )
        page = ingest.ingest_page(
            conn, document_id=doc["document_id"], content=_jpeg(),
            filename="photo.jpg", page_order=1,
            storage_root=lab.settings.storage_root,
            project_id="P1", source_channel="test", declared_mime="image/jpeg",
        )
        ingest.create_preview(
            conn, page_id=page["page_id"], storage_root=lab.settings.storage_root
        )
        proposal_id = "prop_review"
        items = [{
            "item_key": "item-1",
            "event_kind": "operation",
            "raw_text": "取用 8 g",
            "fields": {
                "amount": {"normalized": 8, "unit": "g"},
                "sample_label": {"normalized": "SAMPLE-002"},
            },
            "needs_confirmation": True,
        }]
        conn.execute(
            "INSERT INTO memory_proposals "
            "(proposal_id, project_id, page_id, profile_id, profile_version, "
            "items_json, revision, status, created_by) "
            "VALUES (?, 'P1', ?, 'chemistry_notebook', '1.0', ?, 0, 'awaiting_confirmation', 'x')",
            (proposal_id, page["page_id"], json.dumps(items)),
        )
        conn.commit()
        return page, proposal_id
    finally:
        conn.close()


def test_evidence_list_and_preview(lab):
    page, proposal_id = _seed_page_and_proposal(lab)
    member = lab.context("hum_review", memberships={"P1": "member"})
    listed = lab.invoke(
        member, "evidence.pages.list", {"project_ids": ["P1"]},
        operation_id="op-evidence-list",
    )["result"]
    item = next(p for p in listed["pages"] if p["page_id"] == page["page_id"])
    assert item["preview_materialized"] is True
    assert item["proposal_count"] == 1
    assert item["latest_proposal_id"] == proposal_id
    assert item["filename"].endswith("photo.jpg")

    preview = lab.invoke(
        member, "evidence.page.preview", {"page_id": page["page_id"]},
        operation_id="op-evidence-preview",
    )["result"]
    assert preview["materialized"] is True
    assert preview["mime"] == "image/png"
    assert preview["data_b64"]


def test_auto_confirm_only_safe_quantitative_items(lab):
    from chemclaw_lab import experiments

    lab.execute(
        "INSERT INTO samples (sample_id, project_id, label) VALUES ('smp_safe', 'P1', 'S-1')"
    )
    page = lab.add_page("P1")
    transcript = lab.add_transcript(page["page_id"], ["取用 0.5 g S-1"])
    conn = db.connect(lab.db_path)
    try:
        proposal = experiments.create_proposal(
            conn,
            project_id="P1",
            page_id=page["page_id"],
            transcript_revision=1,
            profile_id="chemistry_notebook",
            profile_version="1.0",
            items=[{
                "item_key": "safe",
                "event_kind": "operation",
                "raw_text": "取用 0.5 g S-1",
                "fields": {
                    "amount": {"normalized": 0.5, "unit": "g"},
                    "sample_label": {"normalized": "S-1"},
                },
                "evidence_refs": [{
                    "page_id": page["page_id"], "transcript_revision": 1,
                    "block_id": transcript["block_ids"][0],
                }],
            }],
            created_by="hum_safe",
            operation_id="op-safe",
            auto_confirm_safe=True,
        )
        conn.commit()
        item = proposal["proposal"]["items"][0]
        assert item["needs_confirmation"] is False
    finally:
        conn.close()


def test_auto_confirm_still_requires_confirmation_for_ambiguous_or_bad_unit(lab):
    from chemclaw_lab import experiments

    page = lab.add_page("P1")
    transcript = lab.add_transcript(page["page_id"], ["取用 0.5 kg S-404"])
    conn = db.connect(lab.db_path)
    try:
        proposal = experiments.create_proposal(
            conn,
            project_id="P1",
            page_id=page["page_id"],
            transcript_revision=1,
            profile_id="chemistry_notebook",
            profile_version="1.0",
            items=[{
                "item_key": "unsafe",
                "event_kind": "operation",
                "raw_text": "取用 0.5 kg S-404",
                "fields": {
                    "amount": {"normalized": 0.5, "unit": "kg"},
                    "sample_label": {"normalized": "S-404"},
                },
                "evidence_refs": [{
                    "page_id": page["page_id"], "transcript_revision": 1,
                    "block_id": transcript["block_ids"][0],
                }],
            }],
            created_by="hum_safe2",
            operation_id="op-unsafe",
            auto_confirm_safe=True,
            acceptance_mode="review",  # This case exercises the explicit legacy review policy.
        )
        conn.commit()
        assert proposal["proposal"]["items"][0]["needs_confirmation"] is True
    finally:
        conn.close()


def test_candidate_edit_creates_new_revision(lab):
    page, proposal_id = _seed_page_and_proposal(lab)
    member = lab.context("hum_review2", memberships={"P1": "member"})
    edited = lab.invoke(
        member, "memory.proposal.item.update",
        {
            "proposal_id": proposal_id,
            "item_key": "item-1",
            "expected_proposal_revision": 0,
            "changes": {
                "fields": {"amount": {"normalized": 7.5, "unit": "g"}},
                "needs_confirmation": True,
            },
        },
        operation_id="op-proposal-edit",
    )["result"]["proposal"]
    assert edited["revision"] == 1
    assert edited["status"] == "awaiting_confirmation"
    assert edited["items"][0]["fields"]["amount"]["normalized"] == 7.5
    assert edited["items"][0]["edited_by"] == "hum_review2"

    with pytest.raises(Exception) as excinfo:
        lab.invoke(
            member, "memory.proposal.item.update",
            {
                "proposal_id": proposal_id,
                "item_key": "item-1",
                "expected_proposal_revision": 0,
                "changes": {"fields": {}},
            },
            operation_id="op-proposal-stale",
        )
    assert "revision" in str(excinfo.value) or "409" in str(excinfo.value)
