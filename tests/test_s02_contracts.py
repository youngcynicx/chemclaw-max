"""S02 acceptance tests: shared contracts, access control, idempotency.

Covers the plan §18 acceptance list:
- cross-project read/write denied and forged confirmed_by rejected
- same-key retry does not double-apply
- same key with different content is a conflict
- concurrent stale-revision submission fails
- migration reproduces on a legacy v3 database and preserves history
plus the durable task/interaction/message contracts used by S04/S05/S06.
"""

from __future__ import annotations

import json
import threading

import pytest

from chemclaw_lab import auth, capabilities, db, evidence, tasks
from chemclaw_lab.capabilities import CapabilityError
from chemclaw_lab.tasks import TaskError


@pytest.fixture()
def lab(tmp_path):
    db_path = tmp_path / "s02.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    conn.execute("INSERT INTO projects (project_id, name) VALUES ('P1', 'alpha'), ('P2', 'beta')")
    conn.execute(
        "INSERT INTO chemicals (chemical_id, name, amount, unit) "
        "VALUES ('chm_nacl', 'Sodium chloride', 100.0, 'g')"
    )
    conn.commit()
    conn.close()

    conn = db.connect(db_path)
    auth.register_principal(conn, principal_id="hum_member", kind="human", display_name="Member")
    auth.grant_membership(conn, principal_id="hum_member", project_id="P1", role="member")
    auth.register_credential(
        conn, credential_id="cred_member", principal_id="hum_member", secret="member-key"
    )
    auth.register_principal(conn, principal_id="hum_viewer", kind="human", display_name="Viewer")
    auth.grant_membership(conn, principal_id="hum_viewer", project_id="P1", role="viewer")
    auth.register_credential(
        conn, credential_id="cred_viewer", principal_id="hum_viewer", secret="viewer-key"
    )
    auth.register_principal(conn, principal_id="hum_outsider", kind="human")
    auth.register_credential(
        conn, credential_id="cred_outsider", principal_id="hum_outsider", secret="outsider-key"
    )
    conn.commit()
    member = auth.authenticate_principal(conn, "member-key")
    viewer = auth.authenticate_principal(conn, "viewer-key")
    outsider = auth.authenticate_principal(conn, "outsider-key")
    conn.close()
    return {"db_path": db_path, "member": member, "viewer": viewer, "outsider": outsider}


# ---------------------------------------------------------------------------
# access control


def test_unknown_credential_is_unauthorized(lab):
    conn = db.connect(lab["db_path"])
    with pytest.raises(auth.AuthError) as excinfo:
        auth.authenticate_principal(conn, "wrong-key")
    assert excinfo.value.code == "unauthorized"
    conn.close()


def test_cross_project_write_denied(lab):
    with pytest.raises(auth.AuthError) as excinfo:
        capabilities.invoke_capability(
            lab["db_path"],
            lab["member"],
            "inventory.message.process",
            {"raw_text": "Used 5 g of Sodium chloride", "project_id": "P2"},
            operation_id="cross-write",
        )
    assert excinfo.value.code == "forbidden_scope"


def test_cross_project_read_denied(lab):
    # member of P1 only: explicit P2 read must fail; omitting the filter only
    # ever returns rows from the principal's own projects.
    with pytest.raises(auth.AuthError) as excinfo:
        capabilities.invoke_capability(
            lab["db_path"],
            lab["member"],
            "inventory.query.list",
            {"kind": "transactions", "project_id": "P2"},
            operation_id="cross-read",
        )
    assert excinfo.value.code == "forbidden_scope"

    conn = db.connect(lab["db_path"])
    conn.execute(
        """
        INSERT INTO inventory_transactions
          (transaction_id, chemical_id, project_id, transaction_type, amount_delta, unit)
        VALUES ('txn_p2', 'chm_nacl', 'P2', 'use', -1.0, 'g')
        """
    )
    conn.commit()
    conn.close()
    envelope = capabilities.invoke_capability(
        lab["db_path"],
        lab["member"],
        "inventory.query.list",
        {"kind": "transactions"},
        operation_id="scope-filtered-read",
    )
    ids = [row["transaction_id"] for row in envelope["result"]["items"]]
    assert "txn_p2" not in ids


