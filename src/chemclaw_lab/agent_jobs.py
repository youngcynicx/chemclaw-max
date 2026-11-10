"""Durable model-directed work: identity, leases and receipts, never routing."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from contextlib import nullcontext
from typing import Any

from . import agents, auth, orchestration, tasks
from .db import connect

TERMINAL = {"completed", "clarification_needed", "needs_review", "partial", "failed", "cancelled"}
_SECRET_KEYS = {"api_key", "apikey", "authorization", "run_token", "token_hash", "password", "secret", "access_token", "refresh_token", "base64", "bytes", "content_base64"}


def safe_json(value, *, limit=2 * 1024 * 1024):
    def visit(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).lower() in _SECRET_KEYS:
                    raise agents.AgentHostError("schema_violation", "Persist references and receipts, never credentials or raw attachment bytes.")
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str) and re.search(r"(?:Bearer\s+[A-Za-z0-9_.-]{16,}|sk-[A-Za-z0-9_-]{16,}|data:[^;]+;base64,)", item):
            raise agents.AgentHostError("schema_violation", "Credentials and inline attachment data cannot be persisted.")
    visit(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > limit:
        raise agents.AgentHostError("schema_violation", "Persisted payload is too large.")
    return encoded


def _public(row):
    out = dict(row)
    for column, key in (("context_json", "context"), ("attachments_json", "attachments_manifest"), ("result_json", "result")):
        raw = out.pop(column, None)
        out[key] = json.loads(raw) if raw else None
    out.pop("started_json", None)
    out.pop("request_hash", None)
    return out


def _project(context, params):
    project = params.get("project_id")
    if not isinstance(project, str) or not project:
        raise agents.AgentHostError("schema_violation", "project_id is required.")
    context.require_project(project, "member")
    if context.kind != "human":
        raise agents.AgentHostError("forbidden_scope", "Assistant jobs require an initiating human authority.", http_status=403)
    return project


def _owned(conn, context, params):
    project = _project(context, params)
    row = conn.execute("SELECT * FROM assistant_jobs WHERE job_id = ? AND principal_id = ? AND project_id = ?", (params.get("job_id"), context.principal_id, project)).fetchone()
    if row is None:
        raise agents.AgentHostError("not_found", "Job not found in the initiating principal's project.", http_status=404)
    return dict(row)


def _event(conn, job_id, event, run_id=None):
    conn.execute("INSERT INTO assistant_job_events(job_id,run_id,event_json) VALUES(?,?,?)", (job_id, run_id, safe_json(event, limit=65536)))


def _events(conn, job_id, after=0):
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise agents.AgentHostError("schema_violation", "after_seq must be a nonnegative integer.")
    return [{**json.loads(row["event_json"]), "seq": row["seq"], "run_id": row["run_id"], "created_at": row["created_at"]} for row in conn.execute("SELECT * FROM assistant_job_events WHERE job_id = ? AND seq > ? ORDER BY seq LIMIT 500", (job_id, after))]


def submit(conn, context, params):
    project = _project(context, params)
    text = params.get("text")
    request_id = params.get("request_id")
    if not isinstance(text, str) or not text.strip() or len(text) > 16000:
        raise agents.AgentHostError("schema_violation", "text must contain 1 to 16000 characters.")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", request_id):
        raise agents.AgentHostError("schema_violation", "request_id must be a stable identifier of at most 160 characters.")
    session_id = params.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or not orchestration._SESSION_RE.fullmatch(session_id)):
        raise agents.AgentHostError("schema_violation", "Invalid session_id.")
    supplied = params.get("context") or {}
    attachments = params.get("attachments_manifest") or []
    if not isinstance(supplied, dict) or not isinstance(attachments, list):
        raise agents.AgentHostError("schema_violation", "context must be an object and attachments_manifest an array.")
    pages = supplied.get("pages", [])
    if not isinstance(pages, list) or len(pages) > 100:
        raise agents.AgentHostError("schema_violation", "context.pages must be an array with at most 100 pages.")
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("page_id"), str) or not page["page_id"].strip() or not isinstance(page.get("mime"), str) or not page["mime"].strip():
            raise agents.AgentHostError("schema_violation", "Each page reference requires a page_id and mime string.")
        if not conn.execute("SELECT 1 FROM evidence_pages p JOIN evidence_documents d ON d.document_id=p.document_id WHERE p.page_id=? AND d.project_id=?", (page["page_id"], project)).fetchone():
            raise agents.AgentHostError("forbidden_scope", "Page reference is outside the job project.", http_status=403)
    if "object_refs" in supplied and not isinstance(supplied["object_refs"], list):
        raise agents.AgentHostError("schema_violation", "context.object_refs must be an array.")
    ctx_json = safe_json(supplied, limit=65536)
    attachments_json = safe_json(attachments, limit=65536)
    fingerprint = hashlib.sha256(safe_json({"text": text.strip(), "session_id": session_id, "context": supplied, "attachments_manifest": attachments}).encode()).hexdigest()
    previous = conn.execute("SELECT * FROM assistant_jobs WHERE principal_id=? AND project_id=? AND request_id=?", (context.principal_id, project, request_id)).fetchone()
    if previous:
        if previous["request_hash"] != fingerprint:
            raise agents.AgentHostError("idempotency_conflict", "request_id already belongs to different task input.", http_status=409)
        return {"job": _public(previous), "duplicate": True}
    if session_id:
        for entry in conn.execute("SELECT principal_id,project_id FROM assistant_jobs WHERE session_id=?", (session_id,)):
            if entry["principal_id"] != context.principal_id or entry["project_id"] != project:
                raise agents.AgentHostError("forbidden_scope", "Session belongs to another principal or project.", http_status=403)
    job_id = f"job_{uuid.uuid4().hex}"
    conn.execute("INSERT INTO assistant_jobs(job_id,principal_id,project_id,request_id,request_hash,session_id,text,context_json,attachments_json) VALUES(?,?,?,?,?,?,?,?,?)", (job_id, context.principal_id, project, request_id, fingerprint, session_id or f"sess_{uuid.uuid4().hex}", text.strip(), ctx_json, attachments_json))
    _event(conn, job_id, {"type": "queued", "text": "任务已保存，等待后台执行"})
    return {"job": _public(_owned(conn, context, {**params, "job_id": job_id}))}


def read(conn, context, params):
    row = _owned(conn, context, params)
    events = _events(conn, row["job_id"], params.get("after_seq", 0))
    return {"job": _public(row), "events": events, "last_seq": events[-1]["seq"] if events else params.get("after_seq", 0)}


def list_jobs(conn, context, params):
    project = _project(context, params)
    clauses, values = ["principal_id=?", "project_id=?"], [context.principal_id, project]
    for key in ("session_id", "request_id"):
        if params.get(key):
            clauses.append(f"{key}=?")
            values.append(params[key])
    statuses = params.get("statuses")
    if statuses:
        if not isinstance(statuses, list) or any(s not in TERMINAL | {"queued", "running"} for s in statuses):
            raise agents.AgentHostError("schema_violation", "Invalid statuses.")
        clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
        values.extend(statuses)
    limit = params.get("limit", 100)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise agents.AgentHostError("schema_violation", "limit must be 1 to 200.")
    rows = conn.execute("SELECT * FROM assistant_jobs WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*values, limit)).fetchall()
    return {"jobs": [_public(row) for row in rows]}


def _lease(params):
    seconds = params.get("lease_seconds", 60)
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 15 <= seconds <= 300:
        raise agents.AgentHostError("schema_violation", "lease_seconds must be 15 to 300.")
    worker = params.get("worker_id")
    if not isinstance(worker, str) or not 1 <= len(worker) <= 160:
        raise agents.AgentHostError("schema_violation", "worker_id is required.")
    return worker, seconds


def _fence(conn, row, params):
    if row["status"] != "running" or row["worker_id"] != params.get("worker_id") or row["claim_epoch"] != params.get("claim_epoch"):
        raise agents.AgentHostError("stale_worker", "Job execution lease is no longer current.", http_status=409)
    if not conn.execute("SELECT 1 FROM assistant_jobs WHERE job_id=? AND lease_expires_at > datetime('now')", (row["job_id"],)).fetchone():
        raise agents.AgentHostError("stale_worker", "Job lease expired; claim the same job before continuing.", http_status=409)


def _runs(conn, root_run_id):
    root = agents.get_run(conn, root_run_id)
    found = [root]
    for row in conn.execute("SELECT run_id,task_id FROM agent_runs WHERE trace_id=? AND run_id<>?", (root["trace_id"], root_run_id)).fetchall():
        if orchestration.metadata(conn, row["task_id"]).get("root_run_id") == root_run_id:
            found.append(agents.get_run(conn, row["run_id"]))
    return found


def _descriptor(conn, run):
    return {"run_id": run["run_id"], "task_id": run["task_id"], "agent_id": run["agent_id"], "claim_epoch": run["claim_epoch"], "status": run["status"], "result": run.get("result"), "manifest": agents.get_manifest(conn, run["agent_id"], version=run["manifest_version"])}


def _rotate(conn, run, worker, seconds):
    # Change only the bearer digest; preserve its scope and consumed budget.
    # Old in-flight requests are fenced by both epoch and token digest.
    plaintext = secrets.token_urlsafe(32)
    conn.execute("UPDATE agent_runs SET status='queued' WHERE run_id=? AND status='running'", (run["run_id"],))
    run = agents.claim_run(conn, run_id=run["run_id"], worker_id=worker, lease_seconds=seconds)
    row = conn.execute("SELECT * FROM agent_run_tokens WHERE run_id=? ORDER BY rowid DESC LIMIT 1", (run["run_id"],)).fetchone()
    if row is None:
        raise agents.AgentHostError("invalid_state", "Missing execution token binding.", http_status=409)
    conn.execute("UPDATE agent_run_tokens SET token_hash=?,claim_epoch=?,worker_id=?,status=?,expires_at=datetime('now',?) WHERE token_id=?", (agents._hash_token(plaintext), run["claim_epoch"], worker, "exhausted" if row["used_tool_calls"] >= row["budget_tool_calls"] else "active", f"+{seconds + 60} seconds", row["token_id"]))
    return {**_descriptor(conn, run), "run_token": plaintext}


def claim(conn, context, params):
    row = _owned(conn, context, params)
    worker, seconds = _lease(params)
    if row["status"] in TERMINAL:
        raise agents.AgentHostError("invalid_state", "Job is already finished.", http_status=409)
    if row["status"] == "running" and conn.execute("SELECT 1 FROM assistant_jobs WHERE job_id=? AND lease_expires_at > datetime('now')", (row["job_id"],)).fetchone():
        raise agents.AgentHostError("run_lease_active", "Another worker currently owns this job.", http_status=409)
    # Serialize turns within a session, so history cannot skip an unfinished turn.
    if conn.execute("SELECT 1 FROM assistant_jobs WHERE session_id=? AND job_id<>? AND (status='running' OR (status='queued' AND rowid < (SELECT rowid FROM assistant_jobs WHERE job_id=?)))", (row["session_id"], row["job_id"], row["job_id"])).fetchone():
        raise agents.AgentHostError("session_busy", "An earlier turn in this session is still pending.", http_status=409)
    started = json.loads(row["started_json"]) if row["started_json"] else None
    if not row["run_id"]:
        started = orchestration.session_start_conn(conn, context, {"project_id": row["project_id"], "session_id": row["session_id"], "text": row["text"], "context": json.loads(row["context_json"])})
        row["run_id"] = started["run_id"]
        # Tokens returned from session creation are kept only in this response.
        stored = {key: value for key, value in started.items() if key != "run_token"}
        conn.execute("UPDATE assistant_jobs SET run_id=?,started_json=? WHERE job_id=?", (row["run_id"], safe_json(stored), row["job_id"]))
    descriptors = {}
    checkpoints = {}
    for run in _runs(conn, row["run_id"]):
        desc = _rotate(conn, run, worker, seconds) if run["status"] in ("queued", "running") else _descriptor(conn, run)
        descriptors[run["run_id"]] = desc
        checkpoint = conn.execute("SELECT checkpoint_json FROM assistant_runtime_checkpoints WHERE run_id=?", (run["run_id"],)).fetchone()
        if checkpoint:
            checkpoints[run["run_id"]] = json.loads(checkpoint["checkpoint_json"])
    started.update(descriptors[row["run_id"]])
    conn.execute("UPDATE assistant_jobs SET status='running',worker_id=?,claim_epoch=?,lease_expires_at=datetime('now',?),updated_at=CURRENT_TIMESTAMP WHERE job_id=?", (worker, started["claim_epoch"], f"+{seconds} seconds", row["job_id"]))
    _event(conn, row["job_id"], {"type": "resumed" if row["status"] == "running" else "started", "text": "后台已接管任务", "claim_epoch": started["claim_epoch"]}, row["run_id"])
    return {"job": _public(_owned(conn, context, params)), "started": started, "resume": {"checkpoints": checkpoints, "runs": descriptors}}


def heartbeat(conn, context, params):
    row = _owned(conn, context, params)
    worker, seconds = _lease(params)
    _fence(conn, row, params)
    conn.execute("UPDATE assistant_jobs SET lease_expires_at=datetime('now',?),updated_at=CURRENT_TIMESTAMP WHERE job_id=?", (f"+{seconds} seconds", row["job_id"]))
    for run in _runs(conn, row["run_id"]):
        if run["status"] == "running":
            agents.heartbeat_run(conn, run_id=run["run_id"], claim_epoch=run["claim_epoch"], lease_seconds=seconds)
            conn.execute("UPDATE agent_run_tokens SET expires_at=datetime('now',?) WHERE run_id=? AND status IN ('active','exhausted')", (f"+{seconds + 60} seconds", run["run_id"]))
    return {"job": _public(_owned(conn, context, params))}


def complete(conn, context, params):
    row = _owned(conn, context, params)
    if row["status"] in TERMINAL and row["status"] != "cancelled" and row["worker_id"] == params.get("worker_id") and row["claim_epoch"] == params.get("claim_epoch"):
        return {"job": _public(row), "result": json.loads(row["result_json"])}
    _fence(conn, row, params)
    result = params.get("result")
    safe_json(result)
    saved = orchestration.session_finish_conn(conn, context, {"project_id": row["project_id"], "run_id": row["run_id"], "claim_epoch": row["claim_epoch"], "result": result})
    # Preserve public host diagnostics outside the LLM's formal result contract.
    for key in ("runtime", "model", "modelUsed", "error", "error_code", "completion", "duration_ms", "runtime_error", "recovery", "classification", "toolCalls"):
        if isinstance(result, dict) and key in result:
            saved[key] = result[key]
    conn.execute("UPDATE assistant_jobs SET status=?,result_json=?,lease_expires_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE job_id=?", (saved["status"], safe_json(saved), row["job_id"]))
    _event(conn, row["job_id"], {"type": "finished", "status": saved["status"], "text": saved["text"][:1000]}, row["run_id"])
    return {"job": _public(_owned(conn, context, params)), "result": saved}


def cancel(conn, context, params):
    row = _owned(conn, context, params)
    if row["status"] in TERMINAL:
        return {"job": _public(row)}
    if row["run_id"]:
        orchestration.cancel_descendants(conn, row["run_id"])
        run = agents.get_run(conn, row["run_id"])
        if run["status"] in ("queued", "running"):
            agents.finish_run(conn, run_id=run["run_id"], status="cancelled", claim_epoch=run["claim_epoch"], result={"reason": "user_cancelled"})
        for item in _runs(conn, row["run_id"]):
            conn.execute("UPDATE agent_run_tokens SET status='revoked' WHERE run_id=?", (item["run_id"],))
            task = tasks.get_task(conn, item["task_id"])
            if task["status"] in ("queued", "running", "waiting_input"):
                tasks.transition_task(conn, item["task_id"], "cancelled")
    result = {"text": "任务已取消；已完成的操作和回执保留。", "status": "cancelled"}
    if row["run_id"]:
        meta = orchestration.metadata(conn, agents.get_run(conn, row["run_id"])["task_id"])
        if meta:
            tasks.record_typed_message(conn, message_kind="task_result", message_type="assistant.agent.reply",
                payload={**result, "principal_id": context.principal_id, "project_id": row["project_id"], "run_id": row["run_id"], "task_id": agents.get_run(conn, row["run_id"])["task_id"], "outcome": "cancelled"},
                correlation_id=meta["session_id"], causation_id=meta["message_id"])
    conn.execute("UPDATE assistant_jobs SET status='cancelled',result_json=?,lease_expires_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE job_id=?", (safe_json(result), row["job_id"]))
    _event(conn, row["job_id"], {"type": "cancelled", **result}, row["run_id"])
    return {"job": _public(_owned(conn, context, params))}


OPERATIONS = {"submit": submit, "read": read, "list": list_jobs, "claim": claim, "heartbeat": heartbeat, "complete": complete, "cancel": cancel}


def preprocess(conn, db_path, context, params, services):
    """Fence the host's parsing adapter with the same lease as model tools."""
    from . import capabilities
    from .agent_gateway import run_write_lock
    row = _owned(conn, context, params)
    if not row["run_id"]:
        raise agents.AgentHostError("invalid_state", "Claim the job before preprocessing.", http_status=409)
    with run_write_lock(conn, row["run_id"]):
        conn.execute("BEGIN IMMEDIATE")
        row = _owned(conn, context, params)
        _fence(conn, row, params)
        capability_id = params.get("capability_id")
        child_params = params.get("params")
        if capability_id not in ("evidence.transcribe.run", "evidence.transcript.import") or not isinstance(child_params, dict):
            raise agents.AgentHostError("forbidden_capability", "Job preprocessing only permits transcription of its own pages.", http_status=403)
        pages = json.loads(row["context_json"]).get("pages", [])
        allowed = {entry.get("page_id") for entry in pages if isinstance(entry, dict)}
        if not child_params.get("page_id") or child_params["page_id"] not in allowed:
            raise agents.AgentHostError("forbidden_scope", "Page is not an attachment of this job.", http_status=403)
        if child_params.get("project_id", row["project_id"]) != row["project_id"]:
            raise agents.AgentHostError("forbidden_scope", "Preprocessing project must match the job.", http_status=403)
        child_params = {**child_params, "project_id": row["project_id"]}
        operation_id = params.get("operation_id") or f"{row['job_id']}:{capability_id}:{child_params['page_id']}"
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 240:
            raise agents.AgentHostError("schema_violation", "Invalid preprocessing operation_id.")
        conn.commit()  # domain capability opens its own transaction
        envelope = capabilities.invoke_capability(db_path, context, capability_id, child_params,
            namespace=f"job-preprocess:{row['job_id']}", operation_id=operation_id, services=services)
        outcome = envelope.get("result") if isinstance(envelope.get("result"), dict) else {}
        error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        if envelope.get("action_status") == "rejected" or error or outcome.get("status") in ("failed", "rejected"):
            raise agents.AgentHostError(error.get("code") or outcome.get("error_code") or "preprocessing_failed",
                error.get("message") or outcome.get("error_message") or "Attachment preprocessing failed.", http_status=409)
        return {"envelope": envelope}


