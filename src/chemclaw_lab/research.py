"""S09 D-domain: research milestones with locatable-evidence criteria (§15).

- Milestones and their acceptance criteria are first-class objects; a
  criterion is checked by OPENING its evidence refs (page/block ids), not by
  asking a model whether it "looks done".
- Status transitions are human decisions (研究者审核); there is deliberately
  no overall research percentage anywhere (§15 D).
- When the source evidence is corrected (a new transcript revision), every
  criterion that pinned the old revision goes STALE (§15 扩展机制验收).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any


class ResearchError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def create_milestone(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    title: str,
    description: str | None,
    created_by: str | None = None,
) -> dict[str, Any]:
    milestone_id = f"ms_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO research_milestones (milestone_id, project_id, title, "
        "description, status, created_by) VALUES (?, ?, ?, ?, 'pending', ?)",
        (milestone_id, project_id, title, description, created_by),
    )
    return {"milestone_id": milestone_id, "project_id": project_id,
            "title": title, "status": "pending"}


def attach_criterion(
    conn: sqlite3.Connection,
    *,
    milestone_id: str,
    description: str,
    evidence_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    milestone = conn.execute(
        "SELECT project_id, status FROM research_milestones WHERE milestone_id = ?",
        (milestone_id,),
    ).fetchone()
    if milestone is None:
        raise ResearchError("not_found", f"Unknown milestone {milestone_id!r}.", http_status=404)
    if milestone["status"] in ("accepted", "rejected"):
        raise ResearchError(
            "invalid_state", "A decided milestone cannot take new criteria.")
    normalized: list[dict[str, Any]] = []
    for ref in evidence_refs or []:
        if not {"page_id", "block_id"} <= set(ref):
            raise ResearchError(
                "schema_violation",
                "Each evidence ref needs at least page_id and block_id (locatable).",
            )
        if "recorded_revision" not in ref:
            page = conn.execute(
                "SELECT active_revision FROM page_transcripts WHERE page_id = ?",
                (ref["page_id"],),
            ).fetchone()
            ref = {**ref, "recorded_revision": page["active_revision"] if page else 0}
        normalized.append(ref)
    criterion_id = f"crit_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO milestone_criteria (criterion_id, milestone_id, description, "
        "evidence_refs, status) VALUES (?, ?, ?, ?, 'pending')",
        (criterion_id, milestone_id, description,
         json.dumps(normalized, ensure_ascii=False)),
    )
    return {"criterion_id": criterion_id, "milestone_id": milestone_id,
            "status": "pending"}


def _open_evidence_ref(
    conn: sqlite3.Connection, ref: dict[str, Any]
) -> dict[str, Any]:
    """Resolve one locatable evidence ref against the page's ACTIVE transcript.

    The block must exist in the active run; corrected pages may have dropped
    the pinned block id, in which case the ref does not open (unverified) and
    :func:`stale_criteria_for_page` flips the criterion to STALE.
    """
    page_id = ref.get("page_id")
    block_id = ref.get("block_id")
    if not page_id or not block_id:
        return {"ref": ref, "open": False, "reason": "incomplete ref"}
    page = conn.execute(
        "SELECT active_run_id, active_revision FROM page_transcripts WHERE page_id = ?",
        (page_id,),
    ).fetchone()
    if page is None or not page["active_run_id"]:
        return {"ref": ref, "open": False, "reason": "page has no active transcript"}
    block = conn.execute(
        "SELECT block_json FROM transcript_blocks WHERE run_id = ? AND block_id = ?",
        (page["active_run_id"], block_id),
    ).fetchone()
    if block is None:
        return {"ref": ref, "open": False, "reason": "block not in active revision"}
    text = str(json.loads(block["block_json"]).get("text") or "")
    return {
        "ref": ref,
        "open": True,
        "page_id": page_id,
        "block_id": block_id,
        "current_revision": page["active_revision"],
        "text_excerpt": text[:200],
    }


def check_criterion(
    conn: sqlite3.Connection,
    *,
    criterion_id: str,
    checked_by: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Verify a criterion by opening its locatable evidence refs.

    Satisfied requires: every ref opens on the page's ACTIVE transcript
    revision. Evidence corrected after the criterion was attached makes the
    criterion STALE first (:func:`stale_criteria_for_page`), and a stale
    criterion refuses to be checked — the reviewer must re-attach fresh refs
    (§15: 更正源证据使依赖进度/分析报告过期).
    """
    row = conn.execute(
        "SELECT mc.*, rm.status AS milestone_status, rm.project_id "
        "FROM milestone_criteria mc "
        "JOIN research_milestones rm ON rm.milestone_id = mc.milestone_id "
        "WHERE mc.criterion_id = ?",
        (criterion_id,),
    ).fetchone()
    if row is None:
        raise ResearchError("not_found", f"Unknown criterion {criterion_id!r}.", http_status=404)
    if row["status"] == "stale":
        raise ResearchError(
            "invalid_state",
            "Criterion is stale after a source-evidence correction; re-attach "
            "fresh evidence refs first.",
        )
    if row["milestone_status"] not in ("in_review", "pending"):
        raise ResearchError(
            "invalid_state",
            f"Milestone is {row['milestone_status']!r}; criteria checks need an "
            "open review.",
        )
    refs = json.loads(row["evidence_refs"] or "[]")
    opened = [_open_evidence_ref(conn, ref) for ref in refs]
    if not refs:
        raise ResearchError(
            "invalid_state",
            "Criterion has no locatable evidence refs; attach evidence first.",
        )
    all_open = all(o["open"] for o in opened)
    status = "satisfied" if all_open else "unverified"
    conn.execute(
        "UPDATE milestone_criteria SET status = ?, check_note = ?, checked_by = ?, "
        "checked_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
        "WHERE criterion_id = ?",
        (status, note, checked_by, criterion_id),
    )
    return {
        "criterion_id": criterion_id,
        "status": status,
        "evidence_opened": opened,
    }


