"""S06: agent manifests, registry, dispatch, run tokens, worker recovery.

Plan §12 contracts implemented here (Core keeps execution authority; the Pi
host only executes roles):

- Agent manifests (§12.2): agent_id/version, modes, required capabilities,
  triggers, timeout, tool-call budget, delegation depth, retry policy. Loaded
  ONLY from the admin config directory via ``load_registry``; declaring a
  capability never grants authority (§12.3) — every tool call goes through
  ``invoke_capability`` with the token-bound principal and is re-authorized.
- Read-only mode enforcement: manifests in read-only modes may declare only
  non-mutating capabilities, so B query/report cannot modify experiment facts
  and a new read-only role needs no business-code change (§12.7, AC 5).
- Dispatch (§12.8): an intent maps deterministically to one enabled manifest;
  a mixed-intent orchestrator with dynamic DAGs is intentionally NOT built in
  the first version (ADR: fixed templates + direct routing).
- Run tokens (§12.5): server-signed per run; identity (principal/task/
  projects/evidence scope) lives in the token row, never in tool parameters.
  The plaintext token is returned exactly once at dispatch.
- Worker lifecycle (§12.6/§12.14): claim bumps ``claim_epoch`` (fencing
  token); heartbeat extends the lease; an expired lease is recovered by
  re-queueing the run — a stale worker's finish is rejected by epoch check,
  and a business commit whose receipt was lost is answered from
  ``operation_results`` by operation_id, never replayed (§12.13).
- Trace: one trace_id spans task -> run -> token -> outbox -> deliveries
  (§12.15). Human waiting never holds a lease (task goes ``waiting_input``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from typing import Any

from . import auth, tasks
from .capabilities import CAPABILITY_REGISTRY

AGENT_HOST_SCHEMA_VERSION = "agent_host_v1"

AGENT_MODES = ("inventory", "b_capture", "b_query", "b_report", "readonly", "planner", "orchestrator")
# Modes whose manifests must not touch experimental FACTS (plan §12.7, AC 2:
# "B query/report cannot modify experimental facts"). Writing a derived
# report (query.report.save) is NOT a fact change and stays allowed; every
# other mutating capability is a fact/identity/evidence write.
READ_ONLY_MODES = ("b_query", "b_report", "readonly")
_MUTATING_FORBIDDEN_IN_READONLY = frozenset(
    {
        "inventory.message.process",
        "inventory.event.resolve",
        "task.create",
        "task.update",
        "task.interaction.answer",
        "evidence.document.create",
        "evidence.page.ingest",
        "evidence.transcribe.run",
        "evidence.transcript.correct",
        "evidence.transcription.retry",
        "memory.proposal.create",
        "memory.proposal.confirm_items",
        "memory.proposal.apply",
        "memory.clarification.open",
        "memory.clarification.answer",
        "memory.event.correct",
        "memory.index.rebuild",
    }
)
AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.\-_]{0,31}$")
INTENT_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

MANIFEST_REQUIRED_FIELDS = (
    "agent_id",
    "version",
    "mode",
    "required_capabilities",
    "triggers",
    "tool_call_budget",
)


class AgentHostError(Exception):
    """Domain error with a stable code for the capability envelope."""

    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


# --------------------------------------------------------------------------
# Manifest validation
# --------------------------------------------------------------------------

def validate_manifest(data: Any) -> dict[str, Any]:
    """Validate one manifest dict; returns the normalized manifest.

    Rejects unknown capability ids, mutating capabilities in read-only modes,
    non-positive budgets, and identity self-assertions (manifests carry no
    principal identity — authorization stays with the token-bound principal).
    """
    if not isinstance(data, dict):
        raise AgentHostError("schema_violation", "manifest must be a JSON object.")
    missing = [f for f in MANIFEST_REQUIRED_FIELDS if f not in data]
    if missing:
        raise AgentHostError(
            "schema_violation", f"manifest missing fields: {', '.join(missing)}."
        )
    agent_id = data["agent_id"]
    if not isinstance(agent_id, str) or not AGENT_ID_RE.match(agent_id):
        raise AgentHostError("schema_violation", "manifest.agent_id must match ^[a-z][a-z0-9_]{1,63}$.")
    version = data["version"]
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise AgentHostError("schema_violation", "manifest.version must be a short version string.")
    mode = data["mode"]
    if mode not in AGENT_MODES:
        raise AgentHostError(
            "schema_violation", f"manifest.mode must be one of {list(AGENT_MODES)}."
        )
    caps = data["required_capabilities"]
    if not isinstance(caps, list) or not caps or not all(isinstance(c, str) for c in caps):
        raise AgentHostError(
            "schema_violation", "manifest.required_capabilities must be a non-empty string list."
        )
    unknown = sorted(set(caps) - set(CAPABILITY_REGISTRY))
    if unknown:
        raise AgentHostError(
            "schema_violation",
            f"manifest declares unknown capabilities: {', '.join(unknown)}.",
        )
    if mode in READ_ONLY_MODES:
        forbidden = sorted(set(caps) & _MUTATING_FORBIDDEN_IN_READONLY)
        if forbidden:
            raise AgentHostError(
                "schema_violation",
                f"read-only mode {mode!r} cannot declare fact-mutating capabilities: "
                f"{', '.join(forbidden)}.",
            )
    triggers = data["triggers"]
    if not isinstance(triggers, list) or not triggers or not all(
        isinstance(t, str) and INTENT_RE.match(t) for t in triggers
    ):
        raise AgentHostError(
            "schema_violation", "manifest.triggers must be a non-empty list of intent strings."
        )
    budget = data["tool_call_budget"]
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        raise AgentHostError("schema_violation", "manifest.tool_call_budget must be a positive integer.")
    timeout_s = data.get("timeout_s", 120)
    if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or timeout_s <= 0:
        raise AgentHostError("schema_violation", "manifest.timeout_s must be a positive number.")
    delegation_depth = data.get("max_delegation_depth", 0)
    if not isinstance(delegation_depth, int) or isinstance(delegation_depth, bool) or delegation_depth < 0:
        raise AgentHostError(
            "schema_violation", "manifest.max_delegation_depth must be a non-negative integer."
        )
    retry = data.get("retry", {})
    if not isinstance(retry, dict):
        raise AgentHostError("schema_violation", "manifest.retry must be an object.")
    max_attempts = retry.get("max_attempts", 1)
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
        raise AgentHostError("schema_violation", "manifest.retry.max_attempts must be >= 1.")
    for field in ("input_schema", "output_schema"):
        if field in data and not isinstance(data[field], dict):
            raise AgentHostError("schema_violation", f"manifest.{field} must be an object.")
    if "principal_id" in data or "project_ids" in data:
        raise AgentHostError(
            "schema_violation",
            "manifests must not carry identity (principal/project); authority is bound "
            "at dispatch time from the authenticated caller.",
        )
    return {
        "agent_id": agent_id,
        "version": version,
        "mode": mode,
        "display_name": data.get("display_name") or agent_id,
        "required_capabilities": sorted(set(caps)),
        "triggers": list(dict.fromkeys(triggers)),
        "input_schema": data.get("input_schema", {}),
        "output_schema": data.get("output_schema", {}),
        "context_scope": data.get("context_scope", {"project_ids": "auto"}),
        "timeout_s": float(timeout_s),
        "tool_call_budget": budget,
        "max_delegation_depth": delegation_depth,
        "retry": {"max_attempts": max_attempts},
        "system_prompt_ref": data.get("system_prompt_ref") or f"prompts/{agent_id}_{version}",
    }


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def load_registry(conn: sqlite3.Connection, config_dir: str | Any) -> dict[str, Any]:
    """Load manifests from the admin config directory into the registry table.

    Loaded manifests start DISABLED; enabling is a separate admin action, so
    placing a file never auto-grants anything. Invalid files are reported and
    skipped, never partially applied.
    """
    from pathlib import Path

    directory = Path(config_dir)
    if not directory.is_dir():
        raise AgentHostError(
            "invalid_request", f"Manifest config directory not found: {directory}", http_status=404
        )
    loaded: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            manifest = validate_manifest(data)
        except (AgentHostError, json.JSONDecodeError) as exc:
            invalid.append({"file": path.name, "error": str(exc)})
            continue
        conn.execute(
            """
            INSERT INTO agent_manifests (agent_id, version, manifest_json, enabled)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(agent_id, version)
            DO UPDATE SET manifest_json = excluded.manifest_json, loaded_at = CURRENT_TIMESTAMP
            """,
            (
                manifest["agent_id"],
                manifest["version"],
                json.dumps(manifest, sort_keys=True, ensure_ascii=False),
            ),
        )
        loaded.append({"file": path.name, "agent_id": manifest["agent_id"], "version": manifest["version"]})
    return {"loaded": loaded, "invalid": invalid}


def set_manifest_enabled(
    conn: sqlite3.Connection, *, agent_id: str, version: str, enabled: bool
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT manifest_row_id FROM agent_manifests WHERE agent_id = ? AND version = ?",
        (agent_id, version),
    ).fetchone()
    if row is None:
        raise AgentHostError("invalid_state", f"Unknown manifest {agent_id}@{version}.", http_status=404)
    conn.execute(
        "UPDATE agent_manifests SET enabled = ? WHERE agent_id = ? AND version = ?",
        (1 if enabled else 0, agent_id, version),
    )
    return {"agent_id": agent_id, "version": version, "enabled": bool(enabled)}


def list_manifests(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT agent_id, version, manifest_json, enabled, loaded_at
        FROM agent_manifests ORDER BY agent_id, version
        """
    ).fetchall()
    result = []
    for row in rows:
        manifest = json.loads(row["manifest_json"])
        result.append(
            {
                "agent_id": row["agent_id"],
                "version": row["version"],
                "manifest": manifest,
                "enabled": bool(row["enabled"]),
                "loaded_at": row["loaded_at"],
            }
        )
    return result