def invoke(db_path, context, operation, params, *, services=None):
    conn = connect(db_path)
    try:
        if operation == "preprocess":
            return preprocess(conn, db_path, context, params, services)
        # Gateway writes and lease rotation/cancel share the root lock. Read
        # again after acquiring it: an earlier lookup cannot authorize work.
        guard = nullcontext()
        if operation in ("claim", "cancel", "complete"):
            row = _owned(conn, context, params)
            if row["run_id"]:
                from .agent_gateway import run_write_lock
                guard = run_write_lock(conn, row["run_id"])
        with guard:
            conn.execute("BEGIN IMMEDIATE")
            result = OPERATIONS[operation](conn, context, params)
            conn.commit()
            return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def runtime_control(conn, token_row, action, body):
    run = orchestration._running(conn, token_row)
    meta = orchestration.metadata(conn, run["task_id"])
    root_id = meta.get("root_run_id", run["run_id"])
    job = conn.execute("SELECT * FROM assistant_jobs WHERE run_id=?", (root_id,)).fetchone()
    if job:
        if job["status"] != "running" or not conn.execute("SELECT 1 FROM assistant_jobs WHERE job_id=? AND lease_expires_at > datetime('now')", (job["job_id"],)).fetchone():
            raise agents.AgentHostError("stale_worker", "Job is stopped or execution lease expired.", http_status=409)
    if action == "status":
        return {"status": job["status"] if job else run["status"], "run_id": run["run_id"], "claim_epoch": run["claim_epoch"]}
    if action == "checkpoint":
        checkpoint = body.get("checkpoint")
        if not isinstance(checkpoint, dict) or checkpoint.get("run_id", run["run_id"]) != run["run_id"]:
            raise agents.AgentHostError("schema_violation", "checkpoint must belong to the current run.")
        encoded = safe_json(checkpoint)
        conn.execute("INSERT INTO assistant_runtime_checkpoints(run_id,checkpoint_json,claim_epoch) VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET checkpoint_json=excluded.checkpoint_json,claim_epoch=excluded.claim_epoch,updated_at=CURRENT_TIMESTAMP", (run["run_id"], encoded, run["claim_epoch"]))
        events = [body["event"]] if body.get("event") else []
    else:
        events = body.get("events")
        if not isinstance(events, list) or len(events) > 50:
            raise agents.AgentHostError("schema_violation", "events must be an array with at most 50 items.")
    if job:
        for event in events:
            if not isinstance(event, dict):
                raise agents.AgentHostError("schema_violation", "Each progress event must be an object.")
            _event(conn, job["job_id"], event, run["run_id"])
    return {"ok": True, "run_id": run["run_id"], "claim_epoch": run["claim_epoch"]}
