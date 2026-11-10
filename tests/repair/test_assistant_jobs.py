"""Durable queue fencing, deduplication and recovery use real Core state."""
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from chemclaw_lab import agents, agent_jobs, db, orchestration
from chemclaw_lab.capabilities import CapabilityError


@pytest.fixture()
def queued(lab):
    context = lab.context("hum_jobs", memberships={"P1": "member", "P2": "member"})
    conn = db.connect(lab.db_path)
    try:
        loaded = agents.load_registry(conn, Path(__file__).resolve().parents[2] / "runtime" / "agents")
        for row in loaded["loaded"]:
            agents.set_manifest_enabled(conn, agent_id=row["agent_id"], version=row["version"], enabled=True)
        conn.commit()
    finally:
        conn.close()
    params = {"project_id": "P1", "request_id": "job-test", "text": "整理实验笔记", "session_id": "session-jobs"}
    job = lab.invoke(context, "assistant.job.submit", params)["result"]["job"]
    return lab, context, params, job


def call(fixture, name, **kwargs):
    lab, context, _, job = fixture
    return lab.invoke(context, f"assistant.job.{name}", {"project_id": "P1", "job_id": job["job_id"], **kwargs})["result"]


def claim(fixture):
    return call(fixture, "claim", worker_id="worker-1", lease_seconds=60)


def test_submit_deduplicates_same_input_but_rejects_changed_input(queued):
    lab, context, params, job = queued
    repeat = lab.invoke(context, "assistant.job.submit", params)["result"]
    assert repeat["duplicate"] and repeat["job"]["job_id"] == job["job_id"]
    with pytest.raises(CapabilityError) as err:
        lab.invoke(context, "assistant.job.submit", {**params, "text": "different"})
    assert err.value.code == "idempotency_conflict"
    assert lab.scalar("SELECT count(*) FROM assistant_jobs") == 1
    assert lab.scalar("SELECT count(*) FROM agent_runs") == 0


def test_owner_project_binding_and_no_shared_job_visibility(queued):
    lab, context, _, job = queued
    outsider = lab.context("hum_other_jobs", memberships={"P1": "member"})
    for other, project in ((outsider, "P1"), (context, "P2")):
        with pytest.raises(CapabilityError):
            lab.invoke(other, "assistant.job.read", {"project_id": project, "job_id": job["job_id"]})
    assert lab.invoke(outsider, "assistant.job.list", {"project_id": "P1"})["result"]["jobs"] == []
    agent = lab.context("agt_jobs", kind="agent", memberships={"P1": "member"})
    with pytest.raises(CapabilityError):
        lab.invoke(agent, "assistant.job.claim", {"project_id": "P1", "job_id": job["job_id"], "worker_id": "bad"})


def test_claim_is_exclusive_and_claim_credentials_never_persist(queued):
    lab, _, _, _ = queued
    first = claim(queued)
    with pytest.raises(CapabilityError) as err:
        claim(queued)
    assert err.value.code == "run_lease_active"
    run = first["started"]
    dump = json.dumps(lab.rows("SELECT * FROM operation_results") + lab.rows("SELECT * FROM assistant_jobs"))
    assert run["run_token"] not in dump
    assert '"run_token"' not in json.dumps(call(queued, "read"))
    assert lab.scalar("SELECT count(*) FROM typed_messages WHERE message_type='assistant.agent.user'") == 1


