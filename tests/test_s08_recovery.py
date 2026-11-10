"""S08 recovery fault matrix (plan §14).

The S06 delivery tests cover the worker primitives; this matrix pins the
§14 scenarios end to end, with the cross-domain stock semantics asserted
explicitly: retries consume existing results only, stock is charged at most
once per confirmed usage, and restarts/backups preserve queue state.
"""

from __future__ import annotations

import base64
import sqlite3

import pytest

from chemclaw_lab import agents, auth, capabilities, db, deliveries, tasks
from chemclaw_lab.db import connect, init_db
from chemclaw_lab.ocr_port import FixtureRasterTranscriber
from chemclaw_lab.service_config import ServiceSettings

# ---------------------------------------------------------------------------
# Minimal S07-style fixture (proposal -> outbox -> subtask bridge).
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path):
    db_path = str(tmp_path / "s08.db")
    init_db(db_path)
    from chemclaw_lab import ocr_port

    ocr_registry = ocr_port.default_registry()
    ocr_registry.register(FixtureRasterTranscriber(name="fixture_ocr"))
    settings = ServiceSettings(
        storage_root=tmp_path / "storage",
        registry_factory=lambda: ocr_registry,
    )
    principals = {
        "owner": auth.PrincipalContext(
            principal_id="prin_root", kind="human", display_name="Owner",
            memberships={"proj_lab": "owner", "proj_other": "owner"},
        ),
        "member": auth.PrincipalContext(
            principal_id="prin_lab", kind="human", display_name="Member",
            memberships={"proj_lab": "member", "proj_other": "member"},
        ),
        "outsider": auth.PrincipalContext(
            principal_id="prin_out", kind="human", display_name="Out",
            memberships={"proj_other": "member"},
        ),
    }
    conn = connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO principals (principal_id, kind, display_name) VALUES (?, ?, ?)",
            [("prin_root", "human", "Owner"), ("prin_lab", "human", "Member"),
             ("prin_out", "human", "Out")],
        )
        conn.executemany(
            "INSERT INTO projects (project_id, name) VALUES (?, ?)",
            [("proj_lab", "Lab"), ("proj_other", "Other")],
        )
        conn.execute(
            "INSERT INTO chemicals (chemical_id, name, amount, unit, threshold, hazard_level, max_auto_delta, revision) "
            "VALUES ('chm_nacl', 'Sodium chloride', 100.0, 'g', 5.0, 'low', 50.0, 0)"
        )
        conn.execute(
            "INSERT INTO samples (sample_id, project_id, label) VALUES ('smp_1', 'proj_lab', 'S-1')"
        )
        conn.commit()
    finally:
        conn.close()
    return {"db_path": db_path, "settings": settings, **principals}


def _invoke(env, capability, params, context=None, operation_id="op-default"):
    return capabilities.invoke_capability(
        env["db_path"], context or env["member"], capability, params,
        operation_id=operation_id, services=env["settings"],
    )


def _png_data_url(seed=0):
    from chemclaw_lab import pnglite

    return "data:image/png;base64," + base64.b64encode(
        pnglite.solid_png(60, 40, (240 + seed % 15, 200 + seed % 40, 180 + seed % 60))
    ).decode("ascii")


def _block(text, order, block_id):
    return {
        "block_id": block_id, "block_kind": "handwriting", "reading_order": order,
        "text": text, "table_cell": None,
        "geometry": {"x": 5, "y": 5 + order * 10, "width": 40, "height": 8},
        "coordinate_system": {"origin": "TOPLEFT", "units": "pixel", "image_ref": "original",
                              "frame_width": 60, "frame_height": 40, "dpi": 300},
        "source_page": 1,
        "extractor": {"provider": "fixture_ocr", "model": "fixture-recognizer", "version": "0.3.0"},
        "quality_flags": [],
    }


