"""S04 experiment memory service (plan §10).

Turns frozen transcripts into stable, correctable sample & experiment
records. Core invariants:

- Chemistry notebook extractions are adopted as correctable knowledge by
  default. This is not a human confirmation or an inventory authorization.
  Other profiles and explicit review mode retain their confirmation flow.
- Plans never become performed operations; computed values never become
  wet-lab observations (modality separation, plan §10.3).
- Sample identity is resolved inside the authorized project scope only;
  same-name, cross-project and scratched-out labels never auto-merge
  (plan §10.4).
- Explicit-review confirmations are bound
  to a specific proposal revision and invalidated when the source evidence
  changes (plan §10.5, §10.10).
- Corrections append immutable revisions bound to ``event_id`` and
  ``expected_revision``; old revisions stay queryable, default reads show
  only the effective one (plan §10.7).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from . import knowledge_state, tasks
from .profiles import ExperimentProfile, get_profile
from .units import normalize_unit

__all__ = [
    "MemoryError",
    "EVENT_MODALITIES",
    "create_proposal",
    "get_proposal",
    "record_confirmation",
    "invalidate_confirmations",
    "apply_proposal",
    "accept_knowledge_proposal",
    "backfill_knowledge_proposals",
    "correct_event",
    "read_event",
    "read_event_history",
    "search_sample_candidates",
    "open_clarification",
    "answer_clarification",
    "write_artifact",
    "invalidate_artifacts",
    "rebuild_artifact",
]

EVENT_MODALITIES = frozenset(
    {"operation", "observation", "measurement", "plan", "negation",
     "interpretation", "computed"}
)


class MemoryError(Exception):
    """Stable-code error for the memory capability layer."""

    def __init__(self, code: str, message: str, *, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _state_fields(conn, *, project_id, fields, sample_ids):
    try:
        return knowledge_state.normalize_state_fields(conn, project_id=project_id,
            fields=fields, sample_ids=sample_ids)
    except knowledge_state.StateError as exc:
        raise MemoryError(exc.code, exc.message, http_status=exc.http_status) from exc


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# Sample identity (plan §10.4)
# ---------------------------------------------------------------------------

def search_sample_candidates(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    label: str,
) -> dict[str, Any]:
    """Search sample candidates strictly inside one authorized project.

    Cross-project same-name samples are INVISIBLE here (never merged, never
    leaked). Returns one of: ``link`` (unique match), ``clarify`` (multiple
    in-project candidates), ``propose_creation`` (none).
    """
    rows = conn.execute(
        """
        SELECT sample_id, label, aliases_json FROM samples
        WHERE project_id = ? AND (label = ? OR aliases_json LIKE ?)
        """,
        (project_id, label, f'%"{label}"%'),
    ).fetchall()
    if not rows:
        return {"action": "propose_creation", "candidates": []}
    exact = [r for r in rows if r["label"] == label]
    chosen = exact or rows
    if len(chosen) == 1:
        return {
            "action": "link",
            "candidates": [
                {"sample_id": chosen[0]["sample_id"], "label": chosen[0]["label"]}
            ],
        }
    return {
        "action": "clarify",
        "candidates": [
            {"sample_id": r["sample_id"], "label": r["label"]} for r in chosen
        ],
    }


def _resolve_sample_links(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    items: list[dict[str, Any]],
) -> None:
    """Attach sample-match results to every item carrying a sample label."""
    for item in items:
        label = (item.get("fields", {}) or {}).get("sample_label")
        if isinstance(label, dict):
            label = label.get("normalized")
        if not label:
            continue
        item["sample_match"] = search_sample_candidates(
            conn, project_id=project_id, label=str(label)
        )


# ---------------------------------------------------------------------------
# Proposal creation (plan §10.3, §10.5)
# ---------------------------------------------------------------------------

def _validate_evidence_refs(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    transcript_revision: int,
    items: list[dict[str, Any]],
    project_id: str | None = None,
    current_only: bool = False,
) -> None:
    """Validate current notebook sources or preserve legacy single-page review.

    Current knowledge can join pages within one project. Each item needs
    evidence; the proposal as a whole must cite its anchor page at least once.
    Source interpretation and fact attribution remain the model's work.
    """
    page = conn.execute(
        "SELECT page_id FROM evidence_pages WHERE page_id = ?", (page_id,)
    ).fetchone()
    if page is None:
        raise MemoryError("invalid_page", f"Unknown page {page_id!r}.", http_status=404)
    pt = conn.execute(
        "SELECT active_revision FROM page_transcripts WHERE page_id = ?", (page_id,)
    ).fetchone()
    active = pt["active_revision"] if pt is not None else 0
    if current_only and (isinstance(transcript_revision, bool) or transcript_revision != active or active < 1):
        raise MemoryError("source_changed", "Re-extract the current anchor transcript instead of adopting an outdated proposal.", http_status=409)
    if transcript_revision > active:
        raise MemoryError(
            "invalid_evidence",
            f"Cited transcript revision {transcript_revision} exceeds active "
            f"revision {active} for page {page_id!r}.",
            http_status=409,
        )
    anchor_cited = False
    for item in items:
        refs = item.get("evidence_refs") or []
        if not refs or (current_only and not isinstance(refs, list)):
            raise MemoryError(
                "invalid_evidence",
                f"Item {item.get('item_key')!r} has no evidence reference.",
            )
        for ref in refs:
            if current_only:
                if not isinstance(ref, dict):
                    raise MemoryError("invalid_evidence", "Evidence references must be objects.")
                ref_page = ref.get("page_id", page_id)
                ref_block = ref.get("block_id")
                # Preserve legacy omitted fields for the anchor only. A
                # supporting page must explicitly name its own exact version.
                ref_revision = ref.get("transcript_revision", transcript_revision if ref_page == page_id else None)
                if (not isinstance(ref_page, str) or not ref_page.strip()
                        or not isinstance(ref_block, str) or not ref_block.strip()
                        or isinstance(ref_revision, bool) or not isinstance(ref_revision, int) or ref_revision < 1):
                    raise MemoryError("invalid_evidence", "Every source needs a non-empty page/block ID and exact transcript revision.")
                cited = conn.execute("""
                    SELECT tr.run_id,tr.page_id,tr.status,d.project_id,o.project_id AS object_project_id,
                           pt.active_revision,pt.active_run_id
                    FROM transcript_blocks tb JOIN transcript_runs tr ON tr.run_id=tb.run_id
                    JOIN evidence_pages p ON p.page_id=tr.page_id
                    JOIN evidence_documents d ON d.document_id=p.document_id
                    JOIN evidence_objects o ON o.evidence_id=p.evidence_id
                    LEFT JOIN page_transcripts pt ON pt.page_id=p.page_id
                    WHERE tb.block_id=?
                """, (ref_block,)).fetchone()
                if cited is None:
                    raise MemoryError("invalid_evidence", "Evidence block does not exist.", http_status=404)
                if cited["project_id"] != project_id or cited["object_project_id"] != project_id:
                    raise MemoryError("forbidden_scope", "Every knowledge source must belong to the requested project.", http_status=403)
                if (cited["page_id"] != ref_page or ref_revision != cited["active_revision"]
                        or cited["run_id"] != cited["active_run_id"] or cited["status"] not in ("succeeded", "needs_human")):
                    raise MemoryError("invalid_evidence", "Knowledge must cite each source page's current transcript revision and block run.", http_status=409)
                anchor_cited = anchor_cited or ref_page == page_id
                continue
            block = conn.execute(
                """
                SELECT tb.block_id FROM transcript_blocks tb
                JOIN transcript_runs tr ON tr.run_id = tb.run_id
                WHERE tb.block_id = ? AND tr.page_id = ?
                """,
                (ref.get("block_id"), page_id),
            ).fetchone()
            if block is None:
                raise MemoryError(
                    "invalid_evidence",
                    f"Item {item.get('item_key')!r} cites unknown block "
                    f"{ref.get('block_id')!r} on page {page_id!r}.",
                    http_status=404,
                )
    if current_only and not anchor_cited:
        raise MemoryError("invalid_evidence", "A knowledge proposal must cite its anchor page at least once.")


def _is_low_risk_item(item: dict[str, Any], profile: ExperimentProfile) -> bool:
    """Auto-confirm only quantitative items with an unambiguous sample link.

    Obvious problems still require a human: missing/non-numeric amount,
    unsupported unit, no sample label, or ambiguous/unknown sample match.
    """
    if item.get("unknown") is True:
        return False
    fields = item.get("fields") or {}
    amount = fields.get("amount")
    unit = fields.get("unit")
    if isinstance(amount, dict):
        unit = amount.get("unit") or unit
        amount = amount.get("normalized")
    if isinstance(unit, dict):
        unit = unit.get("normalized")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return False
    if normalize_unit(unit) not in profile.allowed_units:
        return False
    label = fields.get("sample_label")
    if isinstance(label, dict):
        label = label.get("normalized")
    if not isinstance(label, str) or not label.strip():
        return False
    match = item.get("sample_match") or {}
    return match.get("action") == "link"


def _compute_needs_confirmation(
    item: dict[str, Any],
    profile: ExperimentProfile,
    *,
    auto_confirm_safe: bool = False,
) -> bool:
    """Verify-then-apply unless this input type is explicitly configured for
    auto-apply (plan §10.5).

    ``auto_confirm_safe`` is an explicit opt-in used by the web OCR path: only
    low-risk quantitative items with a unique sample link skip confirmation.
    """
    input_type = item.get("input_type") or "handwritten_text"
    if input_type in profile.auto_apply_inputs:
        return False
    if auto_confirm_safe and _is_low_risk_item(item, profile):
        return False
    return True


def _normalize_proposal_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accept both documented and legacy item shapes.

    The published schema nests values under ``fields``. Real models sometimes
    emit quantities or explicit sample labels/label arrays at the item top
    level. Lifting those explicit values into ``fields`` is a pure shape
    normalization: no value is guessed and the raw item remains auditable.
    """
    normalized: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise MemoryError("invalid_items", "Each proposal item must be an object.")
        item = dict(raw)
        if not isinstance(item.get("fields") or {}, dict):
            raise MemoryError("invalid_items", "Item fields must be an object.")
        fields = dict(item.get("fields") or {})
        for name in ("amount", "unit", "sample_label", "related_sample_labels", "sample_labels", "subject_sample_labels",
                     "material_id", "chemical_id", "item_name"):
            if name in item and name not in fields:
                fields[name] = item[name]
        amount = fields.get("amount")
        legacy_unit = item.get("unit")
        if isinstance(amount, (int, float)) and isinstance(legacy_unit, str):
            fields["amount"] = {"normalized": amount, "unit": legacy_unit}
        item["fields"] = fields
        normalized.append(item)
    return normalized