def test_expired_worker_resumes_same_run_and_checkpoint_revokes_old_bearer(queued):
    lab, _, _, job = queued
    first = claim(queued)
    root = first["started"]
    conn = db.connect(lab.db_path)
    try:
        token = agents.resolve_run_token(conn, root["run_token"])
        agent_jobs.runtime_control(conn, token, "checkpoint", {"checkpoint": {"version": 1, "run_id": root["run_id"], "messages": [{"role": "user", "content": "hello"}], "tool_calls": []}})
        agents.consume_tool_budget(conn, token)
        conn.commit()
    finally:
        conn.close()
    lab.execute("UPDATE assistant_jobs SET lease_expires_at=datetime('now','-1 second') WHERE job_id=?", (job["job_id"],))
    second = call(queued, "claim", worker_id="worker-2")
    assert second["started"]["run_id"] == root["run_id"]
    assert second["started"]["claim_epoch"] > root["claim_epoch"]
    assert second["resume"]["checkpoints"][root["run_id"]]["messages"][0]["content"] == "hello"
    assert lab.scalar("SELECT count(*) FROM agent_runs") == 1
    assert lab.scalar("SELECT used_tool_calls FROM agent_run_tokens WHERE run_id=?", (root["run_id"],)) == 1
    conn = db.connect(lab.db_path)
    try:
        with pytest.raises(agents.AgentHostError):
            agents.resolve_run_token(conn, root["run_token"])
    finally:
        conn.close()
    with pytest.raises(CapabilityError):
        call(queued, "complete", worker_id="worker-1", claim_epoch=root["claim_epoch"], result={"text": "stale", "status": "completed"})


def test_cancel_revokes_root_and_child_tokens_and_rejects_late_complete(queued):
    lab, _, _, _ = queued
    root = claim(queued)["started"]
    conn = db.connect(lab.db_path)
    try:
        token = agents.resolve_run_token(conn, root["run_token"])
        child = orchestration.delegate(conn, token, {"agent_id": "b_query", "message": "查询", "delegation_id": "d1"})
        conn.commit()
    finally:
        conn.close()
    assert call(queued, "cancel")["job"]["status"] == "cancelled"
    assert lab.scalar("SELECT count(*) FROM agent_run_tokens WHERE status <> 'revoked'") == 0
    assert lab.scalar("SELECT count(*) FROM agent_runs WHERE status='running'") == 0
    with pytest.raises(CapabilityError):
        call(queued, "complete", worker_id="worker-1", claim_epoch=root["claim_epoch"], result={"text": "late", "status": "completed"})


def test_delegation_id_replay_does_not_create_another_child(queued):
    lab, _, _, _ = queued
    root = claim(queued)["started"]
    conn = db.connect(lab.db_path)
    try:
        token = agents.resolve_run_token(conn, root["run_token"])
        params = {"agent_id": "b_query", "message": "查询", "delegation_id": "d1"}
        first = orchestration.delegate(conn, token, params)
        second = orchestration.delegate(conn, token, params)
        assert first["run_id"] == second["run_id"]
        assert first["run_token"] != second["run_token"]
        assert conn.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 2
        conn.commit()
    finally:
        conn.close()


def test_finish_is_durable_idempotent_and_events_are_incremental(queued):
    lab, _, _, _ = queued
    root = claim(queued)["started"]
    args = {"worker_id": "worker-1", "claim_epoch": root["claim_epoch"], "result": {"text": "请核对", "status": "needs_review", "runtime": {"name": "pi"}}}
    result = call(queued, "complete", **args)
    assert result["job"]["status"] == "needs_review"
    assert result["job"]["result"]["runtime"]["name"] == "pi"
    assert call(queued, "complete", **args)["result"]["message_id"] == result["result"]["message_id"]
    assert lab.scalar("SELECT count(*) FROM typed_messages WHERE message_type='assistant.agent.reply'") == 1
    first = call(queued, "read")
    assert len(first["events"]) == 3
    assert call(queued, "read", after_seq=first["last_seq"])["events"] == []


