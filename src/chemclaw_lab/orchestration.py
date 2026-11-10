"""Small durable boundary for model-directed sessions and delegation.

The model owns planning and role selection. Core only binds identity, limits
execution and records results. No workflow graph or semantic routing lives here.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from . import agents, auth, tasks
from .db import connect

MAX_CHILDREN = 8
MAX_DEPTH = 3
_SESSION_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_HOST_CAPABILITIES = {
    "agent.registry.load", "agent.registry.set_enabled", "run.claim",
    "run.complete", "run.recover", "delivery.process", "assistant.agent.start",
    "assistant.agent.finish", "task.dispatch",
    *{f"assistant.job.{name}" for name in ("submit", "list", "read", "claim", "heartbeat", "complete", "cancel", "preprocess")},
}


def metadata(conn, task_id: str) -> dict[str, Any]:
    row = tasks.latest_checkpoint(conn, task_id, "agent.session")
    return json.loads(row["input_version"]) if row else {}


def _record_metadata(conn, task_id: str, value: dict[str, Any]) -> None:
    tasks.record_checkpoint(
        conn, task_id=task_id, step_name="agent.session",
        input_version=json.dumps(value, ensure_ascii=False, sort_keys=True),
    )


def _running(conn, token_row: dict[str, Any]) -> dict[str, Any]:
    run = agents.get_run(conn, token_row["run_id"])
    if run["status"] != "running" or int(run["claim_epoch"]) != int(token_row.get("claim_epoch") or 0):
        raise agents.AgentHostError("stale_worker", "A current running execution lease is required.", http_status=409)
    context = agents.principal_from_token(conn, token_row)
    if not context.memberships:
        raise agents.AgentHostError("forbidden_scope", "Run no longer has project access.", http_status=403)
    if not conn.execute("SELECT 1 FROM agent_runs WHERE run_id=? AND lease_expires_at > datetime('now')", (run["run_id"],)).fetchone():
        raise agents.AgentHostError("stale_worker", "Execution lease has expired.", http_status=409)
    root_id = metadata(conn, run["task_id"]).get("root_run_id", run["run_id"])
    job = conn.execute("SELECT status,lease_expires_at FROM assistant_jobs WHERE run_id=?", (root_id,)).fetchone()
    if job and (job["status"] != "running" or not conn.execute("SELECT 1 FROM assistant_jobs WHERE run_id=? AND lease_expires_at > datetime('now')", (root_id,)).fetchone()):
        raise agents.AgentHostError("stale_worker", "Assistant job is stopped or its lease expired.", http_status=409)
    return run


def session_start(db_path: str, context: auth.PrincipalContext, params: dict[str, Any]) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = session_start_conn(conn, context, params)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def session_start_conn(conn, context: auth.PrincipalContext, params: dict[str, Any]) -> dict[str, Any]:
    project_id, text = params.get("project_id"), params.get("text")
    if not isinstance(project_id, str) or not project_id.strip():
        raise agents.AgentHostError("schema_violation", "project_id is required.")
    context.require_project(project_id, "member")
    if not isinstance(text, str) or not text.strip() or len(text) > 16000:
        raise agents.AgentHostError("schema_violation", "text must contain 1 to 16000 characters.")
    session_id = params.get("session_id") or f"sess_{uuid.uuid4().hex}"
    if not isinstance(session_id, str) or not _SESSION_RE.fullmatch(session_id):
        raise agents.AgentHostError("schema_violation", "Invalid session_id.")
    supplied_context = params.get("context") or {}
    if not isinstance(supplied_context, dict) or len(json.dumps(supplied_context)) > 65536:
        raise agents.AgentHostError("schema_violation", "context must be an object of at most 64 KiB.")
    history = []
    rows = conn.execute(
        "SELECT message_type, payload_json FROM typed_messages WHERE correlation_id = ? ORDER BY rowid DESC LIMIT 80",
        (session_id,),
    ).fetchall()
    for row in reversed(rows):
        if row["message_type"] not in ("assistant.agent.user", "assistant.agent.reply"):
            continue  # Legacy messages carry no ownership binding.
        payload = json.loads(row["payload_json"])
        if payload.get("principal_id") != context.principal_id or payload.get("project_id") != project_id:
            raise agents.AgentHostError("forbidden_scope", "Session belongs to another principal or project.", http_status=403)
        if row["message_type"] in ("assistant.agent.user", "assistant.agent.reply"):
            entry = {"role": "user" if row["message_type"].endswith("user") else "assistant", "text": payload.get("text", "")}
            historical_context = dict(payload.get("context") or {})
            if payload.get("object_refs"):
                historical_context["object_refs"] = payload["object_refs"]
            data = payload.get("data")
            if isinstance(data, dict) and isinstance(data.get("pages"), list):
                historical_context["pages"] = data["pages"]
            if historical_context:
                entry["context"] = historical_context
            history.append(entry)
    dispatched = agents.dispatch(
        conn, intent="assistant", context=context, project_id=project_id,
        title=text.strip()[:120], input_payload={"text": text.strip(), "session_id": session_id, "context": supplied_context},
    )
    manifest = agents.get_manifest(conn, dispatched["agent_id"], version=dispatched["manifest_version"])
    if manifest["mode"] != "orchestrator":
        raise agents.AgentHostError("invalid_state", "assistant intent must select an orchestrator manifest.", http_status=409)
    run = agents.claim_run(conn, run_id=dispatched["run_id"], worker_id="web_agent_host", lease_seconds=int(manifest["timeout_s"]) + 60)
    tasks.transition_task(conn, dispatched["task_id"], "running")
    message = tasks.record_typed_message(
        conn, message_kind="command", message_type="assistant.agent.user",
        payload={"intent": "assistant", "principal_id": context.principal_id, "project_id": project_id, "text": text.strip(), "context": supplied_context, "run_id": run["run_id"]},
        correlation_id=session_id,
    )
    _record_metadata(conn, dispatched["task_id"], {
        "session_id": session_id, "message_id": message["message_id"], "root_run_id": run["run_id"],
        "parent_run_id": None, "depth": 0,
        "remaining_depth": min(MAX_DEPTH, manifest["max_delegation_depth"]),
    })
    return {**dispatched, "claim_epoch": run["claim_epoch"], "session_id": session_id, "history": history[-40:], "manifest": manifest}


def session_finish(db_path: str, context: auth.PrincipalContext, params: dict[str, Any]) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = session_finish_conn(conn, context, params)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def session_finish_conn(conn, context: auth.PrincipalContext, params: dict[str, Any]) -> dict[str, Any]:
    project_id = params.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise agents.AgentHostError("schema_violation", "project_id is required.")
    context.require_project(project_id, "member")
    result = params.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        raise agents.AgentHostError("schema_violation", "result.text is required.")
    public_status = result.get("status", "completed")
    if public_status not in ("completed", "clarification_needed", "needs_review", "partial", "failed"):
        raise agents.AgentHostError("schema_violation", "Invalid session result status.")
    result = {key: value for key, value in result.items() if key in (
        "text", "status", "object_refs", "events", "delegations", "warnings", "data", "knowledge_delivery",
        "capture_progress", "runtime_budget_hint",
    )}
    result["status"] = public_status
    for key in ("object_refs", "events", "delegations", "warnings"):
        if key in result and not isinstance(result[key], list):
            raise agents.AgentHostError("schema_violation", f"result.{key} must be an array.")
    if "data" in result and not isinstance(result["data"], dict):
        raise agents.AgentHostError("schema_violation", "result.data must be an object.")
    if "knowledge_delivery" in result and not isinstance(result["knowledge_delivery"], dict):
        raise agents.AgentHostError("schema_violation", "result.knowledge_delivery must be an object.")
    # Bounded control metadata, never an authority to create/verify knowledge.
    # Continuation hosts re-read current sources and objects before reusing it.
    for key in ("capture_progress", "runtime_budget_hint"):
        if key not in result:
            continue
        if not isinstance(result[key], dict):
            raise agents.AgentHostError("schema_violation", f"result.{key} must be an object.")
        try:
            size = len(json.dumps(result[key], ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError, OverflowError):
            raise agents.AgentHostError("schema_violation", f"result.{key} must contain finite JSON values.") from None
        if size > 262144:
            raise agents.AgentHostError("schema_violation", f"result.{key} exceeds its metadata size limit.")
    if "capture_progress" in result:
        progress = result["capture_progress"]
        if (type(progress.get("version")) is not int or progress["version"] != 1
                or progress.get("knowledge_complete") is not None
                or not isinstance(progress.get("pages", []), list)):
            raise agents.AgentHostError("schema_violation", "Invalid structural capture progress; completeness must remain unknown.")
    run = agents.get_run(conn, params.get("run_id"))
    task = tasks.get_task(conn, run["task_id"])
    meta = metadata(conn, run["task_id"])
    if task["principal_id"] != context.principal_id or task["project_id"] != project_id or not meta or meta.get("parent_run_id"):
        raise agents.AgentHostError("forbidden_scope", "Only the initiating principal can finish this session.", http_status=403)
    # A partial result may contain committed receipts, but execution did
    # not finish the requested task. Preserve those receipts while
    # keeping run/task outcome consistent with the public result.
    terminal = "failed" if public_status in ("failed", "partial") else "succeeded"
    finished = agents.finish_run(conn, run_id=run["run_id"], status=terminal, claim_epoch=params.get("claim_epoch"), result=result)
    # Any outstanding child leases stop with the parent. No orphan token
    # may continue making business writes after the user sees completion.
    cancel_descendants(conn, run["run_id"])
    current_task = tasks.get_task(conn, task["task_id"])
    if current_task["status"] == "running":
        tasks.transition_task(conn, task["task_id"], "waiting_input" if public_status in ("clarification_needed", "needs_review") else terminal)
    reply = tasks.record_typed_message(
        conn, message_kind="task_result", message_type="assistant.agent.reply",
        payload={**result, "principal_id": context.principal_id, "project_id": project_id, "run_id": run["run_id"], "task_id": task["task_id"], "outcome": "needs_input" if public_status in ("clarification_needed", "needs_review") else terminal},
        correlation_id=meta["session_id"], causation_id=meta["message_id"],
    )
    return {**result, "session_id": meta["session_id"], "message_id": reply["message_id"], "task_id": task["task_id"], "run_id": finished["run_id"], "agent": run["agent_id"], "intent": "assistant"}


def roles(conn, token_row: dict[str, Any]) -> list[dict[str, Any]]:
    _running(conn, token_row)
    allowed = []
    for item in agents.list_manifests(conn):
        manifest = item["manifest"]
        if not item["enabled"] or manifest["agent_id"] == token_row["agent_id"] or manifest["mode"] == "orchestrator":
            continue
        if set(manifest["required_capabilities"]) & _HOST_CAPABILITIES:
            continue
        allowed.append({key: manifest[key] for key in (
            "agent_id", "version", "display_name", "mode", "required_capabilities",
            "system_prompt_ref", "tool_call_budget", "max_delegation_depth", "timeout_s",
        )})
    return allowed


def delegate(conn, token_row: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    _running(conn, token_row)
    parent_meta = metadata(conn, token_row["task_id"])
    if not parent_meta or parent_meta["remaining_depth"] <= 0:
        raise agents.AgentHostError("delegation_depth_exceeded", "This run cannot delegate further.", http_status=403)
    message = params.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > 16000:
        raise agents.AgentHostError("schema_violation", "message must contain 1 to 16000 characters.")
    candidates = {role["agent_id"]: role for role in roles(conn, token_row)}
    target = params.get("agent_id")
    if not isinstance(target, str) or target not in candidates:
        raise agents.AgentHostError("forbidden_agent", "Target role must be an enabled delegatable manifest.", http_status=403)
    delegation_id = params.get("delegation_id")
    request_hash = hashlib.sha256(json.dumps({"agent_id": target, "message": message.strip()}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if delegation_id is not None:
        if not isinstance(delegation_id, str) or not 1 <= len(delegation_id) <= 240:
            raise agents.AgentHostError("schema_violation", "Invalid delegation_id.")
        previous = conn.execute("SELECT * FROM assistant_delegations WHERE parent_run_id=? AND delegation_id=?", (token_row["run_id"], delegation_id)).fetchone()
        if previous:
            if previous["request_hash"] != request_hash:
                raise agents.AgentHostError("idempotency_conflict", "Delegation ID belongs to another request.", http_status=409)
            from . import agent_jobs
            child_run = agents.get_run(conn, previous["child_run_id"])
            reply = agent_jobs._rotate(conn, child_run, f"parent:{token_row['run_id']}", 120) if child_run["status"] in ("queued", "running") else agent_jobs._descriptor(conn, child_run)
            cp = conn.execute("SELECT checkpoint_json FROM assistant_runtime_checkpoints WHERE run_id=?", (child_run["run_id"],)).fetchone()
            if cp:
                reply["checkpoint"] = json.loads(cp["checkpoint_json"])
            return reply
    root_run = agents.get_run(conn, parent_meta["root_run_id"])
    if root_run["status"] != "running":
        raise agents.AgentHostError("stale_worker", "Parent session is no longer running.", http_status=409)
    root_meta = metadata(conn, root_run["task_id"])
    count = int(root_meta.get("children_created", 0))
    if count >= MAX_CHILDREN:
        raise agents.AgentHostError("delegation_budget_exhausted", "Session child limit reached.", http_status=429)
    # A delegate is itself a tool call and spends the shared root allowance.
    agents.consume_tool_budget(conn, token_row)
    scoped = agents.principal_from_token(conn, token_row)
    parent_task = tasks.get_task(conn, token_row["task_id"])
    project_id = parent_task["project_id"]
    scoped.require_project(project_id, "member")
    child = agents.dispatch(conn, intent="delegated", agent_id=target, context=scoped, project_id=project_id, trace_id=token_row["trace_id"],
        title=message.strip()[:120], input_payload={"message": message.strip(), "parent_run_id": token_row["run_id"]})
    manifest = agents.get_manifest(conn, target, version=child["manifest_version"])
    run = agents.claim_run(conn, run_id=child["run_id"], worker_id=f"parent:{token_row['run_id']}", lease_seconds=int(manifest["timeout_s"]) + 60)
    tasks.transition_task(conn, child["task_id"], "running")
    # Descendants share the trace and frozen project scope of their parent.
    conn.execute("UPDATE agent_run_tokens SET evidence_scope_json = ? WHERE run_id = ?", (
        json.dumps({"root_run_id": parent_meta["root_run_id"]}), child["run_id"],
    ))
    _record_metadata(conn, child["task_id"], {
        "session_id": parent_meta["session_id"], "root_run_id": parent_meta["root_run_id"],
        "parent_run_id": token_row["run_id"], "depth": parent_meta["depth"] + 1,
        "remaining_depth": min(parent_meta["remaining_depth"] - 1, manifest["max_delegation_depth"]),
    })
    root_meta["children_created"] = count + 1
    _record_metadata(conn, root_run["task_id"], root_meta)
    if delegation_id is not None:
        conn.execute("INSERT INTO assistant_delegations(parent_run_id,delegation_id,request_hash,child_run_id) VALUES(?,?,?,?)", (token_row["run_id"], delegation_id, request_hash, child["run_id"]))
    return {**child, "trace_id": token_row["trace_id"], "claim_epoch": run["claim_epoch"], "manifest": manifest}


def complete_child(conn, token_row: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    _running(conn, token_row)
    run = agents.get_run(conn, params.get("run_id"))
    meta = metadata(conn, run["task_id"])
    if meta.get("parent_run_id") != token_row["run_id"]:
        raise agents.AgentHostError("forbidden_scope", "Only the direct parent may finish this child.", http_status=403)
    if run["status"] in ("succeeded", "failed", "cancelled"):
        return {"run": run}
    result = params.get("result") or {}
    if not isinstance(result, dict):
        raise agents.AgentHostError("schema_violation", "result must be an object.")
    finished = agents.finish_run(conn, run_id=run["run_id"], status=params.get("status", "succeeded"), claim_epoch=params.get("claim_epoch"), result=result)
    cancel_descendants(conn, run["run_id"])
    task = tasks.get_task(conn, run["task_id"])
    if task["status"] == "running":
        tasks.transition_task(conn, task["task_id"], finished["status"])
    return {"run": finished}


def cancel_descendants(conn, parent_run_id: str) -> None:
    rows = conn.execute("SELECT run_id, task_id, claim_epoch FROM agent_runs WHERE status = 'running'").fetchall()
    for row in rows:
        if metadata(conn, row["task_id"]).get("parent_run_id") == parent_run_id:
            cancel_descendants(conn, row["run_id"])
            agents.finish_run(conn, run_id=row["run_id"], status="cancelled", claim_epoch=row["claim_epoch"], result={"reason": "parent_finished"})
            task = tasks.get_task(conn, row["task_id"])
            if task["status"] in ("running", "waiting_input", "queued"):
                tasks.transition_task(conn, row["task_id"], "cancelled")