def create_proposal(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    page_id: str,
    transcript_revision: int,
    profile_id: str,
    profile_version: str,
    items: list[dict[str, Any]],
    created_by: str,
    operation_id: str,
    task_id: str | None = None,
    auto_confirm_safe: bool = False,
    acceptance_mode: str | None = None,
) -> dict[str, Any]:
    """Idempotently create a memory proposal from structured items.

    Re-sending the same page (same operation_id) returns the EXISTING
    proposal — it never rebuilds events (plan §17: re-sent page photo, no
    duplicate application).
    """
    profile = get_profile(profile_id, profile_version)
    acceptance_mode = acceptance_mode or ("default" if profile_id == "chemistry_notebook" else "review")
    if acceptance_mode not in ("default", "review") or (acceptance_mode == "default" and profile_id != "chemistry_notebook"):
        raise MemoryError("invalid_request", "Default knowledge adoption is supported only for chemistry_notebook; use review for other profiles.")
    existing = conn.execute(
        "SELECT * FROM memory_proposals WHERE project_id = ? AND operation_id = ?",
        (project_id, operation_id),
    ).fetchone()
    if existing is not None:
        return {"proposal": _proposal_payload(conn, existing["proposal_id"]), "idempotent": True}

    if not items:
        raise MemoryError("empty_proposal", "A proposal needs at least one item.")
    items = _normalize_proposal_items(items)
    keys = [i.get("item_key") for i in items]
    if len(set(keys)) != len(keys) or not all(keys):
        raise MemoryError("invalid_items", "item_key must be present and unique.")
    for item in items:
        if item.get("occurred_at") is not None and not isinstance(item["occurred_at"], str):
            raise MemoryError("invalid_items", "occurred_at must be a source timestamp string or null.")
        if item.get("event_kind") not in EVENT_MODALITIES:
            raise MemoryError(
                "invalid_items",
                f"Item {item.get('item_key')!r} has invalid event_kind "
                f"{item.get('event_kind')!r}.",
            )
        if not item.get("raw_text"):
            raise MemoryError(
                "invalid_items", f"Item {item.get('item_key')!r} lacks raw_text."
            )
    _validate_evidence_refs(
        conn, page_id=page_id, transcript_revision=transcript_revision, items=items,
        project_id=project_id, current_only=acceptance_mode == "default",
    )
    if acceptance_mode == "review":
        _resolve_sample_links(conn, project_id=project_id, items=items)
    for item in items:
        if acceptance_mode == "default":
            item["needs_confirmation"] = False
            item["acceptance"] = "default"
        else:
            item["acceptance"] = "review"
            item.setdefault(
                "needs_confirmation",
                _compute_needs_confirmation(item, profile, auto_confirm_safe=auto_confirm_safe),
            )

    proposal_id = _new_id("prop")
    conn.execute(
        """
        INSERT INTO memory_proposals
          (proposal_id, project_id, task_id, page_id, source_transcript_revision,
           profile_id, profile_version, items_json, revision, status,
           operation_id, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'awaiting_confirmation', ?, ?)
        """,
        (
            proposal_id,
            project_id,
            task_id,
            page_id,
            transcript_revision,
            profile.profile_id,
            profile.version,
            json.dumps(items, sort_keys=True, ensure_ascii=False),
            operation_id,
            created_by,
        ),
    )
    accepted = None
    if acceptance_mode == "default":
        accepted = accept_knowledge_proposal(conn, project_id=project_id, proposal_id=proposal_id, principal_id=created_by)
    return {"proposal": _proposal_payload(conn, proposal_id), "idempotent": False, "knowledge": accepted}


