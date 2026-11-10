"""S02: evidence object registry with scope-checked reads.

Full ingestion (OCR, provenance extraction, binding to experiment records)
lands in S03. This module fixes the contracts early:
- Registration records content sha256 + storage path; the file lives in the
  default storage area (workspace) — M-11 resolved.
- Reads re-check project scope EVERY time (plan §8.6); a reader without the
  project membership gets ``unreadable_evidence`` and never the content.
- Unbound evidence (project_id NULL) is readable by any active member until
  S03 binds it; the scoping rule is a single point here, not scattered SQL.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from . import auth


class EvidenceError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def register_evidence_object(
    conn: sqlite3.Connection,
    *,
    storage_path: str,
    project_id: str | None = None,
    mime: str | None = None,
    page_count: int | None = None,
    source: str | None = None,
    sha256: str | None = None,
    evidence_id: str | None = None,
) -> dict[str, Any]:
    """Register an evidence file. sha256 is computed if not supplied."""
    if project_id is not None:
        if conn.execute("SELECT 1 FROM projects WHERE project_id = ?", (project_id,)).fetchone() is None:
            raise ValueError(f"Unknown project_id: {project_id}")
    digest = sha256 or _hash_file(storage_path)
    evidence_id = evidence_id or f"ev_{digest[:16]}"
    try:
        conn.execute(
            """
            INSERT INTO evidence_objects
              (evidence_id, project_id, storage_path, sha256, mime, page_count, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (evidence_id, project_id, storage_path, digest, mime, page_count, source),
        )
    except sqlite3.IntegrityError as exc:
        raise EvidenceError(
            "invalid_state", f"Evidence {evidence_id!r} already registered.", http_status=409
        ) from exc
    return {
        "evidence_id": evidence_id,
        "project_id": project_id,
        "sha256": digest,
        "mime": mime,
        "page_count": page_count,
        "source": source,
    }


def read_evidence_metadata(
    conn: sqlite3.Connection,
    context: auth.PrincipalContext,
    *,
    evidence_id: str,
) -> dict[str, Any]:
    """Scope-checked metadata read. Returns metadata only — never content."""
    row = conn.execute(
        "SELECT * FROM evidence_objects WHERE evidence_id = ?",
        (evidence_id,),
    ).fetchone()
    if row is None:
        raise EvidenceError("unreadable_evidence", f"Unknown evidence {evidence_id!r}.", http_status=404)
    project_id = row["project_id"]
    if project_id is not None:
        role = context.role_for(project_id)
        if role is None:
            raise EvidenceError(
                "unreadable_evidence",
                "Evidence belongs to a project without membership.",
                http_status=403,
            )
    elif not context.has_any_membership():
        raise EvidenceError(
            "unreadable_evidence",
            "Unbound evidence requires at least one membership.",
            http_status=403,
        )
    return {
        "evidence_id": row["evidence_id"],
        "project_id": row["project_id"],
        "sha256": row["sha256"],
        "mime": row["mime"],
        "page_count": row["page_count"],
        "source": row["source"],
        "created_at": row["created_at"],
    }


def bind_evidence_scope(
    conn: sqlite3.Connection,
    *,
    evidence_id: str,
    project_id: str,
) -> None:
    """Bind previously unbound evidence to a project (S03 will own this flow)."""
    row = conn.execute(
        "SELECT project_id FROM evidence_objects WHERE evidence_id = ?",
        (evidence_id,),
    ).fetchone()
    if row is None:
        raise EvidenceError("unreadable_evidence", f"Unknown evidence {evidence_id!r}.", http_status=404)
    if row["project_id"] is not None and row["project_id"] != project_id:
        raise EvidenceError(
            "invalid_state", "Evidence scope is already bound.", http_status=409
        )
    conn.execute(
        "UPDATE evidence_objects SET project_id = ? WHERE evidence_id = ?",
        (project_id, evidence_id),
    )


