"""S05 shared query service (plan §11).

Read-side over the frozen experiment memory (S04) and evidence (S03):

- Structured retrieval by project/sample/run/date/event kind, plus a
  stdlib full-text scan over active transcript blocks and event payloads.
  No vector store exists; exact matching MUST stay fully functional with
  vectors disabled (§11.11) — there simply are none to disable.
- Snapshot semantics: every ``event_revisions`` row carries
  ``knowledge_as_of`` (the moment that revision became knowledge). A query
  at ``knowledge_as_of=T`` resolves each event to the highest revision
  whose ``knowledge_as_of <= T``. Corrections that happened AFTER T are
  returned separately as ``pending_corrections`` — never folded into the
  historical answer (§11.2/§11.10).
- Scope: candidates are filtered by the caller's server-side authorization
  BEFORE ranking (§11.8). An omitted scope is derived from the principal's
  memberships; an explicit scope outside the memberships is rejected.
  Unfiltered search does not exist.
- Candidates carry object type/id, source revision, page region, hit
  reason and score, plus neighbouring-block context.
- The audit log records query path, structured filters, candidate
  references and snapshot version — NEVER the free-text query or chunk
  content (§11.11).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from . import auth

__all__ = [
    "QueryError",
    "search",
    "sample_timeline",
    "save_report",
    "read_report",
    "log_query",
    "report_to_contract",
]


class QueryError(Exception):
    """Raised for query-service contract violations."""

    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


# ---------------------------------------------------------------------------
# Scope resolution (§11.11): derive or verify, never search unfiltered.
# ---------------------------------------------------------------------------

def resolve_scope(
    conn,
    context: auth.PrincipalContext,
    project_ids: list[str] | None,
) -> list[str]:
    visible = sorted(auth.visible_project_ids(context))
    if not project_ids:
        # No explicit scope: derive the legal range from the principal.
        return visible
    if not isinstance(project_ids, list) or not all(
        isinstance(p, str) and p.strip() for p in project_ids
    ):
        raise QueryError(
            "schema_violation", "project_ids must be a list of project ids.", http_status=400
        )
    requested = sorted({p.strip() for p in project_ids})
    outside = [p for p in requested if p not in visible]
    if outside:
        raise QueryError(
            "forbidden_scope",
            f"Requested project(s) outside authorized scope: {', '.join(outside)}.",
            http_status=403,
        )
    return requested


# ---------------------------------------------------------------------------
# Snapshot revision selection (§11.2/§11.10)
# ---------------------------------------------------------------------------

_EPOCH_MIN = "0000-01-01 00:00:00"
_EPOCH_MAX = "9999-12-31 23:59:59"


def _snapshot_join_sql(as_of_param: str = ":as_of") -> str:
    """JOIN clause selecting the revision valid at ``as_of`` (or current).

    S04 stores event facts ONLY in ``event_revisions`` (there is no
    ``experiment_events`` row); project ownership comes from the page the
    revision cites (revision 1 carries ``$.page_id``).
    """
    return f"""
        JOIN evidence_pages ep
          ON ep.page_id = json_extract(r.field_json, '$.page_id')
        JOIN evidence_documents ed
          ON ed.document_id = ep.document_id
    """


def _snapshot_where_sql(as_of_param: str = ":as_of") -> str:
    return f"""
        r.revision = (
          SELECT MAX(k.revision) FROM event_revisions k
          WHERE k.entity_type = r.entity_type
            AND k.entity_id = r.entity_id
            AND COALESCE(julianday(k.knowledge_as_of),julianday('0001-01-01')) <=
                COALESCE(julianday({as_of_param}),julianday('{_EPOCH_MAX}'))
        )
    """


def _pending_corrections(conn, event_ids: list[str], as_of: str) -> list[dict[str, Any]]:
    """Events whose knowledge moved AFTER ``as_of`` (reported separately)."""
    pending: list[dict[str, Any]] = []
    for event_id in event_ids:
        row = conn.execute(
            """
            SELECT entity_id, MAX(revision) AS rev, MAX(knowledge_as_of) AS became
            FROM event_revisions
            WHERE entity_type = 'experiment_event' AND entity_id = ?
              AND knowledge_as_of IS NOT NULL AND julianday(knowledge_as_of) > julianday(?)
            """,
            (event_id, as_of),
        ).fetchone()
        if row is not None and row["rev"] is not None:
            pending.append(
                {
                    "event_id": event_id,
                    "current_revision": row["rev"],
                    "knowledge_as_of": row["became"],
                    "reason": "corrected_after_snapshot",
                }
            )
    return pending


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _candidate(
    *,
    object_type: str,
    object_id: str,
    revision: int,
    knowledge_as_of: str | None,
    chunk: str,
    hit_reason: str,
    score: float,
    page_id: str | None = None,
    block_id: str | None = None,
    page_region: dict[str, Any] | None = None,
    context_before: str | None = None,
    context_after: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "candidate_id": _new_id("cnd"),
        "object_type": object_type,
        "object_id": object_id,
        "revision": revision,
        "knowledge_as_of": knowledge_as_of,
        "chunk": chunk,
        "hit_reason": hit_reason,
        "score": round(score, 4),
        "page_id": page_id,
        "block_id": block_id,
        "page_region": page_region,
        "context_before": context_before,
        "context_after": context_after,
        "payload": payload or {},
    }


def _search_events_structured(
    conn,
    scope: list[str],
    *,
    sample_label: str | None,
    run_id: str | None,
    date_from: str | None,
    date_to: str | None,
    event_kind: str | None,
    knowledge_as_of: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if not scope:
        return []
    placeholders = ",".join("?" for _ in scope)
    sql = f"""
        SELECT r.entity_id AS event_id, ed.project_id,
               json_extract(r.field_json, '$.event_kind') AS event_kind,
               json_extract(r.field_json, '$.raw_text') AS raw_text,
               r.revision, r.field_json, r.knowledge_as_of,
               r.evidence_refs_json, r.provenance_json, r.created_at
        FROM event_revisions r
        {_snapshot_join_sql()}
        WHERE r.entity_type = 'experiment_event'
          AND ed.project_id IN ({placeholders})
          AND {_snapshot_where_sql('?')}
    """
    params: list[Any] = [*scope, knowledge_as_of]
    event_time = "COALESCE(r.event_time_start, json_extract(r.field_json, '$.occurred_at'), json_extract(r.field_json, '$.performed_at'), r.created_at)"
    if run_id is not None:
        sql += " AND json_extract(r.field_json, '$.run_id') = ?"
        params.append(run_id)
    if event_kind is not None:
        sql += " AND json_extract(r.field_json, '$.event_kind') = ?"
        params.append(event_kind)
    if date_from is not None:
        sql += f" AND {event_time} >= ?"
        params.append(date_from)
    if date_to is not None:
        # A date-only upper bound includes the full recorded calendar day.
        # Timestamp bounds retain their existing precise comparison behavior.
        upper_time = f"substr({event_time}, 1, 10)" if len(date_to) == 10 else event_time
        sql += f" AND {upper_time} <= ?"
        params.append(date_to)
    if sample_label is not None:
        sql += """
          AND EXISTS (
            SELECT 1 FROM samples s
            WHERE s.project_id = ed.project_id
              AND (s.sample_id = json_extract(r.field_json, '$.sample_id') OR EXISTS (
                   SELECT 1 FROM json_each(r.field_json, '$.sample_ids') linked
                   WHERE linked.value = s.sample_id))
              AND (s.label = ? OR EXISTS (
                   SELECT 1 FROM json_each(s.aliases_json) je
                   WHERE je.value = ?
              ))
          )
        """
        params.extend([sample_label, sample_label])
    sql += f" ORDER BY {event_time} DESC"
    sql += " LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        field = json.loads(row["field_json"] or "{}")
        refs = json.loads(row["evidence_refs_json"] or "[]")
        candidates.append(
            _candidate(
                object_type="experiment_event",
                object_id=row["event_id"],
                revision=row["revision"],
                knowledge_as_of=row["knowledge_as_of"],
                chunk=field.get("raw_text") or field.get("event_kind") or "",
                hit_reason="structured_filter",
                score=1.0,
                page_id=field.get("page_id"),
                block_id=(refs[0] or {}).get("block_id") if refs else None,
                page_region=(refs[0] or {}).get("region") if refs else None,
                payload={
                    "project_id": row["project_id"],
                    "event_type": row["event_kind"],
                    "fields": field,
                    "evidence_refs": refs,
                    "provenance_json": row["provenance_json"],
                },
            )
        )
    return candidates


def _block_context(conn, run_id: str, reading_order: int) -> tuple[str | None, str | None]:
    before = conn.execute(
        """
        SELECT block_json FROM transcript_blocks
        WHERE run_id = ? AND reading_order = ?
        """,
        (run_id, reading_order - 1),
    ).fetchone()
    after = conn.execute(
        """
        SELECT block_json FROM transcript_blocks
        WHERE run_id = ? AND reading_order = ?
        """,
        (run_id, reading_order + 1),
    ).fetchone()
    def _text(row) -> str | None:
        if row is None:
            return None
        return (json.loads(row["block_json"] or "{}") or {}).get("text")
    return _text(before), _text(after)


def _search_fulltext(
    conn,
    scope: list[str],
    terms: str,
    *,
    knowledge_as_of: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Exact substring scan over ACTIVE transcript blocks of in-scope pages.

    Deterministic stdlib matching — vectors are off by design and exact
    sample-id/field hits must survive with them disabled (§11.11).
    """
    if not scope or not terms or not terms.strip():
        return []
    needle = terms.strip().lower()
    placeholders = ",".join("?" for _ in scope)
    pages = conn.execute(
        f"""
        SELECT ep.page_id, ep.page_order
        FROM evidence_pages ep
        JOIN evidence_documents ed ON ed.document_id = ep.document_id
        WHERE ed.project_id IN ({placeholders})
        ORDER BY ep.page_id
        """,
        [*scope],
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for page in pages:
        # Select the transcript run that was current AT THE REQUESTED
        # KNOWLEDGE TIME. A run ingested later must never appear in an
        # earlier snapshot; an older run stays readable for history.
        if knowledge_as_of is not None:
            run = conn.execute(
                """
                SELECT run_id, created_at FROM transcript_runs
                WHERE page_id = ? AND status IN ('succeeded','needs_human')
                  AND julianday(created_at) <= julianday(?)
                ORDER BY julianday(created_at) DESC, rowid DESC LIMIT 1
                """,
                (page["page_id"], knowledge_as_of),
            ).fetchone()
        else:
            run = conn.execute(
                """
                SELECT tr.run_id,tr.created_at FROM transcript_runs tr
                JOIN page_transcripts pt ON pt.active_run_id=tr.run_id
                WHERE tr.page_id = ? AND tr.status IN ('succeeded','needs_human')
                """,
                (page["page_id"],),
            ).fetchone()
        if run is None:
            continue
        from .research_reads import transcript_versions
        versions = transcript_versions(conn, page["page_id"])
        revision = next((revision for revision, run_id in versions.items() if run_id == run["run_id"]), None)
        if revision is None:
            raise QueryError("transcript_revision_unavailable", "Search source version cannot be resolved.", http_status=409)
        blocks = conn.execute(
            """
            SELECT block_id, reading_order, block_json
            FROM transcript_blocks
            WHERE run_id = ?
            ORDER BY reading_order
            """,
            (run["run_id"],),
        ).fetchall()
        for row in blocks:
            block = json.loads(row["block_json"] or "{}") or {}
            text = str(block.get("text") or "")
            if needle not in text.lower():
                continue
            geometry = block.get("geometry") or {}
            region = (
                {
                    "frame": geometry.get("frame"),
                    "bbox_topleft": geometry.get("bbox_topleft"),
                }
                if geometry
                else None
            )
            before, after = _block_context(conn, run["run_id"], row["reading_order"])
            candidates.append(
                _candidate(
                    object_type="transcript_block",
                    object_id=row["block_id"],
                    revision=revision,
                    knowledge_as_of=knowledge_as_of,
                    chunk=text,
                    hit_reason=f"fulltext:{needle}",
                    score=1.0,
                    page_id=page["page_id"],
                    block_id=row["block_id"],
                    page_region=region,
                    context_before=before,
                    context_after=after,
                    payload={
                        "page_order": page["page_order"],
                        "reading_order": row["reading_order"],
                        "transcript_run_id": run["run_id"],
                    },
                )
            )
            if len(candidates) >= limit:
                return candidates
    return candidates


def search(
    conn,
    context: auth.PrincipalContext,
    *,
    project_ids: list[str] | None = None,
    sample_label: str | None = None,
    run_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    event_kind: str | None = None,
    text: str | None = None,
    knowledge_as_of: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Combined structured + full-text search with snapshot semantics."""
    scope = resolve_scope(conn, context, project_ids)
    candidates: list[dict[str, Any]] = []
    candidates.extend(
        _search_events_structured(
            conn, scope,
            sample_label=sample_label, run_id=run_id, date_from=date_from,
            date_to=date_to, event_kind=event_kind,
            knowledge_as_of=knowledge_as_of, limit=limit,
        )
    )
    if text:
        candidates.extend(
            _search_fulltext(conn, scope, text, knowledge_as_of=knowledge_as_of, limit=limit)
        )
    event_ids = [c["object_id"] for c in candidates if c["object_type"] == "experiment_event"]
    pending = (
        _pending_corrections(conn, event_ids, knowledge_as_of)
        if knowledge_as_of is not None
        else []
    )
    return {
        "snapshot_version": knowledge_as_of or "current",
        "scope": scope,
        "candidates": candidates,
        "pending_corrections": pending,
        "vector_enabled": False,
    }


# ---------------------------------------------------------------------------
# Sample timeline (§5 页面: 样品时间线)
# ---------------------------------------------------------------------------

def sample_timeline(
    conn,
    context: auth.PrincipalContext,
    *,
    sample_id: str,
    knowledge_as_of: str | None = None,
) -> dict[str, Any]:
    from . import knowledge_state
    row = conn.execute(
        "SELECT sample_id, project_id, label FROM samples WHERE sample_id = ?",
        (sample_id,),
    ).fetchone()
    if row is None:
        raise QueryError("not_found", f"Sample {sample_id!r} not found.", http_status=404)
    resolve_scope(conn, context, [row["project_id"]])
    events = conn.execute(
        f"""
        SELECT r.entity_id AS event_id,
               json_extract(r.field_json, '$.event_kind') AS event_kind,
               json_extract(r.field_json, '$.raw_text') AS raw_text,
               r.revision, r.field_json, r.knowledge_as_of, r.created_at,
               r.event_time_start, r.evidence_refs_json, r.provenance_json
        FROM event_revisions r
        {_snapshot_join_sql()}
        WHERE r.entity_type = 'experiment_event'
          AND ed.project_id = ?
          AND {_snapshot_where_sql('?')}
          AND (json_extract(r.field_json, '$.sample_id') = ? OR EXISTS (
              SELECT 1 FROM json_each(r.field_json, '$.sample_ids') linked
              WHERE linked.value = ?))
          AND COALESCE(json_extract(r.field_json, '$.fields.record_scope'), 'sample_knowledge') != 'source_metadata'
        ORDER BY COALESCE(r.event_time_start, json_extract(r.field_json, '$.occurred_at'),
                          json_extract(r.field_json, '$.performed_at'), r.created_at), r.entity_id
        """,
        [row["project_id"], knowledge_as_of, sample_id, sample_id],
    ).fetchall()
    entries = []
    for erow in events:
        field = json.loads(erow["field_json"] or "{}")
        entries.append(
            {
                "event_id": erow["event_id"],
                "revision": erow["revision"],
                "knowledge_as_of": erow["knowledge_as_of"],
                "event_kind": field.get("event_kind"),
                "performed_at": field.get("performed_at"),
                "occurred_at": field.get("occurred_at"),
                "event_time_start": erow["event_time_start"],
                "page_id": field.get("page_id"),
                "sample_id": field.get("sample_id"),
                "sample_ids": field.get("sample_ids", []),
                "subject_sample_ids": field.get("sample_ids", []) or ([field["sample_id"]] if field.get("sample_id") else []),
                "evidence_refs": json.loads(erow["evidence_refs_json"] or "[]"),
                "provenance": json.loads(erow["provenance_json"] or "{}"),
                "is_corrected": erow["revision"] > 1,
                "unknown": bool(field.get("unknown")),
                "unknowns": field.get("unknowns", []),
                "fields": field.get("fields", {}),
                "raw_text": field.get("raw_text"),
            }
        )
    projection = knowledge_state.project_states(entries, sample_id=sample_id)
    lifecycle = {state["event_id"]: state for state in projection["states"]}
    for entry in entries:
        if entry["event_id"] in lifecycle:
            entry["lifecycle_state"] = lifecycle[entry["event_id"]]
    pending = (
        _pending_corrections(conn, [erow["event_id"] for erow in events], knowledge_as_of)
        if knowledge_as_of is not None
        else []
    )
    return {
        "sample": {
            "sample_id": row["sample_id"],
            "project_id": row["project_id"],
            "label": row["label"],
        },
        "snapshot_version": knowledge_as_of or "current",
        "timeline": entries,
        "current_state": projection["states"],
        "state_issues": projection["issues"],
        "pending_corrections": pending,
    }


# ---------------------------------------------------------------------------
# Reports (§11.6): persisted claims with source revisions + staleness
# ---------------------------------------------------------------------------

def report_to_contract(row) -> dict[str, Any]:
    return {
        "report_id": row["report_id"],
        "project_id": row["project_id"],
        "question": row["question_text"],
        "knowledge_as_of": row["knowledge_as_of"],
        "claims": json.loads(row["claims_json"] or "[]"),
        "rendered_text": row["rendered_text"],
        "based_on": json.loads(row["based_on_json"] or "[]"),
        "source_manifest": json.loads(row["source_manifest_json"] or "[]"),
        "source_project_ids": json.loads(row["source_project_ids_json"] or "[]"),
        "stale": bool(row["stale"]),
        "status": row["status"],
        "created_at": row["created_at"],
    }


def _object_project(conn, object_type: str, object_id: str) -> str | None:
    """Authoritative project ownership for a report source object."""
    if object_type in ("experiment_event", "experiment_run"):
        row = conn.execute(
            """
            SELECT ed.project_id
            FROM event_revisions r
            JOIN evidence_pages ep
              ON ep.page_id = json_extract(r.field_json, '$.page_id')
            JOIN evidence_documents ed ON ed.document_id = ep.document_id
            WHERE r.entity_type = ? AND r.entity_id = ?
            ORDER BY r.revision DESC LIMIT 1
            """,
            (object_type, object_id),
        ).fetchone()
        if row is not None:
            return row["project_id"]
        if object_type == "experiment_run":
            row = conn.execute(
                "SELECT project_id FROM experiment_runs WHERE run_id = ?", (object_id,)
            ).fetchone()
            return row["project_id"] if row is not None else None
        return None
    if object_type == "sample":
        row = conn.execute(
            "SELECT project_id FROM samples WHERE sample_id = ?", (object_id,)
        ).fetchone()
        return row["project_id"] if row is not None else None
    if object_type == "transcript_block":
        row = conn.execute(
            """
            SELECT ed.project_id
            FROM transcript_blocks tb
            JOIN transcript_runs tr ON tr.run_id = tb.run_id
            JOIN evidence_documents ed
              ON ed.document_id = (
                   SELECT document_id FROM evidence_pages WHERE page_id = tr.page_id
              )
            WHERE tb.block_id = ?
            """,
            (object_id,),
        ).fetchone()
        return row["project_id"] if row is not None else None
    if object_type == "query_report":
        row = conn.execute(
            "SELECT project_id FROM query_reports WHERE report_id = ?", (object_id,)
        ).fetchone()
        return row["project_id"] if row is not None else None
    return None


def _safe_report_text(claims: list[dict[str, Any]], gaps: list[dict[str, Any]],
                      snapshot: str) -> str:
    """Server-derived rendering from verified claims (never client free text)."""
    lines = [f"[snapshot {snapshot}]"]
    for claim in claims:
        statement = str(claim.get("statement") or "").strip()
        if not statement:
            continue
        lines.append(f"{statement} [{claim.get('claim_id', 'claim')}]")
    for gap in gaps:
        lines.append(f"缺口：{gap.get('reason') or gap.get('kind')} [{gap.get('gap_id', 'gap')}]")
    return "\n".join(lines)


def save_report(
    conn,
    context: auth.PrincipalContext,
    *,
    project_id: str,
    question: str,
    answer: dict[str, Any],
    knowledge_as_of: str | None = None,
) -> str:
    scope = resolve_scope(conn, context, [project_id])
    if project_id not in scope:
        raise QueryError("forbidden_scope", "Project outside authorized scope.", http_status=403)
    claims = answer.get("claims") if isinstance(answer.get("claims"), list) else []
    manifest: list[dict[str, Any]] = []
    based_on_set: set[tuple[str, str, int]] = set()
    for claim in claims:
        if not isinstance(claim, dict) or not claim.get("object_id"):
            continue
        object_type = str(claim.get("object_type") or "")
        object_id = str(claim.get("object_id"))
        revision = int(claim.get("revision") or 1)
        source_project = _object_project(conn, object_type, object_id)
        if source_project is not None and source_project != project_id:
            # A project report may only contain sources from that project.
            raise QueryError(
                "forbidden_scope",
                f"Claim source {object_type}:{object_id} belongs to "
                f"{source_project!r}, not {project_id!r}.",
                http_status=403,
            )
        based_on_set.add((object_type, object_id, revision))
        manifest.append(
            {
                "object_type": object_type,
                "object_id": object_id,
                "revision": revision,
                "project_id": source_project,
                "field_pointer": claim.get("field_pointer"),
                "evidence_refs": claim.get("evidence_refs") or [],
                "verify": claim.get("verify") or {},
            }
        )
    # The manifest is server-built and must at least contain every cited
    # object. Missing ownership evidence is kept as null, not presumed safe.
    manifest_by_key = {
        (m["object_type"], m["object_id"], m["revision"]): m for m in manifest
    }
    for object_type, object_id, revision in sorted(based_on_set):
        manifest_by_key.setdefault(
            (object_type, object_id, revision),
            {
                "object_type": object_type,
                "object_id": object_id,
                "revision": revision,
                "project_id": _object_project(conn, object_type, object_id),
                "field_pointer": None,
                "evidence_refs": [],
                "verify": {},
            },
        )
    manifest = sorted(
        manifest_by_key.values(),
        key=lambda m: (m["object_type"], m["object_id"], m["revision"]),
    )
    gaps = answer.get("gaps") if isinstance(answer.get("gaps"), list) else []
    report_id = _new_id("rpt")
    conn.execute(
        """
        INSERT INTO query_reports
          (report_id, project_id, principal_id, question_text, knowledge_as_of,
           snapshot_json, claims_json, rendered_text, based_on_json,
           source_manifest_json, source_project_ids_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            report_id,
            project_id,
            context.principal_id,
            question,
            knowledge_as_of,
            json.dumps(answer.get("snapshot", {}), sort_keys=True, ensure_ascii=False),
            json.dumps(claims, sort_keys=True, ensure_ascii=False),
            _safe_report_text(claims, gaps, knowledge_as_of or "current"),
            json.dumps(
                [
                    {"object_type": t, "object_id": i, "revision": r}
                    for t, i, r in sorted(based_on_set)
                ],
                sort_keys=True,
            ),
            json.dumps(manifest, sort_keys=True, ensure_ascii=False),
            json.dumps(
                sorted({m["project_id"] for m in manifest if m["project_id"]})
                or [project_id]
            ),
        ),
    )
    conn.commit()
    return report_id


def read_report(conn, context: auth.PrincipalContext, report_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM query_reports WHERE report_id = ?", (report_id,)
    ).fetchone()
    if row is None:
        raise QueryError("not_found", f"Report {report_id!r} not found.", http_status=404)
    # Scope enforcement is evaluated at READ time: access revoked later
    # immediately restricts old reports (§11.6). Restricted reports keep
    # existing for audit but are no longer readable.
    if row["status"] == "restricted":
        raise QueryError(
            "forbidden_scope", "Report access has been restricted.", http_status=403
        )
    resolve_scope(conn, context, [row["project_id"]])
    contract = report_to_contract(row)

    # Re-authorize EVERY source at read time. A saved report is not a
    # capability snapshot: revoked access restricts old output immediately.
    forbidden_sources: list[dict[str, Any]] = []
    manifest = contract.get("source_manifest") or []
    if not manifest:
        # Legacy reports: rebuild the source list from based_on and verify
        # ownership, never trust the old rendered_text.
        manifest = [
            {
                "object_type": entry["object_type"],
                "object_id": entry["object_id"],
                "revision": entry["revision"],
                "project_id": _object_project(
                    conn, entry["object_type"], entry["object_id"]
                ),
            }
            for entry in contract.get("based_on", [])
        ]
    for source in manifest:
        source_project = source.get("project_id") or _object_project(
            conn, str(source.get("object_type")), str(source.get("object_id"))
        )
        if source_project is None:
            forbidden_sources.append({**source, "reason": "unknown_ownership"})
        elif source_project != row["project_id"]:
            # Cross-project aggregation remains private: a project report
            # must not silently include another project's text.
            forbidden_sources.append({**source, "reason": "cross_project_source"})
        elif source_project not in auth.visible_project_ids(context):
            forbidden_sources.append({**source, "reason": "membership_revoked"})
    if forbidden_sources:
        conn.execute(
            "UPDATE query_reports SET status = 'restricted' WHERE report_id = ?",
            (report_id,),
        )
        conn.commit()
        raise QueryError(
            "forbidden_scope",
            f"Report {report_id!r} contains sources that are no longer authorized "
            f"({len(forbidden_sources)} source(s)); the report is restricted.",
            http_status=403,
        )
    # Staleness: any cited object now at a HIGHER revision than when the
    # report was written means the report reflects superseded knowledge.
    changed: list[dict[str, Any]] = []
    for entry in contract["based_on"]:
        current = conn.execute(
            """
            SELECT MAX(revision) AS rev FROM event_revisions
            WHERE entity_type = ? AND entity_id = ?
            """,
            (entry["object_type"], entry["object_id"]),
        ).fetchone()
        current_rev = current["rev"] if current is not None else None
        if current_rev is not None and current_rev > entry["revision"]:
            changed.append(
                {
                    "object_type": entry["object_type"],
                    "object_id": entry["object_id"],
                    "based_on_revision": entry["revision"],
                    "current_revision": current_rev,
                }
            )
    if changed:
        conn.execute(
            "UPDATE query_reports SET stale = 1 WHERE report_id = ?",
            (report_id,),
        )
        conn.commit()
        contract["stale"] = True
    contract["superseded_objects"] = changed
    return contract


# ---------------------------------------------------------------------------
# Audit log (§11.11): paths + candidate refs + versions, NEVER private text
# ---------------------------------------------------------------------------

def log_query(
    conn,
    context: auth.PrincipalContext,
    *,
    query_path: str,
    filters: dict[str, Any],
    snapshot_version: str | None,
    candidates: list[dict[str, Any]],
) -> str:
    safe_filters = {k: v for k, v in filters.items() if k != "text"}
    refs = [
        {
            "object_type": c["object_type"],
            "object_id": c["object_id"],
            "revision": c["revision"],
            "score": c["score"],
        }
        for c in candidates
    ]
    log_id = _new_id("qlog")
    conn.execute(
        """
        INSERT INTO query_audit_log
          (log_id, principal_id, query_path, filters_json, snapshot_version,
           candidate_refs_json, candidate_count)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            log_id,
            context.principal_id,
            query_path,
            json.dumps(safe_filters, sort_keys=True, ensure_ascii=False),
            snapshot_version,
            json.dumps(refs, sort_keys=True),
            len(refs),
        ),
    )
    return log_id
