"""R04/F06: leases, epochs and stale-worker business writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from chemclaw_lab import agent_gateway, agents, db

REPO_ROOT = Path(__file__).resolve().parents[2]


def _dispatch_capture(lab):
    owner = lab.context("hum_owner_f", memberships={"P1": "owner"})
    conn = db.connect(lab.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        agents.load_registry(conn, str(REPO_ROOT / "runtime" / "agents"))
        agents.set_manifest_enabled(
            conn, agent_id="b_capture", version="1.0.0", enabled=True
        )
        out = agents.dispatch(conn, intent="capture_page", context=owner, project_id="P1")
        conn.commit()
    finally:
        conn.close()
    return out


def test_unexpired_run_cannot_be_taken_over(lab):
    out = _dispatch_capture(lab)
    conn = db.connect(lab.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        agents.claim_run(conn, run_id=out["run_id"], worker_id="w1", lease_seconds=120)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(agents.AgentHostError) as excinfo:
            agents.claim_run(conn, run_id=out["run_id"], worker_id="w2")
        assert excinfo.value.code == "run_lease_active"
        conn.rollback()
    finally:
        conn.close()


def test_stale_worker_token_cannot_write_business(lab):
    out = _dispatch_capture(lab)
    conn = db.connect(lab.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        agents.claim_run(conn, run_id=out["run_id"], worker_id="w1", lease_seconds=120)
        conn.commit()
        old_token = agents.resolve_run_token(conn, out["run_token"])

        # Worker 1 dies. Lease expires; dispatcher recovers; worker 2 claims.
        conn.execute(
            "UPDATE agent_runs SET lease_expires_at = datetime('now', '-5 seconds') "
            "WHERE run_id = ?",
            (out["run_id"],),
        )
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        agents.recover_expired_runs(conn)
        agents.claim_run(conn, run_id=out["run_id"], worker_id="w2", lease_seconds=120)
        conn.commit()

        with pytest.raises(agents.AgentHostError) as excinfo:
            agent_gateway.invoke_agent_tool(
                lab.db_path,
                conn,
                old_token,
                capability_id="memory.proposal.create",
                params={"project_id": "P1", "page_id": "x", "transcript_revision": 1},
            )
        assert excinfo.value.code == "stale_worker"
        assert lab.scalar(
            "SELECT COUNT(*) FROM memory_proposals"
        ) == 0
        with pytest.raises(agents.AgentHostError):
            agents.heartbeat_run(
                conn, run_id=out["run_id"], claim_epoch=old_token["claim_epoch"]
            )
        with pytest.raises(agents.AgentHostError):
            agents.finish_run(
                conn,
                run_id=out["run_id"],
                status="succeeded",
                claim_epoch=old_token["claim_epoch"],
            )
    finally:
        conn.close()
