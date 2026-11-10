"""Research-file allowlist: common types are stored, unknown binary rejected."""

from __future__ import annotations

import io

import pytest

from chemclaw_lab import db, ingest
from chemclaw_lab.ingest import IngestError, sniff_mime


def test_jpeg_is_detected_from_magic_and_extension():
    assert sniff_mime(b"\xff\xd8\xff\xe0" + b"0" * 20, "image/jpeg", "photo.jpg") == "image/jpeg"
    assert sniff_mime(b"\xff\xd8\xff\xe0" + b"0" * 20, None, "photo.JPG") == "image/jpeg"


def test_text_scientific_files_are_refined_by_extension():
    assert sniff_mime(b"a,b\n1,2\n", "text/csv", "data.csv") == "text/csv"
    assert sniff_mime(b"data_x = 1\n", None, "structure.cif") == "chemical/x-cif"
    assert sniff_mime(b">seq\nACGT\n", None, "read.fasta") == "chemical/x-fasta"
    assert sniff_mime(b'{"a": 1}', "application/json", "data.json") == "application/json"


def test_zip_based_office_types_are_refined_by_extension():
    declared = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert sniff_mime(b"PK\x03\x04" + b"0" * 20, declared, "notes.docx") == declared


def test_jpeg_preview_is_materialized_with_pillow(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    image = PIL.new("RGB", (800, 600), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    content = buffer.getvalue()

    db_path = tmp_path / "preview.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        conn.execute("INSERT INTO projects (project_id, name) VALUES ('P1', 'alpha')")
        conn.commit()
        doc = ingest.create_document(
            conn, project_id="P1", notebook_code="NB-JPG", title="jpeg"
        )
        page = ingest.ingest_page(
            conn, document_id=doc["document_id"], content=content,
            filename="photo.jpg", page_order=1, storage_root=tmp_path / "storage",
            project_id="P1", source_channel="test", declared_mime="image/jpeg",
        )
        conn.commit()
        preview = ingest.create_preview(
            conn, page_id=page["page_id"], storage_root=tmp_path / "storage"
        )
        conn.commit()
        assert preview["materialized"] is True
        assert preview["width"] == 400
        assert preview["height"] > 0
        assert preview["sha256"]
    finally:
        conn.close()


def test_unknown_binary_is_still_rejected():
    with pytest.raises(IngestError) as excinfo:
        sniff_mime(b"\x00\x01\x02\x03", "application/octet-stream", "mystery.bin")
    assert excinfo.value.code == "unsupported_file_type"
    assert excinfo.value.http_status == 415