def test_viewer_cannot_invoke_write_capability(lab):
    with pytest.raises(CapabilityError) as excinfo:
        capabilities.invoke_capability(
            lab["db_path"],
            lab["viewer"],
            "inventory.message.process",
            {"raw_text": "Used 5 g of Sodium chloride", "project_id": "P1"},
            operation_id="viewer-write",
        )
    assert excinfo.value.code == "forbidden_capability"


def test_forced_confirmed_by_is_rejected(lab):
    with pytest.raises(CapabilityError) as excinfo:
        capabilities.invoke_capability(
            lab["db_path"],
            lab["member"],
            "inventory.event.resolve",
            {
                "event_id": "evt_whatever",
                "confirmed_by": "someone_else",
            },
            operation_id="forged-confirmed-by",
        )
    assert excinfo.value.code == "schema_violation"


def test_confirmer_is_bound_to_authenticated_principal(lab):
    # Create a pending proposal as the member...
    envelope = capabilities.invoke_capability(
        lab["db_path"],
        lab["member"],
        "inventory.message.process",
        {"raw_text": "I plan to use 5 g of Sodium chloride", "project_id": "P1"},
        operation_id="pending-proposal",
    )
    assert envelope["action_status"] == "clarification_needed"
    event_id = envelope["result"]["event_id"]

    # ...and resolve it: confirmed_by must be the authenticated principal.
    resolved = capabilities.invoke_capability(
        lab["db_path"],
        lab["member"],
        "inventory.event.resolve",
        {"event_id": event_id, "approve": True},
        operation_id="resolve-proposal",
    )
    assert resolved["action_status"] == "applied"
    conn = db.connect(lab["db_path"])
    row = conn.execute(
        "SELECT confirmed_by FROM event_proposals WHERE event_id = ?", (event_id,)
    ).fetchone()
    conn.close()
    assert row["confirmed_by"] == "hum_member"


def test_unlinked_principal_has_no_business_attribution(lab):
    envelope = capabilities.invoke_capability(
        lab["db_path"],
        lab["member"],
        "inventory.message.process",
        {"raw_text": "Used 5 g of Sodium chloride", "project_id": "P1"},
        operation_id="attribution-check",
    )
    assert envelope["action_status"] == "applied"
    assert any("principal_user_links" in warning for warning in envelope["warnings"])
    conn = db.connect(lab["db_path"])
    user_id = conn.execute(
        "SELECT user_id FROM inventory_transactions WHERE event_id = ?",
        (envelope["result"]["event_id"],),
    ).fetchone()["user_id"]
    conn.close()
    assert user_id is None


def test_linked_principal_attribution_uses_legacy_user(lab):
    conn = db.connect(lab["db_path"])
    conn.execute("INSERT INTO users (user_id, name) VALUES ('usr_member', 'Member')")
    conn.execute(
        "INSERT INTO principal_user_links (principal_id, user_id) VALUES ('hum_member', 'usr_member')"
    )
    conn.commit()
    conn.close()

    envelope = capabilities.invoke_capability(
        lab["db_path"],
        lab["member"],
        "inventory.message.process",
        {"raw_text": "Used 5 g of Sodium chloride", "project_id": "P1"},
        operation_id="linked-attribution",
    )
    assert envelope["action_status"] == "applied"
    conn = db.connect(lab["db_path"])
    user_id = conn.execute(
        "SELECT user_id FROM inventory_transactions WHERE event_id = ?",
        (envelope["result"]["event_id"],),
    ).fetchone()["user_id"]
    conn.close()
    assert user_id == "usr_member"


# ---------------------------------------------------------------------------
# idempotency


def test_same_key_retry_does_not_double_apply(lab):
    params = {"raw_text": "Used 5 g of Sodium chloride", "project_id": "P1"}
    first = capabilities.invoke_capability(
        lab["db_path"], lab["member"], "inventory.message.process", params,
        operation_id="op-dedupe",
    )
    assert first["action_status"] == "applied"
    assert first["idempotent_replay"] is False

    replay = capabilities.invoke_capability(
        lab["db_path"], lab["member"], "inventory.message.process", params,
        operation_id="op-dedupe",
    )
    assert replay["idempotent_replay"] is True
    assert replay["result"]["transaction_id"] == first["result"]["transaction_id"]

    conn = db.connect(lab["db_path"])
    amount = conn.execute(
        "SELECT amount FROM chemicals WHERE chemical_id = 'chm_nacl'"
    ).fetchone()["amount"]
    txn_count = conn.execute(
        "SELECT COUNT(*) AS n FROM inventory_transactions WHERE event_id = ?",
        (first["result"]["event_id"],),
    ).fetchone()["n"]
    conn.close()
    assert amount == 95.0
    assert txn_count == 1


