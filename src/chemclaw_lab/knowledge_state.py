"""Explicit, version-bound research state assertions; no language inference."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json

STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled", "superseded", "unknown"})


class StateError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 400):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


def _ids(record):
    values = record.get("sample_ids")
    result = {sid for sid in values if isinstance(sid, str)} if isinstance(values, list) else set()
    if isinstance(record.get("sample_id"), str):
        result.add(record["sample_id"])
    return result


def _status(value):
    return isinstance(value, str) and value in STATUSES


def normalize_state_fields(conn, *, project_id, fields, sample_ids):
    """Validate explicit assertions after subject resolution; return a new dict.

    Caller owns the transaction and project authorization. Client-supplied
    canonical subject ids are recomputed from unique, in-project labels.
    """
    result = deepcopy(fields)
    if "state" in result:
        state = result["state"]
        if not isinstance(state, dict) or not _status(state.get("status")):
            raise StateError("invalid_state_fields", "fields.state must contain a supported status.")
    if "state_updates" not in result:
        return result
    updates = result["state_updates"]
    if not isinstance(updates, list):
        raise StateError("invalid_state_fields", "fields.state_updates must be an array.")
    source_ids = set(sample_ids)
    for update in updates:
        if not isinstance(update, dict):
            raise StateError("invalid_state_fields", "Each state update must be an object.")
        allowed = {"target_event_id", "target_revision", "status", "subject_sample_labels", "reason", "subject_sample_ids"}
        if set(update) - allowed:
            raise StateError("invalid_state_fields", "State updates support existing event ids only; unsupported field(s): "
                             + ", ".join(sorted(set(update) - allowed)))
        target_id, revision = update.get("target_event_id"), update.get("target_revision")
        labels, reason = update.get("subject_sample_labels"), update.get("reason")
        if not isinstance(target_id, str) or not target_id or type(revision) is not int or revision < 1:
            raise StateError("invalid_state_fields", "State updates require target_event_id and a positive target_revision.")
        if not _status(update.get("status")):
            raise StateError("invalid_state_fields", "State update status is unsupported.")
        if not isinstance(labels, list) or not labels or any(not isinstance(label, str) or not label.strip() for label in labels):
            raise StateError("invalid_state_fields", "subject_sample_labels must be a nonempty array of explicit labels.")
        if not isinstance(reason, str) or not reason.strip():
            raise StateError("invalid_state_fields", "State updates require a reason.")
        target = conn.execute("""SELECT r.revision,r.field_json,d.project_id AS page_project
            FROM event_revisions r LEFT JOIN evidence_pages p ON p.page_id=json_extract(r.field_json,'$.page_id')
            LEFT JOIN evidence_documents d ON d.document_id=p.document_id
            WHERE r.entity_type='experiment_event' AND r.entity_id=? ORDER BY r.revision DESC LIMIT 1""",
            (target_id,)).fetchone()
        if target is None:
            raise StateError("state_target_not_found", "Read a real target event before asserting its state.", http_status=404)
        payload = json.loads(target["field_json"])
        owner = target["page_project"] or payload.get("project_id")
        if owner != project_id:
            raise StateError("forbidden_scope", "State target must belong to the knowledge project.", http_status=403)
        if revision != target["revision"]:
            raise StateError("state_target_changed", "State target has changed; read its current revision before retrying.", http_status=409)
        subjects = []
        for label in dict.fromkeys(label.strip() for label in labels):
            matches = conn.execute("""SELECT sample_id FROM samples WHERE project_id=? AND
                (label=? OR EXISTS(SELECT 1 FROM json_each(CASE WHEN json_valid(aliases_json) THEN aliases_json ELSE '[]' END)
                 WHERE type='text' AND value=?))""", (project_id, label, label)).fetchall()
            if len(matches) != 1:
                raise StateError("state_subject_unresolved", "State subjects must uniquely resolve to existing project samples.", http_status=409)
            sid = matches[0]["sample_id"]
            if sid not in source_ids or sid not in _ids(payload):
                raise StateError("invalid_state_subject", "State subjects must be explicitly linked to both source and target events.")
            if sid not in subjects:
                subjects.append(sid)
        update["subject_sample_ids"] = subjects
    return result


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
    except (AttributeError, TypeError, ValueError):
        return float("-inf")


def _basis(record):
    return {"event_id": record["event_id"], "revision": record["revision"],
            "knowledge_as_of": record.get("knowledge_as_of"), "evidence_refs": deepcopy(record.get("evidence_refs") or [])}


def project_states(records, sample_id=None):
    """Project only caller-authorized, snapshot-selected records; never query.

    Same-time incompatible assertions are unknown, with all competing bases
    retained. Event dates and arbitrary historical status strings are ignored.
    """
    records = list(records)
    index = {record["event_id"]: record for record in records}
    states, edges, issues, assertions = {}, [], [], {}
    for record in records:
        fields = record.get("fields") or {}
        initial = fields.get("state")
        explicit = isinstance(initial, dict) and _status(initial.get("status"))
        if not explicit and record.get("event_kind") != "plan":
            continue
        for sid in sorted(_ids(record)):
            if sample_id is not None and sid != sample_id:
                continue
            states[(record["event_id"], sid)] = {"event_id": record["event_id"],
                "target_revision": record["revision"], "sample_id": sid,
                "status": initial["status"] if explicit else "unknown", "explicit": explicit,
                "basis": _basis(record)}
    for source in records:
        updates = (source.get("fields") or {}).get("state_updates") or []
        if not isinstance(updates, list):
            issues.append({"kind": "invalid_state_updates", "event_id": source["event_id"]})
            continue
        for update in updates:
            if not isinstance(update, dict):
                issues.append({"kind": "invalid_state_update", "event_id": source["event_id"]})
                continue
            subjects = update.get("subject_sample_ids")
            if not isinstance(subjects, list) or not subjects or any(not isinstance(sid, str) for sid in subjects):
                issues.append({"kind": "uncanonical_state_subjects", "event_id": source["event_id"]})
                continue
            relevant = sorted(set(subjects) if sample_id is None else set(subjects) & {sample_id})
            if not relevant:
                continue
            edge = {"source_event_id": source["event_id"], "source_revision": source["revision"],
                "target_event_id": update.get("target_event_id"), "target_revision": update.get("target_revision"),
                "status": update.get("status"), "subject_sample_ids": relevant, "reason": update.get("reason"),
                "basis": _basis(source), "applied": False}
            edges.append(edge)
            target_id = update.get("target_event_id")
            target = index.get(target_id) if isinstance(target_id, str) else None
            problem = None
            if target is None:
                problem = "state_target_not_in_snapshot"
            elif target["revision"] != update.get("target_revision"):
                problem = "state_target_revision_mismatch"
            elif not _status(update.get("status")):
                problem = "invalid_state_status"
            elif not set(relevant) <= (_ids(source) & _ids(target)):
                problem = "invalid_state_subject"
            if problem:
                issues.append({"kind": problem, "source_event_id": source["event_id"],
                    "target_event_id": update.get("target_event_id"), "target_revision": update.get("target_revision")})
                continue
            edge["applied"] = True
            for sid in relevant:
                key = (target["event_id"], sid)
                states.setdefault(key, {"event_id": target["event_id"], "target_revision": target["revision"],
                    "sample_id": sid, "status": "unknown", "explicit": False, "basis": _basis(target)})
                assertions.setdefault(key, []).append((_time(source.get("knowledge_as_of")), update["status"], _basis(source)))
    for key, claims in assertions.items():
        latest = max(claim[0] for claim in claims)
        newest = [claim for claim in claims if claim[0] == latest]
        statuses = {claim[1] for claim in newest}
        state = states[key]
        state["explicit"] = True
        if len(statuses) == 1:
            state["status"] = next(iter(statuses))
            bases = sorted((claim[2] for claim in newest), key=lambda basis: (basis["event_id"], basis["revision"]))
            state["basis"] = bases[0]
            if len(bases) > 1:
                state["supporting_bases"] = bases
        else:
            state.update(status="unknown", basis=None)
            issues.append({"kind": "conflicting_state_updates", "target_event_id": key[0], "sample_id": key[1],
                "statuses": sorted(statuses), "bases": [claim[2] for claim in newest]})
    return {"schema_version": "knowledge_state_v1", "states": [states[key] for key in sorted(states)],
            "edges": edges, "issues": issues}