def _hash_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def list_evidence_pages(
    conn: sqlite3.Connection, context: auth.PrincipalContext, *,
    project_ids: list[str] | None = None, limit: int = 50, offset: int = 0,
) -> dict[str, Any]:
    """Discover registered sources independently of interpreted knowledge.

    Caller owns a read transaction. Counts use current event revisions and
    explicit page/sample IDs, including cross-page citations in the same
    project. They do not assert that all facts on a page have been captured.
    Original registration does not assert that its file is still available.
    """
    from . import query, research_reads

    scope = query.resolve_scope(conn, context, project_ids)
    if not scope:
        return {"pages": [], "scope": [], "pagination": {
            "limit": limit, "offset": offset, "total": 0, "has_more": False,
            "next_offset": None, "snapshot": False}}
    marks = ",".join("?" for _ in scope)
    # Both document and original ownership must be readable. Inconsistent
    # cross-project bindings are hidden rather than exposing source metadata.
    where = f"ed.project_id IN ({marks}) AND eo.project_id = ed.project_id"
    joins = """FROM evidence_pages ep
        JOIN evidence_documents ed ON ed.document_id = ep.document_id
        JOIN evidence_objects eo ON eo.evidence_id = ep.evidence_id"""
    total = conn.execute(f"SELECT COUNT(*) {joins} WHERE {where}", scope).fetchone()[0]
    rows = conn.execute(f"""
        SELECT ep.page_id, ep.document_id, ep.evidence_id, ep.page_order, ep.status,
               ep.created_at, ed.project_id, ed.notebook_code, ed.title,
               eo.mime, eo.sha256, eo.storage_path, pt.active_revision,
               tr.status AS active_transcript_status,
               (SELECT da.artifact_id FROM derived_artifacts da
                 WHERE da.page_id = ep.page_id AND da.kind = 'preview' AND da.materialized = 1
                 ORDER BY da.created_at DESC, da.artifact_id DESC LIMIT 1) AS preview_artifact_id,
               (SELECT COUNT(*) FROM memory_proposals mp
                 WHERE mp.page_id = ep.page_id AND mp.project_id = ed.project_id) AS proposal_count,
               (SELECT mp.proposal_id FROM memory_proposals mp
                 WHERE mp.page_id = ep.page_id AND mp.project_id = ed.project_id
                 ORDER BY mp.created_at DESC, mp.proposal_id DESC LIMIT 1) AS latest_proposal_id,
               (SELECT mp.status FROM memory_proposals mp
                 WHERE mp.page_id = ep.page_id AND mp.project_id = ed.project_id
                 ORDER BY mp.created_at DESC, mp.proposal_id DESC LIMIT 1) AS latest_proposal_status
        {joins}
        LEFT JOIN page_transcripts pt ON pt.page_id = ep.page_id
        LEFT JOIN transcript_runs tr ON tr.run_id = pt.active_run_id AND tr.page_id = ep.page_id
        WHERE {where} ORDER BY ep.created_at DESC, ep.page_id DESC LIMIT ? OFFSET ?
        """, (*scope, limit, offset)).fetchall()
    pages = {row["page_id"]: dict(row) for row in rows}
    event_ids = {page_id: set() for page_id in pages}
    sample_ids = {page_id: set() for page_id in pages}
    if pages:
        allowed_samples = {(row["project_id"], row["sample_id"]) for row in conn.execute(
            f"SELECT project_id, sample_id FROM samples WHERE project_id IN ({marks})", scope)}
        # Resolve ownership through the event's primary page, as event reads
        # do. An inaccessible event citing a visible page must not affect
        # observable counts. Read no historical revisions or semantic text.
        linked = conn.execute(f"""
            SELECT r.entity_id, r.field_json, r.evidence_refs_json, r.provenance_json, ed.project_id
            FROM event_revisions r
            JOIN evidence_pages ep ON ep.page_id = json_extract(r.field_json, '$.page_id')
            JOIN evidence_documents ed ON ed.document_id = ep.document_id
            WHERE r.entity_type = 'experiment_event' AND ed.project_id IN ({marks})
              AND r.revision = (SELECT MAX(k.revision) FROM event_revisions k
                WHERE k.entity_type = r.entity_type AND k.entity_id = r.entity_id)
            """, scope)
        for record in linked:
            payload = json.loads(record["field_json"])
            refs = json.loads(record["evidence_refs_json"])
            try:
                research_reads.authorize_event(conn, context, {
                    "field_json": payload, "evidence_refs": refs,
                    "provenance": json.loads(record["provenance_json"]),
                })
            except (query.QueryError, auth.AuthError):
                # Counts cannot reveal an event that direct reads reject,
                # including inconsistent ownership or a private nested source.
                continue
            cited = {ref.get("page_id") for ref in refs if isinstance(ref, dict)}
            cited.add(payload.get("page_id"))
            subjects = set(payload.get("sample_ids") or [])
            if payload.get("sample_id"):
                subjects.add(payload["sample_id"])
            for page_id in cited.intersection(pages):
                if record["project_id"] != pages[page_id]["project_id"]:
                    continue
                event_ids[page_id].add(record["entity_id"])
                sample_ids[page_id].update(s for s in subjects
                    if (record["project_id"], s) in allowed_samples)
    for page_id, item in pages.items():
        item["filename"] = Path(item.pop("storage_path")).name
        item["preview_materialized"] = item["preview_artifact_id"] is not None
        item["source_state"] = {
            "original_registered": True,
            "active_transcript_revision": item["active_revision"],
            "active_transcript_status": item.pop("active_transcript_status"),
            "proposal_count": item["proposal_count"],
            "linked_current_event_count": len(event_ids[page_id]),
            "explicitly_linked_sample_count": len(sample_ids[page_id]),
            "knowledge_complete": None,
        }
    next_offset = offset + len(pages)
    has_more = next_offset < total
    return {"pages": list(pages.values()), "scope": scope, "pagination": {
        "limit": limit, "offset": offset, "total": total, "has_more": has_more,
        "next_offset": next_offset if has_more else None, "snapshot": False}}
