"""Evidence pages: identical content in two notebooks must not collide."""

from __future__ import annotations

from chemclaw_lab import db, ingest


def test_same_content_in_two_documents_gets_distinct_pages(tmp_path):
    db_path = tmp_path / "ingest.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        conn.execute("INSERT INTO projects (project_id, name) VALUES ('P1', 'alpha')")
        conn.commit()
        doc1 = ingest.create_document(
            conn, project_id="P1", notebook_code="NB-1", title="one"
        )
        doc2 = ingest.create_document(
            conn, project_id="P1", notebook_code="NB-2", title="two"
        )
        content = b"shared protocol page"
        storage = tmp_path / "storage"
        page1 = ingest.ingest_page(
            conn, document_id=doc1["document_id"], content=content,
            filename="page.txt", page_order=1, storage_root=storage,
            project_id="P1", source_channel="test",
        )
        page2 = ingest.ingest_page(
            conn, document_id=doc2["document_id"], content=content,
            filename="page.txt", page_order=1, storage_root=storage,
            project_id="P1", source_channel="test",
        )
        conn.commit()
        assert page1["page_id"] != page2["page_id"]
        assert conn.execute(
            "SELECT COUNT(*) FROM evidence_pages"
        ).fetchone()[0] == 2
        # The sha256 original is shared, not duplicated.
        assert conn.execute(
            "SELECT COUNT(*) FROM evidence_objects"
        ).fetchone()[0] == 1
        # Same document/order is still idempotent.
        again = ingest.ingest_page(
            conn, document_id=doc1["document_id"], content=content,
            filename="page.txt", page_order=1, storage_root=storage,
            project_id="P1", source_channel="test",
        )
        conn.commit()
        assert again["page_id"] == page1["page_id"]
        assert again["already_present"] is True
        assert conn.execute(
            "SELECT COUNT(*) FROM evidence_pages"
        ).fetchone()[0] == 2
    finally:
        conn.close()