def _explicit_sample_labels(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Read model-provided identity fields; never search raw chemistry text."""
    fields = item.get("fields") or {}
    raw = []
    if "subject_sample_labels" in fields:
        if not isinstance(fields["subject_sample_labels"], list):
            raise MemoryError("invalid_items", "fields.subject_sample_labels must be an array of explicit sample labels.")
        raw.extend(fields["subject_sample_labels"])
    else:
        if fields.get("sample_label") is not None:
            raw.append(fields["sample_label"])
        for key in ("related_sample_labels", "sample_labels"):
            if key in fields:
                if not isinstance(fields[key], list):
                    raise MemoryError("invalid_items", f"fields.{key} must be an array of explicit sample labels.")
                raw.extend(fields[key])
    labels = []
    seen = {}
    for entry in raw:
        if isinstance(entry, str):
            label, uncertain = entry.strip(), False
        elif isinstance(entry, dict):
            if "uncertain" in entry and not isinstance(entry["uncertain"], bool):
                raise MemoryError("invalid_items", "Sample label uncertain must be boolean.")
            value = entry.get("normalized")
            if value is None:
                label, uncertain = "", True
            elif not isinstance(value, str):
                raise MemoryError("invalid_items", "Sample labels must be strings.")
            else:
                label, uncertain = value.strip(), entry.get("uncertain") is True
        else:
            raise MemoryError("invalid_items", "Sample labels must be strings or {normalized, uncertain} objects.")
        if label not in seen:
            seen[label] = {"label": label, "uncertain": uncertain}
            labels.append(seen[label])
        else:
            seen[label]["uncertain"] = seen[label]["uncertain"] or uncertain
    return labels


def _knowledge_sample_links(conn, *, project_id: str, item: dict[str, Any]) -> dict[str, Any]:
    if (item.get("fields") or {}).get("record_scope") == "source_metadata":
        return {"sample_ids": [], "links": [], "issues": []}
    labels = _explicit_sample_labels(item)
    linked, issues = [], []
    for entry in labels:
        label = entry["label"]
        if not label or entry["uncertain"]:
            issues.append({"label": label, "reason": "uncertain_sample_label"})
            continue
        # Exact catalogue identities and aliases are data, not semantic inference.
        matches = conn.execute("""SELECT sample_id, label FROM samples WHERE project_id=? AND
            (label=? OR EXISTS(SELECT 1 FROM json_each(CASE WHEN json_valid(aliases_json) THEN aliases_json ELSE '[]' END)
             WHERE type='text' AND value=?)) ORDER BY sample_id""", (project_id, label, label)).fetchall()
        if len(matches) > 1:
            issues.append({"label": label, "reason": "ambiguous_sample_label", "candidate_ids": [r["sample_id"] for r in matches]})
            continue
        if matches:
            linked.append({"label": label, "sample_id": matches[0]["sample_id"], "created": False})
        else:
            sample_id = _new_id("smp")
            conn.execute("INSERT INTO samples(sample_id, project_id, label, aliases_json) VALUES(?,?,?,'[]')", (sample_id, project_id, label))
            linked.append({"label": label, "sample_id": sample_id, "created": True})
    # Older callers may supply a real catalogue id rather than a label.
    match = item.get("sample_match") or {}
    if not isinstance(match, dict) or not isinstance(match.get("candidates") or [], list):
        raise MemoryError("invalid_items", "sample_match must contain an array of candidate objects.")
    candidates = match.get("candidates") or []
    canonical_subjects = "subject_sample_labels" in (item.get("fields") or {})
    if not canonical_subjects and not labels and match.get("action") == "link" and len(candidates) == 1:
        if not isinstance(candidates[0], dict):
            raise MemoryError("invalid_items", "Sample candidates must be objects.")
        sample_id = candidates[0].get("sample_id")
        sample = conn.execute("SELECT sample_id,label FROM samples WHERE sample_id=? AND project_id=?", (sample_id, project_id)).fetchone()
        if sample is None:
            raise MemoryError("forbidden_scope", "Explicit sample link must belong to the knowledge project.", http_status=403)
        linked.append({"sample_id": sample["sample_id"], "label": sample["label"], "created": False})
    elif not canonical_subjects and not labels and candidates:
        issues.append({"reason": "ambiguous_sample_reference"})
    sample_ids = list(dict.fromkeys(link["sample_id"] for link in linked))
    return {"sample_ids": sample_ids, "links": linked, "issues": issues}


def accept_knowledge_proposal(conn: sqlite3.Connection, *, project_id: str, proposal_id: str, principal_id: str) -> dict[str, Any]:
    """Adopt cited notebook knowledge without signing approvals or executing stock.

    Caller owns the transaction and project authorization. A current extraction
    is required; replay never rewrites an existing fact or a later correction.
    """
    row = conn.execute("SELECT * FROM memory_proposals WHERE proposal_id=? AND project_id=?", (proposal_id, project_id)).fetchone()
    if row is None:
        raise MemoryError("forbidden_scope", "Proposal is outside the requested project.", http_status=403)
    if row["profile_id"] != "chemistry_notebook" or row["status"] in ("rejected", "expired"):
        raise MemoryError("invalid_state", "Only active notebook knowledge can be adopted.", http_status=409)
    page = conn.execute("SELECT d.project_id, pt.active_revision,pt.active_run_id FROM evidence_pages p JOIN evidence_documents d ON d.document_id=p.document_id LEFT JOIN page_transcripts pt ON pt.page_id=p.page_id WHERE p.page_id=?", (row["page_id"],)).fetchone()
    if page is None or page["project_id"] != project_id:
        raise MemoryError("forbidden_scope", "Knowledge page must belong to the requested project.", http_status=403)
    if page["active_revision"] != row["source_transcript_revision"]:
        raise MemoryError("source_changed", "Re-extract the current transcript instead of adopting an outdated proposal.", http_status=409)
    items = json.loads(row["items_json"])
    _validate_evidence_refs(conn, page_id=row["page_id"], transcript_revision=row["source_transcript_revision"],
                            items=items, project_id=project_id, current_only=True)
    for item in items:
        _explicit_sample_labels(item)  # Validate all identity shapes before writes.
    declined = {r["item_key"] for r in conn.execute("SELECT item_key FROM event_confirmations WHERE proposal_id=? AND proposal_revision=? AND status='valid' AND decision='declined'", (proposal_id, row["revision"]))}
    adopted, skipped = [], []
    for item in items:
        key = item["item_key"]
        if key in declined:
            skipped.append({"item_key": key, "reason": "explicitly_declined"})
            continue
        event_id = _event_id_for(proposal_id, key)
        existing = conn.execute("SELECT revision,field_json,provenance_json FROM event_revisions WHERE entity_type='experiment_event' AND entity_id=? ORDER BY revision DESC LIMIT 1", (event_id,)).fetchone()
        if existing:
            # In particular, never restore revision 1 over a human correction.
            adopted.append({"item_key": key, "event_id": event_id, "revision": existing["revision"], "existing": True})
            item["needs_confirmation"] = False
            item["acceptance"] = "default"
            continue
        resolution = _knowledge_sample_links(conn, project_id=project_id, item=item)
        sample_ids = resolution["sample_ids"]
        item["fields"] = _state_fields(conn, project_id=project_id,
            fields=item.get("fields") or {}, sample_ids=sample_ids)
        event = {"event_kind": item["event_kind"], "raw_text": item.get("raw_text"), "fields": item.get("fields") or {},
                 "sample_id": sample_ids[0] if sample_ids else None, "sample_ids": sample_ids,
                 "sample_resolution": resolution, "page_id": row["page_id"], "project_id": project_id,
                 "acceptance": "default", "knowledge_only": True}
        for name in ("unknown", "unknowns", "occurred_at", "historical_import", "title", "details"):
            if name in item:
                event[name] = item[name]
        provenance = {"proposal_id": proposal_id, "proposal_revision": row["revision"], "profile": f"{row['profile_id']}@{row['profile_version']}",
                      "acceptance": "default", "acceptance_policy": "notebook_knowledge_v1", "confirmed_by": None,
                      "source_transcript_revision": row["source_transcript_revision"], "source_kind": "transcript_extraction",
                      "result_origin": "computed" if item["event_kind"] == "computed" else "reported", "knowledge_only": True}
        conn.execute("""INSERT INTO event_revisions(revision_id,entity_type,entity_id,revision,field_json,event_time_start,
            knowledge_as_of,evidence_refs_json,provenance_json,created_by)
            VALUES(?,'experiment_event',?,1,?,?,CURRENT_TIMESTAMP,?,?,?)""",
            (_new_id("evr"), event_id, json.dumps(event,ensure_ascii=False,sort_keys=True), item.get("occurred_at"),
             json.dumps(item.get("evidence_refs",[]),ensure_ascii=False,sort_keys=True), json.dumps(provenance,ensure_ascii=False,sort_keys=True), principal_id))
        item["needs_confirmation"] = False
        item["acceptance"] = "default"
        if sample_ids:
            item["sample_match"] = {"action": "link", "candidates": [{"sample_id": link["sample_id"], "label": link["label"]} for link in resolution["links"][:1]]}
        else:
            item["sample_match"] = {"action": "clarify" if resolution["issues"] else "none", "candidates": []}
        adopted.append({"item_key": key, "event_id": event_id, "revision": 1, "sample_ids": sample_ids})
    conn.execute("UPDATE memory_proposals SET items_json=?,status='applied',applied_at=COALESCE(applied_at,CURRENT_TIMESTAMP) WHERE proposal_id=? AND project_id=?", (json.dumps(items,ensure_ascii=False,sort_keys=True), proposal_id, project_id))
    if any(not entry.get("existing") for entry in adopted):
        invalidate_artifacts(conn, project_id=project_id)
    return {"acceptance": "default", "knowledge_only": True, "adopted": adopted, "skipped": skipped, "proposal_id": proposal_id}


def backfill_knowledge_proposals(conn: sqlite3.Connection, *, project_id: str, principal_id: str) -> dict[str, Any]:
    """Explicit project-scoped migration; caller authorizes and commits once.

    Rejected/expired rows and explicit-review new clients remain untouched.
    Old unlabelled facts are retained unassigned; no raw-text parser runs.
    """
    if not isinstance(project_id, str) or not project_id:
        raise MemoryError("invalid_request", "An explicit project_id is required for knowledge backfill.")
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    rows = conn.execute("SELECT proposal_id,items_json FROM memory_proposals WHERE project_id=? AND profile_id='chemistry_notebook' AND status NOT IN ('rejected','expired') ORDER BY created_at,rowid", (project_id,)).fetchall()
    outcomes, skipped = [], []
    for row in rows:
        items = json.loads(row["items_json"])
        if items and all(item.get("acceptance") == "review" for item in items):
            skipped.append({"proposal_id": row["proposal_id"], "reason": "explicit_review_policy"})
            continue
        conn.execute("SAVEPOINT knowledge_backfill_row")
        try:
            outcomes.append(accept_knowledge_proposal(conn, project_id=project_id, proposal_id=row["proposal_id"], principal_id=principal_id))
            conn.execute("RELEASE knowledge_backfill_row")
        except MemoryError as exc:
            conn.execute("ROLLBACK TO knowledge_backfill_row")
            conn.execute("RELEASE knowledge_backfill_row")
            skipped.append({"proposal_id": row["proposal_id"], "reason": exc.code})
    return {"project_id": project_id, "proposals": outcomes, "skipped": skipped,
            "new_events": sum(not event.get("existing") for outcome in outcomes for event in outcome["adopted"])}


def get_proposal(conn: sqlite3.Connection, proposal_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM memory_proposals WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    if row is None:
        raise MemoryError("not_found", f"Unknown proposal {proposal_id!r}.", http_status=404)
    return _proposal_payload(conn, proposal_id)


def _proposal_payload(conn: sqlite3.Connection, proposal_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM memory_proposals WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    confirmations = [
        dict(r)
        for r in conn.execute(
            """
            SELECT confirmation_id, proposal_revision, item_key, decision,
                   principal_id, status, created_at
            FROM event_confirmations WHERE proposal_id = ?
            ORDER BY created_at
            """,
            (proposal_id,),
        ).fetchall()
    ]
    clarifications = [
        dict(r)
        for r in conn.execute(
            """
            SELECT clarification_id, proposal_revision, interaction_id,
                   questions_json, answers_json, status, created_at, answered_at
            FROM clarifications WHERE proposal_id = ? ORDER BY created_at
            """,
            (proposal_id,),
        ).fetchall()
    ]
    payload = dict(row)
    payload["items"] = json.loads(row["items_json"])
    knowledge_events = []
    for item in payload["items"]:
        event_id = _event_id_for(proposal_id, item["item_key"])
        head = conn.execute("SELECT revision,field_json,provenance_json FROM event_revisions WHERE entity_type='experiment_event' AND entity_id=? ORDER BY revision DESC LIMIT 1", (event_id,)).fetchone()
        if head:
            provenance = json.loads(head["provenance_json"] or "{}")
            acceptance = provenance.get("acceptance") or ("confirmed" if provenance.get("confirmed_by") else None)
            item.update(event_id=event_id, event_revision=head["revision"], event_acceptance=acceptance,
                        current_event=json.loads(head["field_json"]))
            knowledge_events.append({"item_key": item["item_key"], "event_id": event_id, "revision": head["revision"], "acceptance": acceptance})
    payload["knowledge_events"] = knowledge_events
    payload["acceptance_mode"] = "default" if payload["items"] and all(item.get("acceptance") == "default" for item in payload["items"]) else "review"
    payload["confirmations"] = confirmations
    payload["clarifications"] = [
        {**c, "questions": json.loads(c.pop("questions_json") or "[]"),
         "answers": json.loads(c.pop("answers_json")) if c["answers_json"] else None}
        for c in clarifications
    ]
    return payload


# ---------------------------------------------------------------------------
# Confirmations (plan §10.5, §10.10)
# ---------------------------------------------------------------------------

def record_confirmation(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    item_keys: list[str],
    decision: str,
    principal_id: str,
    expected_proposal_revision: int,
) -> dict[str, Any]:
    """Human confirm/decline of critical proposal items.

    Confirmations are bound to the proposal revision; a stale revision
    (source evidence corrected since) is rejected — the caller must re-read
    the proposal. Business approval is NEVER derived from chat text; only
    this capability creates confirmations.
    """
    row = conn.execute(
        "SELECT * FROM memory_proposals WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    if row is None:
        raise MemoryError("not_found", f"Unknown proposal {proposal_id!r}.", http_status=404)
    if row["revision"] != expected_proposal_revision:
        raise MemoryError(
            "revision_conflict",
            f"Proposal is at revision {row['revision']}, caller expected "
            f"{expected_proposal_revision}.",
            http_status=409,
        )
    if row["status"] in ("applied", "rejected", "expired"):
        raise MemoryError(
            "invalid_state", f"Proposal already {row['status']}.", http_status=409
        )
    items = {i["item_key"]: i for i in json.loads(row["items_json"])}
    for key in item_keys:
        # A dedicated sample-creation confirmation targets the virtual key
        # "sample:create:<item_key>" (plan §10.4/§17): validate the base item.
        base = key.split("sample:create:", 1)[1] if key.startswith("sample:create:") else key
        if base not in items:
            raise MemoryError(
                "invalid_item", f"Unknown item {key!r} in proposal.", http_status=404
            )
    confirmed: list[dict[str, Any]] = []
    for key in item_keys:
        existing = conn.execute(
            """
            SELECT confirmation_id FROM event_confirmations
            WHERE proposal_id = ? AND proposal_revision = ? AND item_key = ?
              AND principal_id = ? AND status = 'valid'
            """,
            (proposal_id, row["revision"], key, principal_id),
        ).fetchone()
        if existing is not None:
            confirmed.append(
                {"item_key": key, "decision": decision, "idempotent": True}
            )
            continue
        conn.execute(
            """
            INSERT INTO event_confirmations
              (confirmation_id, proposal_id, proposal_revision, item_key,
               decision, principal_id, status)
            VALUES (?, ?, ?, ?, ?, ?, 'valid')
            """,
            (_new_id("cfm"), proposal_id, row["revision"], key, decision, principal_id),
        )
        confirmed.append({"item_key": key, "decision": decision, "idempotent": False})
    return {"confirmed": confirmed, "proposal_revision": row["revision"]}


def update_proposal_item(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    item_key: str,
    changes: dict[str, Any],
    expected_proposal_revision: int,
    principal_id: str,
) -> dict[str, Any]:
    """Edit a candidate item BEFORE apply; creates a new proposal revision.

    Human corrections are never written into the OCR run. The original
    transcript and the prior proposal revision remain auditable; confirmations
    from the old revision do not validate the new one.
    """
    row = conn.execute(
        "SELECT * FROM memory_proposals WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    if row is None:
        raise MemoryError("not_found", f"Unknown proposal {proposal_id!r}.", http_status=404)
    if row["status"] in ("applied", "rejected", "expired"):
        raise MemoryError("invalid_state", f"Proposal already {row['status']}.", http_status=409)
    if row["revision"] != expected_proposal_revision:
        raise MemoryError(
            "revision_conflict",
            f"Proposal is at revision {row['revision']}, caller expected "
            f"{expected_proposal_revision}.",
            http_status=409,
        )
    if not isinstance(changes, dict) or not changes:
        raise MemoryError("invalid_item", "changes must be a non-empty object.")
    allowed = {"fields", "event_kind", "needs_confirmation", "unknown", "raw_text"}
    unknown_keys = sorted(set(changes) - allowed)
    if unknown_keys:
        raise MemoryError(
            "invalid_item", f"Unsupported item change(s): {unknown_keys}."
        )
    items = json.loads(row["items_json"])
    target = next((item for item in items if item.get("item_key") == item_key), None)
    if target is None:
        raise MemoryError("invalid_item", f"Unknown item {item_key!r}.", http_status=404)
    if "fields" in changes:
        fields = changes["fields"]
        if not isinstance(fields, dict):
            raise MemoryError("invalid_item", "changes.fields must be an object.")
        merged = dict(target.get("fields") or {})
        merged.update(fields)
        target["fields"] = merged
    for key in ("event_kind", "needs_confirmation", "unknown", "raw_text"):
        if key in changes:
            target[key] = changes[key]
    target["edited_by"] = principal_id
    target["edited_at"] = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
    revision = row["revision"] + 1
    conn.execute(
        """
        UPDATE memory_proposals
        SET items_json = ?, revision = ?, status = 'awaiting_confirmation'
        WHERE proposal_id = ?
        """,
        (json.dumps(items, ensure_ascii=False), revision, proposal_id),
    )
    return _proposal_payload(conn, proposal_id)


def _source_corrected(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """True when the page's active transcript revision has moved past the
    revision this proposal was built from (source evidence changed)."""
    if row["page_id"] is None or row["source_transcript_revision"] is None:
        return False
    pt = conn.execute(
        "SELECT active_revision FROM page_transcripts WHERE page_id = ?",
        (row["page_id"],),
    ).fetchone()
    active = pt["active_revision"] if pt is not None else 0
    return active != row["source_transcript_revision"]


def invalidate_confirmations(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    reason: str,
) -> int:
    """Invalidate all valid confirmations of a proposal and re-open it
    (plan §10.10: corrected source evidence invalidates prior confirmations)."""
    cur = conn.execute(
        """
        UPDATE event_confirmations
        SET status = 'invalidated', invalidated_reason = ?,
            invalidated_at = CURRENT_TIMESTAMP
        WHERE proposal_id = ? AND status = 'valid'
        """,
        (reason, proposal_id),
    )
    conn.execute(
        """
        UPDATE memory_proposals SET status = 'awaiting_confirmation'
        WHERE proposal_id = ? AND status NOT IN ('applied', 'rejected', 'expired')
        """,
        (proposal_id,),
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Clarification (plan §10.6, §10.10; §17 persistence scenario)
# ---------------------------------------------------------------------------

def open_clarification(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    questions: list[dict[str, Any]],
    task_id: str,
    principal_id: str,
    expected_task_revision: int,
) -> dict[str, Any]:
    """Atomically persist waiting_input + the clarification (reuses the S02
    pending-interaction machinery; the worker is released immediately)."""
    row = conn.execute(
        "SELECT * FROM memory_proposals WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    if row is None:
        raise MemoryError("not_found", f"Unknown proposal {proposal_id!r}.", http_status=404)
    if row["status"] in ("applied", "rejected", "expired"):
        raise MemoryError(
            "invalid_state", f"Proposal already {row['status']}.", http_status=409
        )
    task = tasks.get_task(conn, task_id)
    if task["principal_id"] != principal_id:
        raise MemoryError(
            "forbidden_scope", "Only the task owner may open its clarification.",
            http_status=403,
        )
    if task.get("project_id") != row["project_id"]:
        raise MemoryError("forbidden_scope", "Clarification task must belong to the proposal's project.", http_status=403)
    if task["revision"] != expected_task_revision:
        raise MemoryError("revision_conflict", f"Task is at revision {task['revision']}, caller expected {expected_task_revision}.", http_status=409)
    if not questions:
        raise MemoryError("empty_clarification", "Clarification needs questions.")
    if not all(isinstance(question, dict) for question in questions):
        raise MemoryError("invalid_questions", "Clarification questions must be objects.")
    qids = [q.get("question_id") for q in questions]
    if len(set(qids)) != len(qids) or not all(qids):
        raise MemoryError("invalid_questions", "question_id must be present and unique.")
    for q in questions:
        if q.get("prompt") is None:
            raise MemoryError("invalid_questions", f"Question {q.get('question_id')!r} needs a prompt.")

    interaction = tasks.open_pending_interaction(
        conn,
        task_id=task_id,
        question=f"Proposal {proposal_id}: {len(questions)} question(s)",
        context_refs=[
            {"proposal_id": proposal_id, "proposal_revision": row["revision"],
             "question_ids": qids}
        ],
    )
    clarification_id = _new_id("clz")
    conn.execute(
        """
        INSERT INTO clarifications
          (clarification_id, proposal_id, proposal_revision, interaction_id,
           questions_json, status, created_by)
        VALUES (?, ?, ?, ?, ?, 'open', ?)
        """,
        (
            clarification_id,
            proposal_id,
            row["revision"],
            interaction["interaction_id"],
            json.dumps(questions, sort_keys=True, ensure_ascii=False),
            principal_id,
        ),
    )
    conn.execute(
        "UPDATE memory_proposals SET status = 'awaiting_clarification' "
        "WHERE proposal_id = ? AND status != 'applied'",
        (proposal_id,),
    )
    return {
        "clarification_id": clarification_id,
        "interaction_id": interaction["interaction_id"],
        "proposal_id": proposal_id,
        "proposal_revision": row["revision"],
        "task": tasks.get_task(conn, task_id),
    }


def answer_clarification(
    conn: sqlite3.Connection,
    *,
    clarification_id: str,
    answers: list[dict[str, Any]],
    principal_id: str,
    expected_task_revision: int,
    expected_proposal_revision: int,
) -> dict[str, Any]:
    """Authenticated, idempotent resume path bound to clarification_id,
    task revision, proposal revision and principal (plan §10.10).

    - Expired/wrong-task/off-owner replies are rejected.
    - Re-sending identical answers is idempotent.
    - Answers fill values or mark 'unsure'; they NEVER create business
      approvals — confirmations stay a separate capability.
    """
    row = conn.execute(
        "SELECT * FROM clarifications WHERE clarification_id = ?",
        (clarification_id,),
    ).fetchone()
    if row is None:
        raise MemoryError("not_found", f"Unknown clarification {clarification_id!r}.", http_status=404)
    proposal = conn.execute(
        "SELECT * FROM memory_proposals WHERE proposal_id = ?", (row["proposal_id"],)
    ).fetchone()
    if proposal is None:
        raise MemoryError("not_found", "Clarified proposal disappeared.", http_status=404)

    questions = json.loads(row["questions_json"])
    qmap = {q["question_id"]: q for q in questions}

    # Idempotent replay: identical answers to an already-answered clarification.
    if row["status"] == "answered" and row["answers_json"]:
        prior = json.loads(row["answers_json"])
        if _answers_equivalent(prior, answers):
            return {
                "clarification_id": clarification_id,
                "status": row["status"],
                "idempotent": True,
                "proposal_revision": row["proposal_revision"],
            }
        raise MemoryError(
            "invalid_state",
            f"Clarification already {row['status']} with different answers.",
            http_status=409,
        )
    if row["status"] == "partially_answered" and row["answers_json"]:
        prior = json.loads(row["answers_json"])
        if _answers_equivalent(prior, answers):
            return {
                "clarification_id": clarification_id,
                "status": row["status"],
                "idempotent": True,
                "proposal_revision": row["proposal_revision"],
            }
        # Otherwise fall through: this is a SUPPLEMENT answering the
        # remaining questions (merge below), never a contradiction.
    if row["status"] in ("expired", "cancelled"):
        raise MemoryError("invalid_state", f"Clarification {row['status']}.", http_status=409)
    # Optimistic lock against the proposal's CURRENT revision: an earlier
    # answer bumps the proposal, so callers re-read and cite the new revision
    # when supplementing (§10.6/§10.10). Idempotent replays never reach here.
    if proposal["revision"] != expected_proposal_revision:
        raise MemoryError(
            "revision_conflict",
            f"Proposal is at revision {proposal['revision']}, "
            f"caller expected {expected_proposal_revision}.",
            http_status=409,
        )

    # Schema-validate answers against the stored questions (plan §10.10:
    # actually validate the schema).
    for a in answers:
        qid = a.get("question_id")
        if qid not in qmap:
            raise MemoryError(
                "invalid_answer", f"Unknown question {qid!r} for this clarification."
            )
        if "value" not in a:
            raise MemoryError("invalid_answer", f"Answer to {qid!r} needs a 'value'.")

    if row["status"] == "open":
        # First answer: the authoritative authenticated resume path releases
        # the waiting task (S02 machinery, owner + task revision checked).
        interaction = tasks.answer_pending_interaction(
            conn,
            interaction_id=row["interaction_id"],
            answer={"clarification_id": clarification_id, "answers": answers},
            principal_id=principal_id,
            expected_task_revision=expected_task_revision,
        )
        task_payload = interaction["task"]
    else:
        # Supplementary answer to a partially-answered clarification: the
        # interaction itself is one-shot, so validate owner + revision here.
        pi = conn.execute(
            "SELECT task_id FROM pending_interactions WHERE interaction_id = ?",
            (row["interaction_id"],),
        ).fetchone()
        task = tasks.get_task(conn, pi["task_id"])
        if task["principal_id"] != principal_id:
            raise MemoryError(
                "forbidden_scope", "Only the task owner may answer.", http_status=403
            )
        if task["revision"] != expected_task_revision:
            raise MemoryError(
                "revision_conflict",
                f"Task is at revision {task['revision']}, caller expected "
                f"{expected_task_revision}.",
                http_status=409,
            )
        task_payload = tasks.task_to_contract(task)

    merged = answers if not row["answers_json"] else _merge_answers(
        json.loads(row["answers_json"]), answers
    )
    # "answered" means EVERY question is covered by the MERGED answer set —
    # a supplement answering the last remaining question completes it.
    answered_ids = {a["question_id"] for a in merged}
    all_answered = all(q["question_id"] in answered_ids for q in questions)
    status = "answered" if all_answered else "partially_answered"
    conn.execute(
        """
        UPDATE clarifications
        SET answers_json = ?, status = ?, answered_at = CURRENT_TIMESTAMP
        WHERE clarification_id = ?
        """,
        (json.dumps(merged, sort_keys=True, ensure_ascii=False), status, clarification_id),
    )
    _apply_answers_to_items(conn, proposal["proposal_id"], merged)

    # _apply_answers_to_items may bump the proposal revision; report the
    # CURRENT value so callers can cite it for confirmations/supplements.
    current_revision = conn.execute(
        "SELECT revision FROM memory_proposals WHERE proposal_id = ?",
        (proposal["proposal_id"],),
    ).fetchone()["revision"]

    return {
        "clarification_id": clarification_id,
        "status": status,
        "idempotent": False,
        "proposal_revision": current_revision,
        "task": task_payload,
    }


def _answers_equivalent(prior: list[dict[str, Any]], answers: list[dict[str, Any]]) -> bool:
    def norm(lst: list[dict[str, Any]]) -> dict[str, Any]:
        return {a["question_id"]: a.get("value") for a in lst}
    return norm(prior) == norm(answers)


def _merge_answers(
    prior: list[dict[str, Any]], answers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {a["question_id"]: a for a in prior}
    for a in answers:
        by_id[a["question_id"]] = a
    return list(by_id.values())


def _apply_answers_to_items(
    conn: sqlite3.Connection, proposal_id: str, answers: list[dict[str, Any]]
) -> None:
    """Fold answered clarification values back into the proposal items.

    Any item mutation bumps the proposal revision: confirmations bind to the
    revision, so changed values require fresh confirmation (safe-by-default).
    'unsure' answers leave the field unresolved and pending.
    """
    row = conn.execute(
        "SELECT items_json, revision FROM memory_proposals WHERE proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    items = json.loads(row["items_json"])
    changed = False
    for item in items:
        for a in answers:
            target = a.get("item_key")
            if target != item["item_key"]:
                continue
            value = a.get("value")
            field_name = a.get("field") or q_field(a)
            if value == "unsure" or value is None:
                item.setdefault("unresolved", []).append(
                    {"question_id": a["question_id"], "field": field_name}
                )
            elif field_name:
                fields = item.setdefault("fields", {})
                fields[field_name] = {
                    "raw": value,
                    "normalized": value,
                    "clarified": True,
                }
            qids = item.setdefault("clarified_question_ids", [])
            if a["question_id"] not in qids:
                qids.append(a["question_id"])
            changed = True
    if changed:
        conn.execute(
            "UPDATE memory_proposals SET items_json = ?, revision = revision + 1, "
            "status = 'awaiting_confirmation' WHERE proposal_id = ?",
            (json.dumps(items, sort_keys=True, ensure_ascii=False), proposal_id),
        )


def q_field(answer: dict[str, Any]) -> str | None:
    """Extract the field a clarification answer targets."""
    field = answer.get("field")
    return field if isinstance(field, str) and field else None


# ---------------------------------------------------------------------------
# Apply (plan §10.5, §10.6, §10.4; §17 plan-vs-done, computed-vs-observed)
# ---------------------------------------------------------------------------

def apply_proposal(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    principal_id: str,
    operation_id: str,
) -> dict[str, Any]:
    """Apply a proposal item-by-item; partial success returns concrete
    applied/pending objects (plan §10.6).

    Policy per item:
    - needs_confirmation and no valid confirmation  -> pending
    - sample_match.action != 'link' and no confirmed
      sample-creation                                -> pending (never invent samples)
    - unresolved clarification fields                -> pending
    - modality 'plan'                                -> recorded, NO inventory proposal
    - 'computed'                                     -> provenance.result_origin='computed'
    - 'operation' with amount/unit                   -> recorded + inventory outbox
      proposal (only actual usage reaches inventory)
    """
    row = conn.execute(
        "SELECT * FROM memory_proposals WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    if row is None:
        raise MemoryError("not_found", f"Unknown proposal {proposal_id!r}.", http_status=404)
    if row["profile_id"] == "chemistry_notebook" and all(item.get("acceptance") == "default" for item in json.loads(row["items_json"])):
        accepted = accept_knowledge_proposal(conn, project_id=row["project_id"], proposal_id=proposal_id, principal_id=principal_id)
        return {"applied": accepted["adopted"], "pending": [], "already_applied": all(event.get("existing") for event in accepted["adopted"]),
                "proposal": get_proposal(conn, proposal_id), "knowledge_only": True}
    if row["status"] == "applied":
        # Idempotent: return the recorded outcome without re-applying.
        return {"applied": [], "pending": [], "already_applied": True, "proposal": get_proposal(conn, proposal_id)}
    if row["status"] in ("rejected", "expired"):
        raise MemoryError("invalid_state", f"Proposal {row['status']}.", http_status=409)

    # Source evidence corrected since the proposal was built? Invalidate
    # confirmations first; critical items become pending again (§10.10).
    if _source_corrected(conn, row):
        invalidate_confirmations(
            conn,
            proposal_id=proposal_id,
            reason="source_transcript_corrected",
        )
        row = conn.execute(
            "SELECT * FROM memory_proposals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()

    items = json.loads(row["items_json"])
    valid_conf = {
        r["item_key"]: r["decision"]
        for r in conn.execute(
            """
            SELECT item_key, decision FROM event_confirmations
            WHERE proposal_id = ? AND proposal_revision = ?
              AND principal_id = ? AND status = 'valid' AND decision = 'confirmed'
            """,
            (proposal_id, row["revision"], principal_id),
        ).fetchall()
    }
    unresolved_qids = _unresolved_question_ids(conn, proposal_id)

    applied: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for item in items:
        key = item["item_key"]
        if item.get("needs_confirmation") and key not in valid_conf:
            pending.append({"item_key": key, "reason": "missing_confirmation"})
            continue
        if item.get("needs_confirmation") and valid_conf.get(key) == "confirmed" \
                and key in unresolved_qids:
            pending.append({"item_key": key, "reason": "unresolved_clarification"})
            continue
        match = item.get("sample_match") or {"action": "none", "candidates": []}
        sample_id: str | None = None
        if match["action"] == "link":
            sample_id = match["candidates"][0]["sample_id"]
        elif match["action"] in ("clarify", "propose_creation"):
            if not _has_sample_confirmation(conn, row, item):
                pending.append({
                    "item_key": key,
                    "reason": (
                        "sample_identity_clarification"
                        if match["action"] == "clarify"
                        else "sample_creation_needs_confirmation"
                    ),
                })
                continue
            sample_id = _maybe_create_sample(conn, project_id=row["project_id"], item=item)

        event_id = _event_id_for(proposal_id, key)
        existing = conn.execute(
            """
            SELECT 1 FROM event_revisions
            WHERE entity_type = 'experiment_event' AND entity_id = ? AND revision = 1
            """,
            (event_id,),
        ).fetchone()
        if existing is None:
            item["fields"] = _state_fields(conn, project_id=row["project_id"],
                fields=item.get("fields") or {}, sample_ids=[sample_id] if sample_id else [])
            provenance = {
                "proposal_id": proposal_id,
                "proposal_revision": row["revision"],
                "profile": f"{row['profile_id']}@{row['profile_version']}",
                "result_origin": "computed" if item["event_kind"] == "computed" else "reported",
                "confirmed_by": principal_id if key in valid_conf else None,
                "input_type": item.get("input_type"),
            }
            # S07 §13.4: a reported occurrence time rides with the event so
            # cross-channel duplicates (photo + message, same operation) can
            # be detected by fact comparison instead of file hashes. Events
            # without a stated time keep a NULL event_time_start.
            event_time_start = item.get("occurred_at")
            conn.execute(
                """
                INSERT INTO event_revisions
                  (revision_id, entity_type, entity_id, revision, field_json,
                   event_time_start, knowledge_as_of, evidence_refs_json, provenance_json,
                   created_by)
                VALUES (?, 'experiment_event', ?, 1, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?)
                """,
                (
                    _new_id("evr"),
                    event_id,
                    json.dumps(
                        {
                            "event_kind": item["event_kind"],
                            "raw_text": item.get("raw_text"),
                            "fields": item.get("fields", {}),
                            "sample_id": sample_id,
                            "page_id": row["page_id"],
                            # S08 §14 recovery matrix: the owning project rides
                            # with the event facts so cross-domain subtasks
                            # inherit project scoping (without it the receiving
                            # inventory domain could not fence outsiders).
                            "project_id": row["project_id"],
                            # S07 §13.3: the historical-import marker rides
                            # with the event facts; such events never reach
                            # the inventory outbox below (facts-only
                            # bookkeeping unless a user explicitly authorizes
                            # back-dated stock accounting).
                            **(
                                {"historical_import": True}
                                if item.get("historical_import")
                                else {}
                            ),
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                    ),
                    event_time_start,
                    json.dumps(item.get("evidence_refs", []), sort_keys=True),
                    json.dumps(provenance, sort_keys=True, ensure_ascii=False),
                    principal_id,
                ),
            )
            fields = item.get("fields", {}) or {}
            amount_entry = fields.get("amount")
            amount_value = (
                amount_entry.get("normalized")
                if isinstance(amount_entry, dict)
                else amount_entry
            )
            amount_unit = None
            if isinstance(amount_entry, dict):
                amount_unit = amount_entry.get("unit")
            if not amount_unit and isinstance(fields.get("unit"), dict):
                amount_unit = fields["unit"].get("normalized")
            # Only a real, dimension-bearing quantity may reach inventory.
            # Durations/percentages (e.g. "30 分钟") are observations, not
            # stock deltas; a missing unit is held for clarification.
            supported_quantity = (
                amount_value is not None
                and normalize_unit(amount_unit) is not None
            )
            if (
                item["event_kind"] == "operation"
                and supported_quantity
                and sample_id is not None
                and not item.get("historical_import")
            ):
                tasks.enqueue_outbox(
                    conn,
                    aggregate_type="memory_proposal",
                    aggregate_id=proposal_id,
                    message_type="inventory_proposal",
                    payload={
                        "event_id": event_id,
                        "sample_id": sample_id,
                        "amount": amount_value,
                        "unit": amount_unit,
                        "raw_text": item.get("raw_text"),
                        "source": "memory_proposal",
                        "proposal_id": proposal_id,
                        "task_id": row["task_id"],
                        "project_id": row["project_id"],
                    },
                )
        applied.append({"item_key": key, "event_id": event_id, "sample_id": sample_id})

    status = "applied" if (applied and not pending) else (
        "partially_applied" if applied else "awaiting_confirmation"
    )
    conn.execute(
        "UPDATE memory_proposals SET status = ?, applied_at = CURRENT_TIMESTAMP "
        "WHERE proposal_id = ?",
        (status, proposal_id),
    )
    if applied:
        invalidate_artifacts(conn, project_id=row["project_id"])
    return {
        "applied": applied,
        "pending": pending,
        "proposal_status": status,
        "proposal": get_proposal(conn, proposal_id),
    }


def _unresolved_question_ids(conn: sqlite3.Connection, proposal_id: str) -> set[str]:
    """Item keys whose fields were answered 'unsure' (still unresolved)."""
    row = conn.execute(
        "SELECT items_json FROM memory_proposals WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    items = json.loads(row["items_json"])
    return {
        item["item_key"]
        for item in items
        for u in item.get("unresolved", [])
        if u.get("field")
    }


def _has_sample_confirmation(
    conn: sqlite3.Connection, proposal_row: sqlite3.Row, item: dict[str, Any]
) -> bool:
    """A sample may be CREATED only with an explicit confirmation for the
    dedicated creation item (plan §10.4, §17: never invent samples)."""
    key = f"sample:create:{item['item_key']}"
    r = conn.execute(
        """
        SELECT 1 FROM event_confirmations
        WHERE proposal_id = ? AND proposal_revision = ?
          AND item_key = ? AND decision = 'confirmed' AND status = 'valid'
        """,
        (proposal_row["proposal_id"], proposal_row["revision"], key),
    ).fetchone()
    return r is not None


def _maybe_create_sample(
    conn: sqlite3.Connection, *, project_id: str, item: dict[str, Any]
) -> str | None:
    """Create the sample exactly once; label from the item's clarified value."""
    label = (item.get("fields", {}) or {}).get("sample_label")
    normalized = label.get("normalized") if isinstance(label, dict) else label
    if not normalized:
        return None
    existing = conn.execute(
        "SELECT sample_id FROM samples WHERE project_id = ? AND label = ?",
        (project_id, str(normalized)),
    ).fetchone()
    if existing is not None:
        return existing["sample_id"]
    sample_id = _new_id("smp")
    conn.execute(
        "INSERT INTO samples (sample_id, project_id, label, aliases_json) "
        "VALUES (?, ?, ?, '[]')",
        (sample_id, project_id, str(normalized)),
    )
    return sample_id


def _event_id_for(proposal_id: str, item_key: str) -> str:
    """Deterministic event id: same (proposal, item) never duplicates."""
    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"chemclaw:{proposal_id}:{item_key}").hex
    return f"evt_{digest}"


