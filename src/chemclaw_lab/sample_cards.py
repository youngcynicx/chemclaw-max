"""Project-scoped sample cards over latest knowledge revisions.

This is a structural projection: sample identities and sections come from
stored model/human fields. No chemistry words or guessed sample labels.
"""
from __future__ import annotations

import hashlib
import json

from . import knowledge_state


def _decode(raw, fallback):
    try:
        return json.loads(raw) if isinstance(raw, str) else raw or fallback
    except (ValueError, TypeError):
        return fallback


def enrich_samples(conn, samples: list[dict], scope: list[str]) -> dict:
    if not scope:
        return {"samples": [], "unassigned_records": []}
    placeholders = ",".join("?" for _ in scope)
    page_rows = conn.execute(f"""
        SELECT p.page_id, d.project_id, d.title, p.page_order, e.sha256,
               pt.active_revision
        FROM evidence_pages p JOIN evidence_documents d ON d.document_id=p.document_id
        JOIN evidence_objects e ON e.evidence_id=p.evidence_id
        LEFT JOIN page_transcripts pt ON pt.page_id=p.page_id
        WHERE d.project_id IN ({placeholders})
    """, tuple(scope)).fetchall()
    pages = {row["page_id"]: dict(row) for row in page_rows}
    cards = {s["sample_id"]: {**s, "records": [], "source_pages": []} for s in samples}
    labels = {}
    for s in samples:
        for label in [s["label"], *(s.get("aliases") or [])]:
            labels.setdefault((s["project_id"], label), set()).add(s["sample_id"])
    rows = conn.execute(f"""
        SELECT r.*, COALESCE(d.project_id, json_extract(r.field_json,'$.project_id')) AS record_project
        FROM event_revisions r
        JOIN (SELECT entity_id, MAX(revision) rev FROM event_revisions
              WHERE entity_type='experiment_event' GROUP BY entity_id) h
          ON h.entity_id=r.entity_id AND h.rev=r.revision
        LEFT JOIN evidence_pages p ON p.page_id=json_extract(r.field_json,'$.page_id')
        LEFT JOIN evidence_documents d ON d.document_id=p.document_id
        WHERE r.entity_type='experiment_event'
          AND COALESCE(d.project_id,json_extract(r.field_json,'$.project_id')) IN ({placeholders})
        ORDER BY COALESCE(r.event_time_start,r.knowledge_as_of,r.created_at),r.entity_id
    """, tuple(scope)).fetchall()
    unassigned, source_notes = [], []
    for row in rows:
        payload = _decode(row["field_json"], {})
        fields = payload.get("fields") or {}
        provenance = _decode(row["provenance_json"], {})
        refs = _decode(row["evidence_refs_json"], [])
        project = row["record_project"]
        ids = set(payload.get("sample_ids") or []) if isinstance(payload.get("sample_ids"), list) else set()
        if payload.get("sample_id"):
            ids.add(payload["sample_id"])
        # Explicit field labels support old imported records whose IDs were
        # absent. Only exact authorized labels/aliases, never text matching.
        legacy_labels = ((fields["subject_sample_labels"] if "subject_sample_labels" in fields else [
            fields.get("sample_label"), *(fields.get("related_sample_labels") or []),
            *(fields.get("sample_labels") or [])]) if "sample_ids" not in payload else [])
        for entry in legacy_labels:
            if isinstance(entry, dict):
                if entry.get("uncertain"):
                    continue
                entry = entry.get("normalized")
            matches = labels.get((project, entry), set()) if isinstance(entry, str) else set()
            if len(matches) == 1:
                ids.update(matches)
        ids = {sid for sid in ids if sid in cards and cards[sid]["project_id"] == project}
        source_ids = list(dict.fromkeys([ref.get("page_id") for ref in refs if isinstance(ref, dict)
                                        and ref.get("page_id") in pages]
                                       + ([payload["page_id"]] if payload.get("page_id") in pages else [])))
        source_stale = any(ref.get("page_id") in pages
            and ref.get("transcript_revision") is not None
            and ref["transcript_revision"] != pages[ref["page_id"]]["active_revision"]
            for ref in refs if isinstance(ref, dict))
        record = {"event_id": row["entity_id"], "revision": row["revision"],
            "event_kind": payload.get("event_kind"), "raw_text": payload.get("raw_text") or "",
            "fields": fields, "title": payload.get("title"),
            "section": fields.get("section") or payload.get("section"),
            "sample_id": payload.get("sample_id"), "sample_ids": sorted(ids),
            "subject_sample_ids": sorted(ids),
            "page_id": payload.get("page_id"), "evidence_refs": refs,
            "source_pages": [pages[pid] for pid in source_ids], "source_stale": source_stale,
            "event_time_start": row["event_time_start"] or payload.get("occurred_at") or payload.get("event_time_start"),
            "knowledge_as_of": row["knowledge_as_of"], "provenance": provenance,
            "unknown": bool(payload.get("unknown")), "unknowns": payload.get("unknowns") or [],
            "disputed": bool(fields.get("disputed") or (isinstance(fields.get("human_question"), dict)
                and fields["human_question"].get("status") == "open"))}
        if fields.get("record_scope") == "source_metadata":
            source_notes.append(record)
        elif ids:
            for sid in ids:
                cards[sid]["records"].append(record)
        else:
            unassigned.append(record)
    for card in cards.values():
        projection = knowledge_state.project_states(card["records"], sample_id=card["sample_id"])
        card["current_state"] = projection["states"]
        card["state_issues"] = projection["issues"]
        lifecycle = {state["event_id"]: state for state in projection["states"]}
        # Shared event objects need a per-sample copy: a plan may be complete
        # for one subject and pending for another.
        card["records"] = [{**record, **({"lifecycle_state": lifecycle[record["event_id"]]}
                            if record["event_id"] in lifecycle else {})} for record in card["records"]]
        # Exact repeated imports share a display row, retaining all source and
        # event identities for correction. Different values are never merged.
        unique = {}
        for record in card["records"]:
            signature = hashlib.sha256(json.dumps({**{k: record[k] for k in
                ("event_kind", "raw_text", "fields", "event_time_start", "unknown", "unknowns", "source_stale")},
                "source_hashes": sorted({p["sha256"] for p in record["source_pages"]}),
                "lifecycle_state": record.get("lifecycle_state"),
                "source_event": None if record["source_pages"] else record["event_id"]},
                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if signature not in unique:
                unique[signature] = {**record, "related_event_ids": [record["event_id"]],
                    "related_events": [{"event_id": record["event_id"], "revision": record["revision"]}]}
            else:
                prior = unique[signature]
                prior["related_event_ids"].append(record["event_id"])
                prior["related_events"].append({"event_id": record["event_id"], "revision": record["revision"]})
                prior["source_pages"] = list({p["page_id"]: p for p in prior["source_pages"] + record["source_pages"]}.values())
                prior["evidence_refs"] = list({json.dumps(ref, sort_keys=True): ref
                    for ref in prior["evidence_refs"] + record["evidence_refs"]}.values())
        card["records"] = list(unique.values())
        card["source_pages"] = list({p["page_id"]: p for record in card["records"] for p in record["source_pages"]}.values())
        card["source_count"] = len(card["source_pages"])
        card["event_count"] = len(card["records"])
        card["last_event_at"] = max((r["knowledge_as_of"] or "" for r in card["records"]), default=None)
        card["acceptance"] = "default"
    return {"samples": list(cards.values()), "unassigned_records": unassigned, "source_notes": source_notes,
            "acceptance_policy": "default_accept_with_corrections"}
