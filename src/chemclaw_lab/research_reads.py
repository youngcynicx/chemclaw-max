"""Permission-scoped, model-free research reads on the caller's connection.

No storage files are opened. Transcript revisions enumerate successfully activated
runs (including activated needs_human runs) in insertion order; failed attempts and run-name suffixes are not versions.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

from . import auth, query


def _error(code: str, message: str, status: int = 400):
    raise query.QueryError(code, message, http_status=status)


def _text(params, key, *, required=False):
    value = params.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        _error("schema_violation", f"{key} must be a non-empty string.")
    return value.strip()


def _integer(value, key, minimum, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or (maximum is not None and value > maximum):
        _error("schema_violation", f"{key} must be an integer in the supported range.")
    return value


def _safe(value):
    if isinstance(value, dict):
        return {k: _safe(v) for k, v in value.items() if k != "storage_path"}
    if isinstance(value, list):
        return [_safe(v) for v in value]
    return value


def _page(conn, context, page_id):
    row = conn.execute("""SELECT p.*, d.project_id, d.notebook_code, d.title,
        d.author, d.source_channel, o.mime, o.sha256, o.project_id AS object_project_id
        FROM evidence_pages p JOIN evidence_documents d ON d.document_id=p.document_id
        JOIN evidence_objects o ON o.evidence_id=p.evidence_id WHERE p.page_id=?""", (page_id,)).fetchone()
    if row is None:
        _error("not_found", "Page not found.", 404)
    context.require_project(row["project_id"], "viewer")
    if row["object_project_id"] != row["project_id"]:
        _error("forbidden_scope", "Page document and evidence project disagree.", 403)
    return row


def current_source_revision(conn, page_id: str) -> int:
    row = conn.execute("SELECT active_revision FROM page_transcripts WHERE page_id=?", (page_id,)).fetchone()
    return int(row["active_revision"]) if row else 0


def transcript_versions(conn, page_id: str) -> dict[int, str]:
    """Resolve actual successful run order, checking the current pointer.

    Every successful persisted run is activated in the same transaction by the
    transcription service. A corrupt/nonmatching pointer is reported explicitly.
    """
    active = conn.execute("SELECT active_revision,active_run_id FROM page_transcripts WHERE page_id=?", (page_id,)).fetchone()
    if active is None or not active["active_run_id"]:
        return {}
    rows = conn.execute("SELECT run_id FROM transcript_runs WHERE page_id=? AND status IN ('succeeded','needs_human') ORDER BY rowid", (page_id,)).fetchall()
    mapping = {index: row["run_id"] for index, row in enumerate(rows, 1)}
    if mapping.get(active["active_revision"]) != active["active_run_id"] or len(mapping) != active["active_revision"]:
        _error("transcript_revision_unavailable", "Stored transcript runs do not match the active revision; exact history is unavailable.", 409)
    return mapping


def read_page(conn, context: auth.PrincipalContext, params: dict[str, Any]) -> dict[str, Any]:
    page_id = _text(params, "page_id", required=True)
    view = params.get("view", "agent")
    if view not in ("agent", "full"):
        _error("schema_violation", "view must be agent or full.")
    requested = params.get("transcript_revision")
    if requested is not None:
        _integer(requested, "transcript_revision", 1)
    block_ids = params.get("block_ids")
    if block_ids is not None and (not isinstance(block_ids, list) or not all(isinstance(v, str) and v.strip() for v in block_ids)):
        _error("schema_violation", "block_ids must be a list of non-empty block IDs.")
    row = _page(conn, context, page_id)
    page = {k: row[k] for k in row.keys() if k != "object_project_id"}
    page["view"] = view
    active = current_source_revision(conn, page_id)
    versions = transcript_versions(conn, page_id)
    revision = requested if requested is not None else active
    if revision not in versions:
        if requested is not None or block_ids:
            _error("transcript_revision_not_found", "Requested transcript revision is unavailable.", 404)
        page.update(active_revision=0, active_run_id=None, transcript=None)
        return {"page": page}
    run_id = versions[revision]
    run = conn.execute("SELECT provider,model,status,origin,created_at,finished_at FROM transcript_runs WHERE run_id=?", (run_id,)).fetchone()
    rows = conn.execute("SELECT block_id,block_kind,reading_order,block_json FROM transcript_blocks WHERE run_id=? ORDER BY reading_order,block_id", (run_id,)).fetchall()
    missing = set(block_ids or []) - {b["block_id"] for b in rows}
    if missing:
        _error("source_block_not_found", "Requested blocks do not belong to the specified transcript revision.", 404)
    blocks = []
    flags = Counter()
    for block in rows:
        if block_ids is not None and block["block_id"] not in block_ids:
            continue
        payload = _safe(json.loads(block["block_json"]))
        payload.update(block_id=block["block_id"], reading_order=block["reading_order"], block_kind=block["block_kind"])
        if view == "agent":
            payload = {key: value for key, value in payload.items() if key in ("block_id", "text", "reading_order", "block_kind", "quality_flags")}
        flags.update(f for f in (payload.get("quality_flags") or []) if isinstance(f, str))
        blocks.append(payload)
    transcript = dict(run)
    transcript.update(page_id=page_id, transcript_revision=revision, revision=revision,
        active_revision=active, active_run_id=versions[active], run_id=run_id,
        source_ref={"page_id": page_id, "transcript_revision": revision}, blocks=blocks,
        quality_summary={"block_count": len(blocks), "flag_counts": dict(flags)},
        is_current=revision == active)
    page.update(active_revision=active, active_run_id=versions[active], transcript=transcript)
    return {"page": page}


def source_blocks(conn, context, page_id, transcript_revision, block_ids=None, view="agent"):
    return read_page(conn, context, {"page_id": page_id, "transcript_revision": transcript_revision,
        "block_ids": block_ids, "view": view})["page"]["transcript"]["blocks"]


def _page_ids(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "page_id" and isinstance(item, str) and item:
                yield item
            else:
                yield from _page_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from _page_ids(item)


def _event_payload(row):
    payload = dict(row)
    payload["field_json"] = json.loads(payload["field_json"])
    payload["evidence_refs"] = json.loads(payload["evidence_refs_json"])
    payload["provenance"] = json.loads(payload["provenance_json"])
    payload["is_corrected"] = row["revision"] > 1
    payload = _safe(payload)
    # Preserve legacy serialized columns without leaking nested local paths.
    payload["evidence_refs_json"] = json.dumps(payload["evidence_refs"], ensure_ascii=False)
    payload["provenance_json"] = json.dumps(payload["provenance"], ensure_ascii=False)
    return payload


def authorize_event(conn, context, payload):
    """Check authoritative project and every cited/provenance source."""
    fields = payload["field_json"]
    declared = fields.get("project_id")
    page_id = fields.get("page_id")
    owner = _page(conn, context, page_id)["project_id"] if page_id else declared
    if not owner or (declared is not None and declared != owner):
        _error("forbidden_scope", "Event has no consistent authoritative project.", 403)
    context.require_project(owner, "viewer")
    # Recheck every historical source, including correction/provenance sources.
    for source_id in set(_page_ids([fields, payload["evidence_refs"], payload["provenance"]])):
        if _page(conn, context, source_id)["project_id"] != owner:
            _error("forbidden_scope", "Event source is outside its authoritative project.", 403)
    return owner


def read_event(conn, context: auth.PrincipalContext, params: dict[str, Any]) -> dict[str, Any]:
    event_id = _text(params, "event_id", required=True)
    revision = params.get("revision")
    as_of = _text(params, "knowledge_as_of")
    history = params.get("include_history", False)
    if not isinstance(history, bool):
        _error("schema_violation", "include_history must be boolean.")
    if revision is not None:
        _integer(revision, "revision", 1)
    if sum((revision is not None, as_of is not None, history)) > 1:
        _error("schema_violation", "revision, knowledge_as_of and include_history are mutually exclusive.")
    rows = conn.execute("SELECT * FROM event_revisions WHERE entity_type='experiment_event' AND entity_id=? ORDER BY revision", (event_id,)).fetchall()
    if not rows:
        _error("not_found", "Event not found.", 404)
    current = _event_payload(rows[-1])
    authorize_event(conn, context, current)
    selected = rows
    if revision is not None:
        selected = [row for row in rows if row["revision"] == revision]
    elif as_of is not None:
        if conn.execute("SELECT julianday(?)", (as_of,)).fetchone()[0] is None:
            _error("schema_violation", "knowledge_as_of must be a valid timestamp.")
        # SQLite normalizes spaces, ISO T/Z and UTC offsets to the same instant.
        # Missing knowledge times retain the existing earliest-known semantics.
        selected = conn.execute("""SELECT * FROM event_revisions
            WHERE entity_type='experiment_event' AND entity_id=?
              AND (knowledge_as_of IS NULL OR julianday(knowledge_as_of)<=julianday(?))
            ORDER BY revision DESC LIMIT 1""", (event_id, as_of)).fetchall()
    elif not history:
        selected = rows[-1:]
    if not selected:
        _error("event_revision_not_found", "Requested event revision is unavailable.", 404)
    payloads = [_event_payload(row) for row in selected]
    for payload in payloads:
        authorize_event(conn, context, payload)
    if history:
        return {"event_id": event_id, "revisions": payloads, "current_revision": rows[-1]["revision"]}
    result = payloads[0]
    result["current_revision"] = rows[-1]["revision"]
    result["is_current"] = result["revision"] == rows[-1]["revision"]
    return result


def catalog_samples(conn, context: auth.PrincipalContext, params: dict[str, Any]) -> dict[str, Any]:
    project_ids = params.get("project_ids")
    if project_ids is not None and (not isinstance(project_ids, list) or not all(isinstance(p, str) and p.strip() for p in project_ids)):
        _error("schema_violation", "project_ids must be a list of project IDs.")
    scope = query.resolve_scope(conn, context, project_ids)
    limit = _integer(params.get("limit", 50), "limit", 1, 200)
    offset = _integer(params.get("offset", 0), "offset", 0)
    q = _text(params, "q")
    sample_id = _text(params, "sample_id")
    rows = []
    if scope:
        rows = conn.execute(f"SELECT sample_id,project_id,label,aliases_json FROM samples WHERE project_id IN ({','.join('?' for _ in scope)}) ORDER BY sample_id", scope).fetchall()
    samples = []
    for row in rows:
        aliases = json.loads(row["aliases_json"] or "[]")
        if sample_id is not None and sample_id != row["sample_id"]:
            continue
        candidates = [row["label"], *[a for a in aliases if isinstance(a, str)]]
        match = None
        if q is not None:
            if any(q.casefold() == v.casefold() for v in candidates):
                match = "exact"
            elif any(q.casefold() in v.casefold() for v in candidates):
                match = "substring"
            else:
                continue
        item = {"sample_id": row["sample_id"], "project_id": row["project_id"], "label": row["label"], "aliases": aliases}
        if match:
            item["match"] = match
        samples.append(item)
    has_more = offset + limit < len(samples)
    return {"samples": samples[offset:offset + limit], "scope": scope, "view": "catalog",
        "limit": limit, "offset": offset, "has_more": has_more,
        "next_offset": offset + limit if has_more else None, "total": len(samples)}