def get_manifest(
    conn: sqlite3.Connection, agent_id: str, *, version: str | None = None
) -> dict[str, Any]:
    if version is not None:
        row = conn.execute(
            "SELECT manifest_json, enabled FROM agent_manifests WHERE agent_id = ? AND version = ?",
            (agent_id, version),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT manifest_json, enabled FROM agent_manifests
            WHERE agent_id = ? AND enabled = 1
            ORDER BY loaded_at DESC LIMIT 1
            """,
            (agent_id,),
        ).fetchone()
    if row is None:
        raise AgentHostError(
            "agent_not_found",
            f"No enabled manifest for agent {agent_id!r}"
            + (f" at version {version!r}" if version else ""),
            http_status=404,
        )
    if not row["enabled"]:
        raise AgentHostError("agent_disabled", f"Agent {agent_id!r} is disabled.", http_status=409)
    return json.loads(row["manifest_json"])


def resolve_intent(conn: sqlite3.Connection, intent: str) -> dict[str, Any]:
    """Direct routing: exactly one enabled manifest must trigger on intent."""
    rows = conn.execute(
        """
        SELECT agent_id, version, manifest_json FROM agent_manifests
        WHERE enabled = 1 ORDER BY loaded_at DESC
        """
    ).fetchall()
    matches = []
    for row in rows:
        manifest = json.loads(row["manifest_json"])
        if intent in manifest["triggers"]:
            matches.append(manifest)
    if not matches:
        raise AgentHostError("unsupported_intent", f"No enabled agent triggers on intent {intent!r}.", http_status=404)
    if len(matches) > 1:
        raise AgentHostError(
            "ambiguous_intent",
            f"Intent {intent!r} matches {len(matches)} agents; admin must disable all but one.",
            http_status=409,
        )
    return matches[0]


# --------------------------------------------------------------------------
# Dispatch + run tokens
# --------------------------------------------------------------------------

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def dispatch(
    conn: sqlite3.Connection,
    *,
    intent: str,
    context: auth.PrincipalContext,
    project_id: str | None = None,
    title: str | None = None,
    input_payload: dict[str, Any] | None = None,
    token_ttl_s: int | None = None,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Route an intent to its agent, create task+run, and sign a run token.

    The token binds the CALLER's principal and visible projects (authority is
    delegated, never created); the manifest's declared capabilities only limit
    which tools the Pi host is offered. The plaintext token is returned once.
    """
    try:
        if project_id is not None:
            context.require_project(project_id, "viewer")
    except auth.AuthError as exc:
        raise AgentHostError(exc.code, exc.message, http_status=exc.http_status) from exc
    manifest = get_manifest(conn, agent_id) if agent_id else resolve_intent(conn, intent)
    project_ids = [project_id] if project_id else auth.visible_project_ids(context)
    if not project_ids and manifest["required_capabilities"]:
        raise AgentHostError(
            "forbidden_scope",
            "Dispatch requires at least one project membership to bind the run token scope.",
        )
    trace_id = trace_id or f"trace_{uuid.uuid4().hex[:16]}"
    task = tasks.create_task(
        conn,
        task_type=f"agent.{intent}",
        principal_id=context.principal_id,
        project_id=project_id,
        title=title or f"{intent} for {context.principal_id}",
    )
    conn.execute("UPDATE tasks SET trace_id = ? WHERE task_id = ?", (trace_id, task["task_id"]))
    run = tasks.create_agent_run(conn, task_id=task["task_id"], agent_role=manifest["agent_id"])
    conn.execute(
        """
        UPDATE agent_runs
        SET agent_id = ?, manifest_version = ?, trace_id = ?, budget_tool_calls = ?
        WHERE run_id = ?
        """,
        (
            manifest["agent_id"],
            manifest["version"],
            trace_id,
            manifest["tool_call_budget"],
            run["run_id"],
        ),
    )
    token_row = _sign_run_token(
        conn,
        run=run,
        task=task,
        manifest=manifest,
        context=context,
        project_ids=project_ids,
        ttl_s=token_ttl_s or int(manifest["timeout_s"]) + 300,
        trace_id=trace_id,
    )
    from . import workflows as workflows_mod

    workflow = None
    if intent in workflows_mod.WORKFLOW_TEMPLATES:
        workflow = workflows_mod.start_workflow(
            conn,
            workflow_name=intent,
            task_id=task["task_id"],
            agent_id=manifest["agent_id"],
            trace_id=trace_id,
        )
    outbox = tasks.enqueue_outbox(
        conn,
        aggregate_type="agent_run",
        aggregate_id=run["run_id"],
        message_type="agent.dispatched",
        payload={
            "trace_id": trace_id,
            "task_id": task["task_id"],
            "run_id": run["run_id"],
            "agent_id": manifest["agent_id"],
            "manifest_version": manifest["version"],
            "intent": intent,
            "input": input_payload or {},
        },
    )
    conn.execute("UPDATE outbox SET trace_id = ? WHERE outbox_id = ?", (trace_id, outbox["outbox_id"]))
    tasks.create_delivery(conn, outbox_id=outbox["outbox_id"], channel="local", target=context.principal_id)
    conn.execute(
        "UPDATE deliveries SET trace_id = ? WHERE outbox_id = ?",
        (trace_id, outbox["outbox_id"]),
    )
    return {
        "task_id": task["task_id"],
        "run_id": run["run_id"],
        "agent_id": manifest["agent_id"],
        "manifest_version": manifest["version"],
        "trace_id": trace_id,
        "run_token": token_row["plaintext"],
        "token_expires_at": token_row["expires_at"],
        "tool_call_budget": manifest["tool_call_budget"],
        "workflow_run_id": workflow["workflow_run_id"] if workflow else None,
        "workflow_version": workflow["workflow_version"] if workflow else None,
    }


def _sign_run_token(
    conn: sqlite3.Connection,
    *,
    run: dict[str, Any],
    task: dict[str, Any],
    manifest: dict[str, Any],
    context: auth.PrincipalContext,
    project_ids: list[str],
    ttl_s: int,
    trace_id: str,
) -> dict[str, Any]:
    plaintext = secrets.token_urlsafe(32)
    token_id = f"tok_{uuid.uuid4().hex[:12]}"
    conn.execute(
        """
        INSERT INTO agent_run_tokens
          (token_id, token_hash, run_id, task_id, principal_id, agent_id, manifest_version,
           project_ids_json, evidence_scope_json, trace_id, budget_tool_calls, status, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, 'active',
                datetime('now', ?))
        """,
        (
            token_id,
            _hash_token(plaintext),
            run["run_id"],
            task["task_id"],
            context.principal_id,
            manifest["agent_id"],
            manifest["version"],
            json.dumps(project_ids),
            trace_id,
            manifest["tool_call_budget"],
            f"+{int(ttl_s)} seconds",
        ),
    )
    row = conn.execute("SELECT * FROM agent_run_tokens WHERE token_id = ?", (token_id,)).fetchone()
    return {"plaintext": plaintext, "expires_at": row["expires_at"], "token_id": token_id}


def resolve_run_token(conn: sqlite3.Connection, token: str | None, *, allow_exhausted: bool = False) -> dict[str, Any]:
    """Resolve a presented bearer token to its binding row.

    Constant-time digest comparison; expired/revoked/exhausted tokens never
    resolve, and an expired token is marked so its state is visible.
    """
    if not token:
        raise AgentHostError("unauthorized", "Missing agent run token.", http_status=401)
    token_hash = _hash_token(token)
    row = conn.execute(
        "SELECT * FROM agent_run_tokens WHERE token_hash = ?", (token_hash,)
    ).fetchone()
    if row is None or not hmac.compare_digest(row["token_hash"], token_hash):
        raise AgentHostError("unauthorized", "Invalid agent run token.", http_status=401)
    if row["status"] != "active" and not (allow_exhausted and row["status"] == "exhausted"):
        raise AgentHostError(
            "unauthorized", f"Agent run token is {row['status']}.", http_status=401
        )
    expired = conn.execute(
        """
        SELECT 1 FROM agent_run_tokens
        WHERE token_id = ? AND expires_at IS NOT NULL AND expires_at <= datetime('now')
        """,
        (row["token_id"],),
    ).fetchone()
    if expired is not None:
        conn.execute(
            "UPDATE agent_run_tokens SET status = 'expired' WHERE token_id = ?", (row["token_id"],)
        )
        raise AgentHostError("unauthorized", "Agent run token is expired.", http_status=401)
    return dict(row)


def principal_from_token(conn: sqlite3.Connection, token_row: dict[str, Any]) -> auth.PrincipalContext:
    """The tool identity is the token's bound principal, narrowed to the run.

    A run token never widens the principal: its effective memberships are the
    LIVE principal memberships intersected with the project IDs frozen when
    the run token was issued (§R01/F03). Revoking a membership therefore
    narrows the run immediately; an empty intersection yields an empty scope,
    never "all projects".
    """
    base = auth.load_principal_context(conn, token_row["principal_id"])
    try:
        granted = set(json.loads(token_row.get("project_ids_json") or "[]"))
    except (TypeError, ValueError):
        granted = set()
    scoped = {pid: role for pid, role in base.memberships.items() if pid in granted}
    return auth.PrincipalContext(
        principal_id=base.principal_id,
        # Delegating a human's project authority does not delegate their
        # ability to sign human confirmations. Every model tool executes as
        # an agent, even when a human dispatched the original request.
        kind="agent",
        display_name=base.display_name,
        memberships=scoped,
    )


def manifest_for_token(conn: sqlite3.Connection, token_row: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute(
        "SELECT manifest_json FROM agent_manifests WHERE agent_id = ? AND version = ?",
        (token_row["agent_id"], token_row["manifest_version"]),
    ).fetchone()
    if row is None:
        raise AgentHostError(
            "invalid_state",
            f"Manifest {token_row['agent_id']}@{token_row['manifest_version']} vanished.",
            http_status=409,
        )
    return json.loads(row["manifest_json"])


def allowed_capabilities(conn: sqlite3.Connection, token_row: dict[str, Any]) -> set[str]:
    manifest = manifest_for_token(conn, token_row)
    return set(manifest["required_capabilities"])


def consume_tool_budget(conn: sqlite3.Connection, token_row: dict[str, Any]) -> int:
    """Count one tool call against the token budget (fencing by token status)."""
    scope = json.loads(token_row.get("evidence_scope_json") or "{}")
    root_run_id = scope.get("root_run_id")
    if root_run_id and root_run_id != token_row["run_id"]:
        root = conn.execute("SELECT * FROM agent_run_tokens WHERE run_id = ?", (root_run_id,)).fetchone()
        root_run = get_run(conn, root_run_id)
        if root is None or root_run["status"] != "running":
            raise AgentHostError("stale_worker", "Root session is no longer running.", http_status=409)
        expired = conn.execute(
            "SELECT 1 FROM agent_run_tokens WHERE token_id = ? AND expires_at <= datetime('now')",
            (root["token_id"],),
        ).fetchone()
        if expired:
            raise AgentHostError("unauthorized", "Root session token has expired.", http_status=401)
        consume_tool_budget(conn, dict(root))
    cursor = conn.execute(
        """
        UPDATE agent_run_tokens
        SET used_tool_calls = used_tool_calls + 1
        WHERE token_id = ? AND status = 'active' AND used_tool_calls < budget_tool_calls
        """,
        (token_row["token_id"],),
    )
    if cursor.rowcount != 1:
        conn.execute(
            "UPDATE agent_run_tokens SET status = 'exhausted' "
            "WHERE token_id = ? AND status = 'active'",
            (token_row["token_id"],),
        )
        raise AgentHostError(
            "budget_exhausted",
            "Tool-call budget exhausted for this run token.",
            http_status=429,
        )
    used = conn.execute(
        "SELECT used_tool_calls FROM agent_run_tokens WHERE token_id = ?",
        (token_row["token_id"],),
    ).fetchone()
    if used["used_tool_calls"] >= token_row["budget_tool_calls"]:
        conn.execute(
            "UPDATE agent_run_tokens SET status = 'exhausted' WHERE token_id = ?",
            (token_row["token_id"],),
        )
    return int(used["used_tool_calls"])


def revoke_run_token(conn: sqlite3.Connection, token_row: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE agent_run_tokens SET status = 'revoked' WHERE token_id = ? AND status = 'active'",
        (token_row["token_id"],),
    )


# --------------------------------------------------------------------------
# Worker lifecycle
# --------------------------------------------------------------------------

def claim_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    worker_id: str,
    lease_seconds: int = 120,
) -> dict[str, Any]:
    """Claim a queued/running run: bumps claim_epoch and the attempt counter."""
    try:
        run = tasks.claim_agent_run(
            conn, run_id, worker_id=worker_id, lease_seconds=lease_seconds
        )
    except tasks.TaskError as exc:
        raise AgentHostError(exc.code, exc.message, http_status=exc.http_status) from exc
    conn.execute(
        """
        UPDATE agent_runs
        SET attempts = attempts + 1, heartbeat_at = CURRENT_TIMESTAMP
        WHERE run_id = ?
        """,
        (run_id,),
    )
    # Record the execution epoch on the token as audit context. The token
    # itself is NOT rotated here (the dispatcher may hold only this bearer);
    # business fencing is enforced against the run row in agent_gateway.
    conn.execute(
        """
        UPDATE agent_run_tokens
        SET claim_epoch = ?, worker_id = ?
        WHERE run_id = ? AND status = 'active'
        """,
        (run["claim_epoch"], worker_id, run_id),
    )
    claimed = get_run(conn, run_id)
    claimed["worker_id"] = worker_id
    return claimed


def heartbeat_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    claim_epoch: int,
    lease_seconds: int = 120,
) -> dict[str, Any]:
    """Extend the lease only if the caller still holds the current epoch."""
    cursor = conn.execute(
        """
        UPDATE agent_runs
        SET lease_expires_at = datetime('now', ?), heartbeat_at = CURRENT_TIMESTAMP
        WHERE run_id = ? AND claim_epoch = ? AND status = 'running'
        """,
        (f"+{int(lease_seconds)} seconds", run_id, claim_epoch),
    )
    if cursor.rowcount != 1:
        raise AgentHostError(
            "stale_worker",
            f"Heartbeat rejected: run {run_id!r} is not held at epoch {claim_epoch}.",
            http_status=409,
        )
    return get_run(conn, run_id)