def test_capture_progress_and_budget_survive_job_finish_and_repeated_completion(queued):
    started = claim(queued)["started"]
    progress = {"version": 1, "knowledge_complete": None, "pages": [{
        "page_id": "source-1", "transcript_revision": 1,
        "source_block_ids": ["block-1"], "unlinked_block_ids": ["block-1"],
    }], "requires_review": False}
    budget = {"shared_absolute_deadline_ms": 12345, "work_deadline_ms": 12000,
              "remaining_ms": 345, "phase": "closing"}
    args = {"worker_id": "worker-1", "claim_epoch": started["claim_epoch"],
            "result": {"text": "Source saved; structural work remains.", "status": "partial",
                       "capture_progress": progress, "runtime_budget_hint": budget}}
    saved = call(queued, "complete", **args)
    assert saved["job"]["result"]["capture_progress"] == progress
    assert saved["job"]["result"]["runtime_budget_hint"] == budget
    assert saved["result"]["status"] == "partial"
    repeated = call(queued, "complete", **{**args, "result": {**args["result"],
        "capture_progress": {"version": 1, "knowledge_complete": True}}})
    assert repeated["result"]["message_id"] == saved["result"]["message_id"]
    assert repeated["job"]["result"]["capture_progress"] == progress


@pytest.mark.parametrize("key,value", [
    ("capture_progress", []), ("runtime_budget_hint", []),
    ("capture_progress", {"version": True, "pages": []}),
    ("capture_progress", {"version": 1, "knowledge_complete": True}),
    ("capture_progress", {"version": 1, "pages": "invalid"}),
    ("runtime_budget_hint", {"remaining_ms": float("inf")}),
    ("runtime_budget_hint", {"oversize": "x" * 262145}),
])
def test_invalid_control_metadata_does_not_finish_or_replace_the_claim(queued, key, value):
    started = claim(queued)["started"]
    with pytest.raises(CapabilityError) as caught:
        call(queued, "complete", worker_id="worker-1", claim_epoch=started["claim_epoch"],
             result={"text": "Invalid metadata must be refused.", "status": "partial", key: value})
    assert caught.value.code == "schema_violation"
    current = call(queued, "read")["job"]
    assert current["status"] == "running"
    assert current["claim_epoch"] == started["claim_epoch"]


def test_checkpoints_reject_secrets_and_stale_lease(queued):
    lab, _, _, job = queued
    root = claim(queued)["started"]
    conn = db.connect(lab.db_path)
    try:
        token = agents.resolve_run_token(conn, root["run_token"])
        with pytest.raises(agents.AgentHostError):
            agent_jobs.runtime_control(conn, token, "checkpoint", {"checkpoint": {"run_token": "never persist"}})
        conn.rollback()
        conn.execute("UPDATE assistant_jobs SET lease_expires_at=datetime('now','-1 second') WHERE job_id=?", (job["job_id"],))
        with pytest.raises(agents.AgentHostError):
            agent_jobs.runtime_control(conn, token, "checkpoint", {"checkpoint": {"run_id": root["run_id"]}})
    finally:
        conn.close()


def test_waiting_mutation_cannot_write_after_cancel(queued, monkeypatch):
    import threading
    from contextlib import contextmanager
    from chemclaw_lab import agent_gateway, capabilities
    lab, _, _, _ = queued
    root = claim(queued)["started"]
    conn = db.connect(lab.db_path)
    try:
        child = orchestration.delegate(conn, agents.resolve_run_token(conn, root["run_token"]), {"agent_id": "b_query", "message": "report"})
        token = agents.resolve_run_token(conn, child["run_token"])
        lock = agent_gateway.run_write_lock(conn, root["run_id"])
        conn.commit()
    finally:
        conn.close()
    waiting = threading.Event()
    original = agent_gateway.run_write_lock
    @contextmanager
    def observed_lock(connection, run_id):
        if threading.current_thread() is not threading.main_thread():
            waiting.set()
        with original(connection, run_id):
            yield
    monkeypatch.setattr(agent_gateway, "run_write_lock", observed_lock)
    invoked = []
    original_invoke = capabilities.invoke_capability
    def track(*args, **kwargs):
        if args[2] == "query.report.save":
            invoked.append(True)
        return original_invoke(*args, **kwargs)
    monkeypatch.setattr(capabilities, "invoke_capability", track)
    def late_write():
        connection = db.connect(lab.db_path)
        try:
            with pytest.raises(agents.AgentHostError):
                agent_gateway.invoke_agent_tool(lab.db_path, connection, token,
                    capability_id="query.report.save", params={"project_id": "P1"}, operation_id="cancel-race", services=lab.settings)
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with lock:
            future = pool.submit(late_write)
            assert waiting.wait(5)
            call(queued, "cancel")
        future.result(timeout=5)
    assert invoked == []


