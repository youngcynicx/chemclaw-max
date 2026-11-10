"""Uninterpreted sources remain discoverable without broadening read authority."""
import json

import pytest

from chemclaw_lab import auth, capabilities, clients, db, mcp
from chemclaw_lab.read_contracts import READ_INPUT_SCHEMAS, READ_OUTPUT_SCHEMAS
from chemclaw_lab.agent_gateway import AGENT_TOOL_PARAM_SCHEMAS
from chemclaw_lab.validation import validate_payload_against_schema


def _list(lab, context, **params):
    response = lab.invoke(context, "evidence.pages.list", params)
    validate_payload_against_schema(response, READ_OUTPUT_SCHEMAS["evidence.pages.list"])
    return response["result"]


def _snapshot(lab):
    conn = db.connect_read_only(lab.db_path)
    try:
        return {row[0]: sorted([tuple(r) for r in conn.execute(f'SELECT * FROM "{row[0]}"')], key=repr)
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_ocr_only_page_is_discoverable_and_readable_without_samples(lab):
    page = lab.add_page("P1", "pg_unlinked")
    lab.add_transcript(page["page_id"], ["Unprocessed source"])
    viewer = lab.context("reader", kind="service", memberships={"P1": "viewer"})
    before = _snapshot(lab)
    result = _list(lab, viewer, project_ids=["P1"])
    item = result["pages"][0]
    assert item["page_id"] == "pg_unlinked"
    assert item["document_id"] == page["document_id"]
    assert item["evidence_id"].startswith("evd_")
    assert item["source_state"] == {
        "original_registered": True, "active_transcript_revision": 1,
        "active_transcript_status": "succeeded", "proposal_count": 0,
        "linked_current_event_count": 0, "explicitly_linked_sample_count": 0,
        "knowledge_complete": None,
    }
    read = lab.invoke(viewer, "evidence.page.read", {"page_id": item["page_id"], "view": "agent"})
    assert "Unprocessed source" in json.dumps(read, ensure_ascii=False)
    assert "storage_path" not in json.dumps(result)
    assert before == _snapshot(lab)


def test_registered_without_ocr_does_not_claim_transcription_or_completeness(lab):
    page = lab.add_page("P1")
    lab.execute("UPDATE evidence_pages SET status='ingested' WHERE page_id=?", (page["page_id"],))
    viewer = lab.context("reader", memberships={"P1": "viewer"})
    item = _list(lab, viewer)["pages"][0]
    assert item["status"] == "ingested"
    assert item["source_state"]["original_registered"] is True
    assert item["source_state"]["active_transcript_revision"] is None
    assert item["source_state"]["active_transcript_status"] is None
    assert item["source_state"]["knowledge_complete"] is None


def test_scope_filters_every_page_and_rejects_explicit_outside_scope(lab):
    lab.add_page("P1", "pg_visible")
    lab.add_page("P2", "pg_private")
    viewer = lab.context("reader", memberships={"P1": "viewer"})
    assert [p["page_id"] for p in _list(lab, viewer)["pages"]] == ["pg_visible"]
    with pytest.raises(capabilities.CapabilityError) as error:
        _list(lab, viewer, project_ids=["P1", "P2"])
    assert error.value.code == "forbidden_scope"
    with pytest.raises(capabilities.CapabilityError):
        _list(lab, auth.PrincipalContext("none", "service", "none", {}))
    # Reusing a tracing ID must not replay a result after revocation.
    revoked = auth.PrincipalContext("reader", "service", "reader", {"P2": "viewer"})
    assert [p["page_id"] for p in _list(lab, revoked)["pages"]] == ["pg_private"]
    lab.execute("UPDATE evidence_objects SET project_id='P2' WHERE evidence_id=(SELECT evidence_id FROM evidence_pages WHERE page_id='pg_visible')")
    assert _list(lab, viewer)["pages"] == []


def test_pagination_is_unique_with_ties_and_multiple_previews(lab):
    for number in range(5):
        lab.add_page("P1", f"pg_{number}")
    lab.execute("UPDATE evidence_pages SET created_at='2026-01-01 00:00:00'")
    for number in range(2):
        lab.execute("INSERT INTO derived_artifacts (artifact_id,page_id,kind,storage_path,sha256) VALUES (?, 'pg_4', 'preview', '/private/path.png', 'hash')", (f"art_{number}",))
    viewer = lab.context("reader", memberships={"P1": "viewer"})
    result = _list(lab, viewer, limit=2)
    assert result["pagination"] == {"limit": 2, "offset": 0, "total": 5, "has_more": True, "next_offset": 2, "snapshot": False}
    assert result["pages"][0]["preview_artifact_id"] == "art_1"
    seen = []
    while True:
        seen.extend(p["page_id"] for p in result["pages"])
        if not result["pagination"]["has_more"]:
            break
        result = _list(lab, viewer, limit=2, offset=result["pagination"]["next_offset"])
    assert seen == ["pg_4", "pg_3", "pg_2", "pg_1", "pg_0"]
    assert _list(lab, viewer, offset=99)["pagination"]["next_offset"] is None


def test_link_counts_use_current_revision_and_authorized_explicit_links(lab):
    primary = lab.add_page("P1", "pg_primary")
    secondary = lab.add_page("P1", "pg_secondary")
    private = lab.add_page("P2", "pg_private")
    lab.execute("INSERT INTO samples(sample_id,project_id,label) VALUES ('s1','P1','S1'),('s2','P2','S2')")
    event = lab.add_event(project_id="P1", sample_id="s1", evidence_refs=[{"page_id": secondary["page_id"]}])
    # A visible source cited by a private event must not leak that event's existence.
    lab.add_event(project_id="P2", sample_id="s2", evidence_refs=[{"page_id": secondary["page_id"]}])
    viewer = lab.context("reader", memberships={"P1": "viewer"})
    items = {p["page_id"]: p for p in _list(lab, viewer)["pages"]}
    assert items[secondary["page_id"]]["source_state"]["linked_current_event_count"] == 1
    assert items[secondary["page_id"]]["source_state"]["explicitly_linked_sample_count"] == 1
    # A new event revision that removes its secondary citation removes this
    # page's current linkage, while retaining the historical row unchanged.
    lab.add_event(project_id="P1", sample_id="s1", event_id=event, revision=2)
    items = {p["page_id"]: p for p in _list(lab, viewer)["pages"]}
    assert items[secondary["page_id"]]["source_state"]["linked_current_event_count"] == 0
    assert items[primary["page_id"]]["source_state"]["linked_current_event_count"] == 1
    assert items[primary["page_id"]]["source_state"]["knowledge_complete"] is None
    assert lab.scalar("SELECT COUNT(*) FROM event_revisions WHERE entity_id=?", (event,)) == 2


def test_link_counts_skip_events_with_inaccessible_provenance_or_conflicting_owner(lab):
    page = lab.add_page("P1")
    private = lab.add_page("P2")
    lab.add_event(project_id="P1", provenance={"correction": {"page_id": private["page_id"]}})
    event = lab.add_event(project_id="P1")
    lab.execute("UPDATE event_revisions SET field_json=json_set(field_json,'$.project_id','P2') WHERE entity_id=?", (event,))
    viewer = lab.context("reader", memberships={"P1": "viewer"})
    state = _list(lab, viewer)["pages"][0]["source_state"]
    assert state["linked_current_event_count"] == 0
    assert state["knowledge_complete"] is None


@pytest.mark.parametrize("params", [
    {"limit": True}, {"limit": 0}, {"limit": 101}, {"offset": -1},
    {"offset": True}, {"offset": 1.2}, {"project_ids": [" "]},
    {"project_ids": ["P1", "P1"]}, {"cursor": "ignored"},
])
def test_bad_selectors_fail_before_database_open(params):
    viewer = auth.PrincipalContext("reader", "service", "reader", {"P1": "viewer"})
    with pytest.raises(capabilities.CapabilityError) as error:
        capabilities.invoke_capability("missing.db", viewer, "evidence.pages.list", params, operation_id="bad")
    assert error.value.code == "schema_violation"


def test_contract_mcp_and_read_only_client_expose_same_discovery(lab, monkeypatch):
    page = lab.add_page("P1")
    viewer = lab.context("reader", memberships={"P1": "viewer"})
    tool = next(t for t in mcp.list_mcp_tools(viewer) if t["name"] == "evidence.pages.list")
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["inputSchema"] == AGENT_TOOL_PARAM_SCHEMAS["evidence.pages.list"] == READ_INPUT_SCHEMAS["evidence.pages.list"]
    response = mcp.handle_mcp_request(lab.db_path, viewer, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "evidence.pages.list", "arguments": {"project_ids": ["P1"], "limit": 1, "offset": 0}}})
    assert response["result"]["structuredContent"]["result"]["pages"][0]["page_id"] == page["page_id"]
    client = clients.ReadOnlyQueryClient("http://example.invalid", "test")
    requests = []
    monkeypatch.setattr(client, "_request", lambda method, path, body: requests.append((path, body)) or {"result": {"pages": []}})
    assert client.list_pages(project_ids=["P1"], limit=10, offset=20) == {"pages": []}
    assert requests[0][1]["params"] == {"project_ids": ["P1"], "limit": 10, "offset": 20}
    with pytest.raises(clients.ClientError):
        client.invoke("evidence.page.ingest", {})
