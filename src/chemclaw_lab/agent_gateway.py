"""S06: the agent tool gateway between the Pi host and Core capabilities.

The gateway is the ONLY path a running agent has to the Core (no shell, SQL,
or arbitrary file tools are ever offered — plan §12.4). Every call:

1. resolves a server-signed run token (identity lives in the token row);
2. checks the capability against the manifest whitelist (a capability outside
   the manifest is rejected even if the principal would be allowed it);
3. validates project scope from the token binding (params carry no identity);
4. serializes mutating calls per task (first version: writes are serial);
5. counts the call against the run's tool-call budget;
6. re-authorized by ``invoke_capability`` with the token-bound principal —
   memberships/roles/scope are checked by Core again, from the DB.

Errors keep stable codes: ``unauthorized``, ``forbidden_capability``,
``forbidden_scope``, ``budget_exhausted``, plus the capability envelope's own
codes surfaced verbatim so the model can quote ``action_status`` truthfully.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections import Counter
from typing import Any

from . import agents, auth, capabilities, orchestration
from .read_contracts import READ_INPUT_SCHEMAS
from .service_config import ServiceSettings

# Per-task serialization of mutating tool calls (process-wide). SQLite's
# busy_timeout backs this up across processes.
_write_locks: dict[str, Any] = {}
_write_locks_guard = threading.Lock()

# Delegated authority is never an electronic signature by the initiating
# human. Keep these out of discovery and reject them even if a manifest is
# accidentally configured to include them.
_HUMAN_ONLY_TOOLS = frozenset({
    "inventory.event.resolve", "inventory.approval.grant",
    "memory.proposal.confirm_items", "memory.proposal.item.update",
    "evidence.transcript.correct", "memory.clarification.answer",
    "task.interaction.answer", "resource.booking.confirm",
    "research.milestone.decide", "assistant.agent.start", "assistant.agent.finish",
})


def _task_write_lock(task_id: str):
    with _write_locks_guard:
        if task_id not in _write_locks:
            _write_locks[task_id] = threading.RLock()
        return _write_locks[task_id]


def run_write_lock(conn, run_id: str):
    """Serialize a root and its children with job cancellation/reclaim."""
    run = agents.get_run(conn, run_id)
    root_id = orchestration.metadata(conn, run["task_id"]).get("root_run_id", run_id)
    root = agents.get_run(conn, root_id)
    return _task_write_lock(root["task_id"])


def _validate_write_lease(conn, token_row):
    # This check runs under the same tree lock as cancellation and reclaim.
    # Re-read authority: the request may have waited while its token was revoked.
    token = conn.execute("SELECT * FROM agent_run_tokens WHERE token_id=?", (token_row["token_id"],)).fetchone()
    if token is None or token["status"] != "active":
        raise agents.AgentHostError("stale_worker", "Run token is no longer active.", http_status=409)
    if conn.execute("SELECT 1 FROM agent_run_tokens WHERE token_id=? AND expires_at IS NOT NULL AND expires_at<=datetime('now')", (token_row["token_id"],)).fetchone():
        raise agents.AgentHostError("stale_worker", "Run token has expired.", http_status=409)
    run = agents.get_run(conn, token_row["run_id"])
    if run["status"] != "running" or int(run["claim_epoch"]) != int(token_row.get("claim_epoch") or 0):
        raise agents.AgentHostError("stale_worker", "Run execution lease is no longer current.", http_status=409)
    root_id = orchestration.metadata(conn, run["task_id"]).get("root_run_id", run["run_id"])
    # Legacy claimed runs have no durable job. Queued jobs require a live lease.
    job = conn.execute("SELECT job_id,status,lease_expires_at FROM assistant_jobs WHERE run_id=?", (root_id,)).fetchone()
    if job and (job["status"] != "running" or not conn.execute("SELECT 1 FROM assistant_jobs WHERE job_id=? AND lease_expires_at>datetime('now')", (job["job_id"],)).fetchone()):
        raise agents.AgentHostError("stale_worker", "Job is stopped or execution lease expired.", http_status=409)


# Minimal parameter contracts for the agent-facing capability surface. These
# describe WHAT the model may pass — never WHO it is (identity is bound in the
# run token). Full validation still happens inside the capability handlers.
AGENT_TOOL_PARAM_SCHEMAS: dict[str, dict[str, Any]] = {
    "inventory.message.process": {
        "type": "object", "properties": {
            "project_id": {"type": "string"}, "raw_text": {"type": "string", "description": "The user's original inventory instruction; existing Core policy determines proposal/apply."},
            "expected_revision": {"type": "integer"}, "source_message_id": {"type": "string"},
        }, "required": ["project_id", "raw_text"],
    },
    "inventory.subtask.status": {
        "type": "object", "properties": {key: {"type": "string"} for key in ("project_id", "status", "source_event_id", "parent_task_id")},
    },
    "inventory.subtask.execute": {
        "type": "object", "properties": {"subtask_id": {"type": "string"}, "expected_revision": {"type": "integer"}}, "required": ["subtask_id"],
    },
    "inventory.subtask.resolve": {
        "type": "object", "properties": {
            "subtask_id": {"type": "string"}, "chemical_id": {"type": "string"},
            "unit": {"type": "string"}, "amount": {"type": "number"}, "confirm_duplicate": {"type": "boolean"},
        }, "required": ["subtask_id"],
    },
    "inventory.adjustment.propose": {
        "type": "object", "properties": {
            "source_event_id": {"type": "string"}, "corrected_amount": {"type": "number"},
            "corrected_unit": {"type": "string"}, "reason": {"type": "string"},
        }, "required": ["source_event_id", "corrected_amount", "corrected_unit", "reason"],
    },
    "inventory.adjustment.apply": {
        "type": "object", "properties": {
            "adjustment_id": {"type": "string"}, "approval_id": {"type": "string", "description": "Existing exact-revision human grant; the agent cannot issue one."},
            "expected_revision": {"type": "integer"},
        }, "required": ["adjustment_id", "approval_id"],
    },
    "query.samples.list": {
        "type": "object", "properties": {"project_ids": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer"}},
    },
    "query.report.save": {
        "type": "object", "properties": {
            "project_id": {"type": "string"}, "question": {"type": "string"},
            "answer": {"type": "object"}, "knowledge_as_of": {"type": "string"},
        }, "required": ["project_id", "question", "answer"],
    },
    "evidence.pages.list": {
        "type": "object", "properties": {"project_id": {"type": "string"}, "limit": {"type": "integer"}},
    },
    "evidence.page.read": {
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "description": "Page id to read (original + active transcript)."},
            "view": {"type": "string", "enum": ["agent", "full"], "default": "agent",
                     "description": "Default agent view keeps every transcript block and source revision with quality summaries. Use full only when geometry, extraction metadata or individual risk fragments are needed."},
        },
        "required": ["page_id"],
    },
    "evidence.provider.list": {"type": "object", "properties": {}},
    "memory.proposal.create": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project scope; must be within the run token binding."},
            "page_id": {"type": "string"},
            "transcript_revision": {
                "type": "integer",
                "description": "Active transcript revision of the proposal's anchor page. Each evidence_ref names its own page and current transcript revision; supporting pages must be in the same project, and the proposal must cite the anchor at least once.",
            },
            "profile_id": {"type": "string"},
            "profile_version": {"type": "string"},
            "acceptance_mode": {"type": "string", "enum": ["default", "review"],
                                "description": "chemistry_notebook defaults to accepted, correctable knowledge; review explicitly requests the legacy approval flow. Knowledge adoption never authorizes inventory or bookings."},
            "items": {
                "type": "array",
                "description": (
                    "Structured proposal items; every field value must be verbatim "
                    "from the cited transcript block. Omit to let the offline "
                    "proposer build items from the active transcript."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "item_key": {"type": "string", "description": "Stable key, unique within this proposal."},
                        "event_kind": {"type": "string", "enum": ["operation", "plan", "observation", "computed"]},
                        "raw_text": {"type": "string", "description": "Verbatim text of the cited block."},
                        "needs_confirmation": {"type": "boolean", "description": "Optional legacy review flag. Default notebook knowledge is adopted without a prior human confirmation; explicit review mode retains confirmations."},
                        "occurred_at": {"type": ["string", "null"], "description": "Occurrence date/time explicitly stated by the source. Preserve source precision; never supply an invented day, time or midnight."},
                        "unknown": {"type": "boolean", "description": "True when the source explicitly leaves a value unknown; never invent a value."},
                        "unknowns": {"type": "array", "items": {"type": "string"}, "description": "Optional notes on unknown values, retained with the candidate; raw_text and uncertain fields remain the source of record."},
                        "fields": {
                            "type": "object",
                            "required": ["subject_sample_labels"],
                            "additionalProperties": True,
                            "description": (
                                "Structured field values, each verbatim from the cited block: "
                                "amount = {normalized, unit} for quantities, unit = {normalized}, "
                                "subject_sample_labels is the authoritative array of fact-subject label strings, required even when empty. "
                                "Contextual mentions do not belong in that array. Legacy sample_label/related_sample_labels do not override it. "
                                "Other structured JSON fields are allowed for quantities, conditions and phases named by the model. "
                                "Omit unknown values; store their uncertainty without blocking other knowledge or inventing facts."
                            ),
                            "properties": {
                                "subject_sample_labels": {
                                    "type": "array", "items": {"type": "string"}, "uniqueItems": True,
                                    "description": "All and only actual subjects of this item's values/state. Use one label for an individual measurement, multiple for a truly shared fact, [] if unresolved or source metadata. Mentioned/comparison samples go in mentioned_sample_labels instead."
                                },
                                "amount": {
                                    "type": "object",
                                    "properties": {
                                        "normalized": {"type": "number", "description": "Quantity as a number, verbatim from the text."},
                                        "unit": {"type": "string", "description": "Unit exactly as written (g, mL, mg ...)."},
                                    },
                                    "required": ["normalized", "unit"],
                                },
                                "unit": {
                                    "type": "object",
                                    "properties": {"normalized": {"type": "string"}},
                                },
                                "sample_label": {
                                    "type": "object",
                                    "properties": {
                                        "normalized": {"type": "string", "description": "Sample label as written, e.g. SAMPLE-002."},
                                        "raw": {"type": "string"},
                                        "uncertain": {"type": "boolean", "description": "True when the label is ambiguous in the text."},
                                    },
                                    "required": ["normalized"],
                                },
                                "related_sample_labels": {"type": "array", "description": "Legacy compatibility labels; they do not override subject_sample_labels. Use mentioned_sample_labels for contextual mentions.",
                                    "items": {"type": "object", "properties": {"normalized": {"type": "string"}, "raw": {"type": "string"}, "uncertain": {"type": "boolean"}}, "required": ["normalized"]}},
                                "sample_labels": {"type": "array", "items": {"type": "string"}, "description": "Compatibility form for a set of explicit, unambiguous sample labels."},
                            },
                        },
                        "sample_match": {
                            "type": "object",
                            "description": (
                                "Sample resolution. action=link with candidates when "
                                "memory.sample.search found exactly one existing sample; "
                                "action=clarify for ambiguous identity; an explicit new label may use propose_creation. "
                                "Default knowledge policy creates or uniquely links explicit labels without a prior approval. "
                                "Omit when the item has no sample."
                            ),
                            "properties": {
                                "action": {"type": "string", "enum": ["link", "clarify", "propose_creation", "none"]},
                                "candidates": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "sample_id": {"type": "string"},
                                            "label": {"type": "string"},
                                        },
                                    },
                                },
                            },
                            "required": ["action"],
                        },
                        "evidence_refs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "page_id": {"type": "string"},
                                    "transcript_revision": {"type": "integer"},
                                    "block_id": {"type": "string"},
                                },
                                "required": ["page_id", "transcript_revision", "block_id"],
                            },
                        },
                    },
                    "required": ["item_key", "event_kind", "raw_text", "fields", "evidence_refs"],
                },
            },
        },
        "required": ["project_id", "page_id", "transcript_revision", "profile_id"],
    },
    "memory.proposal.read": {
        "type": "object",
        "properties": {"proposal_id": {"type": "string"}},
        "required": ["proposal_id"],
    },
    "memory.proposal.apply": {
        "type": "object",
        "properties": {
            "proposal_id": {"type": "string"},
            "item_keys": {"type": "array", "description": "Confirmed item keys to apply."},
        },
        "required": ["proposal_id"],
    },
    "memory.proposal.confirm_items": {
        "type": "object",
        "properties": {
            "proposal_id": {"type": "string"},
            "item_keys": {"type": "array"},
            "decision": {"type": "string", "enum": ["confirmed", "declined"]},
        },
        "required": ["proposal_id", "item_keys", "decision"],
    },
    "memory.sample.search": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "label": {"type": "string"},
        },
        "required": ["project_id", "label"],
    },
    "memory.event.read": {
        "type": "object",
        "properties": {
            "event_id": {"type": "string"},
            "knowledge_as_of": {"type": "string"},
        },
        "required": ["event_id"],
    },
    "memory.clarification.open": {
        "type": "object",
        "properties": {
            "proposal_id": {"type": "string"},
            "questions": {"type": "array", "minItems": 1, "items": {
                "type": "object", "properties": {
                    "question_id": {"type": "string", "description": "Unique identifier within this clarification."},
                    "prompt": {"type": "string", "description": "Specific question for the researcher."},
                    "item_key": {"type": "string", "description": "Candidate item this question concerns."},
                    "field": {"type": "string", "description": "Candidate field requiring clarification, when applicable."},
                }, "required": ["question_id", "prompt"],
            }},
            "task_id": {"type": "string", "description": "This agent's own context.task_id; never use a parent or sibling task ID."},
            "expected_task_revision": {"type": "integer", "description": "Read this agent's task using task.read and pass its current revision."},
        },
        "required": ["proposal_id", "questions", "task_id", "expected_task_revision"],
    },
    "query.events.search": {
        "type": "object",
        "properties": {
            "project_ids": {"type": "array", "items": {"type": "string"}},
            "sample_label": {"type": "string", "description": "Sample label as written in the notebook (e.g. SAMPLE-001)."},
            "run_id": {"type": "string"},
            "date_from": {"type": "string", "description": "ISO date (inclusive)."},
            "date_to": {"type": "string", "description": "ISO date (inclusive)."},
            "event_kind": {"type": "string", "enum": ["operation", "plan", "observation", "computed"]},
            "text": {"type": "string", "description": "Free-text filter over event content."},
            "knowledge_as_of": {"type": "string"},
            "limit": {"type": "integer"},
        },
    },
    "query.sample.timeline": {
        "type": "object",
        "properties": {
            "sample_id": {"type": "string"},
            "knowledge_as_of": {"type": "string"},
        },
        "required": ["sample_id"],
    },
    "query.answer.build": {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "project_ids": {"type": "array", "items": {"type": "string"}},
            "knowledge_as_of": {"type": "string"},
        },
        "required": ["question"],
    },
    "query.answer.render": {
        "type": "object",
        "properties": {"answer": {"type": "object"}, "mode": {"type": "string"}},
        "required": ["answer"],
    },
    "query.report.read": {
        "type": "object",
        "properties": {"report_id": {"type": "string"}},
        "required": ["report_id"],
    },
    "inventory.query.list": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["chemicals", "transactions"],
                "description": "List chemicals with stock, or recent transactions.",
            },
            "chemical_query": {"type": "string"},
            "project_id": {"type": "string"},
        },
        "required": ["kind"],
    },
    "task.read": {
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
    },
    # S09 (§15): planner / booking role tools. The model generates arguments
    # strictly from these schemas (S08 lesson) — undeclared params never reach
    # the gateway, so every planner capability MUST be declared here.
    "resource.list": {
        "type": "object",
        "properties": {
            "project_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Project ids to scope the listing to; omit for all visible projects.",
            },
            "kind": {"type": "string", "enum": ["instrument", "room", "equipment", "service"]},
        },
    },
    "resource.availability": {
        "type": "object",
        "properties": {
            "resource_id": {"type": "string", "description": "Resource id from resource.list."},
            "window_start": {
                "type": "string",
                "description": "Search-window start as ISO-8601 with timezone offset, e.g. 2026-10-14T09:00:00+08:00.",
            },
            "window_end": {
                "type": "string",
                "description": "Search-window end as ISO-8601 with the same offset.",
            },
            "duration_minutes": {
                "type": "integer",
                "description": "Slot length in minutes (default 60).",
            },
        },
        "required": ["resource_id", "window_start", "window_end"],
    },
    "resource.booking.propose": {
        "type": "object",
        "properties": {
            "resource_id": {"type": "string"},
            "project_id": {
                "type": "string",
                "description": "Project the booking belongs to; must be within the run token binding.",
            },
            "slot_start": {
                "type": "string",
                "description": "Slot start as ISO-8601 with timezone offset.",
            },
            "slot_end": {"type": "string", "description": "Slot end, after slot_start."},
            "plan_summary": {
                "type": "string",
                "description": "One-paragraph plan of what the booked slot is for.",
            },
            "purpose_note": {"type": "string"},
        },
        "required": ["resource_id", "project_id", "slot_start", "slot_end"],
    },
    "resource.booking.status": {
        "type": "object",
        "properties": {
            "project_ids": {"type": "array", "items": {"type": "string"}},
            "resource_id": {"type": "string"},
            "status": {"type": "string", "enum": ["planned", "confirmed", "cancelled", "completed"]},
        },
    },
    "research.milestone.status": {
        "type": "object",
        "properties": {
            "project_ids": {"type": "array", "items": {"type": "string"}},
            "milestone_id": {"type": "string"},
        },
    },
    "external.result.status": {
        "type": "object",
        "properties": {
            "project_ids": {"type": "array", "items": {"type": "string"}},
            "result_id": {"type": "string"},
            "source_kind": {"type": "string", "enum": ["computation", "literature", "external_lab"]},
        },
    },
}

# Read contracts are shared with the public HTTP and MCP surfaces. Keep the
# legacy write declarations above until their own contracts are consolidated.
AGENT_TOOL_PARAM_SCHEMAS.update(READ_INPUT_SCHEMAS)

# Capabilities whose params include a project_id that must sit inside the
# token's project binding (defense in depth; Core re-checks membership).
_PROJECT_SCOPED_TOOLS = {
    "memory.proposal.create",
    "memory.sample.search",
    "inventory.query.list",
}


def describe_agent_tools(
    conn, token_row: dict[str, Any]
) -> list[dict[str, Any]]:
    """OpenAI-style tool specs for exactly the manifest's allowed capabilities."""
    allowed = agents.allowed_capabilities(conn, token_row) - _HUMAN_ONLY_TOOLS
    specs = []
    for capability_id in sorted(allowed):
        spec = capabilities.CAPABILITY_REGISTRY.get(capability_id)
        if spec is None:
            continue
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": capability_id,
                    "mutates": spec.mutates,
                    "description": spec.summary
                    + (" [mutating]" if spec.mutates else " [read-only]"),
                    "parameters": AGENT_TOOL_PARAM_SCHEMAS.get(
                        capability_id,
                        {"type": "object", "properties": {}},
                    ),
                },
            }
        )
    return specs