# ---------------------------------------------------------------------------
# Correction & history (plan §10.7)
# ---------------------------------------------------------------------------

CORRECTION_TOP_LEVEL_ALLOWED = {
    "event_kind",
    "raw_text",
    "fields",
    "sample_id",
    "sample_ids",
    "unknown",
    "unknowns",
    "occurred_at",
    "event_time_start",
    "event_time_end",
    "title",
    "details",
}
CORRECTION_FORBIDDEN_FIELDS = {
    "project_id",
    "created_by",
    "approved_by",
    "confirmed_by",
    "revision",
    "revision_id",
    "provenance_json",
    "knowledge_as_of",
    "page_id",
    "historical_import",
}


def _validate_correction_changes(changes: dict[str, Any]) -> None:
    if "event_kind" in changes and (not isinstance(changes["event_kind"], str) or changes["event_kind"] not in EVENT_MODALITIES):
        raise MemoryError("invalid_correction", "event_kind must be a supported event modality.")
    if "raw_text" in changes and not isinstance(changes["raw_text"], str):
        raise MemoryError("invalid_correction", "raw_text must be text.")
    unknown = sorted(set(changes) - CORRECTION_TOP_LEVEL_ALLOWED)
    if unknown:
        raise MemoryError(
            "invalid_correction",
            f"Correction may not change field(s) {unknown}; corrections use an allow-list.",
            http_status=403,
        )
    for name in ("occurred_at", "event_time_start", "event_time_end"):
        if name in changes and changes[name] is not None and not isinstance(changes[name], str):
            raise MemoryError("invalid_correction", f"{name} must preserve a source timestamp string or null.")
    if "unknown" in changes and not isinstance(changes["unknown"], bool):
        raise MemoryError("invalid_correction", "unknown must be boolean.")
    if "unknowns" in changes and (not isinstance(changes["unknowns"], list) or any(not isinstance(value, str) for value in changes["unknowns"])):
        raise MemoryError("invalid_correction", "unknowns must be an array of strings.")
    fields = changes.get("fields")
    if "fields" in changes:
        if not isinstance(fields, dict):
            raise MemoryError("invalid_correction", "changes.fields must be an object.")
        forbidden = sorted(set(fields) & CORRECTION_FORBIDDEN_FIELDS)
        if forbidden:
            raise MemoryError(
                "invalid_correction",
                f"Correction may not change protected field(s) {forbidden}.",
                http_status=403,
            )
    forbidden = sorted(set(changes) & CORRECTION_FORBIDDEN_FIELDS)
    if forbidden:
        raise MemoryError(
            "invalid_correction",
            f"Correction may not change protected field(s) {forbidden}.",
            http_status=403,
        )


