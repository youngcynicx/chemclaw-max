"""Unified web task session: classify, route and return a real Core result."""

from __future__ import annotations

import base64
import json

import pytest

from chemclaw_lab import db
from chemclaw_lab.capabilities import CapabilityError


def _member(lab):
    return lab.context("hum_assistant", memberships={"P1": "member"})


def test_natural_language_query_routes_to_cited_answer(lab):
    lab.execute(
        "INSERT INTO samples (sample_id, project_id, label) VALUES ('smp_web', 'P1', 'S-1')"
    )
    page = lab.add_page("P1")
    transcript = lab.add_transcript(page["page_id"], ["实际取用 0.5 g S-1"])
    lab.add_event(
        project_id="P1", sample_id="smp_web", amount=0.5, unit="g",
        raw_text="实际取用 0.5 g S-1",
        evidence_refs=[{
            "page_id": page["page_id"], "transcript_revision": 1,
            "block_id": transcript["block_ids"][0],
        }],
    )
    result = lab.invoke(
        _member(lab),
        "assistant.task.process",
        {"project_id": "P1", "text": "S-1 实际取用多少？", "session_id": "sess-test"},
        operation_id="op-assistant-query",
    )
    payload = result["result"]
    assert payload["intent"] == "query_answer"
    assert payload["agent"] == "b_query"
    assert "0.5" in payload["text"]
    assert payload["data"]["status"] == "answered"
    assert result["action_status"] == "applied"
    assert lab.scalar(
        "SELECT COUNT(*) FROM typed_messages WHERE correlation_id = 'sess-test'"
    ) == 2


def test_inventory_command_routes_to_alice_and_respects_policy(lab):
    result = lab.invoke(
        _member(lab),
        "assistant.task.process",
        {"project_id": "P1", "text": "Used 5 g of Sodium chloride"},
        operation_id="op-assistant-inventory",
    )
    payload = result["result"]
    assert payload["intent"] == "record_inventory"
    assert payload["agent"] == "alice_inventory"
    assert result["action_status"] == "applied"
    assert lab.stock() == (95.0, 1)
    assert "Alice" in payload["text"]


def test_text_upload_saves_original_and_adopts_traceable_knowledge_without_inventory_approval(lab):
    data = "实际取用 0.5 g S-1".encode("utf-8")
    result = lab.invoke(
        _member(lab),
        "assistant.task.process",
        {
            "project_id": "P1",
            "text": "整理这份实验记录",
            "attachments": [{
                "name": "note.txt",
                "mime": "text/plain",
                "data_b64": base64.b64encode(data).decode("ascii"),
            }],
        },
        operation_id="op-assistant-upload",
    )
    payload = result["result"]
    assert payload["intent"] == "capture_page"
    assert payload["agent"] == "b_capture"
    assert len(payload["data"]["pages"]) == 1
    assert len(payload["data"]["proposals"]) == 1
    proposal = payload["data"]["proposals"][0]
    assert proposal["status"] == "applied" and proposal["acceptance_mode"] == "default"
    assert "资料默认采纳" in payload["text"]
    assert lab.scalar("SELECT COUNT(*) FROM evidence_objects") == 1
    assert lab.scalar("SELECT COUNT(*) FROM memory_proposals") == 1
    events = lab.rows("SELECT field_json,evidence_refs_json,provenance_json FROM event_revisions "
                      "WHERE entity_type='experiment_event'")
    assert len(events) == 1
    event = json.loads(events[0]["field_json"])
    refs = json.loads(events[0]["evidence_refs_json"])
    provenance = json.loads(events[0]["provenance_json"])
    assert event["raw_text"] == data.decode("utf-8")
    assert event["page_id"] == payload["data"]["pages"][0]["page_id"]
    assert refs and all(ref["page_id"] == event["page_id"] and ref["transcript_revision"] == 1
                        and ref["block_id"] for ref in refs)
    assert provenance["proposal_id"] == proposal["proposal_id"]
    assert provenance["acceptance"] == "default" and provenance["confirmed_by"] is None
    assert lab.scalar("SELECT COUNT(*) FROM event_confirmations") == 0
    assert lab.scalar("SELECT COUNT(*) FROM outbox WHERE message_type='inventory_proposal'") == 0
    assert lab.stock() == (100.0, 0)


def test_model_intent_override_is_validated_and_used(lab):
    result = lab.invoke(
        _member(lab),
        "assistant.task.process",
        {
            "project_id": "P1",
            "text": "Used 5 g of Sodium chloride",
            "intent_override": "query_answer",
        },
        operation_id="op-assistant-override",
    )
    assert result["result"]["intent"] == "query_answer"
    assert result["result"]["agent"] == "b_query"
    assert result["result"]["classification"] == "model"
    assert lab.stock() == (100.0, 0)

    with pytest.raises(CapabilityError) as excinfo:
        lab.invoke(
            _member(lab),
            "assistant.task.process",
            {"project_id": "P1", "text": "x", "intent_override": "delete_everything"},
            operation_id="op-assistant-bad-intent",
        )
    assert excinfo.value.code == "schema_violation"


def test_invalid_attachment_is_rejected_before_any_write(lab):
    with pytest.raises(CapabilityError) as excinfo:
        lab.invoke(
            _member(lab),
            "assistant.task.process",
            {
                "project_id": "P1",
                "text": "整理记录",
                "attachments": [{"name": "bad.txt", "mime": "text/plain",
                                 "data_b64": "@@@@"}],
            },
            operation_id="op-assistant-bad",
        )
    assert excinfo.value.code == "schema_violation"
    assert lab.scalar("SELECT COUNT(*) FROM evidence_objects") == 0


def test_ambiguous_text_returns_clarification_not_a_write(lab):
    result = lab.invoke(
        _member(lab),
        "assistant.task.process",
        {"project_id": "P1", "text": "帮我看看这个"},
        operation_id="op-assistant-clarify",
    )
    assert result["action_status"] == "clarification_needed"
    assert result["result"]["intent"] == "clarify"
    assert result["result"]["agent"] == "orchestrator"
    assert lab.scalar("SELECT COUNT(*) FROM inventory_transactions") == 0