def test_same_key_different_content_is_conflict(lab):
    capabilities.invoke_capability(
        lab["db_path"], lab["member"], "inventory.message.process",
        {"raw_text": "Used 5 g of Sodium chloride", "project_id": "P1"},
        operation_id="op-conflict",
    )
    with pytest.raises(CapabilityError) as excinfo:
        capabilities.invoke_capability(
            lab["db_path"], lab["member"], "inventory.message.process",
            {"raw_text": "Used 7 g of Sodium chloride", "project_id": "P1"},
            operation_id="op-conflict",
        )
    assert excinfo.value.code == "idempotency_conflict"


def test_stale_expected_revision_fails(lab):
    conn = db.connect(lab["db_path"])
    stale_revision = conn.execute(
        "SELECT revision FROM chemicals WHERE chemical_id = 'chm_nacl'"
    ).fetchone()["revision"]
    conn.close()

    # First apply moves the stock revision 0 -> 1.
    first = capabilities.invoke_capability(
        lab["db_path"], lab["member"], "inventory.message.process",
        {"raw_text": "Used 5 g of Sodium chloride", "project_id": "P1",
         "expected_revision": stale_revision},
        operation_id="op-rev-1",
    )
    assert first["action_status"] == "applied"

    # A concurrent submitter that still saw the old revision must fail.
    with pytest.raises(CapabilityError) as excinfo:
        capabilities.invoke_capability(
            lab["db_path"], lab["member"], "inventory.message.process",
            {"raw_text": "Used 5 g of Sodium chloride", "project_id": "P1",
             "expected_revision": stale_revision},
            operation_id="op-rev-2",
        )
    assert excinfo.value.code == "revision_conflict"

    conn = db.connect(lab["db_path"])
    amount = conn.execute(
        "SELECT amount FROM chemicals WHERE chemical_id = 'chm_nacl'"
    ).fetchone()["amount"]
    conn.close()
    assert amount == 95.0


def test_concurrent_submissions_serialize_with_correct_revisions(lab):
    results: list[dict] = []

    def submit(index: int) -> None:
        try:
            envelope = capabilities.invoke_capability(
                lab["db_path"], lab["member"], "inventory.message.process",
                {"raw_text": "Used 1 g of Sodium chloride", "project_id": "P1"},
                operation_id=f"op-concurrent-{index}",
            )
            results.append(envelope)
        except CapabilityError as exc:
            results.append({"error": exc.code})

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    statuses = [
        item.get("action_status") or item.get("error") for item in results
    ]
    assert all(status in ("applied", "failed") for status in statuses)
    conn = db.connect(lab["db_path"])
    amount, revision = conn.execute(
        "SELECT amount, revision FROM chemicals WHERE chemical_id = 'chm_nacl'"
    ).fetchone()
    applied_count = conn.execute(
        "SELECT COUNT(*) AS n FROM inventory_transactions"
    ).fetchone()["n"]
    conn.close()
    assert applied_count == sum(1 for s in statuses if s == "applied")
    assert revision == applied_count
    assert amount == 100.0 - applied_count


# ---------------------------------------------------------------------------
# migration / history preservation


def test_migration_from_legacy_v3_preserves_history(tmp_path):
    legacy = tmp_path / "legacy_v3.db"
    conn = db.connect(legacy)
    try:
        conn.executescript(db.SCHEMA_SQL)
        db._migrate_v02(conn)
        db._migrate_v03(conn)
        conn.execute("PRAGMA user_version = 3")
        conn.execute(
            "INSERT INTO projects (project_id, name) VALUES ('P1', 'legacy')"
        )
        conn.execute(
            "INSERT INTO chemicals (chemical_id, name, amount, unit) "
            "VALUES ('chm_legacy', 'Legacy KBr', 42.0, 'g')"
        )
        conn.execute(
            "INSERT INTO experiment_events (event_id, project_id, event_type, title) "
            "VALUES ('evt_legacy', 'P1', 'observation', 'pre-S02 record')"
        )
        conn.commit()
    finally:
        conn.close()

    db.init_db(legacy)

    conn = db.connect(legacy)
    try:
        # S03 bumped the schema to v5 (evidence ingestion & transcription);
        # this S02-era test asserts the shared contract tables survive the
        # full migration chain, now up to and including S10's v11.
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
        chemical = conn.execute(
            "SELECT name, amount, revision FROM chemicals WHERE chemical_id = 'chm_legacy'"
        ).fetchone()
        assert chemical["name"] == "Legacy KBr"
        assert chemical["amount"] == 42.0
        assert chemical["revision"] == 0
        legacy_event = conn.execute(
            "SELECT provenance FROM experiment_events WHERE event_id = 'evt_legacy'"
        ).fetchone()
        assert legacy_event["provenance"] == "legacy_source_unknown"
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "principals", "memberships", "auth_credentials", "operation_results",
            "tasks", "pending_interactions", "inbound_events", "outbox",
            "deliveries", "typed_messages", "event_revisions", "evidence_objects",
        }.issubset(tables)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# durable task / interaction / message contracts


