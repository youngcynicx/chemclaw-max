"""External reconnects must read current, currently authorized Core data."""
from __future__ import annotations

import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from chemclaw_lab import agents, auth, capabilities, clients, db
from chemclaw_lab.api import create_api_server


def _current(lab, principal):
    conn = db.connect(lab.db_path)
    try:
        return auth.load_principal_context(conn, principal)
    finally:
        conn.close()


def _revoke(lab, principal, project):
    lab.execute("DELETE FROM memberships WHERE principal_id=? AND project_id=?", (principal, project))
    return _current(lab, principal)


def _cache_old_read(lab, context, capability, params, operation):
    """Retain a real receipt as an earlier server version would have done."""
    envelope = lab.invoke(context, capability, params, operation_id=operation)
    capabilities._store_operation_result(lab.db_path,
        namespace=capabilities._operation_storage_namespace(context, "default", params),
        operation_id=operation, context=context, capability_id=capability,
        request_hash=capabilities._request_hash(capability, params), envelope=envelope,
        entity_refs=envelope["object_refs"])
    return envelope


def _notebook(lab):
    author = lab.context("author", memberships={"P1": "member", "P2": "member"})
    page = lab.add_page("P1")
    lab.execute("UPDATE evidence_objects SET mime='text/plain' WHERE evidence_id="
                "(SELECT evidence_id FROM evidence_pages WHERE page_id=?)", (page["page_id"],))
    transcript = lab.add_transcript(page["page_id"], ["A measured 20 units"])
    proposal = lab.invoke(author, "memory.proposal.create", {
        "project_id": "P1", "page_id": page["page_id"], "transcript_revision": 1,
        "profile_id": "chemistry_notebook", "operation_id": "seed-proposal", "items": [{
            "item_key": "mass", "event_kind": "observation", "raw_text": "A measured 20 units",
            "fields": {"sample_label": {"normalized": "A"}},
            "evidence_refs": [{"page_id": page["page_id"], "transcript_revision": 1,
                               "block_id": transcript["block_ids"][0]}],
        }],
    }, operation_id="seed-proposal")["result"]["proposal"]
    event = proposal["items"][0]["event_id"]
    conn = db.connect(lab.db_path)
    try:
        sample = conn.execute("SELECT sample_id FROM samples WHERE label='A'").fetchone()[0]
    finally:
        conn.close()
    return author, page["page_id"], proposal["proposal_id"], event, sample


