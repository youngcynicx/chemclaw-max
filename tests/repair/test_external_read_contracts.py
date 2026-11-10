"""Public read contracts agree across Core, Pi, MCP and the reference client."""
import json

import pytest

from chemclaw_lab import auth, capabilities, clients, db, mcp
from chemclaw_lab.agent_gateway import AGENT_TOOL_PARAM_SCHEMAS, _agent_page_view
from chemclaw_lab.read_contracts import (
    READ_INPUT_SCHEMAS, READ_OUTPUT_SCHEMAS, validate_read_params,
)
from chemclaw_lab.validation import SchemaViolation, validate_payload_against_schema


@pytest.mark.parametrize(("capability", "params"), [
    ("query.sample.context", {"project_id": "P", "sample_id": "S"}),
    ("query.sample.context", {"project_id": "P", "sample_label": "S", "include": [],
                              "snapshot": {"mode": "as_of", "knowledge_as_of": "2026-10-23T00:00:00Z"}}),
    ("query.samples.list", {}),
    ("query.samples.list", {"view": "catalog", "q": "S", "limit": 200, "offset": 0}),
    ("memory.event.read", {"event_id": "E", "revision": 2, "include_history": False}),
    ("memory.event.read", {"event_id": "E", "include_history": True}),
    ("evidence.page.read", {"document_id": "D", "view": "full"}),
    ("evidence.page.read", {"page_id": "P", "transcript_revision": 2, "block_ids": ["B"]}),
    ("query.answer.render", {"question": "What is known?", "project_ids": None}),
])
def test_supported_read_requests(capability, params):
    validate_read_params(capability, params)


@pytest.mark.parametrize(("capability", "params"), [
    ("query.sample.context", {"project_id": "P"}),
    ("query.sample.context", {"project_id": "P", "sample_id": "S", "sample_label": "S"}),
    ("query.sample.context", {"project_id": "P", "sample_id": "S", "cursor": "ignored"}),
    ("query.sample.context", {"project_id": "P", "sample_id": "S", "budget": {"max_bytes": 4095}}),
    ("query.sample.context", {"project_id": "P", "sample_id": "S", "snapshot": {"mode": "as_of"}}),
    ("query.sample.context", {"project_id": "P", "sample_id": "S", "snapshot": {"mode": "current", "knowledge_as_of": "2026-10-23"}}),
    ("query.sample.context", {"project_id": "P", "sample_id": "S", "include": ["events", "events"]}),
    ("query.sample.context", {"project_id": "P", "sample_id": "S", "include": ["provenance"]}),
    ("query.sample.context", {"project_id": "P", "sample_id": "S", "expected_fingerprint": "invalid"}),
    ("query.samples.list", {"q": "S"}),
    ("query.samples.list", {"limit": 5}),
    ("query.samples.list", {"view": "catalog", "limit": True}),
    ("query.samples.list", {"view": "catalog", "limit": 201}),
    ("memory.event.read", {"event_id": "E", "revision": 1, "include_history": True}),
    ("memory.event.read", {"event_id": "E", "revision": 1, "knowledge_as_of": "2026-10-23"}),
    ("memory.event.read", {"event_id": "E", "knowledge_as_of": "yesterday"}),
    ("evidence.page.read", {}),
    ("evidence.page.read", {"page_id": "P", "document_id": "D"}),
    ("evidence.page.read", {"document_id": "D", "block_ids": ["B"]}),
    ("evidence.page.read", {"page_id": "P", "block_ids": []}),
    ("evidence.page.read", {"page_id": "P", "block_ids": ["B", "B"]}),
    ("query.answer.render", {"answer": {}, "mode": "text"}),
    ("query.answer.render", {"question": " "}),
    ("query.sample.timeline", {"sample_id": "S", "operation_id": " "}),
])
def test_invalid_read_requests_are_not_silently_ignored(capability, params):
    with pytest.raises(SchemaViolation):
        validate_read_params(capability, params)