def correct_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    expected_revision: int,
    changes: dict[str, Any],
    reason: str,
    principal_id: str,
    correction_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append an immutable correction revision bound to the current head.

    On revision conflict the correction is NOT applied and NOT lost: it is
    parked as an awaiting-confirmation correction proposal (plan §10.7:
    conflicting corrections enter a pending state).
    """
    head = conn.execute(
        """
        SELECT revision,field_json FROM event_revisions
        WHERE entity_type = 'experiment_event' AND entity_id = ?
        ORDER BY revision DESC LIMIT 1
        """,
        (event_id,),
    ).fetchone()
    if head is None:
        raise MemoryError("not_found", f"Unknown event {event_id!r}.", http_status=404)
    if not reason:
        raise MemoryError("invalid_correction", "A correction requires a reason.")
    _validate_correction_changes(changes or {})
    head_fields = json.loads(head["field_json"])
    head_project = head_fields.get("project_id")
    if not head_project and head_fields.get("page_id"):
        project_row = conn.execute("SELECT d.project_id FROM evidence_pages p JOIN evidence_documents d ON d.document_id=p.document_id WHERE p.page_id=?", (head_fields["page_id"],)).fetchone()
        head_project = project_row["project_id"] if project_row else None
    supplied_ids = changes.get("sample_ids", [])
    if supplied_ids is not None and not isinstance(supplied_ids, list):
        raise MemoryError("invalid_correction", "sample_ids must be an array.")
    identity_ids = list(supplied_ids or []) + ([changes["sample_id"]] if changes.get("sample_id") is not None else [])
    for sample_id in identity_ids:
        if not isinstance(sample_id, str) or not head_project or not conn.execute("SELECT 1 FROM samples WHERE sample_id=? AND project_id=?", (sample_id, head_project)).fetchone():
            raise MemoryError("forbidden_scope", "Correction sample ids must belong to the event project.", http_status=403)
    correction_refs = (correction_source or {}).get("evidence_refs", [])
    if not isinstance(correction_refs, list) or any(not isinstance(ref, dict) for ref in correction_refs):
        raise MemoryError("invalid_correction", "Correction evidence_refs must be an array of references.")
    for ref in correction_refs:
        cited = conn.execute("SELECT tr.page_id,d.project_id FROM transcript_blocks b JOIN transcript_runs tr ON tr.run_id=b.run_id JOIN evidence_pages p ON p.page_id=tr.page_id JOIN evidence_documents d ON d.document_id=p.document_id WHERE b.block_id=?", (ref.get("block_id"),)).fetchone()
        if not cited or cited["project_id"] != head_project or (ref.get("page_id") and ref["page_id"] != cited["page_id"]):
            raise MemoryError("forbidden_scope", "Correction evidence must belong to the event project and cited page.", http_status=403)
    if head["revision"] != expected_revision:
        correction_id = _new_id("corr")
        conn.execute(
            """
            INSERT INTO memory_proposals
              (proposal_id, project_id, page_id, source_transcript_revision,
               profile_id, profile_version, items_json, revision, status,
               operation_id, created_by)
            VALUES (?, NULL, NULL, NULL, 'correction', '1.0', ?, 0,
                    'awaiting_confirmation', ?, ?)
            """,
            (
                correction_id,
                json.dumps(
                    [{
                        "item_key": f"correct:{event_id}:r{expected_revision}",
                        "event_kind": "correction",
                        "raw_text": reason,
                        "fields": changes,
                        "expected_revision": expected_revision,
                        "event_id": event_id,
                        "evidence_refs": (correction_source or {}).get("evidence_refs", []),
                        "correction_source": correction_source,
                        "needs_confirmation": True,
                    }],
                    sort_keys=True,
                    ensure_ascii=False,
                ),
                f"correct:{event_id}:{expected_revision}:{principal_id}",
                principal_id,
            ),
        )
        return {
            "outcome": "parked_as_pending",
            "correction_proposal_id": correction_id,
            "current_revision": head["revision"],
        }

    next_rev = head["revision"] + 1
    prior = conn.execute(
        """
        SELECT field_json, provenance_json, event_time_start, event_time_end FROM event_revisions
        WHERE entity_type = 'experiment_event' AND entity_id = ?
          AND revision = ?
        """,
        (event_id, expected_revision),
    ).fetchone()
    merged = json.loads(prior["field_json"])
    original_project = merged.get("project_id")
    if not original_project and merged.get("page_id"):
        source_page = conn.execute("SELECT d.project_id FROM evidence_pages p JOIN evidence_documents d ON d.document_id=p.document_id WHERE p.page_id=?", (merged["page_id"],)).fetchone()
        original_project = source_page["project_id"] if source_page else None
    for k, v in (changes or {}).items():
        if k == "fields" and isinstance(v, dict) and isinstance(merged.get("fields"), dict):
            merged["fields"].update(v)
        else:
            merged[k] = v
    identity_fields_changed = bool(set((changes or {}).get("fields") or {}) & {
        "sample_label", "related_sample_labels", "sample_labels", "subject_sample_labels"})
    if "sample_id" in changes or "sample_ids" in changes:
        supplied_ids = changes.get("sample_ids")
        if supplied_ids is None:
            supplied_ids = [changes["sample_id"]] if changes.get("sample_id") else []
        if not isinstance(supplied_ids, list) or any(not isinstance(value, str) or not value for value in supplied_ids):
            raise MemoryError("invalid_correction", "sample_ids must be an array of real sample ids.")
        supplied_ids = list(dict.fromkeys(supplied_ids))
        primary = changes.get("sample_id", supplied_ids[0] if supplied_ids else None)
        if primary is not None and (not isinstance(primary, str) or primary not in supplied_ids):
            raise MemoryError("invalid_correction", "sample_id must be one of sample_ids or null.")
        for sample_id in supplied_ids:
            if not original_project or not conn.execute("SELECT 1 FROM samples WHERE sample_id=? AND project_id=?", (sample_id, original_project)).fetchone():
                raise MemoryError("forbidden_scope", "Correction sample ids must belong to the event project.", http_status=403)
        merged["sample_ids"], merged["sample_id"] = supplied_ids, primary
        merged["sample_resolution"] = {"sample_ids": supplied_ids, "links": [], "issues": [], "mode": "explicit_correction"}
    elif identity_fields_changed:
        if not original_project:
            raise MemoryError("forbidden_scope", "Correction requires an authoritative event project.", http_status=403)
        resolution = _knowledge_sample_links(conn, project_id=original_project, item={"fields": merged.get("fields") or {}})
        merged["sample_ids"] = resolution["sample_ids"]
        merged["sample_id"] = resolution["sample_ids"][0] if resolution["sample_ids"] else None
        merged["sample_resolution"] = resolution
    if (identity_fields_changed or "sample_id" in changes or "sample_ids" in changes
            or set(changes.get("fields") or {}) & {"state", "state_updates"}):
        merged["fields"] = _state_fields(conn, project_id=original_project,
            fields=merged.get("fields") or {}, sample_ids=sorted(knowledge_state._ids(merged)))
    for ref in (correction_source or {}).get("evidence_refs", []):
        cited = conn.execute("SELECT tr.page_id,d.project_id FROM transcript_blocks b JOIN transcript_runs tr ON tr.run_id=b.run_id JOIN evidence_pages p ON p.page_id=tr.page_id JOIN evidence_documents d ON d.document_id=p.document_id WHERE b.block_id=?", (ref.get("block_id"),)).fetchone()
        if not cited or cited["project_id"] != original_project or (ref.get("page_id") and ref["page_id"] != cited["page_id"]):
            raise MemoryError("forbidden_scope", "Correction evidence must belong to the event project and cited page.", http_status=403)
    prior_provenance = json.loads(prior["provenance_json"] or "{}")
    source_kind = (correction_source or {}).get("source_kind") or "human_report"
    if not isinstance(source_kind, str):
        raise MemoryError("invalid_correction", "Correction source_kind must be a string.")
    provenance = {key: prior_provenance[key] for key in ("acceptance", "acceptance_policy", "knowledge_only", "proposal_id", "profile", "source_transcript_revision") if key in prior_provenance}
    provenance.update(correction_of=expected_revision, reason=reason, correction_source=correction_source,
                      source_kind=source_kind, confirmed_by=None)
    event_time = changes.get("occurred_at", changes.get("event_time_start", prior["event_time_start"]))
    if "event_time_start" in changes and "occurred_at" not in changes:
        merged["occurred_at"] = event_time
    conn.execute(
        """
        INSERT INTO event_revisions
          (revision_id, entity_type, entity_id, revision, field_json,
           event_time_start,event_time_end,knowledge_as_of, supersedes_revision_id, evidence_refs_json,
           provenance_json, created_by)
        SELECT ?, 'experiment_event', entity_id, ?, ?, ?, ?, CURRENT_TIMESTAMP,
               revision_id, COALESCE(?, evidence_refs_json), ?, ?
        FROM event_revisions
        WHERE entity_type = 'experiment_event' AND entity_id = ? AND revision = ?
        """,
        (
            _new_id("evr"),
            next_rev,
            json.dumps(merged, sort_keys=True, ensure_ascii=False),
            event_time,
            changes.get("event_time_end", prior["event_time_end"]),
            (
                json.dumps(
                    (correction_source or {}).get("evidence_refs", []),
                    sort_keys=True,
                    ensure_ascii=False,
                )
                if correction_source and correction_source.get("evidence_refs")
                else None
            ),
            json.dumps(provenance, sort_keys=True, ensure_ascii=False),
            principal_id,
            event_id,
            expected_revision,
        ),
    )
    entity = conn.execute(
        """
        SELECT json_extract(field_json, '$.page_id') AS page_id
        FROM event_revisions
        WHERE entity_type = 'experiment_event' AND entity_id = ? AND revision = 1
        """,
        (event_id,),
    ).fetchone()
    page_row = conn.execute(
        """
        SELECT d.project_id FROM evidence_pages p
        JOIN evidence_documents d ON d.document_id = p.document_id
        WHERE p.page_id = ?
        """,
        (entity["page_id"],),
    ).fetchone() if entity is not None and entity["page_id"] else None
    project_id = page_row["project_id"] if page_row is not None else None
    invalidate_confirmations_for_event(conn, event_id=event_id, reason="event_corrected")
    invalidate_artifacts(conn, project_id=project_id)
    return {
        "outcome": "applied",
        "event_id": event_id,
        "revision": next_rev,
        "field_json": merged,
    }


def invalidate_confirmations_for_event(
    conn: sqlite3.Connection, *, event_id: str, reason: str
) -> int:
    """Event corrected -> any pending proposal items sourced from the same
    page lose their confirmations (plan §10.10)."""
    page_id_row = conn.execute(
        """
        SELECT json_extract(field_json, '$.page_id') AS page_id
        FROM event_revisions
        WHERE entity_type = 'experiment_event' AND entity_id = ?
        ORDER BY revision DESC LIMIT 1
        """,
        (event_id,),
    ).fetchone()
    if page_id_row is None or page_id_row["page_id"] is None:
        return 0
    proposals = conn.execute(
        "SELECT proposal_id FROM memory_proposals WHERE page_id = ?",
        (page_id_row["page_id"],),
    ).fetchall()
    total = 0
    for p in proposals:
        total += invalidate_confirmations(
            conn, proposal_id=p["proposal_id"], reason=reason
        )
    return total


def read_event(conn: sqlite3.Connection, event_id: str) -> dict[str, Any]:
    """Default read: only the CURRENT EFFECTIVE revision (plan §10.7)."""
    head = conn.execute(
        """
        SELECT * FROM event_revisions
        WHERE entity_type = 'experiment_event' AND entity_id = ?
        ORDER BY revision DESC LIMIT 1
        """,
        (event_id,),
    ).fetchone()
    if head is None:
        raise MemoryError("not_found", f"Unknown event {event_id!r}.", http_status=404)
    payload = dict(head)
    payload["field_json"] = json.loads(head["field_json"])
    payload["evidence_refs"] = json.loads(head["evidence_refs_json"])
    payload["provenance"] = json.loads(head["provenance_json"])
    payload["is_corrected"] = head["revision"] > 1
    return payload


def read_event_history(conn: sqlite3.Connection, event_id: str) -> dict[str, Any]:
    """Full immutable revision chain, oldest first."""
    rows = conn.execute(
        """
        SELECT * FROM event_revisions
        WHERE entity_type = 'experiment_event' AND entity_id = ?
        ORDER BY revision
        """,
        (event_id,),
    ).fetchall()
    if not rows:
        raise MemoryError("not_found", f"Unknown event {event_id!r}.", http_status=404)
    revisions = []
    for r in rows:
        item = dict(r)
        item["field_json"] = json.loads(r["field_json"])
        item["evidence_refs"] = json.loads(r["evidence_refs_json"])
        item["provenance"] = json.loads(r["provenance_json"])
        revisions.append(item)
    return {"event_id": event_id, "revisions": revisions}


# ---------------------------------------------------------------------------
# Derived artifacts / summary index (plan §10.8)
# ---------------------------------------------------------------------------

def write_artifact(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    entity_type: str,
    entity_id: str,
    artifact_kind: str,
    content: dict[str, Any],
    based_on_revisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Persist a DERIVED artifact. Derived artifacts never become facts:
    they carry derived=1 and are never written into event_revisions/samples."""
    existing = conn.execute(
        """
        SELECT artifact_id FROM memory_artifacts
        WHERE entity_type = ? AND entity_id = ? AND artifact_kind = ?
        """,
        (entity_type, entity_id, artifact_kind),
    ).fetchone()
    if existing is not None:
        conn.execute(
            """
            UPDATE memory_artifacts
            SET content_json = ?, based_on_revisions_json = ?, stale = 0,
                rebuilt_at = CURRENT_TIMESTAMP
            WHERE artifact_id = ?
            """,
            (
                json.dumps(content, sort_keys=True, ensure_ascii=False),
                json.dumps(based_on_revisions, sort_keys=True, ensure_ascii=False),
                existing["artifact_id"],
            ),
        )
        return {"artifact_id": existing["artifact_id"], "rebuilt": True}
    artifact_id = _new_id("art")
    conn.execute(
        """
        INSERT INTO memory_artifacts
          (artifact_id, project_id, entity_type, entity_id, artifact_kind,
           content_json, based_on_revisions_json, derived, stale)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0)
        """,
        (
            artifact_id,
            project_id,
            entity_type,
            entity_id,
            artifact_kind,
            json.dumps(content, sort_keys=True, ensure_ascii=False),
            json.dumps(based_on_revisions, sort_keys=True, ensure_ascii=False),
        ),
    )
    return {"artifact_id": artifact_id, "rebuilt": False}


