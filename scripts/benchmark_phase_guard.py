"""Pure scheduling decisions for a benchmark paused by provider quota.

The caller atomically saves returned manifests before another POST/OCR. This
module performs no network, model, database, sleep, retry or file operation.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re

WAITING = "waiting_for_provider_resource"
_QUOTA_CLASSES = {"provider_quota_exhausted", "insufficient_quota"}
_QUOTA_CODES = {"1113", "insufficient_quota"}
_CONTAINERS = ("data", "result", "delegations", "provider_requests", "provider_resource_failure")


def _identifier(value, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value):
        raise ValueError("guard identifiers must be bounded public IDs")
    return value


def _index(value):
    if type(value) is not int or value < 0:
        raise ValueError("step index must be a nonnegative integer")
    return value


def _timestamp(value):
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise ValueError("timestamp must be bounded text")
    return value


def find_provider_resource_failure(job_result):
    """Inspect typed result envelopes, never business text or arbitrary objects.

    A bare business `code:1113`, plain 429, or text mentioning quota/1113 does
    not qualify. Numeric 1113 is accepted only within a provider request/failure
    envelope. No response/error text is copied into the returned receipt.
    """
    root = job_result
    if isinstance(root, dict) and isinstance(root.get("job"), dict):
        root = root["job"].get("result", root["job"])
    seen = set()

    def visit(value, path, provider=False):
        if isinstance(value, list):
            for index, child in enumerate(value):
                found = visit(child, f"{path}[{index}]", provider)
                if found:
                    return found
            return None
        if not isinstance(value, dict) or id(value) in seen:
            return None
        seen.add(id(value))
        explicit = value.get("needs_provider_quota") is True
        classification = next((value.get(key) for key in ("error_class", "error_code", "code", "classification")
            if isinstance(value.get(key), str) and value[key] in _QUOTA_CLASSES), None)
        provider_code = value.get("provider_error_code")
        if provider_code is None and provider:
            provider_code = value.get("code")
        provider_code = str(provider_code) if type(provider_code) in (str, int) else None
        quota_code = provider and provider_code in _QUOTA_CODES
        if explicit or classification or quota_code:
            receipt = {"code": "provider_quota_exhausted", "source_path": path,
                "basis": "needs_provider_quota" if explicit else "classification" if classification else "provider_error_code",
                "provider_error_code": provider_code if provider_code in _QUOTA_CODES else None,
                "http_status": value.get("http_status") if type(value.get("http_status")) is int else None}
            for name in ("request_id", "call_id", "run_id", "agent_id"):
                candidate = value.get(name)
                if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", candidate):
                    receipt[name] = candidate
            return receipt
        for key in _CONTAINERS:
            if key in value:
                found = visit(value[key], f"{path}.{key}", provider or key in {"provider_requests", "provider_resource_failure"})
                if found:
                    return found
        return None

    return visit(root, "result")


def _submitted_steps(values):
    if not isinstance(values, list):
        raise ValueError("submitted_steps must be a list")
    result, indices, requests = [], set(), set()
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("submitted step must be an object")
        index = _index(value.get("step_index"))
        request = _identifier(value.get("request_id"))
        if index in indices or request in requests:
            raise ValueError("submitted step indices and request IDs must be unique")
        indices.add(index)
        requests.add(request)
        pages = value.get("page_ids", [])
        if not isinstance(pages, list):
            raise ValueError("page_ids must be a list")
        status = value.get("status")
        result.append({"step_index": index, "step_id": _identifier(value.get("step_id")),
            "request_id": request, "job_id": _identifier(value.get("job_id"), nullable=True),
            "page_ids": list(dict.fromkeys(_identifier(page) for page in pages)),
            "status": _identifier(status, nullable=True)})
    return result


def _validate_cursor_update(previous, submitted, next_index):
    if any(step["step_index"] >= next_index for step in submitted):
        raise ValueError("next_unsubmitted_step must follow every submitted step")
    if previous.get("next_unsubmitted_step", 0) > next_index:
        raise ValueError("cannot rewind the submitted-step cursor")
    by_index = {step["step_index"]: step for step in submitted}
    for old in previous.get("submitted_steps", []):
        new = by_index.get(old["step_index"])
        if new is None or any(new[key] != old[key] for key in ("step_id", "request_id")):
            raise ValueError("cannot remove or replace an existing submission")
        if old.get("job_id") is not None and old["job_id"] != new.get("job_id"):
            raise ValueError("cannot replace an existing job")
        if not set(old.get("page_ids", [])) <= set(new["page_ids"]):
            raise ValueError("cannot discard existing page IDs")
        if old.get("status") in {"completed", "partial", "failed", "cancelled"} and old["status"] != new.get("status"):
            raise ValueError("cannot revive or reclassify a terminal job")


def pause_for_provider_resource(manifest, job_result, *, project_id, session_id,
        submitted_steps, next_unsubmitted_step, timestamp=None):
    """Return a waiting manifest, or None if no structured quota signal exists.

    The caller must atomic_save the result before allowing further submissions.
    Unknown submitted job IDs are preserved as null and must be reconciled via
    request_id; this function never repeats a possibly accepted submission.
    """
    signal = find_provider_resource_failure(job_result)
    if signal is None:
        return None
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    submitted = _submitted_steps(submitted_steps)
    next_index = _index(next_unsubmitted_step)
    previous = manifest.get("provider_resource_guard")
    _validate_cursor_update(previous if isinstance(previous, dict) else {}, submitted, next_index)
    if isinstance(previous, dict) and previous.get("project_id") is not None and (
            previous.get("project_id") != project_id or previous.get("session_id") != session_id):
        raise ValueError("provider guard project/session mismatch")
    history = deepcopy(previous.get("history", [])) if isinstance(previous, dict) else []
    marker = {"schema_version": 1, "state": WAITING,
        "project_id": _identifier(project_id), "session_id": _identifier(session_id, nullable=True),
        "submitted_steps": submitted, "next_unsubmitted_step": next_index,
        "reason": signal, "paused_at_utc": _timestamp(timestamp),
        "previous_status": manifest.get("status") if manifest.get("status") != WAITING else (
            previous.get("previous_status") if isinstance(previous, dict) else "running"),
        "history": history}
    updated = deepcopy(manifest)
    updated["status"], updated["provider_resource_guard"] = WAITING, marker
    return updated


def advance_phase_cursor(manifest, *, submitted_steps, next_unsubmitted_step):
    """Record acknowledged submissions without making old jobs executable."""
    previous = manifest.get("provider_resource_guard", {})
    if manifest.get("status") == WAITING or previous.get("state") == WAITING:
        raise ValueError("cannot advance submissions while waiting for resource recovery")
    submitted = _submitted_steps(submitted_steps)
    next_index = _index(next_unsubmitted_step)
    _validate_cursor_update(previous, submitted, next_index)
    updated = deepcopy(manifest)
    marker = updated.setdefault("provider_resource_guard", {"schema_version": 1, "state": "running", "history": []})
    marker["submitted_steps"], marker["next_unsubmitted_step"] = submitted, next_index
    return updated


def phase_gate(manifest, step_index, *, operation="submit", existing_page_ids=(), continuation=False):
    """Gate immediately before POST/OCR; reads remain available while waiting.

    Knowledge continuation is a distinct, explicitly supplied new step that
    references existing page IDs. No step/job is created or revived here.
    """
    index = _index(step_index)
    if operation not in {"submit", "ocr", "read"}:
        raise ValueError("operation must be submit, ocr or read")
    marker = manifest.get("provider_resource_guard", {})
    if operation == "read":
        return {"allowed": True, "action": "read_existing_state", "step_index": index}
    if manifest.get("status") == WAITING or marker.get("state") == WAITING:
        return {"allowed": False, "action": "wait_for_operator_confirmation", "step_index": index,
            "reason": "provider_quota_exhausted", "provider_probe_allowed": False}
    submitted = marker.get("submitted_steps", [])
    previous = next((step for step in submitted if step.get("step_index") == index), None)
    if previous:
        return {"allowed": False, "action": "read_existing_job" if previous.get("job_id") else "reconcile_existing_submission",
            "step_index": index, "request_id": previous["request_id"], "job_id": previous.get("job_id"),
            "reason": "already_submitted_no_replay"}
    next_index = marker.get("next_unsubmitted_step")
    if next_index is not None and index != next_index:
        return {"allowed": False, "action": "reconcile_step_order", "step_index": index,
            "next_unsubmitted_step": next_index, "reason": "out_of_order_submission"}
    pages = list(dict.fromkeys(_identifier(page) for page in existing_page_ids))
    known = {page for step in submitted for page in step.get("page_ids", [])}
    if continuation and (not pages or not set(pages) <= known):
        return {"allowed": False, "action": "reject_continuation", "step_index": index,
            "reason": "continuation_requires_known_page_ids"}
    if pages and (operation == "ocr" or not continuation):
        return {"allowed": False, "action": "reject_reprocessing", "step_index": index,
            "reason": "existing_pages_require_explicit_knowledge_step_without_ocr"}
    return {"allowed": True, "action": "submit_knowledge_continuation" if continuation else operation,
        "step_index": index, "existing_page_ids": pages}


def confirm_provider_resource_recovery(manifest, *, project_id, session_id,
        confirmed_by="operator", timestamp=None):
    """Apply an explicit human recovery acknowledgement without a paid probe."""
    marker = manifest.get("provider_resource_guard")
    if not isinstance(marker, dict) or marker.get("state") != WAITING or manifest.get("status") != WAITING:
        raise ValueError("manifest is not waiting for provider resource recovery")
    if (marker.get("project_id") != _identifier(project_id)
            or marker.get("session_id") != _identifier(session_id, nullable=True)):
        raise ValueError("provider recovery project/session mismatch")
    updated = deepcopy(manifest)
    guard = updated["provider_resource_guard"]
    guard["history"].append({"event": "provider_resource_recovery_confirmed",
        "confirmed_by": _identifier(confirmed_by), "confirmed_at_utc": _timestamp(timestamp),
        "paused_at_utc": guard["paused_at_utc"], "next_unsubmitted_step": guard["next_unsubmitted_step"],
        "reason": deepcopy(guard["reason"])})
    guard["state"] = "resource_recovery_confirmed"
    updated["status"] = guard.get("previous_status") or "running"
    return updated