def _agent_page_view(envelope: dict[str, Any]) -> dict[str, Any]:
    """Serialize already-authorized evidence without interpreting its content.

    The underlying capability and operation cache stay complete. No block is
    filtered, abbreviated or filled; source ids and quality flags are retained.
    """
    result = envelope.get("result")
    if isinstance(result, dict) and isinstance(result.get("pages"), list):
        return {**envelope, "result": {**result, "pages": [
            _agent_page_view({"result": {"page": page}})["result"]["page"]
            if isinstance(page, dict) else page for page in result["pages"]
        ]}}
    page = result.get("page") if isinstance(result, dict) else None
    if not isinstance(page, dict):
        return envelope
    if page.get("view") == "agent":
        return envelope
    compact = {key: page[key] for key in (
        "page_id", "document_id", "project_id", "evidence_id", "notebook_code",
        "page_order", "paper_page_number", "mime", "status", "prev_page_id",
        "next_page_id", "active_revision", "active_run_id",
    ) if key in page}
    compact["view"] = "agent"
    transcript = page.get("transcript")
    if not isinstance(transcript, dict):
        # Preserve an explicitly absent transcript rather than inventing one.
        if "transcript" in page:
            compact["transcript"] = transcript
        return {**envelope, "result": {**result, "page": compact}}
    slim = {key: transcript[key] for key in (
        "page_id", "active_revision", "active_run_id", "provider", "status",
    ) if key in transcript}
    slim["source_ref"] = {key: value for key, value in (
        ("page_id", page.get("page_id")),
        ("transcript_revision", transcript.get("active_revision")),
    ) if value is not None}
    blocks = transcript.get("blocks")
    flags = Counter()
    positions: dict[tuple[str | None, str | None], list[Any]] = {}
    if isinstance(blocks, list):
        slim["blocks"] = []
        for block in blocks:
            if not isinstance(block, dict):
                # Do not silently discard malformed source data.
                slim["blocks"].append(block)
                continue
            slim["blocks"].append({key: block[key] for key in (
                "block_id", "text", "reading_order", "block_kind", "quality_flags",
            ) if key in block})
            flags.update(flag for flag in (block.get("quality_flags") or []) if isinstance(flag, str))
            positioning = block.get("positioning")
            if isinstance(positioning, dict) and positioning.get("mode") not in ("direct", "mapped"):
                group = (positioning.get("mode"), positioning.get("reason"))
                positions.setdefault(group, []).append(block.get("block_id"))
    elif "blocks" in transcript:
        slim["blocks"] = blocks
    fragments = transcript.get("risk_fragments")
    kinds = Counter()
    statuses = Counter()
    if isinstance(fragments, list):
        for fragment in fragments:
            if isinstance(fragment, dict):
                if isinstance(fragment.get("kind"), str):
                    kinds[fragment["kind"]] += 1
                if isinstance(fragment.get("status"), str):
                    statuses[fragment["status"]] += 1
    slim["quality_summary"] = {
        "block_count": len(blocks) if isinstance(blocks, list) else None,
        "flag_counts": dict(flags),
        "positioning_issues": [{"mode": mode, "reason": reason, "block_ids": ids}
                               for (mode, reason), ids in positions.items()],
        "risk_fragment_count": len(fragments) if isinstance(fragments, list) else None,
        "risk_kind_counts": dict(kinds), "risk_status_counts": dict(statuses),
        "details_view": "full",
    }
    compact["transcript"] = slim
    return {**envelope, "result": {**result, "page": compact}}