def test_task_transition_state_machine(lab):
    conn = db.connect(lab["db_path"])
    conn.execute("BEGIN IMMEDIATE")
    task = tasks.create_task(
        conn, task_type="literature_screen", principal_id="hum_member", project_id="P1"
    )
    assert task["status"] == "queued"
    tasks.transition_task(conn, task["task_id"], "running")
    with pytest.raises(TaskError) as excinfo:
        tasks.transition_task(conn, task["task_id"], "queued")
    assert excinfo.value.code == "invalid_state"
    tasks.transition_task(conn, task["task_id"], "succeeded")
    with pytest.raises(TaskError) as excinfo:
        tasks.transition_task(conn, task["task_id"], "running")
    assert excinfo.value.code == "invalid_state"
    conn.commit()
    conn.close()


def test_waiting_input_resume_with_revision_check(lab):
    conn = db.connect(lab["db_path"])
    conn.execute("BEGIN IMMEDIATE")
    task = tasks.create_task(
        conn, task_type="data_entry", principal_id="hum_member", project_id="P1"
    )
    interaction = tasks.open_pending_interaction(
        conn, task_id=task["task_id"], question="Which page is the yield on?"
    )
    conn.commit()

    task_after = tasks.get_task(conn, task["task_id"])
    assert task_after["status"] == "waiting_input"

    # Wrong revision: rejected, nothing changes.
    with pytest.raises(TaskError) as excinfo:
        tasks.answer_pending_interaction(
            conn,
            interaction_id=interaction["interaction_id"],
            answer={"yield_percent": 88.5},
            principal_id="hum_member",
            expected_task_revision=task_after["revision"] + 5,
        )
    assert excinfo.value.code == "revision_conflict"
    assert tasks.get_task(conn, task["task_id"])["status"] == "waiting_input"

    # Non-owner: rejected.
    with pytest.raises(TaskError) as excinfo:
        tasks.answer_pending_interaction(
            conn,
            interaction_id=interaction["interaction_id"],
            answer={},
            principal_id="hum_outsider",
            expected_task_revision=task_after["revision"],
        )
    assert excinfo.value.code == "forbidden_scope"

    # Correct revision: resumes and marks the interaction answered.
    outcome = tasks.answer_pending_interaction(
        conn,
        interaction_id=interaction["interaction_id"],
        answer={"yield_percent": 88.5},
        principal_id="hum_member",
        expected_task_revision=task_after["revision"],
    )
    assert outcome["task"]["status"] == "running"
    assert outcome["interaction"]["status"] == "answered"
    conn.close()


def test_agent_run_claim_epoch_fencing(lab):
    conn = db.connect(lab["db_path"])
    conn.execute("BEGIN IMMEDIATE")
    task = tasks.create_task(conn, task_type="ocr_run", principal_id="hum_member", project_id="P1")
    run = tasks.create_agent_run(conn, task_id=task["task_id"], agent_role="ocr_worker")
    claimed = tasks.claim_agent_run(conn, run["run_id"], worker_id="w1", lease_seconds=60)
    assert claimed["claim_epoch"] == 1
    # A stale worker with an old epoch cannot finalize the run.
    with pytest.raises(TaskError) as excinfo:
        tasks.finish_agent_run(conn, run["run_id"], status="succeeded", claim_epoch=0)
    assert excinfo.value.code == "revision_conflict"
    tasks.finish_agent_run(conn, run["run_id"], status="succeeded", claim_epoch=1)
    conn.commit()
    conn.close()


