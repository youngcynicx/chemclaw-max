"""S02: the controlled capability entry shared by HTTP/Web/MCP clients.

Every invocation goes through :func:`invoke_capability`:

1. The subject is an authenticated ``PrincipalContext`` derived from a
   credential 鈥?never a self-reported field (plan 搂8.4).
2. The capability must exist, be implemented, and be allowed for one of the
   principal's roles; project-scoped parameters must match a membership.
3. Idempotency lives in ``operation_results`` keyed by
   ``(namespace, operation_id)`` with a request hash: same key + same request
   replays the stored envelope; same key + different request is a conflict.
4. ``expected_revision`` (optimistic concurrency on the target stock row) is
   checked and bumped inside the SAME transaction as the business apply and
   its audit rows.
5. Results are wrapped in a versioned envelope (capability_envelope_v1) with
   stable error codes and explicit ``action_status``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import (
    answers,
    auth,
    experiments,
    external_results,
    evidence,
    ingest,
    inventory,
    ocr_port,
    proposal as proposal_mod,
    query,
    research,
    resources,
    tasks,
    transcribe,
)
from . import channels
from .db import connect, connect_read_only
from .inventory import (
    list_inventory_subtasks,
    parse_inventory_event,
    process_inventory_message_conn,
    resolve_inventory_event,
)
from .ocr_port import TranscriberError, decode_data_url
from .service_config import ServiceSettings, default_service_settings
from .validation import (
    SchemaViolation,
    load_named_schema,
    validate_payload_against_schema,
)


CAPABILITY_VERSION = "capabilities-v1.0"
ENVELOPE_SCHEMA_NAME = "capability_envelope_v1.json"

FORBIDDEN_V2_FIELDS = ("confirmed_by", "user_id", "principal_id")

ACTION_STATUS_FOR_EVENT_STATUS = {
    "approved": "proposed",
    "pending_confirmation": "clarification_needed",
    "rejected": "rejected",
    "applied": "applied",
    "superseded": "proposed",
    "proposed": "proposed",
}

# Role -> capability matrix. Only implemented capabilities are listed; the
# published list is the intersection of this matrix and the registry.
_MEMORY_MEMBER_CAPS = {
    "memory.proposal.create",
    "memory.proposal.read",
    "memory.proposal.confirm_items",
    "memory.proposal.item.update",
    "memory.proposal.apply",
    "memory.sample.search",
    "memory.clarification.open",
    "memory.clarification.answer",
    "memory.event.correct",
    "memory.event.read",
    "memory.index.rebuild",
}

# S05 read side: query capabilities for humans AND external read-only agents.
_QUERY_VIEWER_CAPS = {
    "query.events.search",
    "query.samples.list",
    "query.sample.timeline",
    "query.sample.context",
    "query.answer.render",
    "query.report.read",
}
_QUERY_MEMBER_CAPS = {
    "query.answer.build",
    "query.report.save",
    *_QUERY_VIEWER_CAPS,
}

# S06 agent host: admin (owner) capabilities for the Pi host lifecycle.
_ASSISTANT_JOB_CAPS = {f"assistant.job.{name}" for name in ("submit", "list", "read", "claim", "heartbeat", "complete", "cancel", "preprocess")}

_AGENT_HOST_CAPS = {
    "agent.registry.load",
    "agent.registry.list",
    "agent.registry.set_enabled",
    "run.claim",
    "run.heartbeat",
    "run.complete",
    "run.recover",
    "run.list",
    "delivery.process",
}

ROLE_CAPABILITIES: dict[str, set[str]] = {
    "viewer": {
        "inventory.query.list",
        "inventory.subtask.status",
        "task.read",
        "evidence.page.read",
        "evidence.page.original",
        "evidence.page.preview",
        "evidence.pages.list",
        "evidence.provider.list",
        "memory.proposal.read",
        "memory.event.read",
        *_QUERY_VIEWER_CAPS,
        "resource.list",
        "resource.booking.status",
        "research.milestone.status",
        "external.result.status",
    },
    "member": {
        "inventory.query.list",
        "inventory.subtask.status",
        "inventory.subtask.execute",
        "inventory.subtask.resolve",
        "inventory.adjustment.propose",
        "inventory.adjustment.apply",
        "inventory.approval.grant",
        "inventory.message.process",
        "inventory.event.resolve",
        "assistant.task.process",
        "assistant.agent.start",
        "assistant.agent.finish",
        *_ASSISTANT_JOB_CAPS,
        "task.create",
        "task.read",
        "task.update",
        "task.interaction.answer",
        "task.dispatch",
        "evidence.document.create",
        "evidence.page.ingest",
        "evidence.page.read",
        "evidence.page.original",
        "evidence.page.preview",
        "evidence.pages.list",
        "evidence.transcribe.run",
        "evidence.transcript.correct",
        "evidence.transcript.import",
        "evidence.transcription.retry",
        "evidence.provider.list",
        *_MEMORY_MEMBER_CAPS,
        *_QUERY_MEMBER_CAPS,
        "resource.list",
        "resource.availability",
        "resource.booking.propose",
        "resource.booking.confirm",
        "resource.booking.cancel",
        "resource.booking.status",
        "research.milestone.create",
        "research.milestone.criterion.attach",
        "research.milestone.criterion.check",
        "research.milestone.status",
        "external.result.submit",
        "external.result.status",
    },
    "owner": {
        "inventory.query.list",
        "inventory.subtask.status",
        "inventory.subtask.execute",
        "inventory.subtask.resolve",
        "inventory.inbound.process",
        "inventory.adjustment.propose",
        "inventory.adjustment.apply",
        "inventory.approval.grant",
        "inventory.message.process",
        "inventory.event.resolve",
        "assistant.task.process",
        "assistant.agent.start",
        "assistant.agent.finish",
        *_ASSISTANT_JOB_CAPS,
        "task.create",
        "task.read",
        "task.update",
        "task.interaction.answer",
        "task.dispatch",
        *_AGENT_HOST_CAPS,
        "evidence.document.create",
        "evidence.page.ingest",
        "evidence.page.read",
        "evidence.page.original",
        "evidence.page.preview",
        "evidence.pages.list",
        "evidence.transcribe.run",
        "evidence.transcript.correct",
        "evidence.transcript.import",
        "evidence.transcription.retry",
        "evidence.provider.list",
        *_MEMORY_MEMBER_CAPS,
        *_QUERY_MEMBER_CAPS,
        "resource.register",
        "resource.list",
        "resource.availability",
        "resource.booking.propose",
        "resource.booking.confirm",
        "resource.booking.cancel",
        "resource.booking.status",
        "research.milestone.create",
        "research.milestone.criterion.attach",
        "research.milestone.criterion.check",
        "research.milestone.status",
        "research.milestone.decide",
        "external.result.submit",
        "external.result.status",
        # S10 messaging channels (plan §16): channel administration is
        # owner-only; the webhook ingest is exposed for standard-path tests.
        "channel.account.register",
        "channel.identity.bind",
        "channel.identity.revoke",
        "channel.messages.read",
        "channel.webhook.ingest",
    },
}


class CapabilityError(Exception):
    """Stable-code error mapped into the envelope and HTTP status."""

    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True)
class CapabilitySpec:
    capability_id: str
    summary: str
    mutates: bool
    min_role: str


CAPABILITY_REGISTRY: dict[str, CapabilitySpec] = {
    spec.capability_id: spec
    for spec in [
        CapabilitySpec("inventory.query.list", "Scope-filtered stock and transaction reads.", False, "viewer"),
        CapabilitySpec("inventory.message.process", "Parse/audit/persist/apply one inventory message.", True, "member"),
        CapabilitySpec("inventory.event.resolve", "Confirm, correct, or reject a pending proposal.", True, "member"),
        CapabilitySpec("task.create", "Create a durable task.", True, "member"),
        CapabilitySpec("task.read", "Read task state.", False, "viewer"),
        CapabilitySpec("task.update", "Transition task status.", True, "member"),
        CapabilitySpec("task.interaction.answer", "Answer a pending interaction.", True, "member"),
        CapabilitySpec("evidence.document.create", "Create notebook/document identity for evidence.", True, "member"),
        CapabilitySpec("evidence.page.ingest", "Controlled page upload: store original, register identity.", True, "member"),
        CapabilitySpec("evidence.page.read", "Scope-checked page metadata + active transcript.", False, "viewer"),
        CapabilitySpec("evidence.page.original", "Scope-checked original bytes for one page.", False, "viewer"),
        CapabilitySpec("evidence.page.preview", "Scope-checked PNG preview bytes for one page.", False, "viewer"),
        CapabilitySpec("evidence.pages.list", "Discover project-scoped sources independently of samples; paginated source/knowledge linkage state is not semantic completeness.", False, "viewer"),
        CapabilitySpec("evidence.transcribe.run", "Run one transcription pass over a page.", True, "member"),
        CapabilitySpec("evidence.transcript.import", "Import an external OCR/vision transcript as a new run.", True, "member"),
        CapabilitySpec("evidence.transcript.correct", "Human correction: append a new transcript revision.", True, "member"),
        CapabilitySpec("evidence.transcription.retry", "Retry a failed/needs_human transcription run.", True, "member"),
        CapabilitySpec("evidence.provider.list", "List transcriber providers and cloud-processing scope.", False, "viewer"),
        CapabilitySpec("memory.proposal.create", "Record cited notebook knowledge by default; explicit review remains available.", True, "member"),
        CapabilitySpec("memory.proposal.read", "Read a proposal with confirmations and clarifications.", False, "viewer"),
        CapabilitySpec("memory.proposal.confirm_items", "Human confirm/decline of critical proposal items.", True, "member"),
        CapabilitySpec("memory.proposal.item.update", "Human edit of a candidate item before apply (new proposal revision).", True, "member"),
        CapabilitySpec("memory.proposal.apply", "Apply a proposal item-by-item (partial success allowed).", True, "member"),
        CapabilitySpec("memory.sample.search", "Project-scoped sample candidate search.", False, "member"),
        CapabilitySpec("memory.clarification.open", "Open a persisted clarification (waiting_input).", True, "member"),
        CapabilitySpec("memory.clarification.answer", "Answer a clarification (idempotent, revision-bound).", True, "member"),
        CapabilitySpec("memory.event.correct", "Append an immutable event correction revision.", True, "member"),
        CapabilitySpec("memory.event.read", "Read current-effective event or full history.", False, "viewer"),
        CapabilitySpec("memory.index.rebuild", "Rebuild a stale derived artifact.", True, "member"),
        CapabilitySpec("query.events.search", "Scope-checked structured + full-text event search.", False, "viewer"),
        CapabilitySpec("query.samples.list", "List project-scoped samples with event counts.", False, "viewer"),
        CapabilitySpec("query.sample.timeline", "Scope-checked sample timeline (as-of aware).", False, "viewer"),
        CapabilitySpec("query.sample.context", "One versioned, cited context for an explicitly linked sample; no model invocation.", False, "viewer"),
        CapabilitySpec("query.answer.build", "Build AnswerClaim JSON over the structured snapshot.", False, "member"),
        CapabilitySpec("query.answer.render", "Deterministic cited answer for humans and machines.", False, "viewer"),
        CapabilitySpec("query.report.save", "Persist a report with source revisions.", True, "member"),
        CapabilitySpec("query.report.read", "Read a report with staleness and scope re-check.", False, "viewer"),
        CapabilitySpec("task.dispatch", "Route an intent to its agent: task+run+one-time run token.", True, "member"),
        CapabilitySpec("assistant.task.process", "Classify a natural-language task and route it to the owning agent domain.", True, "member"),
        *[CapabilitySpec(f"assistant.job.{name}", f"Durable assistant job {name}.", name not in ("read", "list"), "member") for name in ("submit", "list", "read", "claim", "heartbeat", "complete", "cancel", "preprocess")],
        CapabilitySpec("assistant.agent.start", "Start a scoped model-directed agent session.", True, "member"),
        CapabilitySpec("assistant.agent.finish", "Persist the initiating principal's agent session result.", True, "member"),
        CapabilitySpec("agent.registry.load", "Load agent manifests from the admin config directory.", True, "owner"),
        CapabilitySpec("agent.registry.list", "List loaded manifests and enabled states.", False, "owner"),
        CapabilitySpec("agent.registry.set_enabled", "Enable or disable a loaded manifest version.", True, "owner"),
        CapabilitySpec("run.claim", "Worker claims a queued run (lease + fencing epoch).", True, "owner"),
        CapabilitySpec("run.heartbeat", "Extend a held run lease (epoch-conditional).", False, "owner"),
        CapabilitySpec("run.complete", "Finish a run at the caller's claim epoch.", True, "owner"),
        CapabilitySpec("run.recover", "Re-queue runs whose leases expired.", True, "owner"),
        CapabilitySpec("run.list", "List claimable runs for an agent.", False, "owner"),
        CapabilitySpec("delivery.process", "One outbox delivery pass through a local adapter.", True, "owner"),
        # S07 cross-domain closure (plan §13).
        CapabilitySpec("inventory.subtask.status", "List cross-domain inventory subtasks.", False, "viewer"),
        CapabilitySpec("inventory.subtask.execute", "Apply one subtask with stock-revision fencing.", True, "member"),
        CapabilitySpec("inventory.subtask.resolve", "Resolve clarification/duplicate subtasks.", True, "member"),
        CapabilitySpec("inventory.inbound.process", "Ingest sent inventory_proposal outbox messages.", True, "owner"),
        CapabilitySpec("inventory.adjustment.propose", "Link a usage correction to a stock adjustment proposal.", True, "member"),
        CapabilitySpec("inventory.adjustment.apply", "Confirm or reject an adjustment proposal.", True, "member"),
        CapabilitySpec("inventory.approval.grant", "Record a human approval grant for an exact proposal revision.", True, "member"),
        # S09 extension domains (plan §15).
        CapabilitySpec("resource.register", "Register a schedulable resource.", True, "owner"),
        CapabilitySpec("resource.list", "List visible resources.", False, "viewer"),
        CapabilitySpec("resource.availability", "Candidate free slots for a resource.", False, "member"),
        CapabilitySpec("resource.booking.propose", "Propose a booking plan (never blocks a slot).", True, "member"),
        CapabilitySpec("resource.booking.confirm", "Formal booking with in-transaction conflict re-check.", True, "member"),
        CapabilitySpec("resource.booking.cancel", "Cancel a booking.", True, "member"),
        CapabilitySpec("resource.booking.status", "List bookings by scope.", False, "viewer"),
        CapabilitySpec("research.milestone.create", "Create a research milestone (no progress percentage).", True, "member"),
        CapabilitySpec("research.milestone.criterion.attach", "Attach a criterion with locatable evidence refs.", True, "member"),
        CapabilitySpec("research.milestone.criterion.check", "Check a criterion by opening its evidence refs.", True, "member"),
        CapabilitySpec("research.milestone.status", "Milestone overview with criteria.", False, "viewer"),
        CapabilitySpec("research.milestone.decide", "Human researcher decision on a milestone.", True, "owner"),
        CapabilitySpec("external.result.submit", "Persist an external result with provenance.", True, "member"),
        CapabilitySpec("external.result.status", "Read external results by scope.", False, "viewer"),
        # S10 messaging channels (plan §16).
        CapabilitySpec("channel.account.register", "Register a messaging channel account (credential refs, never plaintext).", True, "owner"),
        CapabilitySpec("channel.identity.bind", "Explicit platform-author -> principal binding.", True, "owner"),
        CapabilitySpec("channel.identity.revoke", "Revoke a channel identity binding.", True, "owner"),
        CapabilitySpec("channel.messages.read", "Audit read over the raw channel ledger.", False, "owner"),
        CapabilitySpec("channel.webhook.ingest", "Ingest one platform event through the unified envelope.", True, "owner"),
    ]
}


def list_capabilities(context: auth.PrincipalContext) -> list[dict[str, Any]]:
    """Capabilities implemented AND available to this principal's roles."""
    allowed: set[str] = set()
    for role in context.memberships.values():
        allowed |= ROLE_CAPABILITIES.get(role, set())
    return [
        {
            "capability_id": spec.capability_id,
            "summary": spec.summary,
            "mutates": spec.mutates,
        }
        for spec in CAPABILITY_REGISTRY.values()
        if spec.capability_id in allowed
    ]


def invoke_capability(
    db_path: str,
    context: auth.PrincipalContext,
    capability_id: str,
    params: dict[str, Any],
    *,
    namespace: str = "default",
    operation_id: str,
    services: "ServiceSettings | None" = None,
) -> dict[str, Any]:
    spec = CAPABILITY_REGISTRY.get(capability_id)
    if spec is None:
        raise CapabilityError(
            "forbidden_capability",
            f"Capability {capability_id!r} is not implemented.",
            http_status=404,
        )
    _check_role(context, capability_id)
    _reject_forbidden_fields(params)
    _require_request_hash_input(params)
    from .read_contracts import validate_read_params
    try:
        validate_read_params(capability_id, params)
    except SchemaViolation as exc:
        raise CapabilityError("schema_violation", str(exc)) from exc

    # Jobs own atomic request/lease idempotency. Never cache execution bearer
    # tokens or stale read/heartbeat responses in operation_results.
    if capability_id.startswith("assistant.job."):
        from . import agent_jobs, agents
        try:
            result = agent_jobs.invoke(db_path, context, capability_id.rsplit(".", 1)[1], params, services=services)
        except (agents.AgentHostError, tasks.TaskError) as exc:
            raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
        envelope = {"action_status": "proposed", "project_id": params.get("project_id"), "result": result,
                    "object_refs": [], "evidence_refs": [], "unknowns": [], "warnings": [],
                    "schema_version": "capability_envelope_v1", "capability_id": capability_id,
                    "operation_id": operation_id, "namespace": namespace, "principal_id": context.principal_id}
        return _finalize_envelope(envelope)

    # F01: an operation id belongs to ONE initiating authority and ONE
    # immutable project context. ``namespace`` is a client-visible name, not
    # an authorization boundary; the storage key is derived server-side.
    storage_namespace = _operation_storage_namespace(context, namespace, params)
    request_hash = _request_hash(capability_id, params)
    # A query's operation id identifies a request, not a permanent snapshot.
    # Re-run reads (including lease heartbeats) under current authority even
    # when an older server persisted a result under this same operation id.
    if spec.mutates:
        replay = _load_operation_result(
            db_path, storage_namespace, operation_id, request_hash
        )
        if replay is not None:
            return _authorize_replay(db_path, context, replay, params)

    handler = _HANDLERS[capability_id]
    # Stable logical operation id for handlers that key business idempotency
    # on it (inventory message_logs, proposal create/apply). Excluded from
    # the request hash; never accepted from the client body.
    handler_params = dict(params)
    handler_params["_operation_id"] = operation_id
    if _takes_services(handler):
        envelope = handler(
            db_path, context, handler_params, services or default_service_settings()
        )
    else:
        envelope = handler(db_path, context, handler_params)
    envelope.update(
        {
            "schema_version": "capability_envelope_v1",
            "capability_id": capability_id,
            "operation_id": operation_id,
            "namespace": namespace,
            "principal_id": context.principal_id,
        }
    )
    envelope = _finalize_envelope(envelope)
    if spec.mutates:
        _store_operation_result(
            db_path,
            namespace=storage_namespace,
            operation_id=operation_id,
            context=context,
            capability_id=capability_id,
            request_hash=request_hash,
            envelope=envelope,
            entity_refs=envelope.get("object_refs") or [],
        )
    return envelope