def finish_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    claim_epoch: int | None,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Terminal write with epoch fencing; also revokes the run token."""
    row = conn.execute(
        "SELECT token_id FROM agent_run_tokens WHERE run_id = ?", (run_id,)
    ).fetchone()
    try:
        finished = tasks.finish_agent_run(
            conn, run_id, status=status, result=result, claim_epoch=claim_epoch
        )
    except tasks.TaskError as exc:
        raise AgentHostError(exc.code, exc.message, http_status=exc.http_status) from exc
    if row is not None:
        revoke = conn.execute(
            "UPDATE agent_run_tokens SET status = 'revoked' "
            "WHERE run_id = ? AND status = 'active'",
            (run_id,),
        )
        del revoke
    conn.execute(
        "UPDATE agent_runs SET error_code = ? WHERE run_id = ?", (error_code, run_id)
    )
    return get_run(conn, run_id)


def recover_expired_runs(conn: sqlite3.Connection, *, lease_buffer_s: int = 0) -> list[dict[str, Any]]:
    """Re-queue runs whose lease expired; the next claim bumps the epoch.

    Business effects are NEVER rolled back here: anything already applied
    stays applied and is answered from operation_results on resume (§12.13).
    """
    rows = conn.execute(
        """
        SELECT run_id, claim_epoch, attempts FROM agent_runs
        WHERE status = 'running'
          AND lease_expires_at IS NOT NULL
          AND lease_expires_at <= datetime('now', ?)
        """,
        (f"-{int(lease_buffer_s)} seconds",),
    ).fetchall()
    recovered = []
    for row in rows:
        conn.execute(
            """
            UPDATE agent_runs
            SET status = 'queued', worker_id = NULL, error_code = 'lease_expired'
            WHERE run_id = ? AND status = 'running'
              AND claim_epoch = ?
            """,
            (row["run_id"], row["claim_epoch"]),
        )
        recovered.append({"run_id": row["run_id"], "previous_epoch": row["claim_epoch"], "attempts": row["attempts"]})
    return recovered


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise AgentHostError("invalid_state", f"Unknown run {run_id!r}.", http_status=404)
    payload = dict(row)
    if payload.get("result_json"):
        payload["result"] = json.loads(payload["result_json"])
    return payload


def list_claimable_runs(conn: sqlite3.Connection, *, agent_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM agent_runs WHERE status = 'queued'"
    params: tuple[Any, ...] = ()
    if agent_id is not None:
        sql += " AND agent_id = ?"
        params = (agent_id,)
    sql += " ORDER BY rowid LIMIT 50"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


# --------------------------------------------------------------------------
# Recovery: reuse committed receipts instead of replaying side effects
# --------------------------------------------------------------------------

def reuse_operation_result(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    step_name: str,
    input_version: str,
) -> dict[str, Any] | None:
    """Find a checkpoint for (task, step) whose input is unchanged.

    Returns the recorded output refs and the operation_id of the already
    committed business operation, so a resumed worker can reuse the receipt
    from ``operation_results`` instead of re-applying (§12.13).
    """
    checkpoint = tasks.latest_checkpoint(conn, task_id, step_name)
    if checkpoint is None or checkpoint["input_version"] != input_version:
        return None
    operation_id = checkpoint["operation_id"]
    receipt = None
    if operation_id:
        row = conn.execute(
            "SELECT response_json FROM operation_results WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is not None:
            receipt = {
                "operation_id": operation_id,
                "response": json.loads(row["response_json"]),
            }
    return {
        "checkpoint_id": checkpoint["checkpoint_id"],
        "step_name": step_name,
        "input_version": checkpoint["input_version"],
        "output_refs": json.loads(checkpoint["output_refs_json"]),
        "operation_id": operation_id,
        "receipt": receipt,
    }