@pytest.fixture
def external_http(lab):
    lab.context("external", kind="service", memberships={"P1": "viewer", "P2": "viewer"})
    conn = db.connect(lab.db_path)
    try:
        auth.register_credential(conn, credential_id="external-test", principal_id="external",
                                 secret="external-test-only")
        conn.commit()
    finally:
        conn.close()
    server = create_api_server(lab.db_path, port=0, service_settings=lab.settings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_external_client_reconnect_ids_are_fresh_and_can_change_query(lab, external_http):
    lab.add_page("P1")
    client = lambda: clients.ReadOnlyQueryClient(external_http, "external-test-only")
    assert client().search_events(project_ids=["P1"])["candidates"] == []
    event = lab.add_event(project_id="P1", raw_text="new evidence")
    result = client().search_events(project_ids=["P1"])
    assert [candidate["object_id"] for candidate in result["candidates"]] == [event]
    # A reconnect restarts the reference client's counter at ext-search-1.
    assert client().search_events(project_ids=["P1"], sample_label="missing")["candidates"] == []
    assert lab.scalar("SELECT count(*) FROM operation_results") == 0


def test_old_cached_query_is_ignored_after_new_data_and_filter_changes(lab):
    reader = lab.context("reader", kind="service", memberships={"P1": "viewer"})
    lab.add_page("P1")
    _cache_old_read(lab, reader, "query.events.search", {}, "old-read")
    event = lab.add_event(project_id="P1", raw_text="new evidence")
    fresh = lab.invoke(reader, "query.events.search", {}, operation_id="old-read")
    assert fresh["idempotent_replay"] is False
    assert [candidate["object_id"] for candidate in fresh["result"]["candidates"]] == [event]
    filtered = lab.invoke(reader, "query.events.search", {"sample_label": "absent"}, operation_id="old-read")
    assert filtered["result"]["candidates"] == []
    assert lab.scalar("SELECT count(*) FROM operation_results") == 1  # Old row remains, never reused.


@pytest.mark.parametrize("capability", ["query.events.search", "query.samples.list"])
def test_old_project_union_cache_is_not_replayed_after_partial_revocation(lab, capability):
    _notebook(lab)
    reader = lab.context("reader", kind="service", memberships={"P1": "viewer", "P2": "viewer"})
    _cache_old_read(lab, reader, capability, {}, "old-union")
    current = _revoke(lab, "reader", "P1")
    fresh = lab.invoke(current, capability, {}, operation_id="old-union")
    assert fresh["idempotent_replay"] is False
    assert fresh["result"]["scope"] == ["P2"]
    assert fresh["result"].get("candidates", fresh["result"].get("samples")) == []


@pytest.mark.parametrize("kind", ["event", "history", "page", "proposal", "timeline"])
def test_object_reads_reauthorize_even_with_old_cached_receipt(lab, kind):
    _, page, proposal, event, sample = _notebook(lab)
    reader = lab.context("reader", kind="service", memberships={"P1": "viewer", "P2": "viewer"})
    capability, params = {
        "event": ("memory.event.read", {"event_id": event}),
        "history": ("memory.event.read", {"event_id": event, "include_history": True}),
        "page": ("evidence.page.read", {"page_id": page}),
        "proposal": ("memory.proposal.read", {"proposal_id": proposal}),
        "timeline": ("query.sample.timeline", {"sample_id": sample}),
    }[kind]
    _cache_old_read(lab, reader, capability, params, "old-object")
    current = _revoke(lab, "reader", "P1")
    with pytest.raises((auth.AuthError, capabilities.CapabilityError)) as exc:
        lab.invoke(current, capability, params, operation_id="old-object")
    assert exc.value.code == "forbidden_scope"


def test_event_history_and_transcript_reads_refresh_under_same_operation(lab):
    author, page, _, event, _ = _notebook(lab)
    reader = lab.context("reader", kind="service", memberships={"P1": "viewer"})
    history_params = {"event_id": event, "include_history": True}
    page_params = {"page_id": page}
    _cache_old_read(lab, reader, "memory.event.read", history_params, "old-history")
    _cache_old_read(lab, reader, "evidence.page.read", page_params, "old-page")
    lab.invoke(author, "memory.event.correct", {"event_id": event, "expected_revision": 1,
        "changes": {"raw_text": "A measured 22 units"}, "reason": "human correction"}, operation_id="correct")
    lab.add_transcript(page, ["A measured 22 units"])
    history = lab.invoke(reader, "memory.event.read", history_params, operation_id="old-history")["result"]
    assert [revision["revision"] for revision in history["revisions"]] == [1, 2]
    source = lab.invoke(reader, "evidence.page.read", page_params, operation_id="old-page")["result"]
    assert source["page"]["transcript"]["active_revision"] == 2


def test_write_replay_stays_idempotent_and_changed_request_conflicts(lab):
    author = lab.context("author", memberships={"P1": "member"})
    params = {"project_id": "P1", "raw_text": "Used 5 g of Sodium chloride"}
    first = lab.invoke(author, "inventory.message.process", params, operation_id="write-once")
    replay = lab.invoke(author, "inventory.message.process", params, operation_id="write-once")
    assert replay["idempotent_replay"] is True
    assert replay["result"]["transaction_id"] == first["result"]["transaction_id"]
    assert lab.stock() == (95.0, 1)
    with pytest.raises(capabilities.CapabilityError) as exc:
        lab.invoke(author, "inventory.message.process", {**params, "raw_text": "Used 6 g of Sodium chloride"},
                   operation_id="write-once")
    assert exc.value.code == "idempotency_conflict"


def test_write_replay_resolves_event_owner_when_outer_project_is_missing(lab):
    author, _, _, event, _ = _notebook(lab)
    params = {"event_id": event, "expected_revision": 1, "changes": {"raw_text": "corrected"},
              "reason": "human correction"}
    first = lab.invoke(author, "memory.event.correct", params, operation_id="correct-once")
    assert first["project_id"] is None
    assert lab.invoke(author, "memory.event.correct", params, operation_id="correct-once")["idempotent_replay"]
    current = _revoke(lab, "author", "P1")  # P2 still grants the capability.
    with pytest.raises(capabilities.CapabilityError) as exc:
        lab.invoke(current, "memory.event.correct", params, operation_id="correct-once")
    assert exc.value.code == "forbidden_scope"
    assert lab.scalar("SELECT max(revision) FROM event_revisions WHERE entity_id=?", (event,)) == 2


@pytest.mark.parametrize("nested", [{"scope": ["P2"]}, {"project_id": "P2"}])
def test_legacy_write_replay_checks_nested_project_scope(lab, nested):
    author, _, _, event, _ = _notebook(lab)
    params = {"event_id": event, "expected_revision": 1, "changes": {"raw_text": "corrected"},
              "reason": "human correction"}
    receipt = lab.invoke(author, "memory.event.correct", params, operation_id="nested-write")
    # Older receipts may carry source scope only inside result, not the envelope.
    receipt["result"]["sources"] = [nested]
    lab.execute("UPDATE operation_results SET response_json=? WHERE operation_id='nested-write'",
                (json.dumps(receipt),))
    current = _revoke(lab, "author", "P2")
    with pytest.raises(capabilities.CapabilityError) as exc:
        lab.invoke(current, "memory.event.correct", params, operation_id="nested-write")
    assert exc.value.code == "forbidden_scope"


def test_write_replay_checks_nested_source_object_owner_without_declared_project(lab):
    author, _, _, event, _ = _notebook(lab)
    foreign = lab.add_page("P2")
    params = {"event_id": event, "expected_revision": 1, "changes": {"raw_text": "corrected"},
              "reason": "human correction"}
    receipt = lab.invoke(author, "memory.event.correct", params, operation_id="source-write")
    receipt["result"]["sources"] = [{"page_id": foreign["page_id"]}]
    lab.execute("UPDATE operation_results SET response_json=? WHERE operation_id='source-write'",
                (json.dumps(receipt),))
    current = _revoke(lab, "author", "P2")
    with pytest.raises(capabilities.CapabilityError) as exc:
        lab.invoke(current, "memory.event.correct", params, operation_id="source-write")
    assert exc.value.code == "forbidden_scope"


def test_heartbeat_with_repeated_operation_extends_lease_and_keeps_epoch_fencing(lab):
    owner = lab.context("owner", memberships={"P1": "owner"})
    conn = db.connect(lab.db_path)
    try:
        agents.load_registry(conn, str(Path(__file__).resolve().parents[2] / "runtime/agents"))
        agents.set_manifest_enabled(conn, agent_id="b_capture", version="1.0.0", enabled=True)
        dispatched = agents.dispatch(conn, intent="capture_page", context=owner, project_id="P1")
        run = agents.claim_run(conn, run_id=dispatched["run_id"], worker_id="worker")
        conn.commit()
    finally:
        conn.close()
    params = {"run_id": dispatched["run_id"], "claim_epoch": run["claim_epoch"], "lease_seconds": 120}
    first = lab.invoke(owner, "run.heartbeat", params, operation_id="heartbeat")
    lab.execute("UPDATE agent_runs SET lease_expires_at=datetime('now','+5 seconds') WHERE run_id=?",
                (params["run_id"],))
    second = lab.invoke(owner, "run.heartbeat", params, operation_id="heartbeat")
    assert second["idempotent_replay"] is False
    assert second["result"]["lease_expires_at"] >= first["result"]["lease_expires_at"]
    assert lab.scalar("SELECT count(*) FROM operation_results WHERE operation_id='heartbeat'") == 0
    with pytest.raises(capabilities.CapabilityError) as exc:
        lab.invoke(owner, "run.heartbeat", {**params, "claim_epoch": params["claim_epoch"] + 1},
                   operation_id="heartbeat")
    assert exc.value.code == "stale_worker"


def test_http_read_operation_id_is_optional_but_write_still_requires_it(lab, external_http):
    def post(capability, params):
        request = Request(f"{external_http}/v2/capabilities/{capability}/invoke",
            json.dumps({"params": params}).encode(),
            {"Authorization": "Bearer external-test-only", "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())
    status, receipt = post("query.samples.list", {"project_ids": ["P1"]})
    assert status == 200 and receipt["operation_id"].startswith("read_")
    status, error = post("memory.proposal.create", {"project_id": "P1"})
    assert status == 400 and error["error"]["code"] == "schema_violation"
