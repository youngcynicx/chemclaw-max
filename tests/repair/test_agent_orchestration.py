"""Model-directed sessions preserve identity, budgets and execution boundaries."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from chemclaw_lab import agent_gateway, agents, db, orchestration, tasks
from chemclaw_lab.api import ChemClawHTTPServer
from chemclaw_lab.capabilities import CapabilityError
from chemclaw_lab.validation import validate_payload_against_schema


def test_every_packaged_role_capability_has_an_explicit_tool_schema():
    directory = Path(__file__).resolve().parents[2] / "runtime" / "agents"
    for path in directory.glob("*.json"):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for capability_id in manifest["required_capabilities"]:
            assert capability_id in agent_gateway.AGENT_TOOL_PARAM_SCHEMAS, (path.name, capability_id)


@pytest.fixture()
def session(lab):
    context = lab.context("hum_model", memberships={"P1": "member", "P2": "member"})
    conn = db.connect(lab.db_path)
    try:
        loaded = agents.load_registry(conn, Path(__file__).resolve().parents[2] / "runtime" / "agents")
        assert not loaded["invalid"]
        for row in loaded["loaded"]:
            agents.set_manifest_enabled(conn, agent_id=row["agent_id"], version=row["version"], enabled=True)
        conn.commit()
    finally:
        conn.close()
    root = lab.invoke(context, "assistant.agent.start", {"project_id": "P1", "text": "整理记录并检查库存", "session_id": "session-test"}, operation_id="start-root")["result"]
    return lab, context, root


def _delegate(lab, root, target="b_query"):
    conn = db.connect(lab.db_path)
    try:
        token = agents.resolve_run_token(conn, root["run_token"])
        child = orchestration.delegate(conn, token, {"agent_id": target, "message": "读取当前样品历史"})
        conn.commit()
        return child
    finally:
        conn.close()


def test_member_session_persists_owned_history_and_real_task_state(session):
    lab, context, root = session
    assert root["history"] == []
    assert root["claim_epoch"] == 1
    assert lab.scalar("SELECT status FROM tasks WHERE task_id = ?", (root["task_id"],)) == "running"
    done = lab.invoke(context, "assistant.agent.finish", {
        "project_id": "P1", "run_id": root["run_id"], "claim_epoch": root["claim_epoch"],
        "result": {"text": "请核对一处样品编号", "status": "needs_review"},
    }, operation_id="finish-root")
    assert done["action_status"] == "clarification_needed"
    assert done["result"]["status"] == "needs_review"
    assert "run_token" not in done["result"]
    assert lab.scalar("SELECT status FROM tasks WHERE task_id = ?", (root["task_id"],)) == "waiting_input"
    next_turn = lab.invoke(context, "assistant.agent.start", {
        "project_id": "P1", "text": "编号是 S-1", "session_id": "session-test",
    }, operation_id="start-next")["result"]
    assert next_turn["history"] == [
        {"role": "user", "text": "整理记录并检查库存"},
        {"role": "assistant", "text": "请核对一处样品编号"},
    ]
    outsider = lab.context("hum_other", memberships={"P1": "member"})
    for other_context, project in ((outsider, "P1"), (context, "P2")):
        with pytest.raises(CapabilityError) as exc:
            lab.invoke(other_context, "assistant.agent.start", {
                "project_id": project, "text": "继续", "session_id": "session-test",
            }, operation_id=f"wrong-session-{project}-{other_context.principal_id}")
        assert exc.value.code in ("forbidden_scope", "forbidden_capability")
    with pytest.raises(CapabilityError) as exc:
        lab.invoke(outsider, "assistant.agent.finish", {
            "project_id": "P1", "run_id": next_turn["run_id"], "claim_epoch": 1,
            "result": {"text": "forged", "status": "completed"},
        }, operation_id="finish-forged")
    assert exc.value.code == "forbidden_scope"


def test_delegation_is_scoped_enabled_depth_limited_and_parent_owned(session):
    lab, _, root = session
    child = _delegate(lab, root)
    sibling = _delegate(lab, root, "observer")
    conn = db.connect(lab.db_path)
    try:
        child_token = agents.resolve_run_token(conn, child["run_token"])
        assert json.loads(child_token["project_ids_json"]) == ["P1"]
        assert agents.principal_from_token(conn, child_token).kind == "agent"
        assert child["trace_id"] == root["trace_id"]
        with pytest.raises(agents.AgentHostError) as exc:
            orchestration.delegate(conn, child_token, {"agent_id": "observer", "message": "recurse"})
        assert exc.value.code == "delegation_depth_exceeded"
        with pytest.raises(agents.AgentHostError) as exc:
            orchestration.complete_child(conn, child_token, {"run_id": sibling["run_id"], "claim_epoch": sibling["claim_epoch"]})
        assert exc.value.code == "forbidden_scope"
        parent = agents.resolve_run_token(conn, root["run_token"])
        for target in ("orchestrator", "missing"):
            with pytest.raises(agents.AgentHostError) as exc:
                orchestration.delegate(conn, parent, {"agent_id": target, "message": "do work"})
            assert exc.value.code == "forbidden_agent"
        agents.set_manifest_enabled(conn, agent_id="planner", version="1.0.0", enabled=False)
        with pytest.raises(agents.AgentHostError) as exc:
            orchestration.delegate(conn, parent, {"agent_id": "planner", "message": "do work"})
        assert exc.value.code == "forbidden_agent"
    finally:
        conn.rollback()
        conn.close()


def test_partial_session_preserves_receipts_without_marking_task_successful(session):
    lab, context, root = session
    refs = [{"kind": "task", "id": root["task_id"], "revision": None}]
    result = lab.invoke(context, "assistant.agent.finish", {
        "project_id": "P1", "run_id": root["run_id"], "claim_epoch": root["claim_epoch"],
        "result": {"text": "已有资料保存成功，另一项未完成", "status": "partial", "object_refs": refs,
                   "data": {"receipts": [{"operation_id": "already-saved"}]}, "warnings": ["child_failed"]},
    }, operation_id="finish-partial")["result"]
    assert result["status"] == "partial"
    assert result["object_refs"] == refs
    assert result["data"]["receipts"][0]["operation_id"] == "already-saved"
    assert lab.scalar("SELECT status FROM agent_runs WHERE run_id = ?", (root["run_id"],)) == "failed"
    assert lab.scalar("SELECT status FROM tasks WHERE task_id = ?", (root["task_id"],)) == "failed"
    message = json.loads(lab.scalar("SELECT payload_json FROM typed_messages WHERE message_id = ?", (result["message_id"],)))
    assert message["outcome"] == "failed"
    assert message["status"] == "partial"
    assert message["object_refs"] == refs


def test_legacy_session_messages_without_ownership_are_ignored(session):
    lab, context, _ = session
    conn = db.connect(lab.db_path)
    try:
        tasks.record_typed_message(conn, message_kind="command", message_type="assistant.user_message",
            payload={"intent": "assistant.task.process", "text": "unbound legacy private content"},
            correlation_id="legacy-session")
        tasks.record_typed_message(conn, message_kind="domain_event", message_type="assistant.reply",
            payload={"occurred_ref": "legacy-user", "text": "legacy private answer"},
            correlation_id="legacy-session")
        conn.commit()
    finally:
        conn.close()
    result = lab.invoke(context, "assistant.agent.start", {
        "project_id": "P1", "text": "开始新任务", "session_id": "legacy-session",
    }, operation_id="start-legacy-session")["result"]
    assert result["history"] == []
    assert result["session_id"] == "legacy-session"


def test_revoked_membership_immediately_blocks_root_and_children(session):
    lab, _, root = session
    child = _delegate(lab, root)
    lab.execute("DELETE FROM memberships WHERE principal_id = 'hum_model' AND project_id = 'P1'")
    conn = db.connect(lab.db_path)
    try:
        for token in (root["run_token"], child["run_token"]):
            row = agents.resolve_run_token(conn, token)
            with pytest.raises(agents.AgentHostError) as exc:
                orchestration.roles(conn, row)
            assert exc.value.code == "forbidden_scope"
        with pytest.raises(Exception) as exc:
            agent_gateway.invoke_agent_tool(lab.db_path, conn, agents.resolve_run_token(conn, child["run_token"]),
                capability_id="query.events.search", params={"project_ids": ["P1"]})
        assert exc.value.code in ("forbidden_scope", "forbidden_capability")
    finally:
        conn.close()


def test_children_share_root_budget_and_can_finish_after_budget_limit(session):
    lab, _, root = session
    lab.execute("UPDATE agent_run_tokens SET budget_tool_calls = 3 WHERE run_id = ?", (root["run_id"],))
    child = _delegate(lab, root)  # shared call 1
    conn = db.connect(lab.db_path)
    try:
        row = agents.resolve_run_token(conn, child["run_token"])
        for _ in range(2):
            result = agent_gateway.invoke_agent_tool(lab.db_path, conn, row,
                capability_id="query.events.search", params={"project_ids": ["P1"]}, operation_id=uuid.uuid4().hex)
            assert result["action_status"] == "applied"
        with pytest.raises(agents.AgentHostError) as exc:
            agent_gateway.invoke_agent_tool(lab.db_path, conn, row, capability_id="query.events.search", params={"project_ids": ["P1"]})
        assert exc.value.code == "budget_exhausted"
        parent = agents.resolve_run_token(conn, root["run_token"], allow_exhausted=True)
        finished = orchestration.complete_child(conn, parent, {"run_id": child["run_id"], "claim_epoch": child["claim_epoch"], "result": {"text": "done"}})
        conn.commit()
        assert finished["run"]["status"] == "succeeded"
        assert lab.scalar("SELECT used_tool_calls FROM agent_run_tokens WHERE run_id = ?", (root["run_id"],)) == 3
    finally:
        conn.close()


def test_session_limits_children_and_expired_root_blocks_remaining_child(session):
    lab, _, root = session
    children = [_delegate(lab, root) for _ in range(orchestration.MAX_CHILDREN)]
    conn = db.connect(lab.db_path)
    try:
        parent = agents.resolve_run_token(conn, root["run_token"])
        with pytest.raises(agents.AgentHostError) as exc:
            orchestration.delegate(conn, parent, {"agent_id": "observer", "message": "one more"})
        assert exc.value.code == "delegation_budget_exhausted"
        conn.execute("UPDATE agent_run_tokens SET expires_at = datetime('now', '-1 second') WHERE run_id = ?", (root["run_id"],))
        conn.commit()
        child = agents.resolve_run_token(conn, children[0]["run_token"])
        with pytest.raises(agents.AgentHostError) as exc:
            agent_gateway.invoke_agent_tool(lab.db_path, conn, child, capability_id="query.events.search", params={"project_ids": ["P1"]})
        assert exc.value.code == "unauthorized"
    finally:
        conn.close()


def test_parent_finish_cancels_child_tokens_and_human_actions_are_not_tools(session):
    lab, context, root = session
    child = _delegate(lab, root)
    conn = db.connect(lab.db_path)
    try:
        row = agents.resolve_run_token(conn, child["run_token"])
        manifest = agents.manifest_for_token(conn, row)
        manifest["required_capabilities"].append("memory.proposal.confirm_items")
        conn.execute("UPDATE agent_manifests SET manifest_json = ? WHERE agent_id = ?", (json.dumps(manifest), child["agent_id"]))
        conn.commit()
        names = {tool["function"]["name"] for tool in agent_gateway.describe_agent_tools(conn, row)}
        assert "memory.proposal.confirm_items" not in names
        with pytest.raises(agents.AgentHostError) as exc:
            agent_gateway.invoke_agent_tool(lab.db_path, conn, row,
                capability_id="memory.proposal.confirm_items", params={"proposal_id": "any", "item_keys": [], "decision": "confirmed"})
        assert exc.value.code == "human_approval_required"
    finally:
        conn.close()
    lab.invoke(context, "assistant.agent.finish", {
        "project_id": "P1", "run_id": root["run_id"], "claim_epoch": 1,
        "result": {"text": "done", "status": "completed"},
    }, operation_id="finish-cancel-child")
    assert lab.scalar("SELECT status FROM agent_runs WHERE run_id = ?", (child["run_id"],)) == "cancelled"
    assert lab.scalar("SELECT status FROM agent_run_tokens WHERE run_id = ?", (child["run_id"],)) == "revoked"


def test_explicit_review_cannot_be_bypassed_by_candidate_flags(session):
    lab, _, root = session
    child = _delegate(lab, root, "b_capture")
    page = lab.add_page("P1")
    transcript = lab.add_transcript(page["page_id"], ["实际取用 0.5 g S-1，内温未测"])
    lab.execute("INSERT INTO samples (sample_id, project_id, label) VALUES ('sample-review', 'P1', 'S-1')")
    conn = db.connect(lab.db_path)
    try:
        result = agent_gateway.invoke_agent_tool(lab.db_path, conn, agents.resolve_run_token(conn, child["run_token"]),
            capability_id="memory.proposal.create", params={
                "project_id": "P1", "page_id": page["page_id"], "transcript_revision": 1,
                "profile_id": "chemistry_notebook", "profile_version": "1.0",
                "acceptance_mode": "review",
                # Even hostile undeclared extras cannot manufacture approval.
                "auto_confirm_safe": True,
                "items": [{"item_key": "amount", "event_kind": "operation", "raw_text": "实际取用 0.5 g S-1，内温未测",
                    "needs_confirmation": False, "unknown": True, "unknowns": ["内温未测"],
                    "fields": {"amount": {"normalized": 0.5, "unit": "g"}, "sample_label": {"normalized": "S-1"}},
                    "sample_match": {"action": "link", "candidates": [{"sample_id": "sample-review", "label": "S-1"}]},
                    "evidence_refs": [{"page_id": page["page_id"], "transcript_revision": 1, "block_id": transcript["block_ids"][0]}]}],
            }, operation_id="malicious-auto-confirm", services=lab.settings)
        candidate = result["result"]["proposal"]["items"][0]
        assert candidate["needs_confirmation"] is True
        assert candidate["unknown"] is True
        assert candidate["unknowns"] == ["内温未测"]
        assert lab.scalar("SELECT COUNT(*) FROM experiment_events") == 0
    finally:
        conn.close()


def test_clarification_schema_roundtrip_uses_child_task_revision_and_preserves_parent(session):
    lab, _, root = session
    child = _delegate(lab, root, "b_capture")
    page = lab.add_page("P1")
    transcript = lab.add_transcript(page["page_id"], ["样品标签无法确定"])
    conn = db.connect(lab.db_path)
    try:
        row = agents.resolve_run_token(conn, child["run_token"])
        proposal = agent_gateway.invoke_agent_tool(lab.db_path, conn, row,
            capability_id="memory.proposal.create", params={
                "project_id": "P1", "page_id": page["page_id"], "transcript_revision": 1,
                "profile_id": "chemistry_notebook", "acceptance_mode": "review", "items": [{"item_key": "label", "event_kind": "observation",
                    "raw_text": "样品标签无法确定", "unknown": True,
                    "evidence_refs": [{"page_id": page["page_id"], "transcript_revision": 1, "block_id": transcript["block_ids"][0]}]}],
            }, operation_id="clarification-proposal", services=lab.settings)["result"]["proposal"]
        assert proposal["task_id"] == child["task_id"]
        task = agent_gateway.invoke_agent_tool(lab.db_path, conn, row, capability_id="task.read",
            params={"task_id": child["task_id"]}, operation_id="read-clarification-task")["result"]
        params = {"proposal_id": proposal["proposal_id"], "task_id": child["task_id"],
            "expected_task_revision": task["revision"],
            "questions": [{"question_id": "which-sample", "prompt": "请确认样品编号", "item_key": "label", "field": "sample_label"}]}
        validate_payload_against_schema(params, agent_gateway.AGENT_TOOL_PARAM_SCHEMAS["memory.clarification.open"])
        with pytest.raises(agents.AgentHostError) as exc:
            agent_gateway.invoke_agent_tool(lab.db_path, conn, row, capability_id="memory.clarification.open",
                params={**params, "task_id": root["task_id"]}, operation_id="reject-parent-clarification")
        assert exc.value.code == "forbidden_scope"
        with pytest.raises(CapabilityError) as exc:
            agent_gateway.invoke_agent_tool(lab.db_path, conn, row, capability_id="memory.clarification.open",
                params={**params, "expected_task_revision": task["revision"] - 1}, operation_id="reject-stale-clarification")
        assert exc.value.code == "revision_conflict"
        result = agent_gateway.invoke_agent_tool(lab.db_path, conn, row, capability_id="memory.clarification.open",
            params=params, operation_id="open-child-clarification", services=lab.settings)["result"]
        assert result["task"]["status"] == "waiting_input"
        assert result["task"]["task_id"] == child["task_id"]
        assert lab.scalar("SELECT status FROM tasks WHERE task_id = ?", (root["task_id"],)) == "running"
        assert lab.scalar("SELECT COUNT(*) FROM clarifications WHERE proposal_id = ?", (proposal["proposal_id"],)) == 1
    finally:
        conn.close()


def test_http_role_delegate_and_complete_use_only_run_token(session):
    lab, _, root = session
    server = ChemClawHTTPServer(("127.0.0.1", 0), lab.db_path, service_settings=lab.settings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(path, body=None, token=None):
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token or root['run_token']}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as response:
            return json.load(response)
    try:
        catalog = request("/v2/agent/roles")
        assert "b_query" in {role["agent_id"] for role in catalog["roles"]}
        assert all("run_token" not in role for role in catalog["roles"])
        child = request("/v2/agent/delegate", {"agent_id": "b_query", "message": "read samples"})
        assert child["claim_epoch"] == 1
        with pytest.raises(urllib.error.HTTPError) as exc:
            request("/v2/agent/delegate/complete", {"run_id": root["run_id"], "claim_epoch": 1}, token=child["run_token"])
        assert exc.value.code == 403
        done = request("/v2/agent/delegate/complete", {"run_id": child["run_id"], "claim_epoch": 1, "result": {"text": "read completed"}})
        assert done["run"]["status"] == "succeeded"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