def invoke_agent_tool(
    db_path: str,
    conn,
    token_row: dict[str, Any],
    *,
    capability_id: str,
    params: dict[str, Any] | None,
    operation_id: str | None = None,
    services: ServiceSettings | None = None,
) -> dict[str, Any]:
    """One agent tool call, fully re-authorized by Core."""
    allowed = agents.allowed_capabilities(conn, token_row)
    if capability_id in _HUMAN_ONLY_TOOLS:
        raise agents.AgentHostError("human_approval_required", "This action requires the human interface.", http_status=403)
    if capability_id not in allowed:
        raise agents.AgentHostError(
            "forbidden_capability",
            f"Capability {capability_id!r} is not in agent "
            f"{token_row['agent_id']}@{token_row['manifest_version']} manifest whitelist.",
            http_status=403,
        )
    spec = capabilities.CAPABILITY_REGISTRY[capability_id]
    params = dict(params or {})
    page_view = "agent"
    if capability_id == "evidence.page.read":
        page_view = params.get("view", "agent")
        if page_view not in ("agent", "full"):
            raise agents.AgentHostError("schema_violation", "view must be agent or full.")
    if capability_id in ("memory.proposal.create", "memory.clarification.open", "task.update"):
        bound_task_id = token_row["task_id"]
        if params.get("task_id", bound_task_id) != bound_task_id:
            raise agents.AgentHostError("forbidden_scope", f"Task mutations are bound to this run's task {bound_task_id!r}; parent and sibling tasks cannot be changed.", http_status=403)
    if capability_id == "memory.proposal.create":
        # Knowledge adoption is a Core policy, not a human signature. Legacy
        # review profiles still cannot opt out of their confirmation gate.
        params["auto_confirm_safe"] = False
        params["task_id"] = token_row["task_id"]
        review_mode = (params.get("profile_id") or "chemistry_notebook") != "chemistry_notebook" or params.get("acceptance_mode") == "review"
        if review_mode and isinstance(params.get("items"), list):
            params["items"] = [
                {**item, "needs_confirmation": True} if isinstance(item, dict) else item
                for item in params["items"]
            ]
    token_projects = set(json.loads(token_row["project_ids_json"]))
    # Generic token-scope gate for every tool, not just three hard-coded
    # capabilities (§R01/F03). Core still re-checks object ownership.
    requested_project = params.get("project_id")
    if requested_project is not None and requested_project not in token_projects:
        raise agents.AgentHostError(
            "forbidden_scope",
            f"Project {requested_project!r} is outside the run token project binding.",
            http_status=403,
        )
    requested_projects = params.get("project_ids")
    if requested_projects is not None:
        if not isinstance(requested_projects, list):
            raise agents.AgentHostError(
                "schema_violation", "project_ids must be an array.", http_status=400
            )
        outside = sorted(set(requested_projects) - token_projects)
        if outside:
            raise agents.AgentHostError(
                "forbidden_scope",
                f"Projects outside the run token binding: {', '.join(outside)}.",
                http_status=403,
            )
    context = agents.principal_from_token(conn, token_row)
    if spec.mutates:
        # F06: a dispatch token is not an execution lease. Mutating tools
        # require the run to be claimed and the token to carry the SAME
        # current epoch; an old worker's stale epoch is rejected by Core.
        run = conn.execute(
            "SELECT status, claim_epoch FROM agent_runs WHERE run_id = ?",
            (token_row["run_id"],),
        ).fetchone()
        if run is None or run["status"] != "running":
            raise agents.AgentHostError(
                "run_not_claimed",
                "Mutating tools require a claimed (running) run.",
                http_status=409,
            )
        if int(token_row.get("claim_epoch") or 0) != int(run["claim_epoch"]):
            raise agents.AgentHostError(
                "stale_worker",
                "Run token epoch does not match the current claim; this worker is stale.",
                http_status=409,
            )
    op_id = operation_id or f"agtop_{uuid.uuid4().hex[:16]}"
    params = dict(params or {})
    # Handlers that key idempotency on params.operation_id (S04 proposal
    # create/apply) receive the gateway operation id; the agent host sends it
    # in the X-Operation-Id header, never inside params.
    params.setdefault("operation_id", op_id)
    if spec.mutates:
        lock = run_write_lock(conn, token_row["run_id"])
        with lock:
            _validate_write_lease(conn, token_row)
            agents.consume_tool_budget(conn, token_row)
            conn.commit()  # budget counts even if the capability call fails
            try:
                return capabilities.invoke_capability(
                    db_path,
                    context,
                    capability_id,
                    params,
                    namespace="agent",
                    operation_id=op_id,
                    services=services,
                )
            except auth.AuthError as exc:
                raise agents.AgentHostError(
                    exc.code, exc.message, http_status=exc.http_status
                ) from exc
            except capabilities.CapabilityError as exc:
                if exc.code in ("unauthorized", "forbidden_scope"):
                    raise agents.AgentHostError(
                        exc.code, exc.message, http_status=exc.http_status
                    ) from exc
                raise
    agents.consume_tool_budget(conn, token_row)
    conn.commit()
    try:
        envelope = capabilities.invoke_capability(
            db_path,
            context,
            capability_id,
            params,
            namespace="agent",
            operation_id=op_id,
            services=services,
        )
        return _agent_page_view(envelope) if capability_id == "evidence.page.read" and page_view == "agent" else envelope
    except auth.AuthError as exc:
        raise agents.AgentHostError(
            exc.code, exc.message, http_status=exc.http_status
        ) from exc
    except capabilities.CapabilityError as exc:
        if exc.code in ("unauthorized", "forbidden_scope"):
            raise agents.AgentHostError(
                exc.code, exc.message, http_status=exc.http_status
            ) from exc
        raise