def _apply_operation(env, *, operation_id, raw_text="Used 5 g of Sodium chloride into S-1",
                     amount=5.0):
    """Full A-side path: page -> proposal -> confirm -> apply (enqueues outbox)."""
    seed = sum(ord(c) for c in operation_id)
    doc = _invoke(env, "evidence.document.create", {
        "project_id": "proj_lab", "notebook_code": f"NB-{operation_id}",
        "title": "S08", "author": "yu", "source_channel": "photo_upload",
    }, operation_id=f"{operation_id}-doc")["result"]["document_id"]
    page_id = _invoke(env, "evidence.page.ingest", {
        "document_id": doc, "page_order": 1, "filename": "p.png",
        "content_data_url": _png_data_url(seed),
    }, operation_id=f"{operation_id}-page")["result"]["page"]["page_id"]
    _invoke(env, "evidence.transcribe.run", {
        "page_id": page_id, "provider": "fixture_ocr",
        "config": {"fixture_blocks": [_block(raw_text, 0, f"b-{operation_id}-0")]},
    }, operation_id=f"{operation_id}-run")

    item = {
        "item_key": "t1", "event_kind": "operation", "raw_text": raw_text,
        "fields": {
            "amount": {"normalized": amount, "unit": "g"},
            "unit": {"normalized": "g"},
            "sample_label": {"normalized": "S-1", "raw": "S-1", "uncertain": False},
        },
        "sample_match": {"action": "link", "candidates": [{"sample_id": "smp_1", "label": "S-1"}]},
        "evidence_refs": [{"page_id": page_id, "transcript_revision": 1, "block_id": f"b-{operation_id}-0"}],
    }
    created = _invoke(env, "memory.proposal.create", {
        "project_id": "proj_lab", "page_id": page_id, "transcript_revision": 1,
        "profile_id": "chemistry_notebook", "profile_version": "1.0",
        "acceptance_mode": "review",  # This fixture tests confirm/apply and delivery recovery.
        "operation_id": f"{operation_id}-create", "items": [item],
    }, operation_id=f"{operation_id}-create")
    proposal_id = created["result"]["proposal"]["proposal_id"]
    revision = created["result"]["proposal"]["revision"]
    _invoke(env, "memory.proposal.confirm_items", {
        "proposal_id": proposal_id, "item_keys": ["t1"], "decision": "confirmed",
        "expected_proposal_revision": revision,
    }, operation_id=f"{operation_id}-confirm")
    applied = _invoke(env, "memory.proposal.apply", {
        "proposal_id": proposal_id, "operation_id": f"{operation_id}-apply",
    }, operation_id=f"{operation_id}-apply")
    # Keep the fault matrix deterministic: notification-type outbox messages
    # (agent.run.completed etc.) are treated as already delivered; only the
    # inventory_proposal message stays pending for the fault adapter.
    conn = connect(env["db_path"])
    try:
        conn.execute(
            "UPDATE outbox SET status = 'sent' "
            "WHERE status = 'pending' AND message_type != 'inventory_proposal'"
        )
        conn.commit()
    finally:
        conn.close()
    return proposal_id, applied