# ---------------------------------------------------------------------------
# handlers


def _handle_inventory_query_list(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    kind = params.get("kind")
    if kind not in ("chemicals", "transactions"):
        raise CapabilityError(
            "schema_violation", 'params.kind must be "chemicals" or "transactions".'
        )
    project_id = params.get("project_id")
    if kind == "transactions" and project_id is not None:
        context.require_project(project_id, "viewer")
    if not context.has_any_membership():
        raise CapabilityError(
            "forbidden_scope",
            "Reads require at least one project membership.",
            http_status=403,
        )
    conn = connect(db_path)
    try:
        if kind == "chemicals":
            rows = conn.execute(
                """
                SELECT chemical_id, name, formula, location, amount, unit,
                       threshold, hazard_level, revision
                FROM chemicals ORDER BY name
                """
            ).fetchall()
            return {
                "action_status": "applied",
                "result": {"kind": "chemicals", "items": [dict(row) for row in rows]},
                "warnings": [],
                "unknowns": [],
            }
        params_tuple: tuple[Any, ...]
        if project_id is not None:
            sql = (
                "SELECT transaction_id, chemical_id, project_id, user_id, "
                "transaction_type, amount_delta, unit, confirmation_status, created_at "
                "FROM inventory_transactions WHERE project_id = ? ORDER BY rowid DESC LIMIT 200"
            )
            params_tuple = (project_id,)
        else:
            visible = auth.visible_project_ids(context)
            if not visible:
                return {
                    "action_status": "applied",
                    "result": {"kind": "transactions", "items": []},
                    "warnings": [],
                    "unknowns": [],
                }
            placeholders = ",".join("?" for _ in visible)
            sql = (
                "SELECT transaction_id, chemical_id, project_id, user_id, "
                "transaction_type, amount_delta, unit, confirmation_status, created_at "
                f"FROM inventory_transactions WHERE project_id IN ({placeholders}) "
                "ORDER BY rowid DESC LIMIT 200"
            )
            params_tuple = tuple(visible)
        rows = conn.execute(sql, params_tuple).fetchall()
        return {
            "action_status": "applied",
            "result": {"kind": "transactions", "items": [dict(row) for row in rows]},
            "warnings": [],
            "unknowns": [],
        }
    finally:
        conn.close()


def _handle_inventory_message_process(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    raw_text = params.get("raw_text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise CapabilityError("schema_violation", "params.raw_text must be a non-empty string.")
    project_id = params.get("project_id")
    if project_id is None:
        raise CapabilityError(
            "schema_violation",
            "params.project_id is required on the shared entry (scope is explicit).",
        )
    context.require_project(project_id, "member")
    expected_revision = _optional_int(params, "expected_revision")
    source_message_id = params.get("source_message_id")
    if source_message_id is not None and not isinstance(source_message_id, str):
        raise CapabilityError("schema_violation", "params.source_message_id must be a string.")
    if source_message_id is None:
        # Stable logical command identity: the same operation retried after a
        # crash (receipt not yet written) dedupes on message_logs; a genuinely
        # new real usage uses a new operation id and is applied again.
        source_message_id = (
            f"op:{context.principal_id}:{project_id}:{params.get('_operation_id')}"
        )

    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        warnings: list[str] = []
        user_id = _linked_user_id(conn, context, warnings)

        # Parse first (same transaction) so expected_revision can target the
        # identified chemical before any business write happens.
        chemicals = conn.execute(
            """
            SELECT chemical_id, name, formula, amount, unit, threshold,
                   hazard_level, max_auto_delta
            FROM chemicals ORDER BY length(name) DESC
            """
        ).fetchall()
        parsed = parse_inventory_event(raw_text, chemicals)
        if expected_revision is not None:
            _check_expected_revision(conn, parsed.item_id, expected_revision)

        result = process_inventory_message_conn(
            conn,
            raw_text,
            source="capability",
            source_message_id=source_message_id,
            user_id=user_id,
            project_id=project_id,
            apply_approved=True,
        )
        # S07 §13.3: the balance revision is bumped inside
        # apply_inventory_event in the same transaction as the delta, so the
        # returned value is the post-apply monotonic version (read-only here).
        revision_after = None
        if result.get("applied") and result["event"].get("item_id"):
            revision_after = _current_revision(conn, result["event"]["item_id"])
        object_refs = [
            {"kind": "event_proposal", "id": result["event_id"], "revision": None},
            {"kind": "message_log", "id": result["message_id"], "revision": None},
        ]
        if result.get("transaction_id"):
            object_refs.append({"kind": "transaction", "id": result["transaction_id"], "revision": None})
        if revision_after is not None and result["event"].get("item_id"):
            object_refs.append(
                {"kind": "chemical", "id": result["event"]["item_id"], "revision": revision_after}
            )
        unknowns = []
        audit = result.get("audit") or {}
        if result["status"] == "pending_confirmation":
            unknowns.append(
                f'requires confirmation: {audit.get("required_action") or "unspecified"}'
            )
        conn.commit()
        return {
            "action_status": (
                "applied" if result.get("applied") else ACTION_STATUS_FOR_EVENT_STATUS[result["status"]]
            ),
            "project_id": project_id,
            "object_refs": object_refs,
            "evidence_refs": [],
            "unknowns": unknowns,
            "warnings": warnings,
            "result": result,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_inventory_event_resolve(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    event_id = params.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise CapabilityError("schema_violation", "params.event_id must be a non-empty string.")
    approve = params.get("approve", True)
    if not isinstance(approve, bool):
        raise CapabilityError("schema_violation", "params.approve must be a boolean.")
    corrections = params.get("corrections", {})
    if not isinstance(corrections, dict):
        raise CapabilityError("schema_violation", "params.corrections must be an object.")
    # R03/F04: only a human may resolve/approve. Agents may prepare and
    # request approval, but never sign it on the human's behalf.
    try:
        auth.require_human(context, "inventory.event.resolve")
    except auth.AuthError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    if corrections.get("project_id") is not None or corrections.get("user_id") is not None:
        raise CapabilityError(
            "invalid_correction",
            "A confirmation correction may not reassign project_id or user_id.",
            http_status=403,
        )

    conn = connect(db_path)
    try:
        project_id = _event_project_id(conn, event_id)
        if project_id is not None:
            context.require_project(project_id, "member")
        elif not context.has_any_membership():
            raise CapabilityError(
                "forbidden_scope",
                "Confirmation requires at least one project membership.",
                http_status=403,
            )
    except auth.AuthError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    finally:
        conn.close()

    # The confirmer identity is the authenticated principal. The plan's
    # contract forbids passing it through the request body at all.
    result = resolve_inventory_event(
        db_path,
        event_id,
        approve=approve,
        corrections=corrections,
        confirmed_by=context.principal_id,
    )
    # S07 §13.3: apply_inventory_event bumps the revision in the SAME
    # transaction as the delta now; the old separate bump transaction (apply
    # and version in two commits) is gone along with its torn-update window.
    revision_after = None
    if result.get("applied") and result["event"].get("item_id"):
        conn = connect(db_path)
        try:
            revision_after = _current_revision(conn, result["event"]["item_id"])
        finally:
            conn.close()
    status = result["status"]
    return {
        "action_status": "applied" if result.get("applied") else ACTION_STATUS_FOR_EVENT_STATUS.get(status, "proposed"),
        "object_refs": [
            {"kind": "event_proposal", "id": result["event_id"], "revision": None},
            {"kind": "event_proposal", "id": result.get("supersedes_event_id") or event_id, "revision": None},
        ],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [] if not result.get("confirmation_replay") else ["confirmation_replay"],
        "result": result,
    }


def _handle_task_create(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    task_type = params.get("task_type")
    if not isinstance(task_type, str) or not task_type.strip():
        raise CapabilityError("schema_violation", "params.task_type must be a non-empty string.")
    project_id = params.get("project_id")
    if project_id is not None:
        if not isinstance(project_id, str):
            raise CapabilityError("schema_violation", "params.project_id must be a string.")
        context.require_project(project_id, "member")
    title = params.get("title")
    if title is not None and not isinstance(title, str):
        raise CapabilityError("schema_violation", "params.title must be a string.")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        task = tasks.create_task(
            conn,
            task_type=task_type,
            principal_id=context.principal_id,
            project_id=project_id,
            title=title,
        )
        conn.commit()
        return {
            "action_status": "applied",
            "project_id": project_id,
            "object_refs": [{"kind": "task", "id": task["task_id"], "revision": task["revision"]}],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": [],
            "result": tasks.task_to_contract(task),
        }
    except tasks.TaskError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_task_read(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    task_id = params.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise CapabilityError("schema_violation", "params.task_id must be a non-empty string.")
    conn = connect(db_path)
    try:
        task = tasks.get_task(conn, task_id)
        _check_task_visibility(context, task)
        return {
            "action_status": "applied",
            "object_refs": [{"kind": "task", "id": task_id, "revision": task["revision"]}],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": [],
            "result": tasks.task_to_contract(task),
        }
    finally:
        conn.close()


def _handle_task_update(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    task_id = params.get("task_id")
    new_status = params.get("status")
    if not isinstance(task_id, str) or not task_id.strip():
        raise CapabilityError("schema_violation", "params.task_id must be a non-empty string.")
    if not isinstance(new_status, str):
        raise CapabilityError("schema_violation", "params.status must be a string.")
    expected = _optional_int(params, "expected_task_revision")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        task = tasks.get_task(conn, task_id)
        _check_task_visibility(context, task, mutate=True)
        updated = tasks.transition_task(conn, task_id, new_status, expected_revision=expected)
        conn.commit()
        return {
            "action_status": "applied",
            "object_refs": [{"kind": "task", "id": task_id, "revision": updated["revision"]}],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": [],
            "result": tasks.task_to_contract(updated),
        }
    except tasks.TaskError:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_task_interaction_answer(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    interaction_id = params.get("interaction_id")
    if not isinstance(interaction_id, str) or not interaction_id.strip():
        raise CapabilityError("schema_violation", "params.interaction_id must be a non-empty string.")
    answer = params.get("answer")
    if not isinstance(answer, dict):
        raise CapabilityError("schema_violation", "params.answer must be an object.")
    expected = _optional_int(params, "expected_task_revision")
    if expected is None:
        raise CapabilityError(
            "schema_violation",
            "params.expected_task_revision is required to resume a waiting task.",
        )
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        outcome = tasks.answer_pending_interaction(
            conn,
            interaction_id=interaction_id,
            answer=answer,
            principal_id=context.principal_id,
            expected_task_revision=expected,
        )
        conn.commit()
        return {
            "action_status": "applied",
            "object_refs": [
                {"kind": "task", "id": outcome["task"]["task_id"], "revision": outcome["task"]["revision"]},
                {"kind": "pending_interaction", "id": interaction_id, "revision": None},
            ],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": [],
            "result": {
                "task": tasks.task_to_contract(outcome["task"]),
                "interaction_status": outcome["interaction"]["status"],
            },
        }
    except tasks.TaskError:
        conn.rollback()
        raise
    finally:
        conn.close()


def _takes_services(handler: Callable[..., dict[str, Any]]) -> bool:
    try:
        return len(inspect.signature(handler).parameters) >= 4
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        return False


# ---------------------------------------------------------------------------
# S03 evidence handlers


def _evidence_scope_check(
    context: auth.PrincipalContext,
    project_id: str | None,
    *,
    mutate: bool = False,
) -> None:
    """Mirror evidence.py scoping: project-bound needs membership; unbound
    evidence needs ANY membership (never anonymous)."""
    if project_id is not None:
        context.require_project(project_id, "member" if mutate else "viewer")
    elif not context.has_any_membership():
        raise CapabilityError(
            "forbidden_scope",
            "Unbound evidence requires at least one project membership.",
            http_status=403,
        )


def _handle_evidence_document_create(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
    services: ServiceSettings,
) -> dict[str, Any]:
    notebook_code = params.get("notebook_code")
    if not isinstance(notebook_code, str) or not notebook_code.strip():
        raise CapabilityError("schema_violation", "params.notebook_code must be a non-empty string.")
    project_id = params.get("project_id")
    if project_id is not None:
        if not isinstance(project_id, str):
            raise CapabilityError("schema_violation", "params.project_id must be a string.")
        context.require_project(project_id, "member")
    title = params.get("title")
    author = params.get("author")
    source_channel = params.get("source_channel")
    for name, value in (("title", title), ("author", author), ("source_channel", source_channel)):
        if value is not None and not isinstance(value, str):
            raise CapabilityError("schema_violation", f"params.{name} must be a string.")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        document = ingest.create_document(
            conn,
            project_id=project_id,
            notebook_code=notebook_code.strip(),
            title=title,
            author=author,
            source_channel=source_channel,
        )
        conn.commit()
        return {
            "action_status": "applied",
            "project_id": project_id,
            "object_refs": [{"kind": "evidence_document", "id": document["document_id"], "revision": None}],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": [],
            "result": document,
        }
    except ingest.IngestError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_evidence_page_ingest(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
    services: ServiceSettings,
) -> dict[str, Any]:
    document_id = params.get("document_id")
    if not isinstance(document_id, str) or not document_id.strip():
        raise CapabilityError("schema_violation", "params.document_id must be a non-empty string.")
    page_order = _optional_int(params, "page_order")
    if page_order is None:
        raise CapabilityError("schema_violation", "params.page_order is required (integer >= 1).")
    content_data_url = params.get("content_data_url")
    if not isinstance(content_data_url, str) or not content_data_url:
        raise CapabilityError(
            "schema_violation",
            "params.content_data_url must be a base64 data URL of the page file.",
        )
    filename = params.get("filename")
    if filename is not None and not isinstance(filename, str):
        raise CapabilityError("schema_violation", "params.filename must be a string.")
    paper_page_number = params.get("paper_page_number")
    if paper_page_number is not None and not isinstance(paper_page_number, str):
        raise CapabilityError("schema_violation", "params.paper_page_number must be a string.")

    conn = connect(db_path)
    try:
        document = ingest.get_document(conn, document_id)
        _evidence_scope_check(context, document["project_id"], mutate=True)
        content, detected_mime = decode_data_url(content_data_url)
        conn.execute("BEGIN IMMEDIATE")
        page = ingest.ingest_page(
            conn,
            document_id=document_id,
            content=content,
            filename=filename or f"page_{page_order}",
            page_order=page_order,
            paper_page_number=paper_page_number,
            storage_root=services.storage_root,
            project_id=document["project_id"],
            source_channel=document["source_channel"],
            declared_mime=detected_mime,
        )
        preview = ingest.create_preview(
            conn, page_id=page["page_id"], storage_root=services.storage_root
        )
        conn.commit()
    except ingest.IngestError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except ocr_port.TranscriberError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    page_view = _page_view(db_path=db_path, page_id=page["page_id"], services=services)
    page_view["already_present"] = bool(page.get("already_present"))
    return {
        "action_status": "applied",
        "project_id": document["project_id"],
        "object_refs": [
            {"kind": "evidence_page", "id": page["page_id"], "revision": page["active_revision"]},
            {"kind": "evidence_object", "id": page["evidence_id"], "revision": None},
        ],
        "evidence_refs": [
            {"evidence_id": page["evidence_id"], "revision": int(page["active_revision"])}
        ],
        "unknowns": [] if preview.get("materialized") else ["preview_pending"],
        "warnings": [] if not page.get("already_present") else ["page_already_present"],
        "result": {"page": page_view, "preview": preview},
    }


def _page_view(db_path: str, page_id: str, services: ServiceSettings) -> dict[str, Any]:
    """Build the read view for one page with mapped transcript blocks."""
    from . import pnglite as _png

    conn = connect(db_path)
    try:
        page = ingest.get_page(conn, page_id)
        original_size = None
        if page["mime"] == "image/png":
            content, _ = ingest.read_original_bytes(conn, page_id)
            try:
                original_size = _png.get_png_size(content)
            except _png.PngError:
                original_size = None
        transcript = transcribe.get_page_transcript(
            conn, page_id=page_id, original_size=original_size
        )
    finally:
        conn.close()
    page["transcript"] = transcript
    page["preview"] = _preview_payload(page)
    return page


def _preview_payload(page: dict[str, Any]) -> dict[str, Any]:
    preview = page.get("preview") or {}
    if preview.get("materialized"):
        return {
            "materialized": True,
            "artifact_id": preview.get("artifact_id"),
            "sha256": preview.get("sha256"),
        }
    return {"materialized": False, "reason": "preview_pending: no stdlib codec for this format"}


def _handle_evidence_page_read(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
    services: ServiceSettings,
) -> dict[str, Any]:
    if params.get("page_id") and any(key in params for key in ("view", "transcript_revision", "block_ids")):
        return _research_read(db_path, context, params, "page")
    if params.get("document_id") and "view" in params:
        return _research_read(db_path, context, params, "document")
    page_id = params.get("page_id")
    document_id = params.get("document_id")
    if not isinstance(page_id, str) and not isinstance(document_id, str):
        raise CapabilityError(
            "schema_violation", "params.page_id or params.document_id is required."
        )
    conn = connect(db_path)
    try:
        if isinstance(page_id, str):
            page = ingest.get_page(conn, page_id)
            _evidence_scope_check(context, page["project_id"])
        else:
            ingest.get_document(conn, document_id)
            project_id = conn.execute(
                "SELECT project_id FROM evidence_documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()["project_id"]
            _evidence_scope_check(context, project_id)
    except ingest.IngestError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    finally:
        conn.close()

    if isinstance(page_id, str):
        return {
            "action_status": "applied",
            "object_refs": [{"kind": "evidence_page", "id": page_id, "revision": None}],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": [],
            "result": {"page": _page_view(db_path, page_id, services)},
        }
    conn = connect(db_path)
    try:
        pages = ingest.list_document_pages(conn, document_id)
    except ingest.IngestError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "object_refs": [{"kind": "evidence_document", "id": document_id, "revision": None}],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": {
            "pages": [
                {**{k: v for k, v in page.items() if k != "preview"}, "preview": _preview_payload(page)}
                for page in pages
            ]
        },
    }


def _handle_evidence_page_original(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    page_id = params.get("page_id")
    if not isinstance(page_id, str) or not page_id:
        raise CapabilityError("schema_violation", "params.page_id is required.")
    conn = connect(db_path)
    try:
        page = ingest.get_page(conn, page_id)
        _evidence_scope_check(context, page["project_id"])
        content, mime = ingest.read_original_bytes(conn, page_id)
    except ingest.IngestError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    finally:
        conn.close()
    digest = hashlib.sha256(content).hexdigest()
    return {
        "action_status": "applied",
        "object_refs": [{"kind": "evidence_page", "id": page_id, "revision": None}],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": {
            "page_id": page_id,
            "mime": mime,
            "filename": page.get("filename"),
            "bytes": len(content),
            "sha256": digest,
            "data_b64": base64.b64encode(content).decode("ascii"),
        },
    }


def _handle_evidence_pages_list(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    conn = connect_read_only(db_path)
    try:
        conn.execute("BEGIN")
        result = evidence.list_evidence_pages(
            conn, context, project_ids=params.get("project_ids"),
            limit=params.get("limit", 50), offset=params.get("offset", 0),
        )
        return {
            "action_status": "applied",
            "object_refs": [{"kind": "evidence_page", "id": p["page_id"], "revision": None}
                            for p in result["pages"]],
            "evidence_refs": [], "unknowns": [], "warnings": [], "result": result,
        }
    except query.QueryError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    finally:
        conn.close()


def _handle_evidence_page_preview(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    page_id = params.get("page_id")
    if not isinstance(page_id, str) or not page_id:
        raise CapabilityError("schema_violation", "params.page_id is required.")
    conn = connect(db_path)
    try:
        page = ingest.get_page(conn, page_id)
        _evidence_scope_check(context, page["project_id"])
        artifact = conn.execute(
            "SELECT * FROM derived_artifacts WHERE page_id = ? AND kind = 'preview' "
            "AND materialized = 1 ORDER BY created_at DESC LIMIT 1",
            (page_id,),
        ).fetchone()
        if artifact is None:
            return {
                "action_status": "applied",
                "object_refs": [{"kind": "evidence_page", "id": page_id, "revision": None}],
                "evidence_refs": [], "unknowns": [], "warnings": [],
                "result": {"page_id": page_id, "materialized": False, "data_b64": None},
            }
        path = Path(artifact["storage_path"])
        if not path.is_file():
            raise CapabilityError("preview_missing", "Preview file is missing.", http_status=500)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != artifact["sha256"]:
            raise CapabilityError("preview_integrity_failure", "Preview digest mismatch.", http_status=500)
        return {
            "action_status": "applied",
            "object_refs": [{"kind": "evidence_page", "id": page_id, "revision": None}],
            "evidence_refs": [], "unknowns": [], "warnings": [],
            "result": {
                "page_id": page_id,
                "materialized": True,
                "mime": "image/png",
                "sha256": artifact["sha256"],
                "data_b64": base64.b64encode(content).decode("ascii"),
            },
        }
    except ingest.IngestError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    finally:
        conn.close()


def _handle_evidence_transcribe_run(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
    services: ServiceSettings,
) -> dict[str, Any]:
    page_id = params.get("page_id")
    if not isinstance(page_id, str) or not page_id.strip():
        raise CapabilityError("schema_violation", "params.page_id must be a non-empty string.")
    provider_name = params.get("provider")
    if not isinstance(provider_name, str) or not provider_name.strip():
        raise CapabilityError("schema_violation", "params.provider must be a non-empty string.")
    config = params.get("config") or {}
    if not isinstance(config, dict):
        raise CapabilityError("schema_violation", "params.config must be an object.")
    conn = connect(db_path)
    try:
        page = ingest.get_page(conn, page_id)
        _evidence_scope_check(context, page["project_id"], mutate=True)
    except ingest.IngestError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    finally:
        conn.close()

    registry = services.build_registry()
    try:
        conn = connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            run = transcribe.run_transcription(
                conn,
                page_id=page_id,
                provider_name=provider_name,
                registry=registry,
                cloud_policy=services.cloud_policy,
                config=config,
            )
            conn.commit()
        except (transcribe.TranscriptionError, TranscriberError):
            # Provider/gate failure: run_transcription has already recorded
            # the failed run row and page status. COMMIT that audit trail —
            # plan §8 requires the failure to be retryable and the original
            # to stay queryable, which needs the record to persist.
            conn.commit()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except (transcribe.TranscriptionError, TranscriberError) as exc:
        # Provider/gate errors carry stable codes; run_transcription converts
        # most of them to TranscriptionError, but the cloud gate fires before
        # the run row exists and surfaces as TranscriberError.
        raise CapabilityError(
            getattr(exc, "code", "provider_error"),
            getattr(exc, "message", str(exc)),
            http_status=getattr(exc, "http_status", 400),
        ) from exc

    return {
        "action_status": "applied",
        "object_refs": [
            {"kind": "transcript_run", "id": run["run_id"], "revision": None},
            {"kind": "evidence_page", "id": page_id, "revision": None},
        ],
        "evidence_refs": [],
        "unknowns": [] if run["status"] == "succeeded" else [f"run_status:{run['status']}"],
        "warnings": [],
        "result": run,
    }


def _handle_evidence_transcript_import(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    page_id = params.get("page_id")
    blocks = params.get("blocks")
    provider = params.get("provider")
    model = params.get("model")
    version = params.get("version")
    expected_revision = _optional_int(params, "expected_revision")
    needs_human = params.get("needs_human", False)
    if not isinstance(page_id, str) or not page_id.strip():
        raise CapabilityError("schema_violation", "params.page_id is required.")
    if not isinstance(blocks, list) or not blocks:
        raise CapabilityError("schema_violation", "params.blocks must be a non-empty array.")
    if not isinstance(provider, str) or not provider.strip():
        raise CapabilityError("schema_violation", "params.provider is required.")
    if model is not None and not isinstance(model, str):
        raise CapabilityError("schema_violation", "params.model must be a string.")
    if version is not None and not isinstance(version, str):
        raise CapabilityError("schema_violation", "params.version must be a string.")
    if not isinstance(needs_human, bool):
        raise CapabilityError("schema_violation", "params.needs_human must be boolean.")
    conn = connect(db_path)
    try:
        page = ingest.get_page(conn, page_id)
        _evidence_scope_check(context, page["project_id"], mutate=True)
        conn.execute("BEGIN IMMEDIATE")
        try:
            run = transcribe.import_transcript(
                conn,
                page_id=page_id,
                blocks=blocks,
                provider=provider.strip(),
                model=model,
                version=version,
                raw_output=params.get("raw_output"),
                expected_revision=expected_revision,
                needs_human=needs_human,
            )
            conn.commit()
        except transcribe.TranscriptionError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
    except ingest.IngestError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except transcribe.TranscriptionError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "object_refs": [
            {"kind": "transcript_run", "id": run["run_id"], "revision": None},
            {"kind": "evidence_page", "id": page_id, "revision": None},
        ],
        "evidence_refs": [],
        "unknowns": [] if run["status"] == "succeeded" else [f"run_status:{run['status']}"],
        "warnings": [],
        "result": run,
    }


def _handle_evidence_transcript_correct(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
    services: ServiceSettings,
) -> dict[str, Any]:
    page_id = params.get("page_id")
    if not isinstance(page_id, str) or not page_id.strip():
        raise CapabilityError("schema_violation", "params.page_id must be a non-empty string.")
    blocks = params.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise CapabilityError("schema_violation", "params.blocks must be a non-empty array.")
    expected_revision = _optional_int(params, "expected_revision")
    if expected_revision is None:
        raise CapabilityError(
            "schema_violation",
            "params.expected_revision is required for transcript corrections.",
        )
    reason = params.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise CapabilityError("schema_violation", "params.reason must be a non-empty string.")
    conn = connect(db_path)
    try:
        page = ingest.get_page(conn, page_id)
        _evidence_scope_check(context, page["project_id"], mutate=True)
    except ingest.IngestError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    finally:
        conn.close()

    try:
        conn = connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            run = transcribe.correct_transcript(
                conn,
                page_id=page_id,
                blocks=blocks,
                expected_revision=expected_revision,
                actor=context.principal_id,
                reason=reason.strip(),
            )
            # S09: a source-evidence correction expires dependent research
            # criteria pinned to this page (plan §15 扩展机制验收).
            stale_criteria = research.stale_criteria_for_page(conn, page_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except transcribe.TranscriptionError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except SchemaViolation as exc:
        raise CapabilityError("schema_violation", f"Block schema violation: {exc}") from exc

    warnings = [
        f"{len(stale_criteria)} research criterion/criteria expired by this correction"
    ] if stale_criteria else []
    return {
        "action_status": "applied",
        "object_refs": [
            {"kind": "transcript_run", "id": run["run_id"], "revision": None},
            {"kind": "evidence_page", "id": page_id, "revision": None},
        ],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": warnings,
        "result": run,
    }


def _handle_evidence_transcription_retry(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
    services: ServiceSettings,
) -> dict[str, Any]:
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise CapabilityError("schema_violation", "params.run_id must be a non-empty string.")
    config_override = params.get("config_override") or {}
    if not isinstance(config_override, dict):
        raise CapabilityError("schema_violation", "params.config_override must be an object.")
    conn = connect(db_path)
    try:
        run = transcribe.get_run(conn, run_id)
        page = ingest.get_page(conn, run["page_id"])
        _evidence_scope_check(context, page["project_id"], mutate=True)
    except ingest.IngestError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except transcribe.TranscriptionError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    finally:
        conn.close()

    try:
        conn = connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            retried = transcribe.retry_transcription(
                conn,
                run_id=run_id,
                registry=services.build_registry(),
                cloud_policy=services.cloud_policy,
                config_override=config_override,
            )
            conn.commit()
        except (transcribe.TranscriptionError, TranscriberError):
            # Same audit-trail rule as the direct run path: persist the new
            # failed run row instead of rolling it back.
            conn.commit()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except (transcribe.TranscriptionError, TranscriberError) as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc

    return {
        "action_status": "applied",
        "object_refs": [
            {"kind": "transcript_run", "id": retried["run_id"], "revision": None},
            {"kind": "evidence_page", "id": retried["page_id"], "revision": None},
        ],
        "evidence_refs": [],
        "unknowns": [] if retried["status"] == "succeeded" else [f"run_status:{retried['status']}"],
        "warnings": [],
        "result": retried,
    }


def _handle_evidence_provider_list(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
    services: ServiceSettings,
) -> dict[str, Any]:
    if not context.has_any_membership():
        raise CapabilityError(
            "forbidden_scope", "Reads require at least one project membership.", http_status=403
        )
    registry = services.build_registry()
    return {
        "action_status": "applied",
        "object_refs": [],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": {
            "providers": registry.list(),
            "cloud_policy": services.cloud_policy.describe(),
        },
    }


def _page_evidence_ref(conn: sqlite3.Connection, page_id: str, revision: int) -> dict[str, Any] | None:
    """Map a page+transcript-revision onto the envelope's evidence-ref shape."""
    row = conn.execute(
        "SELECT evidence_id FROM evidence_pages WHERE page_id = ?", (page_id,)
    ).fetchone()
    if row is None:
        return None
    return {"evidence_id": row["evidence_id"], "revision": revision}


# ---------------------------------------------------------------------------
# S04 handlers: experiment memory service


def _event_project_id(conn: sqlite3.Connection, event_id: str) -> str | None:
    """Authoritative project for an event/proposal, never self-reported."""
    row = conn.execute(
        """
        SELECT ed.project_id
        FROM event_revisions r
        JOIN evidence_pages ep
          ON ep.page_id = json_extract(r.field_json, '$.page_id')
        JOIN evidence_documents ed ON ed.document_id = ep.document_id
        WHERE r.entity_type = 'experiment_event' AND r.entity_id = ?
        ORDER BY r.revision DESC LIMIT 1
        """,
        (event_id,),
    ).fetchone()
    if row is not None and row["project_id"]:
        return row["project_id"]
    row = conn.execute(
        "SELECT payload_json FROM event_proposals WHERE event_id = ?", (event_id,)
    ).fetchone()
    if row is not None:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except ValueError:
            return None
        return payload.get("project_id")
    return None


def _memory_scope_check(
    context: auth.PrincipalContext, project_id: str | None, *, mutate: bool
) -> None:
    """Project-bound memory ops need membership; unbound needs ANY membership
    (never anonymous) — same rule as evidence."""
    if project_id is not None:
        context.require_project(project_id, "member" if mutate else "viewer")
    elif not context.has_any_membership():
        raise CapabilityError(
            "forbidden_scope",
            "Unbound records require at least one project membership.",
            http_status=403,
        )


def _handle_memory_proposal_create(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    project_id = params.get("project_id")
    page_id = params.get("page_id")
    if not isinstance(project_id, str) or not project_id:
        raise CapabilityError("schema_violation", "params.project_id must be a non-empty string.")
    if not isinstance(page_id, str) or not page_id:
        raise CapabilityError("schema_violation", "params.page_id must be a non-empty string.")
    transcript_revision = params.get("transcript_revision")
    if not isinstance(transcript_revision, int) or transcript_revision < 0:
        raise CapabilityError(
            "schema_violation", "params.transcript_revision must be a non-negative integer."
        )
    profile_id = params.get("profile_id") or "chemistry_notebook"
    profile_version = params.get("profile_version") or "1.0"
    operation_id = params.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        raise CapabilityError(
            "schema_violation", "params.operation_id must be a non-empty string."
        )
    items = params.get("items")
    if items is not None and not isinstance(items, list):
        raise CapabilityError("schema_violation", "params.items must be a list when given.")
    # S07 §13.6: the requesting task (when the capability is invoked from a
    # task context) is stamped on the proposal so the B-side inventory subtask
    # can carry parent_task_id back to the origin request.
    task_id = params.get("task_id")
    if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
        raise CapabilityError("schema_violation", "params.task_id must be a non-empty string when present.")
    auto_confirm_safe = params.get("auto_confirm_safe", False)
    if not isinstance(auto_confirm_safe, bool):
        raise CapabilityError("schema_violation", "params.auto_confirm_safe must be boolean.")
    acceptance_mode = params.get("acceptance_mode")
    if acceptance_mode is not None and acceptance_mode not in ("default", "review"):
        raise CapabilityError("schema_violation", "params.acceptance_mode must be default or review.")
    context.require_project(project_id, "member")

    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # The proposal's page MUST belong to the same project (§8.5): a page
        # from another project would misattribute the resulting events and
        # leak cross-project evidence through page references.
        page_project = conn.execute(
            """
            SELECT d.project_id FROM evidence_pages p
            JOIN evidence_documents d ON d.document_id = p.document_id
            WHERE p.page_id = ?
            """,
            (page_id,),
        ).fetchone()
        if page_project is None:
            raise CapabilityError(
                "invalid_page", f"Unknown page {page_id!r}.", http_status=404
            )
        if page_project["project_id"] != project_id:
            raise CapabilityError(
                "forbidden_scope",
                f"Page {page_id!r} belongs to project {page_project['project_id']!r}, "
                f"not {project_id!r}; cross-project page references are refused.",
                http_status=403,
            )
        if task_id is not None:
            task_row = conn.execute(
                "SELECT project_id FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task_row is None or task_row["project_id"] != project_id:
                raise CapabilityError(
                    "invalid_task",
                    f"params.task_id {task_id!r} does not exist in project {project_id!r}.",
                    http_status=404,
                )
        if items is None:
            items = proposal_mod.build_proposal_items(
                conn,
                page_id=page_id,
                transcript_revision=transcript_revision,
                profile_id=profile_id,
                profile_version=profile_version,
            )
        created = experiments.create_proposal(
            conn,
            project_id=project_id,
            page_id=page_id,
            transcript_revision=transcript_revision,
            profile_id=profile_id,
            profile_version=profile_version,
            items=items,
            created_by=context.principal_id,
            operation_id=operation_id,
            task_id=task_id,
            auto_confirm_safe=auto_confirm_safe,
            acceptance_mode=acceptance_mode,
        )
        conn.commit()
        result = {
            "proposal": created["proposal"],
            "idempotent": created["idempotent"],
            "knowledge": created.get("knowledge"),
        }
        status = created["proposal"]["status"]
        unknowns = []
        if status in ("awaiting_confirmation", "awaiting_clarification"):
            unknowns.append(f"proposal_status:{status}")
        page_ref = _page_evidence_ref(conn, page_id, transcript_revision)
        return {
            "action_status": "applied",
            "object_refs": [
                {"kind": "memory_proposal", "id": created["proposal"]["proposal_id"],
                 "revision": created["proposal"]["revision"]}
            ] + [{"kind": "experiment_event", "id": event["event_id"], "revision": event["revision"]}
                 for event in created["proposal"].get("knowledge_events", [])],
            "evidence_refs": [page_ref] if page_ref else [],
            "unknowns": unknowns,
            "warnings": [],
            "result": result,
        }
    except experiments.MemoryError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except ValueError as exc:
        conn.rollback()
        raise CapabilityError("invalid_request", str(exc), http_status=409) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_memory_proposal_read(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    proposal_id = params.get("proposal_id")
    if not isinstance(proposal_id, str) or not proposal_id:
        raise CapabilityError("schema_violation", "params.proposal_id must be a non-empty string.")
    conn = connect(db_path)
    try:
        payload = experiments.get_proposal(conn, proposal_id)
        _memory_scope_check(context, payload.get("project_id"), mutate=False)
        return {
            "action_status": "applied",
            "object_refs": [
                {"kind": "memory_proposal", "id": proposal_id, "revision": payload["revision"]}
            ],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": [],
            "result": {"proposal": payload},
        }
    except experiments.MemoryError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    finally:
        conn.close()


def _handle_memory_proposal_item_update(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    proposal_id = params.get("proposal_id")
    item_key = params.get("item_key")
    changes = params.get("changes")
    expected = params.get("expected_proposal_revision")
    if not isinstance(proposal_id, str) or not proposal_id:
        raise CapabilityError("schema_violation", "params.proposal_id is required.")
    if not isinstance(item_key, str) or not item_key:
        raise CapabilityError("schema_violation", "params.item_key is required.")
    if not isinstance(changes, dict) or not changes:
        raise CapabilityError("schema_violation", "params.changes must be a non-empty object.")
    if not isinstance(expected, int):
        raise CapabilityError(
            "schema_violation", "params.expected_proposal_revision must be an integer."
        )
    try:
        auth.require_human(context, "memory.proposal.item.update")
    except auth.AuthError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        head = experiments.get_proposal(conn, proposal_id)
        _memory_scope_check(context, head.get("project_id"), mutate=True)
        payload = experiments.update_proposal_item(
            conn,
            proposal_id=proposal_id,
            item_key=item_key,
            changes=changes,
            expected_proposal_revision=expected,
            principal_id=context.principal_id,
        )
        conn.commit()
        return {
            "action_status": "applied",
            "object_refs": [
                {"kind": "memory_proposal", "id": proposal_id,
                 "revision": payload["revision"]}
            ],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": [],
            "result": {"proposal": payload},
        }
    except experiments.MemoryError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_memory_proposal_confirm_items(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    proposal_id = params.get("proposal_id")
    item_keys = params.get("item_keys")
    decision = params.get("decision")
    expected = params.get("expected_proposal_revision")
    if not isinstance(proposal_id, str) or not proposal_id:
        raise CapabilityError("schema_violation", "params.proposal_id must be a non-empty string.")
    if not isinstance(item_keys, list) or not item_keys or not all(isinstance(k, str) for k in item_keys):
        raise CapabilityError("schema_violation", "params.item_keys must be a non-empty string list.")
    if decision not in ("confirmed", "declined"):
        raise CapabilityError("schema_violation", 'params.decision must be "confirmed" or "declined".')
    if not isinstance(expected, int):
        raise CapabilityError(
            "schema_violation", "params.expected_proposal_revision must be an integer."
        )
    # R03/F04: confirmations are human actions. An Agent may read the proposal
    # and apply already-confirmed items, but must not click "confirm" itself.
    try:
        auth.require_human(context, "memory.proposal.confirm")
    except auth.AuthError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        head = experiments.get_proposal(conn, proposal_id)
        _memory_scope_check(context, head.get("project_id"), mutate=True)
        outcome = experiments.record_confirmation(
            conn,
            proposal_id=proposal_id,
            item_keys=item_keys,
            decision=decision,
            principal_id=context.principal_id,
            expected_proposal_revision=expected,
        )
        conn.commit()
        return {
            "action_status": "applied",
            "object_refs": [{"kind": "memory_proposal", "id": proposal_id, "revision": outcome["proposal_revision"]}],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": [],
            "result": outcome,
        }
    except experiments.MemoryError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_memory_proposal_apply(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    proposal_id = params.get("proposal_id")
    operation_id = params.get("operation_id")
    if not isinstance(proposal_id, str) or not proposal_id:
        raise CapabilityError("schema_violation", "params.proposal_id must be a non-empty string.")
    if not isinstance(operation_id, str) or not operation_id:
        raise CapabilityError(
            "schema_violation", "params.operation_id must be a non-empty string."
        )
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        head = experiments.get_proposal(conn, proposal_id)
        _memory_scope_check(context, head.get("project_id"), mutate=True)
        outcome = experiments.apply_proposal(
            conn,
            proposal_id=proposal_id,
            principal_id=context.principal_id,
            operation_id=operation_id,
        )
        conn.commit()
        applied, pending = outcome["applied"], outcome["pending"]
        unknowns = [f"pending_item:{p['item_key']}:{p['reason']}" for p in pending]
        return {
            "action_status": "applied" if applied else "clarification_needed",
            "object_refs": [
                {"kind": "experiment_event", "id": a["event_id"], "revision": 1}
                for a in applied
            ],
            "evidence_refs": [],
            "unknowns": unknowns,
            "warnings": [],
            "result": outcome,
        }
    except experiments.MemoryError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_memory_sample_search(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    project_id = params.get("project_id")
    label = params.get("label")
    if not isinstance(project_id, str) or not project_id:
        raise CapabilityError("schema_violation", "params.project_id must be a non-empty string.")
    if not isinstance(label, str) or not label.strip():
        raise CapabilityError("schema_violation", "params.label must be a non-empty string.")
    context.require_project(project_id, "member")
    conn = connect(db_path)
    try:
        match = experiments.search_sample_candidates(conn, project_id=project_id, label=label.strip())
        return {
            "action_status": "applied",
            "object_refs": [],
            "evidence_refs": [],
            "unknowns": (
                [] if match["action"] == "link"
                else [f"sample_match:{match['action']}"]
            ),
            "warnings": [],
            "result": match,
        }
    finally:
        conn.close()


def _handle_memory_clarification_open(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    proposal_id = params.get("proposal_id")
    questions = params.get("questions")
    task_id = params.get("task_id")
    expected_task_revision = params.get("expected_task_revision")
    if not isinstance(proposal_id, str) or not proposal_id:
        raise CapabilityError("schema_violation", "params.proposal_id must be a non-empty string.")
    if not isinstance(questions, list) or not questions:
        raise CapabilityError(
            "schema_violation", "params.questions must be a non-empty list."
        )
    if not isinstance(task_id, str) or not task_id:
        raise CapabilityError("schema_violation", "params.task_id must be a non-empty string.")
    if not isinstance(expected_task_revision, int):
        raise CapabilityError(
            "schema_violation", "params.expected_task_revision must be an integer."
        )
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        head = experiments.get_proposal(conn, proposal_id)
        _memory_scope_check(context, head.get("project_id"), mutate=True)
        outcome = experiments.open_clarification(
            conn,
            proposal_id=proposal_id,
            questions=questions,
            task_id=task_id,
            principal_id=context.principal_id,
            expected_task_revision=expected_task_revision,
        )
        conn.commit()
        return {
            "action_status": "applied",
            "object_refs": [
                {"kind": "clarification", "id": outcome["clarification_id"], "revision": None},
                {"kind": "memory_proposal", "id": proposal_id, "revision": outcome["proposal_revision"]},
            ],
            "evidence_refs": [],
            "unknowns": ["task_status:waiting_input"],
            "warnings": [],
            "result": outcome,
        }
    except experiments.MemoryError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except tasks.TaskError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_memory_clarification_answer(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    clarification_id = params.get("clarification_id")
    answers = params.get("answers")
    expected_task_revision = params.get("expected_task_revision")
    expected_proposal_revision = params.get("expected_proposal_revision")
    if not isinstance(clarification_id, str) or not clarification_id:
        raise CapabilityError(
            "schema_violation", "params.clarification_id must be a non-empty string."
        )
    if not isinstance(answers, list) or not answers:
        raise CapabilityError("schema_violation", "params.answers must be a non-empty list.")
    for a in answers:
        if not isinstance(a, dict) or "question_id" not in a or "value" not in a:
            raise CapabilityError(
                "schema_violation",
                "Each answer needs question_id and value.",
            )
    if not isinstance(expected_task_revision, int) or not isinstance(expected_proposal_revision, int):
        raise CapabilityError(
            "schema_violation",
            "params.expected_task_revision and expected_proposal_revision must be integers.",
        )
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        outcome = experiments.answer_clarification(
            conn,
            clarification_id=clarification_id,
            answers=answers,
            principal_id=context.principal_id,
            expected_task_revision=expected_task_revision,
            expected_proposal_revision=expected_proposal_revision,
        )
        conn.commit()
        proposal_id = conn.execute(
            "SELECT proposal_id FROM clarifications WHERE clarification_id = ?",
            (clarification_id,),
        ).fetchone()["proposal_id"]
        return {
            "action_status": "applied",
            "object_refs": [
                {"kind": "clarification", "id": clarification_id, "revision": None},
                {"kind": "memory_proposal", "id": proposal_id, "revision": outcome["proposal_revision"]},
            ],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": [],
            "result": outcome,
        }
    except experiments.MemoryError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except tasks.TaskError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_memory_event_correct(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    event_id = params.get("event_id")
    expected_revision = params.get("expected_revision")
    changes = params.get("changes")
    reason = params.get("reason")
    if not isinstance(event_id, str) or not event_id:
        raise CapabilityError("schema_violation", "params.event_id must be a non-empty string.")
    if not isinstance(expected_revision, int) or expected_revision < 1:
        raise CapabilityError(
            "schema_violation", "params.expected_revision must be a positive integer."
        )
    if not isinstance(changes, dict) or not changes:
        raise CapabilityError("schema_violation", "params.changes must be a non-empty object.")
    if not isinstance(reason, str) or not reason.strip():
        raise CapabilityError("schema_violation", "params.reason must be a non-empty string.")
    correction_source = params.get("correction_source")
    if correction_source is not None and not isinstance(correction_source, dict):
        raise CapabilityError(
            "schema_violation", "params.correction_source must be an object when given."
        )
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        project_id = _event_project_id(conn, event_id)
        if project_id is None:
            raise CapabilityError(
                "forbidden_scope",
                "The event has no authoritative project; correction is refused.",
                http_status=403,
            )
        context.require_project(project_id, "member")
        outcome = experiments.correct_event(
            conn,
            event_id=event_id,
            expected_revision=expected_revision,
            changes=changes,
            reason=reason.strip(),
            principal_id=context.principal_id,
            correction_source=correction_source,
        )
        conn.commit()
        parked = outcome.get("outcome") == "parked_as_pending"
        return {
            "action_status": "applied" if not parked else "proposed",
            "object_refs": [
                {"kind": "experiment_event", "id": event_id,
                 "revision": outcome.get("revision", expected_revision)},
            ],
            "evidence_refs": [],
            "unknowns": ["correction_pending_confirmation"] if parked else [],
            "warnings": [],
            "result": outcome,
        }
    except experiments.MemoryError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except auth.AuthError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _research_read(db_path, context, params, kind):
    """One consistent read view, shared by every external transport."""
    from . import research_reads, sample_context
    conn = connect_read_only(db_path)
    try:
        conn.execute("BEGIN")
        if kind == "context":
            result = sample_context.get_sample_context(conn, context, params)
            refs = ([{"kind": "sample", "id": result["sample"]["sample_id"], "revision": None}]
                if result.get("sample") else [])
        elif kind == "catalog":
            result = research_reads.catalog_samples(conn, context, params)
            refs = [{"kind": "sample", "id": sample["sample_id"], "revision": None} for sample in result["samples"]]
        elif kind == "page":
            result = research_reads.read_page(conn, context, params)
            transcript = result["page"].get("transcript") or {}
            refs = [{"kind": "evidence_page", "id": params["page_id"],
                "revision": transcript.get("transcript_revision")}]
        elif kind == "document":
            from . import research_reads
            document = conn.execute("SELECT project_id FROM evidence_documents WHERE document_id=?",
                (params["document_id"],)).fetchone()
            if document is None:
                raise query.QueryError("not_found", "Document not found.", http_status=404)
            context.require_project(document["project_id"], "viewer")
            pages = conn.execute("SELECT page_id FROM evidence_pages WHERE document_id=? ORDER BY page_order,page_id",
                (params["document_id"],)).fetchall()
            result = {"pages": [research_reads.read_page(conn, context,
                {"page_id": page["page_id"], "view": params["view"]})["page"] for page in pages]}
            refs = [{"kind": "evidence_document", "id": params["document_id"], "revision": None}]
        else:
            result = research_reads.read_event(conn, context, params)
            current = result["revisions"][-1] if "revisions" in result else result
            refs = [{"kind": "experiment_event", "id": params["event_id"], "revision": current["revision"]}]
        conn.commit()
        return {"action_status": "applied", "project_id": params.get("project_id"),
            "object_refs": refs, "evidence_refs": [], "unknowns": [], "warnings": [], "result": result}
    except (query.QueryError, auth.AuthError) as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    finally:
        conn.close()


def _handle_query_sample_context(db_path, context, params):
    return _research_read(db_path, context, params, "context")


def _handle_memory_event_read(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    return _research_read(db_path, context, params, "event")


def _handle_memory_index_rebuild(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    artifact_id = params.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise CapabilityError("schema_violation", "params.artifact_id must be a non-empty string.")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        outcome = experiments.rebuild_artifact(conn, artifact_id=artifact_id)
        conn.commit()
        return {
            "action_status": "applied",
            "object_refs": [{"kind": "memory_artifact", "id": artifact_id, "revision": None}],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": [],
            "result": outcome,
        }
    except experiments.MemoryError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_query_events_search(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = query.search(
            conn, context,
            project_ids=params.get("project_ids"),
            sample_label=params.get("sample_label"),
            run_id=params.get("run_id"),
            date_from=params.get("date_from"),
            date_to=params.get("date_to"),
            event_kind=params.get("event_kind"),
            text=params.get("text"),
            knowledge_as_of=params.get("knowledge_as_of"),
            limit=params.get("limit") or 50,
        )
        query.log_query(
            conn, context,
            query_path="query.events.search",
            filters={
                k: v for k, v in params.items()
                if k in ("project_ids", "sample_label", "run_id", "date_from",
                          "date_to", "event_kind")
            },
            snapshot_version=result["snapshot_version"],
            candidates=result["candidates"],
        )
        conn.commit()
        return {
            "action_status": "applied",
            "object_refs": [],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": [],
            "result": result,
        }
    except query.QueryError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_query_samples_list(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    if params.get("view") == "catalog":
        return _research_read(db_path, context, params, "catalog")
    conn = connect(db_path)
    try:
        scope = query.resolve_scope(conn, context, params.get("project_ids"))
        if not scope:
            return {
                "action_status": "applied",
                "object_refs": [],
                "evidence_refs": [],
                "unknowns": [],
                "warnings": [],
                "result": {"samples": [], "scope": []},
            }
        placeholders = ",".join("?" for _ in scope)
        rows = conn.execute(
            f"""
            SELECT s.sample_id, s.project_id, s.label, s.aliases_json,
                   s.created_at,
                   COUNT(DISTINCT r.entity_id) AS event_count,
                   MAX(COALESCE(r.knowledge_as_of, r.created_at)) AS last_event_at
            FROM samples s
            LEFT JOIN event_revisions r
              ON r.entity_type = 'experiment_event'
             AND json_extract(r.field_json, '$.sample_id') = s.sample_id
            WHERE s.project_id IN ({placeholders})
            GROUP BY s.sample_id
            ORDER BY s.rowid DESC
            """,
            tuple(scope),
        ).fetchall()
        samples = []
        for row in rows:
            item = dict(row)
            try:
                item["aliases"] = json.loads(item.pop("aliases_json") or "[]")
            except ValueError:
                item["aliases"] = []
            samples.append(item)
        from .sample_cards import enrich_samples
        knowledge = enrich_samples(conn, samples, scope)
        samples = knowledge["samples"]
        return {
            "action_status": "applied",
            "object_refs": [
                {"kind": "sample", "id": item["sample_id"], "revision": None}
                for item in samples
            ],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": [],
            "result": {**knowledge, "scope": scope},
        }
    except query.QueryError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    finally:
        conn.close()


def _handle_query_sample_timeline(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    sample_id = params.get("sample_id")
    if not isinstance(sample_id, str) or not sample_id.strip():
        raise CapabilityError("schema_violation", "params.sample_id must be a non-empty string.")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = query.sample_timeline(
            conn, context,
            sample_id=sample_id.strip(),
            knowledge_as_of=params.get("knowledge_as_of"),
        )
        query.log_query(
            conn, context,
            query_path="query.sample.timeline",
            filters={"sample_id": sample_id},
            snapshot_version=result["snapshot_version"],
            candidates=[],
        )
        conn.commit()
        return {
            "action_status": "applied",
            "object_refs": [{"kind": "sample", "id": sample_id, "revision": None}],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": [],
            "result": result,
        }
    except query.QueryError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _build_answer_payload(
    conn,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    question = params.get("question")
    if not isinstance(question, str) or not question.strip():
        raise CapabilityError("schema_violation", "params.question must be a non-empty string.")
    project_ids = params.get("project_ids")
    if project_ids is not None and not isinstance(project_ids, list):
        raise CapabilityError("schema_violation", "params.project_ids must be a list.")
    answer = answers.build_answer(
        conn, context,
        question,
        project_ids=project_ids,
        knowledge_as_of=params.get("knowledge_as_of"),
    )
    query.log_query(
        conn, context,
        query_path="query.answer.build",
        filters={"project_ids": project_ids},
        snapshot_version=answer["snapshot_version"],
        candidates=[],
    )
    return answer


def _handle_query_answer_build(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        answer = _build_answer_payload(conn, context, params)
        conn.commit()
        return {
            "action_status": "applied",
            "object_refs": [],
            "evidence_refs": [],
            "unknowns": [g["gap_id"] for g in answer.get("gaps", [])],
            "warnings": [],
            "result": answer,
        }
    except (answers.AnswerError, query.QueryError) as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_query_answer_render(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        answer = _build_answer_payload(conn, context, params)
        conn.commit()
        return {
            "action_status": "applied",
            "object_refs": [],
            "evidence_refs": [],
            "unknowns": [g["gap_id"] for g in answer.get("gaps", [])],
            "warnings": [],
            "result": answer,
        }
    except (answers.AnswerError, query.QueryError) as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_query_report_save(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    project_id = params.get("project_id")
    question = params.get("question")
    answer = params.get("answer")
    if not isinstance(project_id, str) or not project_id.strip():
        raise CapabilityError("schema_violation", "params.project_id must be a non-empty string.")
    if not isinstance(question, str) or not question.strip():
        raise CapabilityError("schema_violation", "params.question must be a non-empty string.")
    if not isinstance(answer, dict):
        raise CapabilityError("schema_violation", "params.answer must be an answer object.")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        report_id = query.save_report(
            conn, context,
            project_id=project_id.strip(),
            question=question.strip(),
            answer=answer,
            knowledge_as_of=params.get("knowledge_as_of"),
        )
        conn.commit()
        return {
            "action_status": "applied",
            "object_refs": [{"kind": "query_report", "id": report_id, "revision": None}],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": [],
            "result": {"report_id": report_id},
        }
    except query.QueryError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_query_report_read(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    report_id = params.get("report_id")
    if not isinstance(report_id, str) or not report_id.strip():
        raise CapabilityError("schema_violation", "params.report_id must be a non-empty string.")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        report = query.read_report(conn, context, report_id.strip())
        conn.commit()
        warnings = (
            [f"Report is stale: {len(report.get('superseded_objects', []))} cited "
             "object(s) moved to newer revisions."]
            if report.get("stale") else []
        )
        return {
            "action_status": "applied",
            "object_refs": [
                {"kind": "query_report", "id": report_id, "revision": None}
            ],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": warnings,
            "result": report,
        }
    except query.QueryError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "inventory.query.list": _handle_inventory_query_list,
    "inventory.message.process": _handle_inventory_message_process,
    "inventory.event.resolve": _handle_inventory_event_resolve,
    "task.create": _handle_task_create,
    "task.read": _handle_task_read,
    "task.update": _handle_task_update,
    "task.interaction.answer": _handle_task_interaction_answer,
    "evidence.document.create": _handle_evidence_document_create,
    "evidence.page.ingest": _handle_evidence_page_ingest,
    "evidence.page.read": _handle_evidence_page_read,
    "evidence.page.original": _handle_evidence_page_original,
    "evidence.page.preview": _handle_evidence_page_preview,
    "evidence.pages.list": _handle_evidence_pages_list,
    "evidence.transcribe.run": _handle_evidence_transcribe_run,
    "evidence.transcript.import": _handle_evidence_transcript_import,
    "evidence.transcript.correct": _handle_evidence_transcript_correct,
    "evidence.transcription.retry": _handle_evidence_transcription_retry,
    "evidence.provider.list": _handle_evidence_provider_list,
    "memory.proposal.create": _handle_memory_proposal_create,
    "memory.proposal.read": _handle_memory_proposal_read,
    "memory.proposal.confirm_items": _handle_memory_proposal_confirm_items,
    "memory.proposal.item.update": _handle_memory_proposal_item_update,
    "memory.proposal.apply": _handle_memory_proposal_apply,
    "memory.sample.search": _handle_memory_sample_search,
    "memory.clarification.open": _handle_memory_clarification_open,
    "memory.clarification.answer": _handle_memory_clarification_answer,
    "memory.event.correct": _handle_memory_event_correct,
    "memory.event.read": _handle_memory_event_read,
    "memory.index.rebuild": _handle_memory_index_rebuild,
    "query.events.search": _handle_query_events_search,
    "query.samples.list": _handle_query_samples_list,
    "query.sample.timeline": _handle_query_sample_timeline,
    "query.sample.context": _handle_query_sample_context,
    "query.answer.build": _handle_query_answer_build,
    "query.answer.render": _handle_query_answer_render,
    "query.report.save": _handle_query_report_save,
    "query.report.read": _handle_query_report_read,
}


# ---------------------------------------------------------------------------
# helpers


def _check_role(context: auth.PrincipalContext, capability_id: str) -> None:
    allowed = CAPABILITY_REGISTRY.get(capability_id)
    if allowed is None:
        raise CapabilityError(
            "forbidden_capability", f"Capability {capability_id!r} is not implemented.", http_status=404
        )
    for role in context.memberships.values():
        if capability_id in ROLE_CAPABILITIES.get(role, set()):
            return
    raise CapabilityError(
        "forbidden_capability",
        f"Principal {context.principal_id!r} has no role granting {capability_id!r}.",
        http_status=403,
    )


def _reject_forbidden_fields(params: dict[str, Any]) -> None:
    present = [field for field in FORBIDDEN_V2_FIELDS if field in params]
    if present:
        raise CapabilityError(
            "schema_violation",
            f"Field(s) {present} are derived from the authenticated principal "
            "and must not be sent by clients.",
        )


def _require_request_hash_input(params: dict[str, Any]) -> None:
    if not isinstance(params, dict):
        raise CapabilityError("schema_violation", "params must be an object.")


def _check_expected_revision(
    conn: sqlite3.Connection, item_id: str | None, expected_revision: int
) -> None:
    if item_id is None:
        return
    row = conn.execute(
        "SELECT revision FROM chemicals WHERE chemical_id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise CapabilityError("ambiguous_entity", f"Unknown chemical {item_id!r}.", http_status=409)
    if row["revision"] != expected_revision:
        raise CapabilityError(
            "revision_conflict",
            f"Stock revision moved: expected {expected_revision}, current {row['revision']}.",
            http_status=409,
        )


def _current_revision(conn: sqlite3.Connection, item_id: str) -> int:
    """Read the balance revision after an apply (S07: bumped by the apply)."""
    row = conn.execute(
        "SELECT revision FROM chemicals WHERE chemical_id = ?", (item_id,)
    ).fetchone()
    return int(row["revision"]) if row is not None else 0


def _linked_user_id(
    conn: sqlite3.Connection, context: auth.PrincipalContext, warnings: list[str]
) -> str | None:
    row = conn.execute(
        "SELECT user_id FROM principal_user_links WHERE principal_id = ?",
        (context.principal_id,),
    ).fetchone()
    if row is None:
        warnings.append(
            "no principal_user_links row; business attribution carried by "
            "operation_results.principal_id only"
        )
        return None
    return row["user_id"]


def _check_task_visibility(
    context: auth.PrincipalContext, task: dict[str, Any], *, mutate: bool = False
) -> None:
    if task["principal_id"] == context.principal_id:
        return
    project_id = task.get("project_id")
    if project_id is not None:
        context.require_project(project_id, "member" if mutate else "viewer")
        return
    raise CapabilityError(
        "forbidden_scope", "Task is not visible to this principal.", http_status=403
    )


def _operation_storage_namespace(
    context: auth.PrincipalContext, namespace: str, params: dict[str, Any]
) -> str:
    """Server-derived idempotency scope.

    Binds the initiating authority (the authenticated principal) and the
    immutable project context requested by the command. A different subject
    or project scope can never hit another subject's receipt even when the
    client uses the same namespace + operation id.
    """
    project_context: list[str] = []
    if isinstance(params.get("project_id"), str):
        project_context.append(params["project_id"])
    if isinstance(params.get("project_ids"), list):
        project_context.extend(
            sorted(str(p) for p in params["project_ids"] if isinstance(p, str))
        )
    digest = hashlib.sha256(
        json.dumps(
            {
                "principal": context.principal_id,
                "namespace": namespace,
                "projects": sorted(set(project_context)),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return f"opscope:{digest}"


def _replay_object_project(conn, kind: str, object_id: str) -> str | None:
    """Resolve ownership from stored identities, not a cached payload's claim."""
    kind = {"event": "experiment_event", "memory_event": "experiment_event",
            "event_proposal": "experiment_event", "proposal": "memory_proposal",
            "evidence_object": "evidence"}.get(kind, kind)
    if kind == "experiment_event":
        return _event_project_id(conn, object_id)
    tables = {
        "memory_proposal": ("memory_proposals", "proposal_id"),
        "sample": ("samples", "sample_id"), "task": ("tasks", "task_id"),
        "evidence_document": ("evidence_documents", "document_id"),
        "evidence": ("evidence_objects", "evidence_id"),
        "query_report": ("query_reports", "report_id"),
        "resource": ("resources", "resource_id"),
        "resource_booking": ("resource_bookings", "booking_id"),
        "research_milestone": ("research_milestones", "milestone_id"),
        "external_result": ("external_results", "result_id"),
        "memory_artifact": ("memory_artifacts", "artifact_id"),
        "inventory_subtask": ("inventory_subtasks", "subtask_id"),
        "adjustment_proposal": ("inventory_adjustment_proposals", "adjustment_id"),
        "transaction": ("inventory_transactions", "transaction_id"),
    }
    if kind in tables:
        table, column = tables[kind]
        row = conn.execute(f"SELECT project_id FROM {table} WHERE {column} = ?", (object_id,)).fetchone()
    elif kind == "evidence_page":
        row = conn.execute("SELECT d.project_id FROM evidence_pages p JOIN evidence_documents d "
                           "ON d.document_id=p.document_id WHERE p.page_id=?", (object_id,)).fetchone()
    elif kind == "transcript_run":
        row = conn.execute("SELECT d.project_id FROM transcript_runs r JOIN evidence_pages p "
                           "ON p.page_id=r.page_id JOIN evidence_documents d ON d.document_id=p.document_id "
                           "WHERE r.run_id=?", (object_id,)).fetchone()
    elif kind == "transcript_block":
        row = conn.execute("SELECT d.project_id FROM transcript_blocks b JOIN transcript_runs r "
                           "ON r.run_id=b.run_id JOIN evidence_pages p ON p.page_id=r.page_id "
                           "JOIN evidence_documents d ON d.document_id=p.document_id WHERE b.block_id=?",
                           (object_id,)).fetchone()
    elif kind in {"agent_run", "workflow_run"}:
        table, column = ("agent_runs", "run_id") if kind == "agent_run" else ("workflow_runs", "workflow_run_id")
        row = conn.execute(f"SELECT t.project_id FROM {table} r JOIN tasks t "
                           f"ON t.task_id=r.task_id WHERE r.{column}=?", (object_id,)).fetchone()
    elif kind == "milestone_criterion":
        row = conn.execute("SELECT m.project_id FROM milestone_criteria c JOIN research_milestones m "
                           "ON m.milestone_id=c.milestone_id WHERE c.criterion_id=?", (object_id,)).fetchone()
    else:
        return None
    return row["project_id"] if row is not None else None


def _replay_project_ids(conn, *values) -> set[str]:
    """Collect structural scope and re-resolve referenced object ownership.

    Old envelopes often left outer project_id empty while returning a nested
    proposal, revision or source. Checking just that outer field is insufficient.
    """
    projects: set[str] = set()
    objects: set[tuple[str, str]] = set()
    identity_keys = {
        "event_id": "experiment_event", "source_event_id": "experiment_event",
        "proposal_id": "memory_proposal", "page_id": "evidence_page",
        "document_id": "evidence_document", "evidence_id": "evidence",
        "sample_id": "sample", "task_id": "task", "report_id": "query_report",
        "resource_id": "resource", "booking_id": "resource_booking",
        "milestone_id": "research_milestone", "criterion_id": "milestone_criterion",
        "workflow_run_id": "workflow_run", "subtask_id": "inventory_subtask",
        "adjustment_id": "adjustment_proposal", "transaction_id": "transaction",
    }
    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key == "project_id" and isinstance(item, str) and item:
                    projects.add(item)
                elif key in {"project_ids", "scope", "source_project_ids"} and isinstance(item, list):
                    projects.update(project for project in item if isinstance(project, str) and project)
                if key in identity_keys and isinstance(item, str) and item:
                    objects.add((identity_keys[key], item))
                elif key == "sample_ids" and isinstance(item, list):
                    objects.update(("sample", sid) for sid in item if isinstance(sid, str) and sid)
                elif key == "run_id" and isinstance(item, str) and item:
                    # Run ids occur in two separate Core domains.
                    objects.update((kind, item) for kind in ("agent_run", "transcript_run"))
                visit(item)
            kind = value.get("kind") or value.get("object_type") or value.get("entity_type")
            object_id = value.get("id") or value.get("object_id") or value.get("entity_id")
            if isinstance(kind, str) and isinstance(object_id, str) and object_id:
                objects.add((kind, object_id))
    for value in values:
        visit(value)
    for kind, object_id in objects:
        project = _replay_object_project(conn, kind, object_id)
        if project:
            projects.add(project)
    return projects


def _authorize_replay(
    db_path: str, context: auth.PrincipalContext, envelope: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """Re-check CURRENT authority before returning a stored receipt."""
    if envelope.get("principal_id") != context.principal_id:
        raise CapabilityError(
            "forbidden_scope",
            "Stored operation receipt belongs to a different principal.",
            http_status=403,
        )
    conn = connect_read_only(db_path)
    try:
        for project_id in _replay_project_ids(conn, envelope, params):
            context.require_project(project_id, "viewer")
    except auth.AuthError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    finally:
        conn.close()
    return envelope


def _load_operation_result(
    db_path: str, namespace: str, operation_id: str, request_hash: str
) -> dict[str, Any] | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM operation_results WHERE namespace = ? AND operation_id = ?",
            (namespace, operation_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        raise CapabilityError(
            "idempotency_conflict",
            "operation_id was already used with a different request.",
            http_status=409,
        )
    envelope = json.loads(row["response_json"])
    envelope["idempotent_replay"] = True
    return envelope


def _store_operation_result(
    db_path: str,
    *,
    namespace: str,
    operation_id: str,
    context: auth.PrincipalContext,
    capability_id: str,
    request_hash: str,
    envelope: dict[str, Any],
    entity_refs: list[dict[str, Any]],
) -> None:
    entity = entity_refs[0] if entity_refs else None
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO operation_results
              (namespace, operation_id, principal_id, capability_id, request_hash,
               response_json, entity_type, entity_id, revision_after,
               authority_scope, project_context_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                namespace,
                operation_id,
                context.principal_id,
                capability_id,
                request_hash,
                json.dumps(envelope, sort_keys=True, ensure_ascii=False),
                entity["kind"] if entity else None,
                entity["id"] if entity else None,
                entity.get("revision") if entity else None,
                context.principal_id,
                json.dumps(
                    {
                        "project_id": envelope.get("project_id"),
                        "scope": envelope.get("scope"),
                    },
                    sort_keys=True,
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _request_hash(capability_id: str, params: dict[str, Any]) -> str:
    # operation_id is the idempotency key itself (it names the row), so it
    # must NOT participate in the request hash: the agent gateway injects it
    # into params after auth, and a replay must hash the business request
    # only — otherwise the same op id with the same business request would
    # falsely conflict (S06 live integration).
    business = {
        k: v for k, v in params.items()
        if k != "operation_id" and not k.startswith("_")
    }
    canonical = json.dumps(
        {"capability_id": capability_id, "params": business},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _finalize_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    full = {
        "schema_version": "capability_envelope_v1",
        "idempotent_replay": False,
        "object_refs": [],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "error": None,
        "result": None,
        "principal_id": None,
        "project_id": None,
        "namespace": "default",
        **envelope,
    }
    try:
        validate_payload_against_schema(full, load_named_schema(ENVELOPE_SCHEMA_NAME))
    except SchemaViolation as exc:
        raise CapabilityError("schema_violation", f"Envelope contract violation: {exc}") from exc
    return full


def _optional_int(params: dict[str, Any], field: str) -> int | None:
    value = params.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CapabilityError("schema_violation", f"params.{field} must be an integer.")
    return value


# ---------------------------------------------------------------------------
# S06: agent host handlers (imported lazily — agents.py imports this module)


_ASSISTANT_STRONG_QUERY_RE = re.compile(
    r"(多少|什么|哪些|是否|为什么|如何|\?|？|how\s+much|what|which)",
    re.IGNORECASE,
)
_ASSISTANT_QUERY_RE = re.compile(
    r"(查询|库存|余量|用量|情况|status|stock|remaining)",
    re.IGNORECASE,
)
_ASSISTANT_INVENTORY_RE = re.compile(
    r"(取用|用了|使用|称取|加入|添加|消耗|used|use\s|consume|add\s)",
    re.IGNORECASE,
)
_ASSISTANT_BOOKING_RE = re.compile(
    r"(预约|排期|时段|占用|instrument|booking|reserve|slot)",
    re.IGNORECASE,
)
_MAX_ASSISTANT_ATTACHMENTS = 5
_MAX_ASSISTANT_ATTACHMENT_BYTES = 20 * 1024 * 1024


def _assistant_nested(
    db_path: str,
    context: auth.PrincipalContext,
    capability_id: str,
    params: dict[str, Any],
    *,
    operation_id: str,
    services: ServiceSettings,
) -> dict[str, Any]:
    """Run one domain capability inside the same Core authority path."""
    return invoke_capability(
        db_path,
        context,
        capability_id,
        params,
        namespace="assistant",
        operation_id=operation_id,
        services=services,
    )


def _assistant_classify(text: str, attachments: list[dict[str, Any]]) -> tuple[str, str]:
    if attachments:
        return "capture_page", "b_capture"
    # A question marker wins over a usage verb ("取用多少？" is a query).
    if _ASSISTANT_STRONG_QUERY_RE.search(text):
        return "query_answer", "b_query"
    if _ASSISTANT_INVENTORY_RE.search(text):
        return "record_inventory", "alice_inventory"
    if _ASSISTANT_BOOKING_RE.search(text):
        return "plan_experiment", "planner"
    if _ASSISTANT_QUERY_RE.search(text):
        return "query_answer", "b_query"
    return "clarify", "orchestrator"


def _handle_assistant_task_process(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
    services: ServiceSettings,
) -> dict[str, Any]:
    """One unified natural-language task entry for the web workbench.

    This is a controlled write path, not an autonomous approver:
    - queries use the cited answer builder;
    - low-risk inventory commands follow the existing policy (may apply);
    - uploads persist originals and (for text) create a proposal awaiting
      human confirmation;
    - booking requests only read resources/bookings and ask for a formal form.
    """
    project_id = params.get("project_id")
    text = params.get("text")
    attachments = params.get("attachments") or []
    session_id = params.get("session_id")
    intent_override = params.get("intent_override")
    allowed_intents = {
        "query_answer", "record_inventory", "capture_page",
        "plan_experiment", "clarify",
    }
    if intent_override is not None and intent_override not in allowed_intents:
        raise CapabilityError("schema_violation", "params.intent_override is invalid.")
    if not isinstance(project_id, str) or not project_id.strip():
        raise CapabilityError("schema_violation", "params.project_id is required.")
    if not isinstance(text, str) or not text.strip():
        raise CapabilityError("schema_violation", "params.text must be a non-empty string.")
    if len(text) > 4000:
        raise CapabilityError("schema_violation", "params.text is too long (max 4000).")
    if not isinstance(attachments, list):
        raise CapabilityError("schema_violation", "params.attachments must be an array.")
    if len(attachments) > _MAX_ASSISTANT_ATTACHMENTS:
        raise CapabilityError(
            "schema_violation",
            f"At most {_MAX_ASSISTANT_ATTACHMENTS} attachments are allowed.",
        )
    if session_id is not None and (not isinstance(session_id, str) or len(session_id) > 128):
        raise CapabilityError("schema_violation", "params.session_id is invalid.")
    session_id = session_id or f"sess_{uuid.uuid4().hex[:16]}"
    context.require_project(project_id, "member")

    validated: list[dict[str, Any]] = []
    for index, item in enumerate(attachments):
        if not isinstance(item, dict):
            raise CapabilityError("schema_violation", "attachment items must be objects.")
        data_b64 = item.get("data_b64")
        if not isinstance(data_b64, str) or not data_b64:
            raise CapabilityError("schema_violation", "attachment.data_b64 is required.")
        try:
            decoded = base64.b64decode(data_b64, validate=True)
        except Exception as exc:
            raise CapabilityError(
                "schema_violation", f"attachment {index} is not valid base64."
            ) from exc
        if not decoded:
            raise CapabilityError("schema_violation", "empty attachment is not allowed.")
        if len(decoded) > _MAX_ASSISTANT_ATTACHMENT_BYTES:
            raise CapabilityError(
                "schema_violation",
                f"attachment {index} exceeds {_MAX_ASSISTANT_ATTACHMENT_BYTES} bytes.",
            )
        validated.append({
            "name": str(item.get("name") or f"upload-{index + 1}"),
            "mime": str(item.get("mime") or "application/octet-stream"),
            "data_b64": data_b64,
        })

    operation_id = str(params.get("_operation_id") or f"assistant_{uuid.uuid4().hex[:16]}")
    classification = "rules"
    if validated:
        # Uploads are always the capture workflow; a model may not route
        # file bytes into a query/inventory path.
        intent, agent = "capture_page", "b_capture"
    elif intent_override is not None:
        intent = intent_override
        agent = {
            "query_answer": "b_query",
            "record_inventory": "alice_inventory",
            "capture_page": "b_capture",
            "plan_experiment": "planner",
            "clarify": "orchestrator",
        }[intent]
        classification = "model"
    else:
        intent, agent = _assistant_classify(text.strip(), validated)
    object_refs: list[dict[str, Any]] = []
    warnings: list[str] = []
    data: dict[str, Any] = {}
    action_status = "applied"

    if intent == "query_answer":
        envelope = _assistant_nested(
            db_path, context, "query.answer.build",
            {"project_ids": [project_id], "question": text.strip()},
            operation_id=f"{operation_id}:answer", services=services,
        )
        answer = envelope.get("result") or {}
        data = answer
        result_text = answer.get("rendered_text") or "没有可用的引用式回答。"
        if answer.get("status") in ("needs_review", "unknown", "partial"):
            result_text = f"[{answer.get('status')}] {result_text}"
        object_refs.extend(
            {"kind": c.get("object_type"), "id": c.get("object_id"),
             "revision": c.get("revision")}
            for c in answer.get("claims", [])
            if c.get("object_id")
        )
    elif intent == "record_inventory":
        envelope = _assistant_nested(
            db_path, context, "inventory.message.process",
            {"project_id": project_id, "raw_text": text.strip()},
            operation_id=f"{operation_id}:inventory", services=services,
        )
        action_status = envelope.get("action_status") or "applied"
        data = {"action_status": action_status, "inventory_result": envelope.get("result")}
        object_refs.extend(envelope.get("object_refs") or [])
        if action_status == "applied":
            result_text = "Alice 已记录并应用这次库存操作。"
        elif action_status in ("clarification_needed", "proposed"):
            result_text = "Alice 已记录，但需要人工确认；当前不会自动扣账。"
        else:
            result_text = f"Alice 未能应用该操作（{action_status}）。"
    elif intent == "capture_page":
        notebook_code = f"WEB-{uuid.uuid4().hex[:8].upper()}"
        document = _assistant_nested(
            db_path, context, "evidence.document.create",
            {"project_id": project_id, "notebook_code": notebook_code,
             "title": text.strip()[:80] or "Web upload",
             "source_channel": "web_assistant"},
            operation_id=f"{operation_id}:document", services=services,
        )
        document_id = (document.get("result") or {}).get("document_id")
        object_refs.append({"kind": "evidence_document", "id": document_id, "revision": None})
        pages: list[dict[str, Any]] = []
        proposals: list[dict[str, Any]] = []
        for index, item in enumerate(validated):
            data_url = f"data:{item['mime']};base64,{item['data_b64']}"
            page_env = _assistant_nested(
                db_path, context, "evidence.page.ingest",
                {"document_id": document_id, "page_order": index + 1,
                 "content_data_url": data_url, "filename": item["name"]},
                operation_id=f"{operation_id}:page:{index}", services=services,
            )
            page = (page_env.get("result") or {}).get("page") or {}
            pages.append(page)
            object_refs.append(
                {"kind": "evidence_page", "id": page.get("page_id"), "revision": None}
            )
            mime = page.get("mime") or item["mime"]
            if mime not in ("text/plain", "text/markdown"):
                warnings.append(
                    f"parser_not_configured:{page.get('page_id')}:{mime}:"
                    "original saved; automatic transcription/parsing is not configured"
                )
                continue
            transcribe_env = _assistant_nested(
                db_path, context, "evidence.transcribe.run",
                {"page_id": page.get("page_id"), "provider": "text_input"},
                operation_id=f"{operation_id}:transcribe:{index}", services=services,
            )
            run = transcribe_env.get("result") or {}
            if run.get("status") != "succeeded":
                warnings.append(f"transcription_failed:{page.get('page_id')}")
                continue
            conn = connect(db_path)
            try:
                row = conn.execute(
                    "SELECT active_revision FROM page_transcripts WHERE page_id = ?",
                    (page.get("page_id"),),
                ).fetchone()
            finally:
                conn.close()
            revision = int(row["active_revision"]) if row is not None else 1
            proposal_env = _assistant_nested(
                db_path, context, "memory.proposal.create",
                {"project_id": project_id, "page_id": page.get("page_id"),
                 "transcript_revision": revision, "profile_id": "chemistry_notebook",
                 "profile_version": "1.0", "operation_id": f"{operation_id}:proposal:{index}"},
                operation_id=f"{operation_id}:proposal:{index}", services=services,
            )
            proposal = (proposal_env.get("result") or {}).get("proposal") or {}
            if proposal.get("proposal_id"):
                proposals.append(proposal)
                object_refs.append(
                    {"kind": "memory_proposal", "id": proposal["proposal_id"],
                     "revision": proposal.get("revision")}
                )
        data = {"document_id": document_id, "pages": pages, "proposals": proposals}
        if proposals:
            result_text = (
                f"B 已保存 {len(pages)} 个页面并整理出 {len(proposals)} 份知识记录；"
                "资料默认采纳，可在知识与样品中查看，并随时质疑纠错。"
            )
        else:
            result_text = (
                f"原件已保存（{len(pages)} 个页面）。"
                "当前没有可自动转写的文本页；该文件类型的自动解析/OCR 尚未配置，"
                "可人工转写或稍后接入对应解析器。"
            )
    elif intent == "plan_experiment":
        resources_env = _assistant_nested(
            db_path, context, "resource.list",
            {"project_ids": [project_id]},
            operation_id=f"{operation_id}:resources", services=services,
        )
        bookings_env = _assistant_nested(
            db_path, context, "resource.booking.status",
            {"project_ids": [project_id]},
            operation_id=f"{operation_id}:bookings", services=services,
        )
        data = {
            "resources": (resources_env.get("result") or {}).get("resources", []),
            "bookings": (bookings_env.get("result") or {}).get("bookings", []),
        }
        result_text = (
            "planner 已读取当前项目的资源与预约记录。正式预约需要明确资源、"
            "起止时间和时区，并在仪器与预约页提交；本次没有创建预约。"
        )
    else:
        action_status = "clarification_needed"
        result_text = (
            "我还不能确定要交给哪个 Agent。请说明是查询记录、记录库存取用、"
            "上传实验记录，还是预约仪器。"
        )

    warnings = list(dict.fromkeys(warnings))
    persisted_user = None
    persisted_reply = None
    try:
        conn = connect(db_path)
        try:
            persisted_user = tasks.record_typed_message(
                conn,
                message_kind="command",
                message_type="assistant.user_message",
                payload={"intent": "assistant.task.process", "text": text.strip(),
                         "attachments": [item["name"] for item in validated]},
                correlation_id=session_id,
            )
            persisted_reply = tasks.record_typed_message(
                conn,
                message_kind="domain_event",
                message_type="assistant.reply",
                payload={"occurred_ref": persisted_user["message_id"],
                         "intent": intent, "agent": agent,
                         "status": action_status, "text": result_text},
                correlation_id=session_id,
                causation_id=persisted_user["message_id"],
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        warnings.append("session_persist_failed")

    return {
        "action_status": action_status,
        "project_id": project_id,
        "object_refs": object_refs,
        "evidence_refs": [],
        "unknowns": [],
        "warnings": warnings,
        "result": {
            "session_id": session_id,
            "message_id": persisted_reply["message_id"] if persisted_reply else None,
            "intent": intent,
            "agent": agent,
            "status": action_status,
            "text": result_text,
            "data": data,
            "object_refs": object_refs,
            "warnings": warnings,
            "classification": classification,
        },
    }


def _handle_assistant_agent_start(db_path, context, params):
    from . import orchestration
    from .agents import AgentHostError
    try:
        result = orchestration.session_start(db_path, context, params)
    except (AgentHostError, tasks.TaskError) as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    return {"action_status": "proposed", "project_id": params["project_id"], "result": result,
            "object_refs": [{"kind": "task", "id": result["task_id"], "revision": None}],
            "evidence_refs": [], "unknowns": [], "warnings": []}


def _handle_assistant_agent_finish(db_path, context, params):
    from . import orchestration
    from .agents import AgentHostError
    try:
        result = orchestration.session_finish(db_path, context, params)
    except (AgentHostError, tasks.TaskError) as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    # A completed conversation is not proof that any business write applied.
    action_status = "clarification_needed" if result["status"] in ("clarification_needed", "needs_review") else "proposed"
    return {"action_status": action_status, "project_id": params["project_id"], "result": result,
            "object_refs": result.get("object_refs") or [], "evidence_refs": [],
            "unknowns": [], "warnings": result.get("warnings") or []}


def _handle_task_dispatch(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    from . import agents as agents_mod

    intent = params.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        raise CapabilityError("schema_violation", "params.intent must be a non-empty string.")
    project_id = params.get("project_id")
    if project_id is not None and not isinstance(project_id, str):
        raise CapabilityError("schema_violation", "params.project_id must be a string.")
    title = params.get("title")
    if title is not None and not isinstance(title, str):
        raise CapabilityError("schema_violation", "params.title must be a string.")
    input_payload = params.get("input") or {}
    if not isinstance(input_payload, dict):
        raise CapabilityError("schema_violation", "params.input must be an object.")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        dispatch = agents_mod.dispatch(
            conn,
            intent=intent,
            context=context,
            project_id=project_id,
            title=title,
            input_payload=input_payload,
        )
        conn.commit()
    except agents_mod.AgentHostError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except tasks.TaskError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "object_refs": [
            {"kind": "task", "id": dispatch["task_id"], "revision": None},
            {"kind": "agent_run", "id": dispatch["run_id"], "revision": None},
            {"kind": "workflow_run", "id": dispatch["workflow_run_id"], "revision": None},
        ],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [
            "run_token is shown exactly once; store it with the worker process"
        ],
        "result": dispatch,
    }


def _handle_agent_registry_load(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
    services: ServiceSettings,
) -> dict[str, Any]:
    from . import agents as agents_mod

    config_dir = params.get("config_dir")
    if config_dir is not None and not isinstance(config_dir, str):
        raise CapabilityError("schema_violation", "params.config_dir must be a string.")
    root = services.manifest_dir
    if config_dir:
        candidate = Path(config_dir)
        try:
            candidate.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise CapabilityError(
                "forbidden_scope",
                f"config_dir must stay inside the admin manifest directory {root}.",
                http_status=403,
            ) from exc
        target = candidate
    else:
        target = root
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        summary = agents_mod.load_registry(conn, target)
        conn.commit()
    except agents_mod.AgentHostError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "object_refs": [],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [f"{len(summary['invalid'])} manifest file(s) rejected"] if summary["invalid"] else [],
        "result": summary,
    }


def _handle_agent_registry_list(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    from . import agents as agents_mod

    conn = connect(db_path)
    try:
        manifests = agents_mod.list_manifests(conn)
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "object_refs": [],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": {"manifests": manifests},
    }


def _handle_agent_registry_set_enabled(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    from . import agents as agents_mod

    agent_id = params.get("agent_id")
    version = params.get("version")
    enabled = params.get("enabled")
    for name, value, kind in (
        ("agent_id", agent_id, "non-empty string"),
        ("version", version, "non-empty string"),
    ):
        if not isinstance(value, str) or not value.strip():
            raise CapabilityError("schema_violation", f"params.{name} must be a {kind}.")
    if not isinstance(enabled, bool):
        raise CapabilityError("schema_violation", "params.enabled must be a boolean.")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = agents_mod.set_manifest_enabled(
            conn, agent_id=agent_id, version=version, enabled=enabled
        )
        conn.commit()
    except agents_mod.AgentHostError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "object_refs": [{"kind": "agent_manifest", "id": f"{agent_id}@{version}", "revision": None}],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": result,
    }


def _handle_run_claim(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    from . import agents as agents_mod

    run_id = params.get("run_id")
    worker_id = params.get("worker_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise CapabilityError("schema_violation", "params.run_id must be a non-empty string.")
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise CapabilityError("schema_violation", "params.worker_id must be a non-empty string.")
    lease_seconds = _optional_int(params, "lease_seconds") or 120
    if lease_seconds <= 0 or lease_seconds > 3600:
        raise CapabilityError("schema_violation", "params.lease_seconds must be 1..3600.")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        run = agents_mod.claim_run(conn, run_id=run_id, worker_id=worker_id, lease_seconds=lease_seconds)
        conn.commit()
    except agents_mod.AgentHostError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except tasks.TaskError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "object_refs": [{"kind": "agent_run", "id": run_id, "revision": None}],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": _run_contract(run),
    }


def _handle_run_heartbeat(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    from . import agents as agents_mod

    run_id = params.get("run_id")
    claim_epoch = _optional_int(params, "claim_epoch")
    if not isinstance(run_id, str) or not run_id.strip():
        raise CapabilityError("schema_violation", "params.run_id must be a non-empty string.")
    if claim_epoch is None:
        raise CapabilityError("schema_violation", "params.claim_epoch is required.")
    lease_seconds = _optional_int(params, "lease_seconds") or 120
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        run = agents_mod.heartbeat_run(
            conn, run_id=run_id, claim_epoch=claim_epoch, lease_seconds=lease_seconds
        )
        conn.commit()
    except agents_mod.AgentHostError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "object_refs": [{"kind": "agent_run", "id": run_id, "revision": None}],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": _run_contract(run),
    }


def _handle_run_complete(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    from . import agents as agents_mod

    run_id = params.get("run_id")
    status = params.get("status")
    claim_epoch = _optional_int(params, "claim_epoch")
    if not isinstance(run_id, str) or not run_id.strip():
        raise CapabilityError("schema_violation", "params.run_id must be a non-empty string.")
    if status not in ("succeeded", "failed", "cancelled"):
        raise CapabilityError(
            "schema_violation", 'params.status must be "succeeded"|"failed"|"cancelled".'
        )
    if claim_epoch is None:
        raise CapabilityError(
            "schema_violation", "params.claim_epoch is required (fencing token)."
        )
    result = params.get("result") or {}
    if not isinstance(result, dict):
        raise CapabilityError("schema_violation", "params.result must be an object.")
    error_code = params.get("error_code")
    if error_code is not None and not isinstance(error_code, str):
        raise CapabilityError("schema_violation", "params.error_code must be a string.")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        run = agents_mod.finish_run(
            conn, run_id=run_id, status=status, claim_epoch=claim_epoch,
            result=result, error_code=error_code,
        )
        conn.commit()
    except agents_mod.AgentHostError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "action_status": "applied" if status == "succeeded" else "rejected",
        "object_refs": [{"kind": "agent_run", "id": run_id, "revision": None}],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": _run_contract(run),
    }


def _handle_run_recover(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    from . import agents as agents_mod

    lease_buffer_s = _optional_int(params, "lease_buffer_s") or 0
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        recovered = agents_mod.recover_expired_runs(conn, lease_buffer_s=lease_buffer_s)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "object_refs": [{"kind": "agent_run", "id": item["run_id"], "revision": None} for item in recovered],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": {"recovered": recovered},
    }


def _handle_run_list(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    from . import agents as agents_mod

    agent_id = params.get("agent_id")
    if agent_id is not None and not isinstance(agent_id, str):
        raise CapabilityError("schema_violation", "params.agent_id must be a string.")
    conn = connect_read_only(db_path)
    try:
        runs = agents_mod.list_claimable_runs(conn, agent_id=agent_id)
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "object_refs": [{"kind": "agent_run", "id": run["run_id"], "revision": None} for run in runs],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": {"runs": [_run_contract(run) for run in runs]},
    }


def _handle_delivery_process(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    from . import deliveries as deliveries_mod

    adapter_kind = params.get("adapter") or "echo"
    if adapter_kind == "echo":
        adapter = deliveries_mod.EchoAdapter()
    elif adapter_kind == "fault_local":
        adapter = deliveries_mod.FaultInjectionAdapter(
            drop_first_n=_optional_int(params, "drop_first_n") or 0,
            timeout_first_n=_optional_int(params, "timeout_first_n") or 0,
        )
    else:
        raise CapabilityError(
            "schema_violation", 'params.adapter must be "echo" or "fault_local".'
        )
    retry_backoff_s = _optional_int(params, "retry_backoff_s") or 60
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        processed = deliveries_mod.process_outbox_once(
            conn, adapter=adapter, retry_backoff_s=retry_backoff_s
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "object_refs": [],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": processed,
    }


def _run_contract(run: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "run_id", "task_id", "agent_role", "agent_id", "manifest_version", "status",
        "worker_id", "claim_epoch", "lease_expires_at", "attempts", "used_tool_calls",
        "budget_tool_calls", "trace_id", "error_code", "started_at", "finished_at",
    )
    payload = {key: run.get(key) for key in keep}
    if run.get("result") is not None:
        payload["result"] = run["result"]
    return payload


# ---------------------------------------------------------------------------
# S09 extension domains (plan §15): C resources/bookings, D milestones,
# external results.


def _require_project_param(context: auth.PrincipalContext, project_id: str | None) -> str:
    if not isinstance(project_id, str) or not project_id.strip():
        raise CapabilityError("schema_violation", "params.project_id is required.")
    context.require_project(project_id, "member")
    return project_id


def _booking_envelope(
    result: dict[str, Any]
) -> dict[str, Any]:
    warnings = []
    if result.get("conflict_detected"):
        warnings.append("planned slot overlaps confirmed bookings; confirm will re-check")
    if result.get("idempotent_replay"):
        warnings.append("idempotent replay")
    return {
        "action_status": "applied"
        if result.get("status") in ("confirmed", "cancelled")
        else "proposed",
        "project_id": result.get("project_id"),
        "object_refs": [
            {"kind": "resource_booking", "id": result["booking_id"], "revision": None}
        ],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": warnings,
        "result": result,
    }


def _handle_resource_register(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    name = params.get("name")
    kind = params.get("kind")
    if not isinstance(name, str) or not name.strip():
        raise CapabilityError("schema_violation", "params.name must be a non-empty string.")
    if not isinstance(kind, str):
        raise CapabilityError("schema_violation", "params.kind must be a string.")
    capacity = _optional_int(params, "capacity")
    if capacity is None:
        capacity = 1
    project_id = params.get("project_id")
    if project_id is not None:
        if not isinstance(project_id, str) or not project_id.strip():
            raise CapabilityError("schema_violation", "params.project_id must be a string.")
        context.require_project(project_id, "owner")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        created = resources.register_resource(
            conn, name=name.strip(), kind=kind, project_id=project_id,
            capacity=capacity, created_by=context.principal_id,
        )
        conn.commit()
    except resources.ResourceError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "project_id": created["project_id"],
        "object_refs": [{"kind": "resource", "id": created["resource_id"], "revision": None}],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": created,
    }


def _handle_resource_list(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    project_ids = params.get("project_ids")
    if project_ids is None:
        if not context.has_any_membership():
            raise CapabilityError(
                "forbidden_scope", "Reads require at least one project membership.",
                http_status=403,
            )
        project_ids = auth.visible_project_ids(context)
    else:
        if not isinstance(project_ids, list) or not all(
            isinstance(p, str) and p.strip() for p in project_ids
        ):
            raise CapabilityError(
                "schema_violation", "params.project_ids must be a list of project ids."
            )
        outside = [p for p in project_ids if p not in auth.visible_project_ids(context)]
        if outside:
            raise CapabilityError(
                "forbidden_scope", f"No membership for projects {outside}.", http_status=403
            )
    kind = _optional_str(params, "kind")
    conn = connect_read_only(db_path)
    try:
        rows = resources.list_resources(conn, project_ids=project_ids, kind=kind)
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "project_id": None,
        "object_refs": [],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": {"resources": rows},
    }


def _handle_resource_availability(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    resource_id = _optional_str(params, "resource_id")
    if resource_id is None:
        raise CapabilityError("schema_violation", "params.resource_id is required.")
    window_start = _optional_str(params, "window_start")
    window_end = _optional_str(params, "window_end")
    if window_start is None or window_end is None:
        raise CapabilityError(
            "schema_violation", "params.window_start and params.window_end are required."
        )
    duration_minutes = _optional_int(params, "duration_minutes") or 60
    conn = connect_read_only(db_path)
    try:
        resource = resources.get_resource(conn, resource_id)
        if resource["project_id"] is not None:
            context.require_project(resource["project_id"], "viewer")
        candidates = resources.find_candidate_slots(
            conn, resource_id=resource_id, window_start=window_start,
            window_end=window_end, duration_minutes=duration_minutes,
        )
    except resources.ResourceError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "project_id": resource["project_id"],
        "object_refs": [{"kind": "resource", "id": resource_id, "revision": None}],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": {"resource_id": resource_id, "candidates": candidates},
    }


def _handle_resource_booking_propose(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    resource_id = _optional_str(params, "resource_id")
    if resource_id is None:
        raise CapabilityError("schema_violation", "params.resource_id is required.")
    slot_start = _optional_str(params, "slot_start")
    slot_end = _optional_str(params, "slot_end")
    if slot_start is None or slot_end is None:
        raise CapabilityError(
            "schema_violation", "params.slot_start and params.slot_end are required."
        )
    project_id = _require_project_param(context, params.get("project_id"))
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        resource = resources.get_resource(conn, resource_id)
        if resource["project_id"] is not None:
            context.require_project(resource["project_id"], "viewer")
        proposed = resources.propose_booking(
            conn, resource_id=resource_id, project_id=project_id,
            slot_start=slot_start, slot_end=slot_end,
            purpose_note=_optional_str(params, "purpose_note"),
            plan_summary=_optional_str(params, "plan_summary"),
            task_id=_optional_str(params, "task_id"),
            created_by=context.principal_id,
        )
        proposed["project_id"] = project_id
        conn.commit()
    except resources.ResourceError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    warnings = []
    if proposed.get("conflict_detected"):
        warnings.append(
            "planned slot overlaps confirmed bookings; confirmation will re-check")
    return {
        "action_status": "proposed",
        "project_id": project_id,
        "object_refs": [
            {"kind": "resource_booking", "id": proposed["booking_id"], "revision": None}
        ],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": warnings,
        "result": proposed,
    }


def _handle_resource_booking_confirm(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    booking_id = _optional_str(params, "booking_id")
    if booking_id is None:
        raise CapabilityError("schema_violation", "params.booking_id is required.")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT project_id, resource_id FROM resource_bookings WHERE booking_id = ?",
            (booking_id,),
        ).fetchone()
        if row is None:
            raise CapabilityError("invalid_state", f"Unknown booking {booking_id!r}.", http_status=404)
        if row["project_id"]:
            context.require_project(row["project_id"], "member")
        resource = resources.get_resource(conn, row["resource_id"])
        if resource["project_id"] is not None:
            context.require_project(resource["project_id"], "member")
        result = resources.confirm_booking(conn, booking_id=booking_id, confirmed_by=context.principal_id)
        result["project_id"] = row["project_id"]
        conn.commit()
    except resources.ResourceError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    warnings = []
    if result.get("idempotent_replay"):
        warnings.append("idempotent replay")
    return {
        "action_status": "applied",
        "project_id": result.get("project_id"),
        "object_refs": [{"kind": "resource_booking", "id": booking_id, "revision": None}],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": warnings,
        "result": result,
    }


def _handle_resource_booking_cancel(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    booking_id = _optional_str(params, "booking_id")
    if booking_id is None:
        raise CapabilityError("schema_violation", "params.booking_id is required.")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT project_id FROM resource_bookings WHERE booking_id = ?",
            (booking_id,),
        ).fetchone()
        if row is None:
            raise CapabilityError("invalid_state", f"Unknown booking {booking_id!r}.", http_status=404)
        if row["project_id"]:
            context.require_project(row["project_id"], "member")
        result = resources.cancel_booking(conn, booking_id=booking_id)
        result["project_id"] = row["project_id"]
        conn.commit()
    except resources.ResourceError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    warnings = []
    if result.get("idempotent_replay"):
        warnings.append("idempotent replay")
    return {
        "action_status": "applied",
        "project_id": result.get("project_id"),
        "object_refs": [{"kind": "resource_booking", "id": booking_id, "revision": None}],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": warnings,
        "result": result,
    }


def _handle_resource_booking_status(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    project_ids = params.get("project_ids")
    if project_ids is None:
        if not context.has_any_membership():
            raise CapabilityError(
                "forbidden_scope", "Reads require at least one project membership.",
                http_status=403,
            )
        project_ids = auth.visible_project_ids(context)
    else:
        if not isinstance(project_ids, list) or not all(
            isinstance(p, str) and p.strip() for p in project_ids
        ):
            raise CapabilityError(
                "schema_violation", "params.project_ids must be a list of project ids."
            )
        outside = [p for p in project_ids if p not in auth.visible_project_ids(context)]
        if outside:
            raise CapabilityError(
                "forbidden_scope", f"No membership for projects {outside}.", http_status=403
            )
    status = _optional_str(params, "status")
    resource_id = _optional_str(params, "resource_id")
    conn = connect_read_only(db_path)
    try:
        bookings = resources.list_bookings(
            conn, project_ids=project_ids, resource_id=resource_id, status=status
        )
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "project_id": None,
        "object_refs": [],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": {"bookings": bookings},
    }


def _handle_research_milestone_create(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    title = params.get("title")
    if not isinstance(title, str) or not title.strip():
        raise CapabilityError("schema_violation", "params.title must be a non-empty string.")
    project_id = _require_project_param(context, params.get("project_id"))
    description = params.get("description")
    if description is not None and not isinstance(description, str):
        raise CapabilityError("schema_violation", "params.description must be a string.")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        created = research.create_milestone(
            conn, project_id=project_id, title=title.strip(),
            description=description, created_by=context.principal_id,
        )
        conn.commit()
    except research.ResearchError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "action_status": "proposed",
        "project_id": project_id,
        "object_refs": [{"kind": "research_milestone", "id": created["milestone_id"], "revision": None}],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": created,
    }


def _handle_research_criterion_attach(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    milestone_id = _optional_str(params, "milestone_id")
    if milestone_id is None:
        raise CapabilityError("schema_violation", "params.milestone_id is required.")
    description = params.get("description")
    if not isinstance(description, str) or not description.strip():
        raise CapabilityError("schema_violation", "params.description must be a non-empty string.")
    evidence_refs = params.get("evidence_refs") or []
    if not isinstance(evidence_refs, list) or not all(isinstance(r, dict) for r in evidence_refs):
        raise CapabilityError(
            "schema_violation", "params.evidence_refs must be a list of ref objects."
        )
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        milestone = conn.execute(
            "SELECT project_id FROM research_milestones WHERE milestone_id = ?",
            (milestone_id,),
        ).fetchone()
        if milestone is None:
            raise CapabilityError(
                "invalid_state", f"Unknown milestone {milestone_id!r}.", http_status=404)
        context.require_project(milestone["project_id"], "member")
        attached = research.attach_criterion(
            conn, milestone_id=milestone_id, description=description,
            evidence_refs=evidence_refs,
        )
        conn.commit()
    except research.ResearchError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "action_status": "proposed",
        "project_id": milestone["project_id"],
        "object_refs": [
            {"kind": "milestone_criterion", "id": attached["criterion_id"], "revision": None},
            {"kind": "research_milestone", "id": milestone_id, "revision": None},
        ],
        # envelope.evidence_refs is the evidence-OBJECT contract (evidence_id,
        # revision); page/block criterion refs travel in the result payload.
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": attached,
    }


def _handle_research_criterion_check(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    criterion_id = _optional_str(params, "criterion_id")
    if criterion_id is None:
        raise CapabilityError("schema_violation", "params.criterion_id is required.")
    note = _optional_str(params, "note")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT rm.project_id FROM milestone_criteria mc "
            "JOIN research_milestones rm ON rm.milestone_id = mc.milestone_id "
            "WHERE mc.criterion_id = ?",
            (criterion_id,),
        ).fetchone()
        if row is None:
            raise CapabilityError(
                "invalid_state", f"Unknown criterion {criterion_id!r}.", http_status=404)
        context.require_project(row["project_id"], "member")
        checked = research.check_criterion(
            conn, criterion_id=criterion_id, checked_by=context.principal_id, note=note
        )
        conn.commit()
    except research.ResearchError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "action_status": "applied" if checked["status"] == "satisfied" else "proposed",
        "project_id": row["project_id"],
        "object_refs": [
            {"kind": "milestone_criterion", "id": criterion_id, "revision": None}
        ],
        "evidence_refs": [],
        "unknowns": [
            f"criterion {criterion_id}: evidence ref did not open: {o['ref']}"
            for o in checked["evidence_opened"] if not o.get("open")
        ],
        "warnings": [],
        "result": checked,
    }


def _handle_research_milestone_status(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    milestone_id = _optional_str(params, "milestone_id")
    project_ids = params.get("project_ids")
    if milestone_id is None and project_ids is None:
        if not context.has_any_membership():
            raise CapabilityError(
                "forbidden_scope", "Reads require at least one project membership.",
                http_status=403,
            )
        project_ids = auth.visible_project_ids(context)
    if project_ids is not None:
        if not isinstance(project_ids, list) or not all(
            isinstance(p, str) and p.strip() for p in project_ids
        ):
            raise CapabilityError(
                "schema_violation", "params.project_ids must be a list of project ids."
            )
        outside = [p for p in project_ids if p not in auth.visible_project_ids(context)]
        if outside:
            raise CapabilityError(
                "forbidden_scope", f"No membership for projects {outside}.", http_status=403
            )
    conn = connect_read_only(db_path)
    try:
        overview = research.milestone_overview(
            conn, project_ids=project_ids, milestone_id=milestone_id
        )
        if project_ids is not None:
            overview["milestones"] = [
                m for m in overview["milestones"] if m["project_id"] in project_ids
            ]
    except research.ResearchError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "project_id": None,
        "object_refs": [
            {"kind": "research_milestone", "id": m["milestone_id"], "revision": None}
            for m in overview["milestones"]
        ],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": overview,
    }


def _handle_research_milestone_decide(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    milestone_id = _optional_str(params, "milestone_id")
    if milestone_id is None:
        raise CapabilityError("schema_violation", "params.milestone_id is required.")
    decision = params.get("decision")
    if decision not in ("accepted", "rejected"):
        raise CapabilityError(
            "schema_violation", 'params.decision must be "accepted" or "rejected".'
        )
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT project_id FROM research_milestones WHERE milestone_id = ?",
            (milestone_id,),
        ).fetchone()
        if row is None:
            raise CapabilityError(
                "invalid_state", f"Unknown milestone {milestone_id!r}.", http_status=404)
        context.require_project(row["project_id"], "owner")
        result = research.decide_milestone(
            conn, milestone_id=milestone_id, decision=decision,
            decided_by=context.principal_id,
        )
        result["project_id"] = row["project_id"]
        conn.commit()
    except research.ResearchError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    warnings = []
    if result.get("idempotent_replay"):
        warnings.append("idempotent replay")
    return {
        "action_status": "applied",
        "project_id": result["project_id"],
        "object_refs": [{"kind": "research_milestone", "id": milestone_id, "revision": None}],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": warnings,
        "result": result,
    }


def _handle_external_result_submit(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    title = params.get("title")
    if not isinstance(title, str) or not title.strip():
        raise CapabilityError("schema_violation", "params.title must be a non-empty string.")
    source_kind = params.get("source_kind")
    if not isinstance(source_kind, str):
        raise CapabilityError("schema_violation", "params.source_kind must be a string.")
    project_id = _require_project_param(context, params.get("project_id"))
    inputs = params.get("inputs") or {}
    if not isinstance(inputs, dict):
        raise CapabilityError("schema_violation", "params.inputs must be an object.")
    artifacts = params.get("artifacts") or []
    if not isinstance(artifacts, list):
        raise CapabilityError("schema_violation", "params.artifacts must be a list.")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        submitted = external_results.submit_external_result(
            conn, project_id=project_id, source_kind=source_kind,
            title=title.strip(),
            summary=_optional_str(params, "summary"),
            tool_name=_optional_str(params, "tool_name"),
            tool_version=_optional_str(params, "tool_version"),
            method=_optional_str(params, "method"),
            inputs=inputs,
            input_source=_optional_str(params, "input_source"),
            artifacts=artifacts,
            task_id=_optional_str(params, "task_id"),
            submitted_by=context.principal_id,
        )
        submitted["project_id"] = project_id
        conn.commit()
    except external_results.ExternalResultError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "action_status": "proposed",
        "project_id": project_id,
        "object_refs": [{"kind": "external_result", "id": submitted["result_id"], "revision": None}],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [
            "computation result: NOT a wet-lab measurement; excluded from "
            "experiment events and inventory" if source_kind == "computation" else ""
        ] if source_kind == "computation" else [],
        "result": submitted,
    }


def _handle_external_result_status(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    result_id = _optional_str(params, "result_id")
    project_ids = params.get("project_ids")
    if result_id is None and project_ids is None:
        if not context.has_any_membership():
            raise CapabilityError(
                "forbidden_scope", "Reads require at least one project membership.",
                http_status=403,
            )
        project_ids = auth.visible_project_ids(context)
    if project_ids is not None:
        if not isinstance(project_ids, list) or not all(
            isinstance(p, str) and p.strip() for p in project_ids
        ):
            raise CapabilityError(
                "schema_violation", "params.project_ids must be a list of project ids."
            )
        outside = [p for p in project_ids if p not in auth.visible_project_ids(context)]
        if outside:
            raise CapabilityError(
                "forbidden_scope", f"No membership for projects {outside}.", http_status=403
            )
    source_kind = _optional_str(params, "source_kind")
    conn = connect_read_only(db_path)
    try:
        rows = external_results.list_external_results(
            conn, project_ids=project_ids, source_kind=source_kind, result_id=result_id
        )
    except external_results.ExternalResultError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "project_id": None,
        "object_refs": [
            {"kind": "external_result", "id": r["result_id"], "revision": None}
            for r in rows
        ],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": {"results": rows},
    }


# ---------------------------------------------------------------------------
# S07 cross-domain inventory closure (plan §13)


_SUBTASK_STATUS_TO_ACTION = {
    "pending": "clarification_needed",
    "clarification": "clarification_needed",
    "submitted": "proposed",
    "applied": "applied",
    "failed": "failed",
    "duplicate_suspect": "clarification_needed",
    "rejected": "rejected",
    "cancelled": "rejected",
}

_ADJUSTMENT_STATUS_TO_ACTION = {
    "pending_confirmation": "proposed",
    "applied": "applied",
    "rejected": "rejected",
    "stale": "failed",
}


def _optional_str(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CapabilityError("schema_violation", f"params.{key} must be a non-empty string when present.")
    return value


def _optional_number(params: dict[str, Any], key: str) -> float | None:
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CapabilityError("schema_violation", f"params.{key} must be a number when present.")
    return float(value)


def _subtask_envelope(conn: sqlite3.Connection, result: dict[str, Any]) -> dict[str, Any]:
    status = result["status"]
    unknowns: list[str] = []
    warnings: list[str] = []
    if result.get("idempotent_replay"):
        warnings.append("idempotent replay of an already-ingested/executed subtask")
    if result.get("error_code"):
        unknowns.append(f"subtask {result['subtask_id']}: {result['error_code']}")
    object_refs = [
        {"kind": "inventory_subtask", "id": result["subtask_id"], "revision": None},
        {"kind": "memory_event", "id": result["source_event_id"], "revision": result.get("source_event_revision")},
    ]
    if result.get("inventory_transaction_id"):
        object_refs.append(
            {"kind": "transaction", "id": result["inventory_transaction_id"], "revision": None}
        )
    if result.get("event_id"):
        object_refs.append({"kind": "event_proposal", "id": result["event_id"], "revision": None})
    row = conn.execute(
        "SELECT project_id, parent_task_id FROM inventory_subtasks WHERE subtask_id = ?",
        (result["subtask_id"],),
    ).fetchone()
    return {
        "action_status": _SUBTASK_STATUS_TO_ACTION.get(status, "unknown"),
        "project_id": row["project_id"] if row is not None else None,
        "object_refs": object_refs,
        "evidence_refs": [],
        "unknowns": unknowns,
        "warnings": warnings,
        "result": result,
    }


def _handle_inventory_subtask_status(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Mixed-request status view (§13.6): every child subtask of a task, one
    project, or the whole visible scope, with the event/transaction links."""
    project_id = _optional_str(params, "project_id")
    status = _optional_str(params, "status")
    source_event_id = _optional_str(params, "source_event_id")
    parent_task_id = _optional_str(params, "parent_task_id")
    if project_id is not None:
        context.require_project(project_id, "viewer")
    conn = connect(db_path)
    try:
        # S08 §14 scope fence: a listing without an explicit project filter is
        # limited to the caller's own memberships — never a cross-project leak.
        visible_projects = (
            [project_id] if project_id is not None else list(context.memberships.keys())
        )
        items = inventory.list_inventory_subtasks(
            conn, status=status, project_id=project_id, source_event_id=source_event_id,
            project_ids=visible_projects,
        )
        if parent_task_id is not None:
            items = [item for item in items if item.get("parent_task_id") == parent_task_id]
        return {
            "action_status": "applied",
            "project_id": project_id,
            "object_refs": [],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": [],
            "result": {"items": items, "count": len(items)},
        }
    finally:
        conn.close()


def _handle_inventory_subtask_execute(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    subtask_id = _optional_str(params, "subtask_id")
    if subtask_id is None:
        raise CapabilityError("schema_violation", "params.subtask_id is required.")
    expected_revision = _optional_int(params, "expected_revision")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT project_id FROM inventory_subtasks WHERE subtask_id = ?",
            (subtask_id,),
        ).fetchone()
        if row is None:
            raise CapabilityError("invalid_state", f"Unknown subtask {subtask_id!r}.", http_status=404)
        if row["project_id"] is not None:
            context.require_project(row["project_id"], "member")
        result = inventory.execute_inventory_subtask(
            conn, subtask_id, principal_id=context.principal_id, expected_revision=expected_revision
        )
        envelope = _subtask_envelope(conn, result)
        conn.commit()
        return envelope
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_inventory_subtask_resolve(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    subtask_id = _optional_str(params, "subtask_id")
    if subtask_id is None:
        raise CapabilityError("schema_violation", "params.subtask_id is required.")
    chemical_id = _optional_str(params, "chemical_id")
    unit = _optional_str(params, "unit")
    amount = _optional_number(params, "amount")
    confirm_duplicate = params.get("confirm_duplicate", False)
    if not isinstance(confirm_duplicate, bool):
        raise CapabilityError("schema_violation", "params.confirm_duplicate must be a boolean.")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT project_id FROM inventory_subtasks WHERE subtask_id = ?",
            (subtask_id,),
        ).fetchone()
        if row is None:
            raise CapabilityError("invalid_state", f"Unknown subtask {subtask_id!r}.", http_status=404)
        if row["project_id"] is not None:
            context.require_project(row["project_id"], "member")
        result = inventory.resolve_inventory_subtask(
            conn,
            subtask_id,
            chemical_id=chemical_id,
            unit=unit,
            amount=amount,
            confirm_duplicate=confirm_duplicate,
            confirmed_by=context.principal_id,
        )
        envelope = _subtask_envelope(conn, result)
        conn.commit()
        return envelope
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_inventory_inbound_process(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Cross-domain bridge worker: sent inventory_proposal outbox messages
    become persisted subtasks. Idempotent; one transaction per pass."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        processed = inventory.process_inventory_inbound(conn)
        conn.commit()
        return {
            "action_status": "applied",
            "object_refs": [],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": [],
            "result": processed,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_inventory_adjustment_propose(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    source_event_id = _optional_str(params, "source_event_id")
    if source_event_id is None:
        raise CapabilityError("schema_violation", "params.source_event_id is required.")
    corrected_amount = _optional_number(params, "corrected_amount")
    if corrected_amount is None:
        raise CapabilityError("schema_violation", "params.corrected_amount is required.")
    corrected_unit = _optional_str(params, "corrected_unit")
    if corrected_unit is None:
        raise CapabilityError("schema_violation", "params.corrected_unit is required.")
    reason = _optional_str(params, "reason")
    if reason is None:
        raise CapabilityError("schema_violation", "params.reason is required.")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = inventory.propose_inventory_adjustment(
            conn,
            source_event_id,
            corrected_amount=corrected_amount,
            corrected_unit=corrected_unit,
            reason=reason,
            created_by=context.principal_id,
        )
        row = conn.execute(
            "SELECT project_id FROM inventory_adjustment_proposals WHERE adjustment_id = ?",
            (result["adjustment_id"],),
        ).fetchone()
        envelope = {
            "action_status": _ADJUSTMENT_STATUS_TO_ACTION.get(result["status"], "proposed"),
            "project_id": row["project_id"] if row is not None else None,
            "object_refs": [
                {"kind": "adjustment_proposal", "id": result["adjustment_id"], "revision": None},
                {"kind": "transaction", "id": result["original_transaction_id"], "revision": None},
            ],
            "evidence_refs": [],
            "unknowns": (
                [] if result.get("idempotent_replay")
                else ["stock adjustment awaits explicit human confirmation"]
            ),
            "warnings": (
                ["idempotent replay of an open adjustment proposal"]
                if result.get("idempotent_replay") else []
            ),
            "result": result,
        }
        conn.commit()
        return envelope
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _approval_content_digest(
    conn: sqlite3.Connection, proposal_type: str, proposal_id: str, allowed_action: str
) -> tuple[str | None, str]:
    """Server-computed digest binding a grant to exact proposal content."""
    if proposal_type == "inventory_adjustment":
        row = conn.execute(
            """
            SELECT project_id, source_event_id, original_transaction_id, chemical_id,
                   amount_delta, unit, reason, status
            FROM inventory_adjustment_proposals WHERE adjustment_id = ?
            """,
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise CapabilityError(
                "not_found", f"Unknown adjustment {proposal_id!r}.", http_status=404
            )
        content = {k: row[k] for k in row.keys()}
        return row["project_id"], hashlib.sha256(
            json.dumps(
                {"type": proposal_type, "action": allowed_action, "content": content},
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
    if proposal_type == "inventory_event":
        row = conn.execute(
            "SELECT payload_json, status, confirmed_by, supersedes_event_id "
            "FROM event_proposals WHERE event_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise CapabilityError(
                "not_found", f"Unknown inventory event {proposal_id!r}.", http_status=404
            )
        payload = json.loads(row["payload_json"] or "{}")
        content = {
            "payload": payload,
            "status": row["status"],
            "supersedes": row["supersedes_event_id"],
        }
        return payload.get("project_id"), hashlib.sha256(
            json.dumps(
                {"type": proposal_type, "action": allowed_action, "content": content},
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
    if proposal_type == "memory_proposal":
        row = conn.execute(
            "SELECT project_id, items_json, revision, status FROM memory_proposals "
            "WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise CapabilityError(
                "not_found", f"Unknown proposal {proposal_id!r}.", http_status=404
            )
        content = {
            "items": json.loads(row["items_json"] or "[]"),
            "revision": row["revision"],
            "status": row["status"],
        }
        return row["project_id"], hashlib.sha256(
            json.dumps(
                {"type": proposal_type, "action": allowed_action, "content": content},
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
    raise CapabilityError(
        "schema_violation", f"Unknown proposal_type {proposal_type!r}."
    )


def _consume_approval_grant(
    conn: sqlite3.Connection,
    *,
    approval_id: str,
    proposal_type: str,
    proposal_id: str,
    project_id: str | None,
    allowed_action: str,
    operation_id: str,
) -> dict[str, Any] | None:
    """Validate and consume a human grant inside the business transaction."""
    if not isinstance(approval_id, str) or not approval_id.strip():
        raise CapabilityError(
            "human_approval_required",
            "params.approval_id is required when a non-human applies an approved action.",
            http_status=403,
        )
    row = conn.execute(
        "SELECT * FROM approval_grants WHERE approval_id = ?", (approval_id,)
    ).fetchone()
    if row is None:
        raise CapabilityError(
            "human_approval_required", f"Unknown approval grant {approval_id!r}.", http_status=403
        )
    if row["status"] != "active":
        raise CapabilityError(
            "approval_consumed",
            f"Approval grant {approval_id!r} is {row['status']!r}.",
            http_status=409,
        )
    if (
        row["proposal_type"] != proposal_type
        or row["proposal_id"] != proposal_id
        or row["allowed_action"] != allowed_action
        or (row["project_id"] or None) != (project_id or None)
    ):
        raise CapabilityError(
            "approval_scope_mismatch",
            "Approval grant does not match this proposal/action/project.",
            http_status=403,
        )
    if row["expires_at"] is not None:
        expired = conn.execute(
            "SELECT 1 WHERE datetime('now') >= ?", (row["expires_at"],)
        ).fetchone()
        if expired is not None:
            conn.execute(
                "UPDATE approval_grants SET status = 'expired' WHERE approval_id = ?",
                (approval_id,),
            )
            raise CapabilityError(
                "approval_expired", "Approval grant has expired.", http_status=409
            )
    actual_digest = _approval_content_digest(
        conn, proposal_type, proposal_id, allowed_action
    )[1]
    if not hmac.compare_digest(str(row["content_digest"]), actual_digest):
        raise CapabilityError(
            "approval_content_changed",
            "The proposal content changed after approval; a fresh human grant is required.",
            http_status=409,
        )
    cursor = conn.execute(
        """
        UPDATE approval_grants
        SET status = 'consumed', consumed_by_operation_id = ?, consumed_at = CURRENT_TIMESTAMP
        WHERE approval_id = ? AND status = 'active'
        """,
        (operation_id, approval_id),
    )
    if cursor.rowcount != 1:
        raise CapabilityError(
            "approval_consumed", "Approval grant was already consumed.", http_status=409
        )
    return dict(row)


def _handle_inventory_approval_grant(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    proposal_type = params.get("proposal_type")
    proposal_id = params.get("proposal_id")
    allowed_action = params.get("allowed_action") or "apply"
    expires_in_s = params.get("expires_in_s", 3600)
    if proposal_type not in (
        "inventory_event", "inventory_adjustment", "memory_proposal"
    ):
        raise CapabilityError("schema_violation", "params.proposal_type is invalid.")
    if not isinstance(proposal_id, str) or not proposal_id.strip():
        raise CapabilityError("schema_violation", "params.proposal_id is required.")
    if not isinstance(allowed_action, str) or not allowed_action.strip():
        raise CapabilityError("schema_violation", "params.allowed_action is invalid.")
    if not isinstance(expires_in_s, int) or expires_in_s <= 0:
        raise CapabilityError("schema_violation", "params.expires_in_s must be positive.")
    try:
        auth.require_human(context, "inventory.approval.grant")
    except auth.AuthError as exc:
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        project_id, digest = _approval_content_digest(
            conn, proposal_type, proposal_id, allowed_action
        )
        if project_id is not None:
            context.require_project(project_id, "member")
        approval_id = f"apr_{uuid.uuid4().hex[:16]}"
        conn.execute(
            """
            INSERT INTO approval_grants
              (approval_id, proposal_type, proposal_id, project_id, allowed_action,
               content_digest, approved_by, approval_source, status, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'human', 'active', datetime('now', ?))
            """,
            (
                approval_id, proposal_type, proposal_id, project_id, allowed_action,
                digest, context.principal_id, f"+{int(expires_in_s)} seconds",
            ),
        )
        conn.commit()
    except auth.AuthError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "project_id": project_id,
        "object_refs": [
            {"kind": "approval_grant", "id": approval_id, "revision": None}
        ],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": {
            "approval_id": approval_id,
            "proposal_type": proposal_type,
            "proposal_id": proposal_id,
            "allowed_action": allowed_action,
            "approval_source": "human",
            "approved_by": context.principal_id,
        },
    }


def _handle_inventory_adjustment_apply(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    adjustment_id = _optional_str(params, "adjustment_id")
    if adjustment_id is None:
        raise CapabilityError("schema_violation", "params.adjustment_id is required.")
    expected_revision = _optional_int(params, "expected_revision")
    reject = params.get("reject", False)
    if not isinstance(reject, bool):
        raise CapabilityError("schema_violation", "params.reject must be a boolean.")
    approval_id = params.get("approval_id")
    if reject:
        try:
            auth.require_human(context, "inventory.adjustment.reject")
        except auth.AuthError as exc:
            raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT project_id FROM inventory_adjustment_proposals WHERE adjustment_id = ?",
            (adjustment_id,),
        ).fetchone()
        if row is None:
            raise CapabilityError("invalid_state", f"Unknown adjustment {adjustment_id!r}.", http_status=404)
        if row["project_id"] is not None:
            context.require_project(row["project_id"], "member")
        approval_source = "human"
        if context.kind not in auth.HUMAN_KINDS:
            grant = _consume_approval_grant(
                conn,
                approval_id=approval_id,
                proposal_type="inventory_adjustment",
                proposal_id=adjustment_id,
                project_id=row["project_id"],
                allowed_action="apply",
                operation_id=str(params.get("_operation_id") or ""),
            )
            approval_source = f"human_grant:{grant['approval_id']}"
        result = inventory.apply_inventory_adjustment(
            conn,
            adjustment_id,
            principal_id=context.principal_id,
            expected_revision=expected_revision,
            reject=reject,
        )
        conn.execute(
            "UPDATE inventory_adjustment_proposals SET approval_id = ?, "
            "approval_source = ? WHERE adjustment_id = ?",
            (
                approval_id if context.kind not in auth.HUMAN_KINDS else None,
                approval_source,
                adjustment_id,
            ),
        )
        envelope = {
            "action_status": _ADJUSTMENT_STATUS_TO_ACTION.get(result["status"], "proposed"),
            "project_id": row["project_id"],
            "object_refs": [
                {"kind": "adjustment_proposal", "id": result["adjustment_id"], "revision": None},
                {"kind": "transaction", "id": result["original_transaction_id"], "revision": None},
            ],
            "evidence_refs": [],
            "unknowns": [],
            "warnings": (
                ["idempotent replay"] if result.get("idempotent_replay") else []
            ),
            "result": result,
        }
        conn.commit()
        return envelope
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# S10 messaging channels (plan §16).
# ---------------------------------------------------------------------------

def _handle_channel_account_register(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    platform = params.get("platform")
    if not isinstance(platform, str):
        raise CapabilityError("schema_violation", "params.platform must be a string.")
    display_name = params.get("display_name")
    credential_ref = params.get("credential_ref")
    verify_secret_ref = params.get("verify_secret_ref")
    allow_group_threads = bool(params.get("allow_group_threads", False))
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        created = channels.register_channel_account(
            conn,
            platform=platform,
            display_name=display_name if isinstance(display_name, str) else None,
            credential_ref=credential_ref if isinstance(credential_ref, str) else None,
            verify_secret_ref=verify_secret_ref if isinstance(verify_secret_ref, str) else None,
            allow_group_threads=allow_group_threads,
            created_by=context.principal_id,
        )
        conn.commit()
    except channels.ChannelError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "object_refs": [{"kind": "channel_account", "id": created["channel_account_id"], "revision": None}],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": created,
    }


def _handle_channel_identity_bind(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    channel_account_id = params.get("channel_account_id")
    platform_user_id = params.get("platform_user_id")
    # ``principal_id`` is a forbidden v2 field (derived from the caller); the
    # admin names the TARGET principal explicitly as target_principal_id.
    principal_id = params.get("target_principal_id")
    if not isinstance(channel_account_id, str) or not channel_account_id.strip():
        raise CapabilityError("schema_violation", "params.channel_account_id is required.")
    if not isinstance(platform_user_id, str) or not platform_user_id.strip():
        raise CapabilityError("schema_violation", "params.platform_user_id is required.")
    if not isinstance(principal_id, str) or not principal_id.strip():
        raise CapabilityError("schema_violation", "params.target_principal_id is required.")
    display_name = params.get("display_name")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        binding = channels.bind_channel_identity(
            conn,
            channel_account_id=channel_account_id.strip(),
            platform_user_id=platform_user_id.strip(),
            principal_id=principal_id.strip(),
            bound_by=context.principal_id,
            display_name=display_name if isinstance(display_name, str) else None,
        )
        conn.commit()
    except channels.ChannelError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "object_refs": [{"kind": "channel_identity", "id": binding["binding_id"], "revision": None}],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": binding,
    }


def _handle_channel_identity_revoke(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    binding_id = params.get("binding_id")
    if not isinstance(binding_id, str) or not binding_id.strip():
        raise CapabilityError("schema_violation", "params.binding_id is required.")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        binding = channels.revoke_channel_identity(conn, binding_id.strip())
        conn.commit()
    except channels.ChannelError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "object_refs": [{"kind": "channel_identity", "id": binding["binding_id"], "revision": None}],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": ["identity revoked; further messages from this author stay unmapped"],
        "result": binding,
    }


def _handle_channel_messages_read(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    channel_account_id = params.get("channel_account_id")
    status = params.get("status")
    limit = _optional_int(params, "limit") or 100
    conn = connect_read_only(db_path)
    try:
        rows = channels.list_channel_messages(
            conn,
            channel_account_id=channel_account_id if isinstance(channel_account_id, str) else None,
            status=status if isinstance(status, str) else None,
            limit=min(limit, 500),
        )
    finally:
        conn.close()
    return {
        "action_status": "applied",
        "object_refs": [],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": [],
        "result": {"channel_messages": rows},
    }


def _handle_channel_webhook_ingest(
    db_path: str,
    context: auth.PrincipalContext,
    params: dict[str, Any],
    services: "ServiceSettings",
) -> dict[str, Any]:
    """Process one platform webhook event through the unified envelope.

    This is the same entry the HTTP webhook endpoint uses after transport
    signature verification; exposing it as a capability keeps the mapping
    testable over the standard auth/idempotency path. Attachments arrive as
    base64 in params and are persisted to the storage area BEFORE the event
    is processed (§16.6 outage rule).
    """
    channel_account_id = params.get("channel_account_id")
    event = params.get("event")
    if not isinstance(channel_account_id, str) or not channel_account_id.strip():
        raise CapabilityError("schema_violation", "params.channel_account_id is required.")
    if not isinstance(event, dict):
        raise CapabilityError("schema_violation", "params.event must be an object.")
    # Deep copy only; decoding/validation is centralized in
    # channels.normalize_attachments so HTTP and capability ingress cannot
    # diverge (F10). Bad base64/empty bytes raise a stable ChannelError.
    event = json.loads(json.dumps(event))
    raw_attachments = event.get("attachments") or []
    if not isinstance(raw_attachments, list):
        raise CapabilityError("schema_violation", "event.attachments must be an array.")

    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = channels.ingest_channel_event(
            conn,
            channel_account_id=channel_account_id.strip(),
            event=event,
            storage_root=Path(services.storage_root),
        )
        conn.commit()
    except channels.ChannelError as exc:
        conn.rollback()
        raise CapabilityError(exc.code, exc.message, http_status=exc.http_status) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    warnings = []
    if result.get("status") == "unmapped":
        warnings.append("sender is not bound to a principal; message archived, not processed")
    if result.get("status") == "rejected":
        warnings.append(f"event rejected: {result.get('error_code')}")
    return {
        "action_status": "applied",
        "object_refs": [{"kind": "channel_message", "id": result["channel_message_id"], "revision": None}],
        "evidence_refs": [],
        "unknowns": [],
        "warnings": warnings,
        "result": result,
    }


# S06 handlers are defined after this point; register them here so the
# _HANDLERS literal above stays ordered like the original file.
_HANDLERS.update(
    {
        "task.dispatch": _handle_task_dispatch,
    "assistant.task.process": _handle_assistant_task_process,
    "assistant.agent.start": _handle_assistant_agent_start,
    "assistant.agent.finish": _handle_assistant_agent_finish,
        "agent.registry.load": _handle_agent_registry_load,
        "agent.registry.list": _handle_agent_registry_list,
        "agent.registry.set_enabled": _handle_agent_registry_set_enabled,
        "run.claim": _handle_run_claim,
        "run.heartbeat": _handle_run_heartbeat,
        "run.complete": _handle_run_complete,
        "run.recover": _handle_run_recover,
        "run.list": _handle_run_list,
        "delivery.process": _handle_delivery_process,
        # S07 cross-domain inventory closure.
        "inventory.subtask.status": _handle_inventory_subtask_status,
        "inventory.subtask.execute": _handle_inventory_subtask_execute,
        "inventory.subtask.resolve": _handle_inventory_subtask_resolve,
        "inventory.inbound.process": _handle_inventory_inbound_process,
        "inventory.adjustment.propose": _handle_inventory_adjustment_propose,
        "inventory.adjustment.apply": _handle_inventory_adjustment_apply,
        "inventory.approval.grant": _handle_inventory_approval_grant,
        # S09 extension domains (plan §15).
        "resource.register": _handle_resource_register,
        "resource.list": _handle_resource_list,
        "resource.availability": _handle_resource_availability,
        "resource.booking.propose": _handle_resource_booking_propose,
        "resource.booking.confirm": _handle_resource_booking_confirm,
        "resource.booking.cancel": _handle_resource_booking_cancel,
        "resource.booking.status": _handle_resource_booking_status,
        "research.milestone.create": _handle_research_milestone_create,
        "research.milestone.criterion.attach": _handle_research_criterion_attach,
        "research.milestone.criterion.check": _handle_research_criterion_check,
        "research.milestone.status": _handle_research_milestone_status,
        "research.milestone.decide": _handle_research_milestone_decide,
        "external.result.submit": _handle_external_result_submit,
        "external.result.status": _handle_external_result_status,
        # S10 messaging channels (plan §16).
        "channel.account.register": _handle_channel_account_register,
        "channel.identity.bind": _handle_channel_identity_bind,
        "channel.identity.revoke": _handle_channel_identity_revoke,
        "channel.messages.read": _handle_channel_messages_read,
        "channel.webhook.ingest": _handle_channel_webhook_ingest,
    }
)


def envelope_to_json(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True)