def test_concurrent_claims_have_one_winner(queued):
    def attempt(worker):
        try:
            return call(queued, "claim", worker_id=worker)
        except CapabilityError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["a", "b"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "run_lease_active" in results


def test_session_turns_wait_for_preceding_turn(queued):
    lab, context, params, first = queued
    second = lab.invoke(context, "assistant.job.submit", {**params, "request_id": "turn2", "text": "continue"})["result"]["job"]
    with pytest.raises(CapabilityError) as err:
        lab.invoke(context, "assistant.job.claim", {"project_id": "P1", "job_id": second["job_id"], "worker_id": "b"})
    assert err.value.code == "session_busy"
    call(queued, "cancel")
    started = lab.invoke(context, "assistant.job.claim", {"project_id": "P1", "job_id": second["job_id"], "worker_id": "b"})["result"]
    assert started["started"]["history"] == []


def test_preprocess_is_page_bound_and_fenced_after_cancel(queued, monkeypatch):
    from chemclaw_lab import capabilities
    lab, context, params, original_job = queued
    page = lab.add_page("P1")
    job = lab.invoke(context, "assistant.job.submit", {**params, "request_id": "attached-job", "session_id": "another-session", "context": {"pages": [{**page, "mime": "image/png"}]}})["result"]["job"]
    base = {"project_id": "P1", "job_id": job["job_id"], "worker_id": "parse-worker"}
    root = lab.invoke(context, "assistant.job.claim", base)["result"]["started"]
    base["claim_epoch"] = root["claim_epoch"]
    original = capabilities.invoke_capability
    called = []
    def fake(*args, **kwargs):
        if args[2] == "evidence.transcribe.run":
            called.append(args[3])
            return {"result": {"page_id": args[3]["page_id"]}}
        return original(*args, **kwargs)
    monkeypatch.setattr(capabilities, "invoke_capability", fake)
    good = {**base, "capability_id": "evidence.transcribe.run", "params": {"page_id": page["page_id"]}}
    assert lab.invoke(context, "assistant.job.preprocess", good)["result"]["envelope"]["result"]["page_id"] == page["page_id"]
    assert called[0]["project_id"] == "P1"
    with pytest.raises(CapabilityError):
        lab.invoke(context, "assistant.job.preprocess", {**good, "params": {"page_id": "other-page"}})
    with pytest.raises(CapabilityError):
        lab.invoke(context, "assistant.job.preprocess", {**good, "capability_id": "memory.proposal.create"})
    lab.invoke(context, "assistant.job.cancel", base)
    with pytest.raises(CapabilityError):
        lab.invoke(context, "assistant.job.preprocess", good)
    assert len(called) == 1


@pytest.mark.parametrize("context", [{"pages": [None]}, {"pages": ["bad"]}, {"pages": {}}, {"pages": [{"page_id": "x"}]}, {"object_refs": {}}])
def test_submit_rejects_malformed_context(queued, context):
    lab, principal, params, _ = queued
    with pytest.raises(CapabilityError) as err:
        lab.invoke(principal, "assistant.job.submit", {**params, "request_id": "malformed", "context": context})
    assert err.value.code == "schema_violation"


def test_submit_rejects_page_reference_outside_project(queued):
    lab, principal, params, _ = queued
    page = lab.add_page("P2")
    with pytest.raises(CapabilityError) as err:
        lab.invoke(principal, "assistant.job.submit", {**params, "request_id": "wrong-page", "context": {"pages": [{**page, "mime": "image/png"}]}})
    assert err.value.code == "forbidden_scope"