def stale_criteria_for_page(conn: sqlite3.Connection, page_id: str) -> list[str]:
    """Mark criteria STALE when their pinned evidence was corrected away
    (§15: 更正源证据使依赖进度/分析报告过期). A criterion goes stale when its
    recorded_revision no longer matches the page's active revision, or when
    its block id disappeared from the active run. Returns stale ids."""
    active = conn.execute(
        "SELECT active_revision, active_run_id FROM page_transcripts WHERE page_id = ?",
        (page_id,),
    ).fetchone()
    if active is None:
        return []
    rows = conn.execute(
        "SELECT criterion_id, evidence_refs FROM milestone_criteria "
        "WHERE status IN ('pending', 'satisfied', 'unverified')"
    ).fetchall()
    stale_ids: list[str] = []
    for row in rows:
        refs = json.loads(row["evidence_refs"] or "[]")
        hit = False
        for ref in refs:
            if ref.get("page_id") != page_id:
                continue
            recorded = ref.get("recorded_revision")
            if recorded is not None and recorded != active["active_revision"]:
                hit = True
                break
            if active["active_run_id"]:
                present = conn.execute(
                    "SELECT 1 FROM transcript_blocks WHERE run_id = ? AND block_id = ?",
                    (active["active_run_id"], ref.get("block_id")),
                ).fetchone()
                if present is None:
                    hit = True
                    break
        if hit:
            conn.execute(
                "UPDATE milestone_criteria SET status = 'stale', check_note = "
                "'source evidence corrected', updated_at = CURRENT_TIMESTAMP "
                "WHERE criterion_id = ?",
                (row["criterion_id"],),
            )
            stale_ids.append(row["criterion_id"])
    return stale_ids


def decide_milestone(
    conn: sqlite3.Connection,
    *,
    milestone_id: str,
    decision: str,
    decided_by: str,
) -> dict[str, Any]:
    """Human researcher decision — the ONLY path to accepted/rejected (§15 D)."""
    row = conn.execute(
        "SELECT status FROM research_milestones WHERE milestone_id = ?",
        (milestone_id,),
    ).fetchone()
    if row is None:
        raise ResearchError("not_found", f"Unknown milestone {milestone_id!r}.", http_status=404)
    if decision not in ("accepted", "rejected"):
        raise ResearchError("schema_violation", "decision must be accepted or rejected.")
    if row["status"] == decision:
        return {"milestone_id": milestone_id, "status": decision, "idempotent_replay": True}
    if row["status"] not in ("pending", "in_review"):
        raise ResearchError(
            "invalid_state",
            f"Milestone is {row['status']!r} and cannot be re-decided.",
        )
    conn.execute(
        "UPDATE research_milestones SET status = ?, decided_by = ?, "
        "updated_at = CURRENT_TIMESTAMP WHERE milestone_id = ?",
        (decision, decided_by, milestone_id),
    )
    return {"milestone_id": milestone_id, "status": decision, "decided_by": decided_by}


def set_in_review(conn: sqlite3.Connection, *, milestone_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT status FROM research_milestones WHERE milestone_id = ?",
        (milestone_id,),
    ).fetchone()
    if row is None:
        raise ResearchError("not_found", f"Unknown milestone {milestone_id!r}.", http_status=404)
    if row["status"] != "pending":
        raise ResearchError(
            "invalid_state", f"Milestone is {row['status']!r}, not pending.")
    conn.execute(
        "UPDATE research_milestones SET status = 'in_review', "
        "updated_at = CURRENT_TIMESTAMP WHERE milestone_id = ?",
        (milestone_id,),
    )
    return {"milestone_id": milestone_id, "status": "in_review"}


def milestone_overview(
    conn: sqlite3.Connection,
    *,
    project_ids: list[str] | None = None,
    milestone_id: str | None = None,
) -> dict[str, Any]:
    if milestone_id is not None:
        milestones = conn.execute(
            "SELECT * FROM research_milestones WHERE milestone_id = ?",
            (milestone_id,),
        ).fetchall()
    elif project_ids is not None:
        placeholders = ",".join("?" for _ in project_ids)
        milestones = conn.execute(
            f"SELECT * FROM research_milestones WHERE project_id IN ({placeholders}) "
            "ORDER BY created_at",
            project_ids,
        ).fetchall()
    else:
        milestones = conn.execute(
            "SELECT * FROM research_milestones ORDER BY created_at"
        ).fetchall()
    result = []
    for m in milestones:
        item = dict(m)
        criteria = conn.execute(
            "SELECT criterion_id, description, evidence_refs, status, check_note, "
            "checked_by, checked_at FROM milestone_criteria WHERE milestone_id = ? "
            "ORDER BY created_at",
            (m["milestone_id"],),
        ).fetchall()
        item["criteria"] = [
            {**dict(c), "evidence_refs": json.loads(c["evidence_refs"] or "[]")}
            for c in criteria
        ]
        result.append(item)
    if milestone_id is not None and not result:
        raise ResearchError("not_found", f"Unknown milestone {milestone_id!r}.", http_status=404)
    return {"milestones": result}
