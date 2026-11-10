"""Shared fixtures for the S10 repair-round regression tests.

These tests target the confirmed F01-F14/E01-E04 defects from the 2026-10-15
review. They are deliberately black-box where possible: they assert business
tables, versions, stock, audit/outbox state and stable error codes, not just
HTTP 200.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from chemclaw_lab import auth, capabilities, db  # noqa: E402
from chemclaw_lab.service_config import ServiceSettings  # noqa: E402


class Lab:
    def __init__(self, db_path: Path, settings: ServiceSettings):
        self.db_path = str(db_path)
        self.settings = settings

    def context(
        self,
        principal_id: str,
        kind: str = "human",
        memberships: dict[str, str] | None = None,
        display_name: str | None = None,
    ) -> auth.PrincipalContext:
        memberships = memberships or {}
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO principals (principal_id, kind, display_name) "
                "VALUES (?, ?, ?)",
                (principal_id, kind, display_name or principal_id),
            )
            for project_id, role in memberships.items():
                conn.execute(
                    "INSERT OR REPLACE INTO memberships (principal_id, project_id, role) "
                    "VALUES (?, ?, ?)",
                    (principal_id, project_id, role),
                )
            conn.commit()
        finally:
            conn.close()
        return auth.PrincipalContext(
            principal_id=principal_id,
            kind=kind,
            display_name=display_name or principal_id,
            memberships=dict(memberships),
        )

    def invoke(
        self,
        context: auth.PrincipalContext,
        capability: str,
        params: dict,
        *,
        operation_id: str = "op-default",
    ):
        return capabilities.invoke_capability(
            self.db_path,
            context,
            capability,
            params,
            operation_id=operation_id,
            services=self.settings,
        )

    def stock(self, chemical_id: str = "chm_nacl") -> tuple[float, int]:
        conn = db.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT amount, revision FROM chemicals WHERE chemical_id = ?",
                (chemical_id,),
            ).fetchone()
            return (float(row["amount"]), int(row["revision"]))
        finally:
            conn.close()

    def scalar(self, sql: str, params: tuple = ()):
        conn = db.connect(self.db_path)
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = db.connect(self.db_path)
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple = ()) -> None:
        conn = db.connect(self.db_path)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    # --- evidence / transcript fixtures ---------------------------------
    def add_page(self, project_id: str, page_id: str | None = None) -> dict:
        page_id = page_id or f"pg_{uuid.uuid4().hex[:8]}"
        evidence_id = f"evd_{uuid.uuid4().hex[:8]}"
        document_id = f"doc_{page_id}"
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO evidence_objects "
                "(evidence_id, project_id, storage_path, sha256, mime) "
                "VALUES (?, ?, ?, ?, 'image/png')",
                (evidence_id, project_id, f"{page_id}.png", "0" * 64),
            )
            conn.execute(
                "INSERT INTO evidence_documents "
                "(document_id, project_id, notebook_code, title) VALUES (?, ?, ?, ?)",
                (document_id, project_id, f"NB-{page_id}", "repair fixture"),
            )
            conn.execute(
                "INSERT INTO evidence_pages "
                "(page_id, document_id, evidence_id, page_order, status) "
                "VALUES (?, ?, ?, 1, 'transcribed')",
                (page_id, document_id, evidence_id),
            )
            conn.commit()
        finally:
            conn.close()
        return {"page_id": page_id, "document_id": document_id, "project_id": project_id}

    def add_transcript(
        self,
        page_id: str,
        texts: list[str],
        *,
        created_at: str | None = None,
        run_id: str | None = None,
    ) -> dict:
        run_id = run_id or f"run_{uuid.uuid4().hex[:8]}"
        block_ids = []
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO transcript_runs "
                "(run_id, page_id, provider, status, input_sha256, origin, created_at) "
                "VALUES (?, ?, 'fixture', 'succeeded', 'hash', 'ocr', "
                "COALESCE(?, CURRENT_TIMESTAMP))",
                (run_id, page_id, created_at),
            )
            for index, text in enumerate(texts):
                block_id = f"{run_id}-b{index}"
                block_ids.append(block_id)
                conn.execute(
                    "INSERT INTO transcript_blocks "
                    "(block_id, run_id, block_kind, reading_order, block_json) "
                    "VALUES (?, ?, 'paragraph', ?, ?)",
                    (
                        block_id,
                        run_id,
                        index,
                        json.dumps({"text": text}, ensure_ascii=False),
                    ),
                )
            conn.execute(
                "INSERT INTO page_transcripts (page_id, active_run_id, active_revision) "
                "VALUES (?, ?, 1) "
                "ON CONFLICT(page_id) DO UPDATE SET active_run_id = excluded.active_run_id, "
                "active_revision = page_transcripts.active_revision + 1",
                (page_id, run_id),
            )
            conn.commit()
        finally:
            conn.close()
        return {"run_id": run_id, "block_ids": block_ids}

    def add_event(
        self,
        *,
        project_id: str,
        sample_id: str | None = None,
        amount: float | None = None,
        unit: str | None = None,
        event_kind: str = "operation",
        raw_text: str | None = None,
        evidence_refs: list[dict] | None = None,
        provenance: dict | None = None,
        revision: int = 1,
        knowledge_as_of: str | None = None,
        event_id: str | None = None,
        supersedes_revision_id: str | None = None,
    ) -> str:
        event_id = event_id or f"evt_{uuid.uuid4().hex[:12]}"
        conn = db.connect(self.db_path)
        try:
            page = conn.execute(
                "SELECT d.document_id, p.page_id FROM evidence_documents d "
                "JOIN evidence_pages p ON p.document_id = d.document_id "
                "WHERE d.project_id = ? LIMIT 1",
                (project_id,),
            ).fetchone()
            page_id = page["page_id"] if page is not None else None
            fields = {}
            if amount is not None:
                fields["amount"] = {
                    "normalized": amount,
                    "raw": f"{amount}",
                    "uncertain": False,
                }
                if unit is not None:
                    fields["amount"]["unit"] = unit
            if unit is not None:
                fields["unit"] = {"raw": unit, "normalized": unit, "uncertain": False}
            field_json = {
                "event_kind": event_kind,
                "raw_text": raw_text or "",
                "fields": fields,
                "sample_id": sample_id,
                "page_id": page_id,
                "project_id": project_id,
            }
            conn.execute(
                "INSERT INTO event_revisions "
                "(revision_id, entity_type, entity_id, revision, field_json, "
                "knowledge_as_of, supersedes_revision_id, evidence_refs_json, "
                "provenance_json, created_by) "
                "VALUES (?, 'experiment_event', ?, ?, ?, "
                "COALESCE(?, CURRENT_TIMESTAMP), ?, ?, ?, 'fixture')",
                (
                    f"evr_{uuid.uuid4().hex[:12]}",
                    event_id,
                    revision,
                    json.dumps(field_json, ensure_ascii=False),
                    knowledge_as_of,
                    supersedes_revision_id,
                    json.dumps(evidence_refs or [], ensure_ascii=False),
                    json.dumps(provenance or {}, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return event_id


@pytest.fixture()
def lab(tmp_path) -> Lab:
    db_path = tmp_path / "repair.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    conn.execute(
        "INSERT INTO projects (project_id, name) VALUES ('P1', 'alpha'), ('P2', 'beta')"
    )
    conn.execute(
        "INSERT INTO chemicals "
        "(chemical_id, name, amount, unit, threshold, hazard_level, max_auto_delta) "
        "VALUES ('chm_nacl', 'Sodium chloride', 100.0, 'g', 10.0, 'low', 100.0)"
    )
    conn.commit()
    conn.close()
    return Lab(db_path, ServiceSettings(storage_root=tmp_path / "storage"))