def _outbox_rows(env, message_type="inventory_proposal"):
    conn = connect(env["db_path"])
    try:
        rows = conn.execute(
            "SELECT outbox_id, payload_json, status, trace_id FROM outbox "
            "WHERE message_type = ? ORDER BY rowid",
            (message_type,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _stock(env, chemical_id="chm_nacl"):
    conn = connect(env["db_path"])
    try:
        row = conn.execute(
            "SELECT amount, revision FROM chemicals WHERE chemical_id = ?", (chemical_id,)
        ).fetchone()
        return float(row["amount"]), int(row["revision"])
    finally:
        conn.close()


def _subtask_count(env):
    conn = connect(env["db_path"])
    try:
        return conn.execute("SELECT COUNT(*) FROM inventory_subtasks").fetchone()[0]
    finally:
        conn.close()


def _deliver_once(env, adapter, *, backoff=0):
    conn = connect(env["db_path"])
    try:
        conn.execute("BEGIN IMMEDIATE")
        processed = deliveries.process_outbox_once(conn, adapter=adapter, retry_backoff_s=backoff)
        conn.commit()
        return processed
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# §14 matrix: receipt lost / send failed / retry semantics on real stock.
# ---------------------------------------------------------------------------


def test_receipt_lost_stock_charged_once(env):
    """Business committed but the receipt was lost: the delivery stays
    delivery_unknown (never auto-resent); the message that DID reach the
    receiving domain is ingested once, so stock moves exactly once."""
    _apply_operation(env, operation_id="op-r1")
    adapter = deliveries.FaultInjectionAdapter(timeout_first_n=1)

    first = _deliver_once(env, adapter)
    assert first["failed"] == 1 and first["unknown"] == 1
    assert len(adapter.inner.sent) == 1  # the message DID go out, receipt lost
    # The receiver picks up the message that actually went out: the wire
    # payload recorded by the adapter is ingested directly (receipt was lost,
    # but the message reached the receiving domain).
    from chemclaw_lab import inventory as inventory_mod

    def _wire_ingest(op, idx):
        conn = connect(env["db_path"])
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = inventory_mod.ingest_inventory_proposal(
                conn, adapter.inner.sent[idx]["payload"], trace_id=f"tr-{op}"
            )
            conn.commit()
            return result
        finally:
            conn.close()

    _wire_ingest("op-r1", 0)
    assert _subtask_count(env) == 1

    # A second pass MUST NOT resend an unknown-outcome delivery.
    second = _deliver_once(env, adapter)
    assert second["delivered"] == 0 and second["skipped_terminal"] == 1
    assert len(adapter.inner.sent) == 1

    outbox = _outbox_rows(env)[0]
    assert outbox["status"] == "pending"  # not falsely marked sent

    # Execute the subtask exactly once: one transaction, one charge.
    conn = connect(env["db_path"])
    try:
        sub = conn.execute("SELECT subtask_id FROM inventory_subtasks").fetchone()
    finally:
        conn.close()
    executed = _invoke(env, "inventory.subtask.execute",
                       {"subtask_id": sub["subtask_id"]}, operation_id="op-r1-exec")
    assert executed["result"]["status"] == "applied"
    assert _stock(env) == (95.0, 1)

    # Replaying the execution is idempotent: the recorded transaction replays,
    # no second charge (§14: 模型与送达重试不得多扣库存).
    again = _invoke(env, "inventory.subtask.execute",
                    {"subtask_id": sub["subtask_id"]}, operation_id="op-r1-exec2")
    assert again["result"]["idempotent_replay"] is True
    assert _stock(env) == (95.0, 1)


def test_send_failed_backoff_then_single_charge(env):
    """Business applied but the send failed: retries deliver the SAME result;
    stock still moves exactly once."""
    _apply_operation(env, operation_id="op-r2")
    adapter = deliveries.FaultInjectionAdapter(drop_first_n=2)

    assert _deliver_once(env, adapter)["failed"] == 1
    assert _deliver_once(env, adapter)["failed"] == 1
    assert _subtask_count(env) == 0  # never reached the receiving domain
    # Backoff keeps the worker from hammering: next_retry_at is in the future.
    conn = connect(env["db_path"])
    try:
        row = conn.execute(
            "SELECT next_retry_at FROM deliveries WHERE status = 'failed' "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row["next_retry_at"] is not None

    third = _deliver_once(env, adapter)  # backoff elapsed (retry_backoff_s=0)
    assert third["delivered"] == 1
    assert len(adapter.inner.sent) == 1
    _invoke(env, "inventory.inbound.process", {}, context=env["owner"],
            operation_id="op-r2-in")
    assert _subtask_count(env) == 1

    conn = connect(env["db_path"])
    try:
        sub = conn.execute("SELECT subtask_id FROM inventory_subtasks").fetchone()
    finally:
        conn.close()
    _invoke(env, "inventory.subtask.execute", {"subtask_id": sub["subtask_id"]},
            operation_id="op-r2-exec")
    assert _stock(env) == (95.0, 1)  # one charge despite two failed attempts


def test_retries_never_retrigger_the_business_side(env):
    """Retries consume the existing result: the proposal stays applied, the
    event revision head count never grows, no new outbox is minted."""
    proposal_id, _ = _apply_operation(env, operation_id="op-r3")
    before_outbox = _outbox_rows(env)
    adapter = deliveries.FaultInjectionAdapter(drop_first_n=3, timeout_first_n=1)

    for _ in range(3):
        _deliver_once(env, adapter)
    _deliver_once(env, adapter)  # fourth attempt: send passes, timeout fires

    conn = connect(env["db_path"])
    try:
        head = conn.execute(
            "SELECT COUNT(*) FROM event_revisions WHERE entity_type = 'experiment_event'"
        ).fetchone()[0]
        prop = conn.execute(
            "SELECT status, revision FROM memory_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
    finally:
        conn.close()
    assert head == 1  # exactly one A-side event, ever
    assert prop["status"] == "applied"
    after_outbox = _outbox_rows(env)
    assert len(after_outbox) == len(before_outbox)  # no new messages minted
    assert [(r["outbox_id"], r["payload_json"]) for r in after_outbox] == [
        (r["outbox_id"], r["payload_json"]) for r in before_outbox
    ]


def test_late_redelivery_after_sent_is_skipped(env):
    """A late duplicate pass never re-sends an already-sent outbox message."""
    _apply_operation(env, operation_id="op-r4")
    adapter = deliveries.EchoAdapter()
    assert _deliver_once(env, adapter)["delivered"] == 1
    again = _deliver_once(env, adapter)
    assert again["attempted"] == 0 and again["delivered"] == 0
    assert len(adapter.sent) == 1


# ---------------------------------------------------------------------------
# §14 matrix: restarts, backups, worker fencing, stale confirmations.
# ---------------------------------------------------------------------------


def test_restart_preserves_queue_and_business_state(env):
    """Service restart (process death) loses nothing: outbox stays pending,
    applied business facts survive, and the pipeline continues cleanly."""
    _apply_operation(env, operation_id="op-r5")
    assert _outbox_rows(env)[0]["status"] == "pending"
    amount_before, revision_before = _stock(env)

    # Simulate a hard restart: fresh connection + full init_db re-run.
    init_db(env["db_path"])
    conn = connect(env["db_path"])
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
        outbox = conn.execute(
            "SELECT status FROM outbox WHERE message_type = 'inventory_proposal'"
        ).fetchone()
        prop = conn.execute(
            "SELECT status FROM memory_proposals WHERE proposal_id = ?",
            (conn.execute("SELECT proposal_id FROM memory_proposals").fetchone()[0],),
        ).fetchone()
    finally:
        conn.close()
    assert outbox["status"] == "pending"
    assert prop["status"] == "applied"
    assert _stock(env) == (amount_before, revision_before)

    # And the pipeline continues: bridge -> execute works after the restart.
    _invoke(env, "delivery.process", {}, context=env["owner"], operation_id="op-r5-deliver")
    bridge = _invoke(env, "inventory.inbound.process", {}, context=env["owner"],
                     operation_id="op-r5-inbound")
    sub = bridge["result"]["subtasks"][0]
    executed = _invoke(env, "inventory.subtask.execute",
                       {"subtask_id": sub["subtask_id"]}, operation_id="op-r5-exec")
    assert executed["result"]["status"] == "applied"
    assert _stock(env) == (95.0, 1)


def test_backup_restore_roundtrip(env):
    """Backup -> restore keeps every layer consistent (plan §14.3)."""
    _apply_operation(env, operation_id="op-r6")
    _invoke(env, "delivery.process", {}, context=env["owner"], operation_id="op-r6-deliver")
    _invoke(env, "inventory.inbound.process", {}, context=env["owner"], operation_id="op-r6-inbound")
    conn = connect(env["db_path"])
    try:
        sub = conn.execute("SELECT subtask_id FROM inventory_subtasks").fetchone()
    finally:
        conn.close()
    _invoke(env, "inventory.subtask.execute", {"subtask_id": sub["subtask_id"]},
            operation_id="op-r6-exec")

    restore_path = env["db_path"] + ".restore"
    src = connect(env["db_path"])
    dst = sqlite3.connect(restore_path)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()

    def _snapshot(path):
        c = connect(path)
        try:
            return {
                "user_version": c.execute("PRAGMA user_version").fetchone()[0],
                "chemicals": c.execute("SELECT amount, revision FROM chemicals").fetchall(),
                "subtasks": c.execute("SELECT COUNT(*) FROM inventory_subtasks").fetchone()[0],
                "events": c.execute(
                    "SELECT COUNT(*) FROM event_revisions WHERE entity_type = 'experiment_event'"
                ).fetchone()[0],
                "transactions": c.execute("SELECT COUNT(*) FROM inventory_transactions").fetchone()[0],
                "outbox": c.execute("SELECT COUNT(*) FROM outbox WHERE status = 'sent'").fetchone()[0],
            }
        finally:
            c.close()

    assert _snapshot(restore_path) == _snapshot(env["db_path"])


def test_stale_worker_epoch_fencing(env):
    """Lease expires, a NEW worker claims, and the OLD worker's terminal write
    is rejected (§14: lease 过期后旧 worker 返回)."""
    conn = connect(env["db_path"])
    try:
        conn.execute("BEGIN IMMEDIATE")
        task = tasks.create_task(conn, task_type="lab_request",
                                 principal_id="prin_lab", project_id="proj_lab")
        run = tasks.create_agent_run(conn, task_id=task["task_id"],
                                     agent_role="b_capture")
        conn.commit()
    finally:
        conn.close()

    conn = connect(env["db_path"])
    try:
        conn.execute("BEGIN IMMEDIATE")
        claimed = agents.claim_run(conn, run_id=run["run_id"], worker_id="worker-a",
                                   lease_seconds=120)
        epoch_a = claimed["claim_epoch"]
        # Worker A dies; the lease expires; the dispatcher re-queues the run.
        conn.execute(
            "UPDATE agent_runs SET lease_expires_at = datetime('now', '-5 seconds') "
            "WHERE run_id = ?",
            (run["run_id"],),
        )
        recovered = agents.recover_expired_runs(conn)
        conn.commit()
        assert recovered and recovered[0]["run_id"] == run["run_id"]

        conn.execute("BEGIN IMMEDIATE")
        claimed_b = agents.claim_run(conn, run_id=run["run_id"], worker_id="worker-b",
                                     lease_seconds=120)
        epoch_b = claimed_b["claim_epoch"]
        assert epoch_b == epoch_a + 1
        conn.commit()

        # The stale worker finally comes back and tries to finish.
        with pytest.raises(agents.AgentHostError) as excinfo:
            conn.execute("BEGIN IMMEDIATE")
            agents.finish_run(conn, run_id=run["run_id"], status="succeeded",
                              claim_epoch=epoch_a, result={"ok": True})
            conn.commit()
        assert excinfo.value.code == "revision_conflict"
        assert "Stale worker" in excinfo.value.message
        conn.rollback()

        # The current worker finishes fine.
        conn.execute("BEGIN IMMEDIATE")
        agents.finish_run(conn, run_id=run["run_id"], status="succeeded",
                          claim_epoch=epoch_b, result={"ok": True})
        conn.commit()
        final = agents.get_run(conn, run["run_id"])
        assert final["status"] == "succeeded"
    finally:
        conn.close()


def test_duplicate_human_confirmation_is_fenced(env):
    """The same human confirmation replayed (double answer) never double-applies
    or mints a second outbox message (§14: 重复人工答复)."""
    proposal_id, _ = _apply_operation(env, operation_id="op-r8")
    outbox_before = _outbox_rows(env)

    conn = connect(env["db_path"])
    try:
        row = conn.execute(
            "SELECT revision FROM memory_proposals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
    finally:
        conn.close()

    # Replay the same confirmation through a DIFFERENT operation id: the
    # proposal is already applied, so the late answer is fenced (409).
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(env, "memory.proposal.confirm_items", {
            "proposal_id": proposal_id, "item_keys": ["t1"], "decision": "confirmed",
            "expected_proposal_revision": row["revision"],
        }, operation_id="op-r8-reconfirm")
    assert excinfo.value.code == "invalid_state"

    # A confirmation against a stale revision is refused.
    with pytest.raises(capabilities.CapabilityError) as excinfo:
        _invoke(env, "memory.proposal.confirm_items", {
            "proposal_id": proposal_id, "item_keys": ["t1"], "decision": "confirmed",
            "expected_proposal_revision": row["revision"] - 1,
        }, operation_id="op-r8-stale-confirm")
    assert excinfo.value.code == "revision_conflict"

    assert _outbox_rows(env) == outbox_before  # no second message minted
    assert _subtask_count(env) == 0


def test_stale_confirmation_after_evidence_correction(env):
    """After an evidence correction bumps the proposal revision, the OLD
    confirmation can no longer apply (§14: 证据更正后的旧确认)."""
    # Build a proposal but do NOT confirm/apply yet.
    doc = _invoke(env, "evidence.document.create", {
        "project_id": "proj_lab", "notebook_code": "NB-r9", "title": "S08",
        "author": "yu", "source_channel": "photo_upload",
    }, operation_id="op-r9-doc")["result"]["document_id"]
    page_id = _invoke(env, "evidence.page.ingest", {
        "document_id": doc, "page_order": 1, "filename": "p.png",
        "content_data_url": _png_data_url(),
    }, operation_id="op-r9-page")["result"]["page"]["page_id"]
    _invoke(env, "evidence.transcribe.run", {
        "page_id": page_id, "provider": "fixture_ocr",
        "config": {"fixture_blocks": [_block("Used 5 g of Sodium chloride into S-1", 0, "b-r9-0")]},
    }, operation_id="op-r9-run")
    item = {
        "item_key": "t1", "event_kind": "operation",
        "raw_text": "Used 5 g of Sodium chloride into S-1",
        "fields": {
            "amount": {"normalized": 5.0, "unit": "g"},
            "unit": {"normalized": "g"},
            "sample_label": {"normalized": "S-1", "raw": "S-1", "uncertain": False},
        },
        "sample_match": {"action": "link", "candidates": [{"sample_id": "smp_1", "label": "S-1"}]},
        "evidence_refs": [{"page_id": page_id, "transcript_revision": 1, "block_id": "b-r9-0"}],
    }
    created = _invoke(env, "memory.proposal.create", {
        "project_id": "proj_lab", "page_id": page_id, "transcript_revision": 1,
        "profile_id": "chemistry_notebook", "profile_version": "1.0",
        "acceptance_mode": "review",  # Stale human confirmation is the subject of this case.
        "operation_id": "op-r9-create", "items": [item],
    }, operation_id="op-r9-create")
    proposal_id = created["result"]["proposal"]["proposal_id"]
    revision_1 = created["result"]["proposal"]["revision"]

    # The human confirms at revision 1 (recorded, not yet applied).
    _invoke(env, "memory.proposal.confirm_items", {
        "proposal_id": proposal_id, "item_keys": ["t1"], "decision": "confirmed",
        "expected_proposal_revision": revision_1,
    }, operation_id="op-r9-confirm-1")

    # A correction lands: transcript fixed to 7 g -> the proposal built on the
    # old transcript is invalidated (S04 semantics), so the recorded
    # confirmation can never apply.
    _invoke(env, "evidence.transcript.correct", {
        "page_id": page_id, "expected_revision": 1,
        "blocks": [_block("Used 7 g of Sodium chloride into S-1", 0, "b-r9-0c")],
        "reason": "OCR misread",
    }, context=env["owner"], operation_id="op-r9-correct")

    conn = connect(env["db_path"])
    try:
        prop_status = conn.execute(
            "SELECT status, revision FROM memory_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
    finally:
        conn.close()
    # The correction pulls the proposal back to awaiting_confirmation: the
    # recorded confirmation no longer authorizes anything.
    assert prop_status["status"] == "awaiting_confirmation"

    # Applying now refuses to commit business facts: it lands as
    # clarification_needed and mints NO inventory message, moves NO stock.
    reapplied = _invoke(env, "memory.proposal.apply", {
        "proposal_id": proposal_id, "operation_id": "op-r9-apply-stale",
    }, operation_id="op-r9-apply-stale")
    assert reapplied["action_status"] == "clarification_needed"

    conn = connect(env["db_path"])
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE message_type = 'inventory_proposal'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 0  # the stale confirmation never reached the bridge
    assert _stock(env) == (100.0, 0)


def test_cancelled_subtask_execution_refused(env):
    """Cancel/commit race: a cancelled subtask refuses execution and the stock
    never moves (§14: 取消与提交竞态)."""
    occurred = "2026-10-11T10:00:00+08:00"

    def _run(op):
        _, _ = _apply_operation(
            env, operation_id=op,
            raw_text="Used 3 g of Sodium chloride into S-1", amount=3.0,
        )
        conn = connect(env["db_path"])
        try:
            conn.execute(
                "UPDATE event_revisions SET event_time_start = ? "
                "WHERE entity_type = 'experiment_event' AND event_time_start IS NULL",
                (occurred,),
            )
            conn.commit()
        finally:
            conn.close()
        _invoke(env, "delivery.process", {}, context=env["owner"], operation_id=f"{op}-deliver")
        return _invoke(env, "inventory.inbound.process", {}, context=env["owner"],
                       operation_id=f"{op}-inbound")

    _run("op-r10a")
    _run("op-r10b")

    conn = connect(env["db_path"])
    try:
        rows = conn.execute(
            "SELECT subtask_id, status FROM inventory_subtasks ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    suspect = next(r for r in rows if r["status"] == "duplicate_suspect")
    first = next(r for r in rows if r["status"] == "pending")

    # Human cancels the suspect (duplicate)…
    _invoke(env, "inventory.subtask.resolve", {
        "subtask_id": suspect["subtask_id"], "confirm_duplicate": False,
    }, operation_id="op-r10-cancel")
    # …but a racing worker still tries to execute it.
    with pytest.raises(ValueError, match="cannot be executed"):
        _invoke(env, "inventory.subtask.execute",
                {"subtask_id": suspect["subtask_id"]}, operation_id="op-r10-race")
    assert _stock(env) == (100.0, 0)  # the losing side of the race moved nothing

    # The legitimate one still commits exactly once.
    executed = _invoke(env, "inventory.subtask.execute",
                       {"subtask_id": first["subtask_id"]}, operation_id="op-r10-commit")
    assert executed["result"]["status"] == "applied"
    assert _stock(env) == (97.0, 1)


def test_cross_project_switch_refused(env):
    """Cross-project session switch: the outsider sees nothing of proj_lab
    (§14: 跨项目会话切换; §17 row 4)."""
    _apply_operation(env, operation_id="op-r11")
    _invoke(env, "delivery.process", {}, context=env["owner"], operation_id="op-r11-deliver")
    _invoke(env, "inventory.inbound.process", {}, context=env["owner"], operation_id="op-r11-inbound")
    conn = connect(env["db_path"])
    try:
        sub = conn.execute("SELECT subtask_id FROM inventory_subtasks").fetchone()
    finally:
        conn.close()

    # Cross-project execution is refused on BOTH the AuthError membership
    # fence and the capability-scope fence; nothing leaks, nothing moves.
    with pytest.raises((capabilities.CapabilityError, auth.AuthError)) as excinfo:
        _invoke(env, "inventory.subtask.execute",
                {"subtask_id": sub["subtask_id"]}, context=env["outsider"],
                operation_id="op-r11-out")
    if isinstance(excinfo.value, capabilities.CapabilityError):
        assert excinfo.value.code in ("forbidden_scope", "forbidden_role", "not_found")

    # A scope-less status listing is filtered to the caller's memberships:
    # the outsider sees ZERO proj_lab subtasks (no values, no ids, no leaks).
    listing = _invoke(env, "inventory.subtask.status", {}, context=env["outsider"],
                      operation_id="op-r11-out-list")
    assert listing["result"]["count"] == 0
    member_listing = _invoke(env, "inventory.subtask.status", {}, context=env["member"],
                             operation_id="op-r11-in-list")
    assert member_listing["result"]["count"] == 1  # the member still sees their own
    assert _stock(env) == (100.0, 0)