def test_checkpoint_records_operation_reference(lab):
    conn = db.connect(lab["db_path"])
    conn.execute("BEGIN IMMEDIATE")
    task = tasks.create_task(conn, task_type="pipeline", principal_id="hum_member", project_id="P1")
    checkpoint = tasks.record_checkpoint(
        conn,
        task_id=task["task_id"],
        step_name="parse_pages",
        input_version="ocr-poc-v1",
        output_refs=[{"kind": "event_proposal", "id": "evt_x"}],
        operation_id="op-parse-1",
    )
    latest = tasks.latest_checkpoint(conn, task["task_id"], "parse_pages")
    conn.commit()
    conn.close()
    assert latest["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert latest["operation_id"] == "op-parse-1"
    assert json.loads(latest["output_refs_json"]) == [
        {"kind": "event_proposal", "id": "evt_x"}
    ]


def test_inbound_events_dedupe_on_transport_event_id(lab):
    conn = db.connect(lab["db_path"])
    conn.execute("BEGIN IMMEDIATE")
    payload = {"text": "Used 2 g of Sodium chloride", "project_id": "P1"}
    first = tasks.enqueue_inbound(
        conn, source="wechat", transport_event_id="evt-001", payload=payload
    )
    duplicate = tasks.enqueue_inbound(
        conn, source="wechat", transport_event_id="evt-001", payload=payload
    )
    conn.commit()
    conn.close()
    assert first["dedupe"] == "queued"
    assert duplicate["dedupe"] == "duplicate"
    assert duplicate["inbound_id"] == first["inbound_id"]


def test_outbox_delivery_are_separate_idempotency_layers(lab):
    conn = db.connect(lab["db_path"])
    conn.execute("BEGIN IMMEDIATE")
    outbox = tasks.enqueue_outbox(
        conn,
        aggregate_type="event_proposal",
        aggregate_id="evt_x",
        message_type="proposal.pending_confirmation",
        payload={"event_id": "evt_x", "question": "confirm 5 g?"},
    )
    delivery = tasks.create_delivery(
        conn, outbox_id=outbox["outbox_id"], channel="wechat", target="user-42"
    )
    conn.commit()
    assert tasks.list_pending_outbox(conn)[0]["outbox_id"] == outbox["outbox_id"]
    marked = tasks.mark_delivery(
        conn, delivery["delivery_id"], status="delivered", transport_event_id="wx-777"
    )
    conn.commit()
    conn.close()
    assert marked["status"] == "delivered"
    assert marked["transport_event_id"] == "wx-777"
    assert marked["attempt_count"] == 1


def test_typed_messages_validate_payload_shape(lab):
    conn = db.connect(lab["db_path"])
    message = tasks.record_typed_message(
        conn,
        message_kind="command",
        message_type="inventory.message.process",
        payload={"intent": "record_usage", "raw_text": "Used 1 g of Sodium chloride"},
        correlation_id="corr-1",
    )
    with pytest.raises(TaskError) as excinfo:
        tasks.record_typed_message(
            conn,
            message_kind="task_result",
            message_type="ocr.run",
            payload={"missing": "task_id"},
        )
    with pytest.raises(TaskError) as excinfo:
        tasks.record_typed_message(
            conn,
            message_kind="domain_event",
            message_type="inventory.proposal.pending",
            payload={"no_occurred_ref": True},
        )
    conn.close()
    assert message["message_id"].startswith("tmsg_")
    assert excinfo.value.code == "schema_violation"


# ---------------------------------------------------------------------------
# evidence scope


def test_evidence_read_requires_project_membership(lab):
    evidence_file = lab["db_path"].parent / "lab_page_1.png"
    evidence_file.write_bytes(b"fake png bytes")
    conn = db.connect(lab["db_path"])
    conn.execute("BEGIN IMMEDIATE")
    registered = evidence.register_evidence_object(
        conn,
        storage_path=str(evidence_file),
        project_id="P1",
        mime="image/png",
        page_count=1,
        source="scan",
    )
    conn.commit()

    meta = evidence.read_evidence_metadata(
        conn, lab["member"], evidence_id=registered["evidence_id"]
    )
    assert meta["sha256"].startswith("")

    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.read_evidence_metadata(
            conn, lab["outsider"], evidence_id=registered["evidence_id"]
        )
    assert excinfo.value.code == "unreadable_evidence"
    conn.close()


# ---------------------------------------------------------------------------
# capability listing contract


def test_capability_listing_only_publishes_allowed_and_implemented(lab):
    listing = capabilities.list_capabilities(lab["viewer"])
    ids = {item["capability_id"] for item in listing}
    # S03 added read-only evidence capabilities to the viewer role;
    # S04 added read-only experiment-memory capabilities alongside them.
    # S05 added the read-only shared-query surface on top.
    # S07 added the read-only cross-domain subtask status listing (§13.6).
    # S09 added the read-only extension-domain listings (§15).
    assert ids == {
        "inventory.query.list",
        "task.read",
        "evidence.page.read",
        "evidence.page.original",
        "evidence.page.preview",
        "evidence.pages.list",
        "evidence.provider.list",
        "inventory.subtask.status",
        "memory.proposal.read",
        "memory.event.read",
        "query.events.search",
        "query.samples.list",
        "query.sample.timeline",
        "query.sample.context",
        "query.answer.render",
        "query.report.read",
        "resource.list",
        "resource.booking.status",
        "research.milestone.status",
        "external.result.status",
    }

    listing = capabilities.list_capabilities(lab["member"])
    ids = {item["capability_id"] for item in listing}
    assert "inventory.message.process" in ids
    assert "inventory.event.resolve" in ids

    assert capabilities.list_capabilities(lab["outsider"]) == []


# ---------------------------------------------------------------------------
# HTTP /v2 entry


@pytest.fixture()
def http_server(lab):
    from chemclaw_lab.api import create_api_server

    server = create_api_server(lab["db_path"], host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _http_json(method: str, url: str, *, payload=None, headers=None):
    import json as json_module
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    request = Request(url, method=method, headers=headers or {})
    data = None
    if payload is not None:
        data = json_module.dumps(payload).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, data=data, timeout=10) as response:
            return response.status, json_module.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json_module.loads(exc.read().decode("utf-8"))


def test_http_v2_requires_principal_credential(http_server):
    status, body = _http_json("GET", f"{http_server}/v2/capabilities")
    assert status == 401
    assert body["error"]["code"] == "unauthorized"

    status, body = _http_json(
        "GET",
        f"{http_server}/v2/capabilities",
        headers={"Authorization": "Bearer member-key"},
    )
    assert status == 200
    assert body["capability_version"] == capabilities.CAPABILITY_VERSION
    ids = {item["capability_id"] for item in body["capabilities"]}
    assert "inventory.message.process" in ids


def test_http_v2_invoke_end_to_end(http_server):
    headers = {"Authorization": "Bearer member-key", "X-Operation-Id": "http-op-1"}
    status, body = _http_json(
        "POST",
        f"{http_server}/v2/capabilities/inventory.message.process/invoke",
        payload={"params": {"raw_text": "Used 5 g of Sodium chloride", "project_id": "P1"}},
        headers=headers,
    )
    assert status == 200
    assert body["action_status"] == "applied"
    assert body["principal_id"] == "hum_member"

    # Replay with the same operation id returns the stored envelope.
    status, replay = _http_json(
        "POST",
        f"{http_server}/v2/capabilities/inventory.message.process/invoke",
        payload={"params": {"raw_text": "Used 5 g of Sodium chloride", "project_id": "P1"}},
        headers=headers,
    )
    assert status == 200
    assert replay["idempotent_replay"] is True
    assert replay["result"]["transaction_id"] == body["result"]["transaction_id"]

    # Cross-project write is refused over HTTP as well.
    status, body = _http_json(
        "POST",
        f"{http_server}/v2/capabilities/inventory.message.process/invoke",
        payload={"params": {"raw_text": "Used 5 g of Sodium chloride", "project_id": "P2"}},
        headers={"Authorization": "Bearer member-key", "X-Operation-Id": "http-op-2"},
    )
    assert status == 403
    assert body["error"]["code"] == "forbidden_scope"


def test_http_v2_missing_operation_id_is_rejected(http_server):
    status, body = _http_json(
        "POST",
        f"{http_server}/v2/capabilities/inventory.message.process/invoke",
        payload={"params": {"project_id": "P1", "raw_text": "Used 5 g of Sodium chloride"}},
        headers={"Authorization": "Bearer member-key"},
    )
    assert status == 400
    assert body["error"]["code"] == "schema_violation"
