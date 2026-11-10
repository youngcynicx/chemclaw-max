"""Versioned research context over explicit links, without semantic inference.

One caller-owned SQLite read transaction covers identity, event revisions and
source blocks. No model, original file I/O or business writes occur here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from . import auth, knowledge_state, query, research_reads

SECTIONS = ("current_state", "events", "cited_blocks", "provenance", "revision_index", "relations", "source_context")


def _json_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _decoded(raw, fallback):
    return json.loads(raw) if isinstance(raw, str) else raw if raw is not None else fallback


def _labels(fields):
    entries = fields["subject_sample_labels"] if "subject_sample_labels" in fields else [
        fields.get("sample_label"), *(fields.get("related_sample_labels") or []), *(fields.get("sample_labels") or [])]
    for entry in entries:
        if isinstance(entry, dict):
            if entry.get("uncertain"):
                continue
            entry = entry.get("normalized")
        if isinstance(entry, str) and entry:
            yield entry


def _links(payload, identities):
    ids = set(payload.get("sample_ids") or []) if isinstance(payload.get("sample_ids"), list) else set()
    if payload.get("sample_id"):
        ids.add(payload["sample_id"])
    if "sample_ids" not in payload:
        for label in _labels(payload.get("fields") or {}):
            matches = identities.get(label, set())
            if len(matches) == 1:
                ids.update(matches)
    return ids


def _pages(record):
    return {ref["page_id"] for ref in record.get("evidence_refs", []) if isinstance(ref, dict) and ref.get("page_id")} | (
        {record["field_json"]["page_id"]} if record.get("field_json", {}).get("page_id") else set())


def _event_projection(record, source_revisions, include_provenance):
    payload = record["field_json"]
    refs = record.get("evidence_refs") or []
    fields = payload.get("fields") or {}
    result = {**payload, "event_id": record["entity_id"], "revision": record["revision"],
        "event_kind": payload.get("event_kind"), "occurred_at": payload.get("occurred_at"),
        "event_time_start": record.get("event_time_start"), "knowledge_as_of": record.get("knowledge_as_of"),
        "raw_text": payload.get("raw_text"), "fields": fields,
        "sample_id": payload.get("sample_id"), "sample_ids": payload.get("sample_ids", []),
        "subject_sample_ids": payload.get("sample_ids", []) or ([payload["sample_id"]] if payload.get("sample_id") else []),
        "page_id": payload.get("page_id"), "evidence_refs": refs,
        "unknown": bool(payload.get("unknown")), "unknowns": payload.get("unknowns") or [],
        "is_corrected": record["revision"] > 1,
        "current_revision": record.get("current_revision", record["revision"]),
        "is_current": record.get("is_current", True),
        "disputed": bool(fields.get("disputed") or (isinstance(fields.get("human_question"), dict)
            and fields["human_question"].get("status") == "open")),
        "source_stale": any(ref.get("transcript_revision") is not None
            and ref.get("page_id") in source_revisions
            and ref["transcript_revision"] != source_revisions[ref["page_id"]]
            for ref in refs if isinstance(ref, dict))}
    if include_provenance:
        result["provenance"] = record.get("provenance") or {}
    return result


def get_sample_context(conn, context, params):
    project_id = params["project_id"]
    context.require_project(project_id, "viewer")
    include = set(params.get("include") if params.get("include") is not None else SECTIONS)
    snapshot = params.get("snapshot") or {"mode": "current"}
    as_of = snapshot.get("knowledge_as_of") if snapshot.get("mode") == "as_of" else None
    budget = (params.get("budget") or {}).get("max_bytes", 64000)
    samples = [dict(row) for row in conn.execute(
        "SELECT sample_id,project_id,label,aliases_json,created_at FROM samples WHERE project_id=? ORDER BY sample_id", (project_id,))]
    identities = {}
    for sample in samples:
        sample["aliases"] = _decoded(sample.pop("aliases_json"), [])
        for label in [sample["label"], *sample["aliases"]]:
            identities.setdefault(label, set()).add(sample["sample_id"])
    sample_id, label = params.get("sample_id"), params.get("sample_label")
    matches = [sample for sample in samples if sample["sample_id"] == sample_id] if sample_id else [
        sample for sample in samples if sample["sample_id"] in identities.get(label, set())]
    read_at = datetime.now(timezone.utc).isoformat()
    result = {"schema_version": "sample_context_v1", "status": "complete", "project_id": project_id,
        "resolution": {"status": "resolved" if len(matches) == 1 else "ambiguous" if matches else "not_found",
            "candidates": [{k: sample[k] for k in ("sample_id", "label", "aliases")} for sample in matches]},
        "sample": matches[0] if len(matches) == 1 else None,
        "snapshot": {"mode": snapshot.get("mode", "current"), "knowledge_as_of": as_of, "read_at": read_at,
            "identity_scope": "current", "source_status_scope": "current", "fingerprint": None},
        "events": [], "evidence": [], "revision_index": [], "relations": [], "source_context": [], "current_state": [],
        "quality": {"issues": [], "semantic_coverage": "not_assessed"},
        "coverage": {"requested_sections": sorted(include), "scope": "explicit_authorized_links",
            "retrieval_complete": True, "knowledge_complete": None, "omitted": [],
            "totals": {}, "returned": {}, "budget_bytes": budget, "result_bytes": 0,
            "byte_encoding": "compact UTF-8 JSON result; excludes transport envelope",
            "pagination": "Use explicit versioned object reads for omitted sections; fixed snapshot cursors are not implemented."}}
    if len(matches) != 1:
        if params.get("expected_fingerprint"):
            raise query.QueryError("snapshot_changed", "Sample identity no longer resolves uniquely.", http_status=409)
        result["status"] = result["resolution"]["status"]
        result["coverage"]["retrieval_complete"] = False
        _measure(result)
        if _json_size(result) > budget:
            raise query.QueryError("budget_too_small", "Identity candidates exceed budget; use the compact sample catalogue.", http_status=413)
        return result
    sample_id = matches[0]["sample_id"]
    # A snapshot is selected before links are tested. Older assignments cannot
    # leak into the current view; future corrections cannot enter an as-of view.
    rows = conn.execute("""
        SELECT r.*, COALESCE(d.project_id,json_extract(r.field_json,'$.project_id')) AS owner_project
        FROM event_revisions r
        LEFT JOIN evidence_pages p ON p.page_id=json_extract(r.field_json,'$.page_id')
        LEFT JOIN evidence_documents d ON d.document_id=p.document_id
        WHERE r.entity_type='experiment_event'
          AND COALESCE(d.project_id,json_extract(r.field_json,'$.project_id'))=?
          AND r.revision=(SELECT MAX(v.revision) FROM event_revisions v
              WHERE v.entity_type=r.entity_type AND v.entity_id=r.entity_id
                AND COALESCE(julianday(v.knowledge_as_of),julianday('0001-01-01'))
                    <=COALESCE(julianday(?),julianday('9999-12-31 23:59:59')))
        ORDER BY COALESCE(r.event_time_start,r.knowledge_as_of,r.created_at),r.entity_id
    """, (project_id, as_of)).fetchall()
    selected, note_rows, unassigned = [], [], []
    for row in rows:
        payload = _decoded(row["field_json"], {})
        fields = payload.get("fields") or {}
        if fields.get("record_scope") == "source_metadata":
            note_rows.append(row)
        elif sample_id in _links(payload, identities):
            selected.append(row)
        elif not _links(payload, identities):
            unassigned.append(row)
    records = [research_reads.read_event(conn, context, {"event_id": row["entity_id"], "revision": row["revision"]}) for row in selected]
    page_ids = set().union(*(_pages(record) for record in records)) if records else set()
    note_records = []
    for row in note_rows:
        note_pages = {ref.get("page_id") for ref in _decoded(row["evidence_refs_json"], []) if isinstance(ref, dict)}
        note_pages.add(_decoded(row["field_json"], {}).get("page_id"))
        if note_pages & page_ids:
            note_records.append(research_reads.read_event(conn, context, {"event_id": row["entity_id"], "revision": row["revision"]}))
    source_page_ids = page_ids | set().union(*(_pages(record) for record in note_records)) if note_records else page_ids
    source_revisions = {pid: research_reads.current_source_revision(conn, pid) for pid in sorted(source_page_ids)}
    event_units = [_event_projection(record, source_revisions, "provenance" in include) for record in records]
    state_projection = knowledge_state.project_states(event_units, sample_id=sample_id)
    lifecycle = {state["event_id"]: state for state in state_projection["states"]}
    for event in event_units:
        if event["event_id"] in lifecycle:
            event["lifecycle_state"] = lifecycle[event["event_id"]]
    evidence_units, page_context_units, issues, evidence_keys = [], [], list(state_projection["issues"]), set()
    state_issue_count = len(issues)
    # Exact source versions, never the active transcript substituted for an old
    # citation. Missing evidence stays visible with a concrete reason.
    for record in records + note_records:
        for ref in record.get("evidence_refs") or []:
            page_id, revision, block_id = ref.get("page_id"), ref.get("transcript_revision"), ref.get("block_id")
            key = (page_id, revision, block_id)
            if key in evidence_keys:
                continue
            evidence_keys.add(key)
            if not page_id or not revision or not block_id:
                issues.append({"kind": "incomplete_evidence_ref", "event_id": record["entity_id"], "ref": ref})
                if page_id and revision and "source_context" in include:
                    try:
                        page = research_reads.read_page(conn, context, {"page_id": page_id,
                            "transcript_revision": revision, "view": "agent"})["page"]
                        page_context_units.append({"kind": "page_context", "page_id": page_id,
                            "transcript_revision": revision, "blocks": page["transcript"]["blocks"],
                            "relation": "source_context_not_sample_facts"})
                    except query.QueryError as error:
                        if error.code == "forbidden_scope":
                            raise
                        issues.append({"kind": "unreadable_source_context", "ref": ref, "reason": error.code})
                continue
            if "cited_blocks" not in include:
                continue
            try:
                page = research_reads.read_page(conn, context, {"page_id": page_id,
                    "transcript_revision": revision, "block_ids": [block_id], "view": "agent"})["page"]
                evidence_units.append({"page_id": page_id, "transcript_revision": revision,
                    "active_revision": source_revisions.get(page_id), "block_id": block_id,
                    "blocks": page["transcript"]["blocks"], "source_stale": revision != source_revisions.get(page_id)})
            except query.QueryError as error:
                if error.code == "forbidden_scope":
                    raise
                issues.append({"kind": "unreadable_evidence_ref", "event_id": record["entity_id"],
                    "ref": ref, "reason": error.code})
    revisions = []
    for record in records:
        entries = conn.execute("""SELECT revision,knowledge_as_of,supersedes_revision_id FROM event_revisions
            WHERE entity_type='experiment_event' AND entity_id=? AND revision<=? ORDER BY revision""",
            (record["entity_id"], record["revision"])).fetchall()
        visible_entries, restricted = [], False
        if "revision_index" in include:
            for entry in entries:
                try:
                    research_reads.read_event(conn, context, {"event_id": record["entity_id"], "revision": entry["revision"]})
                    visible_entries.append(dict(entry))
                except (query.QueryError, auth.AuthError) as error:
                    if error.code != "forbidden_scope":
                        raise
                    restricted = True
            revisions.append({"event_id": record["entity_id"], "selected_revision": record["revision"],
                "current_revision": record["current_revision"],
                "revisions": visible_entries, "history_visibility": "restricted" if restricted else "authorized",
                "read_capability": "memory.event.read"})
    allowed_samples = {sample["sample_id"] for sample in samples}
    relations = [{"kind": "shared_event", "event_id": record["entity_id"], "revision": record["revision"],
        "sample_ids": sorted(_links(record["field_json"], identities) & allowed_samples),
        "basis": "stored_sample_links", "evidence_refs": record.get("evidence_refs") or []}
        for record in records if len(_links(record["field_json"], identities) & allowed_samples) > 1]
    relations.extend({"kind": "state_update", **edge} for edge in state_projection["edges"])
    notes = [_event_projection(record, source_revisions, "provenance" in include) for record in note_records]
    unassigned_on_pages = [row for row in unassigned if ({ref.get("page_id")
        for ref in _decoded(row["evidence_refs_json"], []) if isinstance(ref, dict)}
        | {_decoded(row["field_json"], {}).get("page_id")}) & page_ids]
    unresolved = len(unassigned_on_pages)
    fingerprint = {"sample": matches[0], "events": [(record["entity_id"], record["revision"], record["current_revision"]) for record in records],
        "evidence_refs": sorted(evidence_keys, key=str), "current_source_revisions": source_revisions,
        "source_notes": [(note["event_id"], note["revision"], note["current_revision"]) for note in notes],
        "unassigned_records": [(row["entity_id"], row["revision"]) for row in unassigned_on_pages],
        "identities": samples, "authorization_scope": sorted(auth.visible_project_ids(context))}
    result["snapshot"]["fingerprint"] = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if params.get("expected_fingerprint") and params["expected_fingerprint"] != result["snapshot"]["fingerprint"]:
        raise query.QueryError("snapshot_changed", "Sample context changed; request a new context before expanding.", http_status=409)
    evidence_incomplete = len(issues) > state_issue_count
    result["quality"].update(issues=issues, unassigned_records_on_linked_pages=unresolved,
        source_stale_event_ids=[event["event_id"] for event in event_units if event["source_stale"]],
        disputed_event_ids=[event["event_id"] for event in event_units if event["disputed"]])
    if unresolved:
        result["quality"]["issues"].append({"kind": "unassigned_records_on_linked_pages", "count": unresolved,
            "notice": "These records are not asserted to belong to this sample."})
    sections = {"current_state": state_projection["states"], "events": event_units, "evidence": evidence_units, "revision_index": revisions,
        "relations": relations, "source_context": notes + page_context_units}
    section_requests = {"evidence": "cited_blocks"}
    for key, units in sections.items():
        requested = section_requests.get(key, key) in include
        result["coverage"]["totals"][key] = len(units) if requested else None
        result["coverage"]["returned"][key] = 0
    # Plan the complete omission metadata before fitting whole objects. This
    # prevents a large result from hiding truncation in a too-small footer.
    for key, units in sections.items():
        if units and section_requests.get(key, key) in include:
            result["coverage"]["omitted"].append({"section": key, "count": len(units), "reason": "byte_budget",
                "expand": {"capability": "query.sample.context", "params": {"project_id": project_id,
                    "sample_id": sample_id, "include": sorted(include), "expected_fingerprint": result["snapshot"]["fingerprint"],
                    "snapshot": snapshot, "budget": {"max_bytes": 1048576}}}})
    # Bound verbose quality details too; stable counts describe omitted items.
    if _json_size(result) > budget:
        result["quality"] = {"semantic_coverage": "not_assessed", "issue_count": len(issues),
            "unassigned_records_on_linked_pages": unresolved, "details_omitted": True}
        result["coverage"]["retrieval_complete"] = False
    for key, units in sections.items():
        if section_requests.get(key, key) not in include:
            continue
        omission = next((entry for entry in result["coverage"]["omitted"] if entry["section"] == key), None)
        for unit in units:
            result[key].append(unit)
            result["coverage"]["returned"][key] += 1
            omission["count"] -= 1
            if not omission["count"]:
                result["coverage"]["omitted"].remove(omission)
            _measure(result)
            if _json_size(result) > budget:
                result[key].pop()
                result["coverage"]["returned"][key] -= 1
                if not omission["count"]:
                    result["coverage"]["omitted"].append(omission)
                omission["count"] += 1
    if result["coverage"]["omitted"]:
        result["coverage"]["retrieval_complete"] = False
    if evidence_incomplete and {"cited_blocks", "source_context"}.intersection(include):
        result["coverage"]["retrieval_complete"] = False
    result["status"] = "complete" if result["coverage"]["retrieval_complete"] else "partial"
    _measure(result)
    if _json_size(result) > budget:
        raise query.QueryError("budget_too_small", "Metadata exceeds budget; request fewer sections or a larger budget.", http_status=413)
    return result


def _measure(result):
    for _ in range(4):
        size = _json_size(result)
        if result["coverage"]["result_bytes"] == size:
            break
        result["coverage"]["result_bytes"] = size