def invalidate_artifacts(
    conn: sqlite3.Connection, *, project_id: str | None
) -> int:
    """Mark dependent summaries/indices stale after an event change."""
    cur = conn.execute(
        "UPDATE memory_artifacts SET stale = 1 WHERE project_id = ?", (project_id,)
    )
    return cur.rowcount


def rebuild_artifact(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
) -> dict[str, Any]:
    """Rebuild one stale artifact from current effective revisions."""
    row = conn.execute(
        "SELECT * FROM memory_artifacts WHERE artifact_id = ?", (artifact_id,)
    ).fetchone()
    if row is None:
        raise MemoryError("not_found", f"Unknown artifact {artifact_id!r}.", http_status=404)
    if row["artifact_kind"] != "timeline":
        raise MemoryError(
            "invalid_artifact",
            f"Kind {row['artifact_kind']!r} has no rebuild procedure.",
        )
    events = conn.execute(
        """
        SELECT entity_id, MAX(revision) AS revision
        FROM event_revisions
        WHERE entity_type = 'experiment_event'
        GROUP BY entity_id
        """
    ).fetchall()
    timeline = []
    for e in events:
        head = conn.execute(
            """
            SELECT field_json, provenance_json FROM event_revisions
            WHERE entity_type = 'experiment_event' AND entity_id = ?
              AND revision = ?
            """,
            (e["entity_id"], e["revision"]),
        ).fetchone()
        fields = json.loads(head["field_json"])
        if fields.get("page_id") and _page_in_project(
            conn, fields["page_id"], row["project_id"]
        ):
            timeline.append(
                {
                    "event_id": e["entity_id"],
                    "revision": e["revision"],
                    "event_kind": fields.get("event_kind"),
                    "sample_id": fields.get("sample_id"),
                }
            )
    conn.execute(
        """
        UPDATE memory_artifacts
        SET content_json = ?, based_on_revisions_json = ?, stale = 0,
            rebuilt_at = CURRENT_TIMESTAMP
        WHERE artifact_id = ?
        """,
        (
            json.dumps({"timeline": timeline}, sort_keys=True, ensure_ascii=False),
            json.dumps(
                [{"event_id": t["event_id"], "revision": t["revision"]} for t in timeline],
                sort_keys=True,
            ),
            artifact_id,
        ),
    )
    return {"artifact_id": artifact_id, "stale": False, "events": len(timeline)}


def _page_in_project(conn: sqlite3.Connection, page_id: str, project_id: str | None) -> bool:
    r = conn.execute(
        """
        SELECT d.project_id FROM evidence_pages p
        JOIN evidence_documents d ON d.document_id = p.document_id
        WHERE p.page_id = ?
        """,
        (page_id,),
    ).fetchone()
    return r is not None and r["project_id"] == project_id
