"""Vision OCR import: original bytes stay scoped and imported text is a new run."""

from __future__ import annotations

import base64
import hashlib

import pytest

from chemclaw_lab import db
from chemclaw_lab.capabilities import CapabilityError


def _page_with_original(lab):
    page = lab.add_page("P1")
    content = b"\xff\xd8\xff\xe0" + b"0" * 64
    path = lab.settings.storage_root / f"{page['page_id']}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    conn = db.connect(lab.db_path)
    try:
        conn.execute(
            "UPDATE evidence_objects SET sha256 = ?, mime = 'image/jpeg', storage_path = ? "
            "WHERE evidence_id = (SELECT evidence_id FROM evidence_pages WHERE page_id = ?)",
            (hashlib.sha256(content).hexdigest(), str(path), page["page_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return page, content


def _block(index: int, text: str, block_id: str) -> dict:
    return {
        "block_id": block_id,
        "block_kind": "handwriting",
        "reading_order": index,
        "text": text,
        "table_cell": None,
        "geometry": None,
        "coordinate_system": None,
        "source_page": 1,
        "extractor": {
            "provider": "vision_model",
            "model": "glm-4.6v-flash",
            "version": "1.0",
            "config": {"transport": "openai-chat"},
        },
        "quality_flags": ["geometry_untrusted"],
    }


def test_original_bytes_are_scoped_and_import_creates_revision(lab):
    page, content = _page_with_original(lab)
    member = lab.context("hum_ocr", memberships={"P1": "member"})
    original = lab.invoke(
        member,
        "evidence.page.original",
        {"page_id": page["page_id"]},
        operation_id="op-ocr-original",
    )["result"]
    assert original["mime"] == "image/jpeg"
    assert base64.b64decode(original["data_b64"]) == content
    assert original["sha256"] == hashlib.sha256(content).hexdigest()

    run = lab.invoke(
        member,
        "evidence.transcript.import",
        {
            "page_id": page["page_id"],
            "blocks": [_block(0, "取用 0.5 g", "b1"), _block(1, "SAMPLE-002", "b2")],
            "provider": "vision_model",
            "model": "glm-4.6v-flash",
            "version": "1.0",
            "raw_output": {"blocks": ["取用 0.5 g", "SAMPLE-002"]},
            "needs_human": True,
        },
        operation_id="op-ocr-import",
    )["result"]
    assert run["status"] == "needs_human"
    assert run["active_revision"] == 1
    assert lab.scalar(
        "SELECT active_revision FROM page_transcripts WHERE page_id = ?",
        (page["page_id"],),
    ) == 1
    assert lab.scalar(
        "SELECT COUNT(*) FROM transcript_blocks WHERE run_id = ?",
        (run["run_id"],),
    ) == 2
    assert lab.scalar(
        "SELECT status FROM evidence_pages WHERE page_id = ?", (page["page_id"],)
    ) == "needs_human"


def test_imported_blocks_are_schema_validated(lab):
    page, _ = _page_with_original(lab)
    member = lab.context("hum_ocr2", memberships={"P1": "member"})
    with pytest.raises(CapabilityError) as excinfo:
        lab.invoke(
            member,
            "evidence.transcript.import",
            {
                "page_id": page["page_id"],
                "blocks": [{"block_id": "bad", "text": "x"}],
                "provider": "vision_model",
            },
            operation_id="op-ocr-bad",
        )
    assert excinfo.value.code == "invalid_block_schema"
    assert lab.scalar("SELECT COUNT(*) FROM transcript_runs") == 0