def test_pi_and_mcp_publish_shared_read_contracts():
    viewer = auth.PrincipalContext("viewer", "service", "External AI", memberships={"P": "viewer"})
    tools = {tool["name"]: tool for tool in mcp.list_mcp_tools(viewer)}
    for key, schema in READ_INPUT_SCHEMAS.items():
        assert AGENT_TOOL_PARAM_SCHEMAS[key] == schema
        if key in tools:
            assert tools[key]["inputSchema"] == schema
            assert tools[key]["outputSchema"] == READ_OUTPUT_SCHEMAS[key]
            assert tools[key]["annotations"]["readOnlyHint"] is True
    assert "question" in tools["query.answer.render"]["inputSchema"]["required"]


def test_mcp_structured_success_and_failure_follow_envelope_schema(tmp_path):
    path = tmp_path / "contracts.db"
    db.init_db(path)
    viewer = auth.PrincipalContext("viewer", "service", "External AI", memberships={"P": "viewer"})
    success = mcp.handle_mcp_request(str(path), viewer, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "query.samples.list", "arguments": {},
        },
    })["result"]
    assert success["isError"] is False
    assert success["structuredContent"] == json.loads(success["content"][0]["text"])
    validate_payload_against_schema(success["structuredContent"], READ_OUTPUT_SCHEMAS["query.samples.list"])
    failure = mcp.handle_mcp_request(str(path), viewer, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "query.samples.list", "arguments": {"unsupported": True},
        },
    })["result"]
    assert failure["isError"] is True
    assert failure["structuredContent"]["error"]["code"] == "schema_violation"
    validate_payload_against_schema(failure["structuredContent"], READ_OUTPUT_SCHEMAS["query.samples.list"])


def test_core_rejects_contract_errors_before_opening_database():
    viewer = auth.PrincipalContext("viewer", "service", "AI", memberships={"P": "viewer"})
    with pytest.raises(capabilities.CapabilityError) as error:
        capabilities.invoke_capability("missing.db", viewer, "query.answer.render",
                                       {"answer": {}}, operation_id="bad-contract")
    assert error.value.code == "schema_violation"


def test_client_reconnect_ids_are_unique_and_version_selectors_are_transmitted(monkeypatch):
    requests = []
    first = clients.ReadOnlyQueryClient("http://example.invalid", "test")
    second = clients.ReadOnlyQueryClient("http://example.invalid", "test")
    def capture(method, path, payload=None):
        requests.append((path, payload))
        return {"result": {}}
    monkeypatch.setattr(first, "_request", capture)
    monkeypatch.setattr(second, "_request", capture)
    first.search_events(text="one")
    second.search_events(text="two")
    first.read_event("E", revision=3)
    first.read_page("P", transcript_revision=2, block_ids=["B"])
    first.get_sample_context("P", sample_label="S", expected_fingerprint="f" * 64)
    first.list_samples(project_ids=["P"], q="S")
    ids = [payload["operation_id"] for _, payload in requests]
    assert len(set(ids)) == len(ids)
    assert requests[2][1]["params"]["revision"] == 3
    assert requests[3][1]["params"]["block_ids"] == ["B"]
    assert requests[4][1]["params"]["expected_fingerprint"] == "f" * 64
    assert requests[5][1]["params"]["view"] == "catalog"
    with pytest.raises(clients.ClientError):
        first.invoke("memory.proposal.apply", {})


def test_gateway_preserves_already_compacted_agent_page():
    envelope = {"result": {"page": {"page_id": "P", "view": "agent",
                                     "transcript": {"source_ref": {"transcript_revision": 2}}}}}
    assert _agent_page_view(envelope) == envelope


@pytest.mark.parametrize("arguments", [[], False, "", None])
def test_mcp_rejects_nonobject_arguments_instead_of_coercing_them(arguments):
    viewer = auth.PrincipalContext("viewer", "service", "AI", memberships={"P": "viewer"})
    result = mcp.handle_mcp_request("unused.db", viewer, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "query.samples.list", "arguments": arguments},
    })["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "schema_violation"
