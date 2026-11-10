"""Agent evidence views reduce repeated metadata without changing evidence."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from chemclaw_lab import agent_gateway, agents, db
from chemclaw_lab.validation import SchemaViolation, validate_payload_against_schema


@pytest.fixture()
def page_reader(lab):
    human = lab.context("hum_payload", memberships={"P1": "member", "P2": "member"})
    page = lab.add_page("P1")
    original = Path(lab.db_path).parent / "payload-original.txt"
    original.write_bytes(b"Temperature not measured\nPlan 120 C\nObserved white powder")
    transcript = lab.add_transcript(page["page_id"], ["Temperature not measured", "Plan 120 C", "Observed white powder"])
    conn = db.connect(lab.db_path)
    try:
        conn.execute("UPDATE evidence_objects SET mime='text/plain',storage_path=?,sha256=? WHERE evidence_id=(SELECT evidence_id FROM evidence_pages WHERE page_id=?)",
                     (str(original), hashlib.sha256(original.read_bytes()).hexdigest(), page["page_id"]))
        loaded = agents.load_registry(conn, Path(__file__).resolve().parents[2] / "runtime" / "agents")
        for row in loaded["loaded"]:
            agents.set_manifest_enabled(conn, agent_id=row["agent_id"], version=row["version"], enabled=True)
        started = agents.dispatch(conn, intent="capture", agent_id="b_capture", context=human, project_id="P1")
        agents.claim_run(conn, run_id=started["run_id"], worker_id="test-payload")
        for index, block_id in enumerate(transcript["block_ids"]):
            text = ("Temperature not measured", "Plan 120 C", "Observed white powder")[index]
            block = {"block_id": block_id, "reading_order": index, "block_kind": "handwriting", "text": text,
                     "quality_flags": ["geometry_untrusted", "needs_human"], "geometry": None, "coordinate_system": None,
                     "extractor": {"provider": "fixture", "config": {"diagnostic": "metadata " * 1000}}}
            conn.execute("UPDATE transcript_blocks SET block_kind='handwriting',block_json=? WHERE block_id=?", (json.dumps(block), block_id))
            conn.execute("INSERT INTO risk_fragments(fragment_id,run_id,block_id,kind,value_text,status) VALUES(?,?,?,?,?,'unknown')",
                         (f"risk_{index}", transcript["run_id"], block_id, "number", text))
        conn.commit()
    finally:
        conn.close()
    return lab, human, page, started


def read_page(fixture, view=None, page_id=None, operation_id=None):
    lab, _, page, started = fixture
    conn = db.connect(lab.db_path)
    try:
        params = {"page_id": page_id or page["page_id"]}
        if view is not None:
            params["view"] = view
        return agent_gateway.invoke_agent_tool(lab.db_path, conn, agents.resolve_run_token(conn, started["run_token"]),
            capability_id="evidence.page.read", params=params, operation_id=operation_id, services=lab.settings)
    finally:
        conn.close()


def test_default_reduces_payload_and_preserves_all_source_text(page_reader):
    full = read_page(page_reader, "full")
    compact = read_page(page_reader)
    old, new = full["result"]["page"], compact["result"]["page"]
    assert len(json.dumps(compact).encode()) < len(json.dumps(full).encode()) * 0.25
    assert new["view"] == "agent"
    for key in ("page_id", "document_id", "project_id", "evidence_id", "active_revision", "active_run_id"):
        assert new[key] == old[key]
    for key in ("active_revision", "active_run_id"):
        assert new["transcript"][key] == old["transcript"][key]
    # Full describes the selected transcription run; the compact legacy view
    # describes the activated page transcript. These statuses have different scopes.
    assert old["transcript"]["status"] == "succeeded"
    assert new["transcript"]["status"] == "transcribed"
    before, after = old["transcript"]["blocks"], new["transcript"]["blocks"]
    assert len(after) == len(before)
    for original, slim in zip(before, after):
        for key in ("block_id", "text", "reading_order", "block_kind", "quality_flags"):
            assert slim[key] == original[key]
        assert "extractor" not in slim and "geometry" not in slim and "positioning" not in slim
    assert new["transcript"]["source_ref"] == {"page_id": new["page_id"], "transcript_revision": old["active_revision"]}
    assert "risk_fragments" not in new["transcript"]
    summary = new["transcript"]["quality_summary"]
    assert summary["block_count"] == 3
    assert summary["flag_counts"] == {"geometry_untrusted": 3, "needs_human": 3}
    assert summary["risk_fragment_count"] == 3
    assert summary["risk_kind_counts"] == {"number": 3}
    assert summary["risk_status_counts"] == {"unknown": 3}
    assert summary["positioning_issues"] == [{"mode": "none", "reason": "block has no raster geometry", "block_ids": [b["block_id"] for b in before]}]


def test_full_and_legacy_human_views_preserve_source_despite_different_metadata(page_reader):
    lab, human, page, _ = page_reader
    human_page = lab.invoke(human, "evidence.page.read", {"page_id": page["page_id"]}, operation_id="human-original")["result"]["page"]
    agent_page = read_page(page_reader, "full")["result"]["page"]
    for key in ("page_id", "document_id", "project_id", "evidence_id", "mime", "sha256",
                "active_revision", "active_run_id"):
        assert agent_page[key] == human_page[key]
    assert agent_page["view"] == "full"
    assert "storage_path" not in agent_page
    assert agent_page["transcript"]["transcript_revision"] == human_page["active_revision"]
    assert agent_page["transcript"]["run_id"] == human_page["active_run_id"]
    assert agent_page["transcript"]["source_ref"] == {
        "page_id": page["page_id"], "transcript_revision": human_page["active_revision"]}
    full_blocks, human_blocks = agent_page["transcript"]["blocks"], human_page["transcript"]["blocks"]
    assert len(full_blocks) == len(human_blocks) == 3
    for full, legacy in zip(full_blocks, human_blocks):
        for key in ("block_id", "text", "reading_order", "block_kind", "quality_flags",
                    "geometry", "coordinate_system", "extractor"):
            assert key in full and full[key] == legacy[key]
    assert len(human_page["transcript"]["risk_fragments"]) == 3
    assert agent_page["transcript"]["quality_summary"]["block_count"] == 3


def test_view_reads_are_uncached_and_same_operation_reads_current_source(page_reader):
    compact = read_page(page_reader, operation_id="same-read")
    full = read_page(page_reader, "full", operation_id="same-read")
    assert compact["idempotent_replay"] is False
    assert full["idempotent_replay"] is False
    assert "extractor" in full["result"]["page"]["transcript"]["blocks"][0]
    assert "risk_fragments" not in compact["result"]["page"]["transcript"]
    lab, _, page, _ = page_reader
    lab.add_transcript(page["page_id"], ["New current source text"])
    fresh = read_page(page_reader, "full", operation_id="same-read")
    assert fresh["idempotent_replay"] is False
    assert fresh["result"]["page"]["transcript"]["transcript_revision"] == 2
    assert [block["text"] for block in fresh["result"]["page"]["transcript"]["blocks"]] == ["New current source text"]
    assert len(compact["result"]["page"]["transcript"]["blocks"]) == 3
    assert compact["result"]["page"]["transcript"]["source_ref"]["transcript_revision"] == 1
    assert lab.scalar("SELECT count(*) FROM operation_results") == 0


@pytest.mark.parametrize("view", ["agent", "full"])
def test_page_view_does_not_bypass_project_scope(page_reader, view):
    lab, _, _, _ = page_reader
    outside = lab.add_page("P2")
    with pytest.raises(agents.AgentHostError) as error:
        read_page(page_reader, view, page_id=outside["page_id"])
    assert error.value.code == "forbidden_scope"


def test_schema_advertises_optional_view_and_invalid_view_is_rejected(page_reader):
    schema = agent_gateway.AGENT_TOOL_PARAM_SCHEMAS["evidence.page.read"]
    assert schema["properties"]["view"]["default"] == "full"
    assert schema["properties"]["view"]["enum"] == ["agent", "full"]
    params = {"page_id": page_reader[2]["page_id"]}
    validate_payload_against_schema(params, schema)
    assert "view" not in params  # Schema validation never fills arguments.
    assert read_page(page_reader)["result"]["page"]["view"] == "agent"  # The gateway supplies its own default.
    with pytest.raises(agents.AgentHostError) as error:
        read_page(page_reader, "guess")
    assert error.value.code == "schema_violation"


def test_projection_is_pure_and_preserves_absent_transcript():
    original = {"warnings": ["unavailable"], "result": {"page": {"page_id": "p", "active_revision": 0, "transcript": None}}}
    before = copy.deepcopy(original)
    slim = agent_gateway._agent_page_view(original)
    assert original == before
    assert slim["result"]["page"]["transcript"] is None
    assert slim["warnings"] == original["warnings"]


def test_document_read_projects_every_page_without_dropping_entries():
    envelope = {"result": {"pages": [{"page_id": "p1", "transcript": {"blocks": []}}, {"page_id": "p2", "transcript": None}]}}
    projected = agent_gateway._agent_page_view(envelope)
    assert [p["page_id"] for p in projected["result"]["pages"]] == ["p1", "p2"]
    assert all(p["view"] == "agent" for p in projected["result"]["pages"])


def test_pi_fields_require_canonical_subjects_while_legacy_core_writes_remain_valid(page_reader):
    lab, human, page, _ = page_reader
    block = lab.rows("SELECT block_id FROM transcript_blocks ORDER BY reading_order")[0]["block_id"]
    item = {"item_key": "legacy", "event_kind": "observation", "raw_text": "Observed white powder",
        "fields": {"sample_label": {"normalized": "Legacy-A"}},
        "evidence_refs": [{"page_id": page["page_id"], "transcript_revision": 1, "block_id": block}]}
    schema = agent_gateway.AGENT_TOOL_PARAM_SCHEMAS["memory.proposal.create"]
    params = {"project_id": "P1", "page_id": page["page_id"], "transcript_revision": 1,
              "profile_id": "chemistry_notebook", "items": [item]}
    with pytest.raises(SchemaViolation):
        validate_payload_against_schema(params, schema)
    missing_fields = copy.deepcopy(params)
    del missing_fields["items"][0]["fields"]
    with pytest.raises(SchemaViolation):
        validate_payload_against_schema(missing_fields, schema)
    modern = copy.deepcopy(params)
    modern["items"][0]["fields"]["subject_sample_labels"] = ["Legacy-A"]
    validate_payload_against_schema(modern, schema)
    result = lab.invoke(human, "memory.proposal.create", {**params, "operation_id": "legacy-core"},
                        operation_id="legacy-core")
    event = result["result"]["proposal"]["items"][0]["current_event"]
    assert len(event["sample_ids"]) == 1
    assert event["fields"]["sample_label"]["normalized"] == "Legacy-A"
    assert "subject_sample_labels" not in event["fields"]
