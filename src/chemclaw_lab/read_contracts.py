"""Shared, zero-model read contracts for Core, HTTP, MCP and Pi.

These schemas describe transport-neutral inputs. Object authorization and
version resolution remain in Core; a selector must never be silently ignored.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import re
from typing import Any

from .validation import SchemaViolation, load_named_schema, validate_payload_against_schema

ReadContractError = SchemaViolation


def _string(*, nullable: bool = False) -> dict[str, Any]:
    return {"type": ["string", "null"] if nullable else "string", "minLength": 1}


def _object(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


_PROJECT_IDS = {"type": ["array", "null"], "items": _string(), "uniqueItems": True}
_AS_OF = {**_string(nullable=True), "description": "Knowledge cutoff as an ISO-8601 timestamp."}
_REVISION = {"type": ["integer", "null"], "minimum": 1}
CONTEXT_SECTIONS = ("current_state", "events", "cited_blocks", "provenance", "revision_index", "relations", "source_context")

READ_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "query.samples.list": _object({
        "project_ids": _PROJECT_IDS,
        "view": {"type": "string", "enum": ["expanded", "catalog"], "default": "expanded"},
        "q": {**_string(), "description": "Catalog label/alias query; requires view=catalog."},
        "sample_id": _string(),
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        "offset": {"type": "integer", "minimum": 0,
                   "description": "Catalog offset in sample ID order; pagination is not a fixed snapshot."},
    }),
    "query.sample.context": _object({
        "project_id": _string(), "sample_id": _string(), "sample_label": _string(),
        "expected_fingerprint": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$",
                                 "description": "Reject if the object-version fingerprint has changed since a preceding context read."},
        "include": {"type": "array", "items": {"type": "string", "enum": list(CONTEXT_SECTIONS)},
                    "uniqueItems": True, "default": list(CONTEXT_SECTIONS)},
        "snapshot": _object({
            "mode": {"type": "string", "enum": ["current", "as_of"]},
            "knowledge_as_of": _string(),
        }, ("mode",)),
        "budget": _object({"max_bytes": {"type": "integer", "minimum": 4096,
                                           "maximum": 1048576, "default": 64000,
                                           "description": "Budget for compact UTF-8 JSON result, excluding envelope."}}),
    }, ("project_id",)),
    "memory.event.read": _object({
        "event_id": _string(), "revision": _REVISION, "knowledge_as_of": _AS_OF,
        "include_history": {"type": "boolean", "default": False,
                            "description": "Full revision history; mutually exclusive with revision or knowledge_as_of."},
    }, ("event_id",)),
    "evidence.page.read": _object({
        "page_id": _string(), "document_id": _string(), "transcript_revision": _REVISION,
        "block_ids": {"type": "array", "items": _string(), "minItems": 1, "uniqueItems": True},
        "view": {"type": "string", "enum": ["agent", "full"], "default": "full",
                 "description": "Full preserves legacy page reads; request agent for compact text without local storage paths."},
    }),
    "query.events.search": _object({
        "project_ids": _PROJECT_IDS, "sample_label": _string(nullable=True),
        "run_id": _string(nullable=True), "date_from": _string(nullable=True),
        "date_to": _string(nullable=True), "text": _string(nullable=True),
        "event_kind": {"type": ["string", "null"], "enum": [None, "operation", "plan", "observation", "computed"]},
        "knowledge_as_of": _AS_OF,
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
    }),
    "query.sample.timeline": _object({"sample_id": _string(), "knowledge_as_of": _AS_OF}, ("sample_id",)),
    "query.answer.build": _object({"question": _string(), "project_ids": _PROJECT_IDS,
                                    "knowledge_as_of": _AS_OF}, ("question",)),
    "query.answer.render": _object({"question": _string(), "project_ids": _PROJECT_IDS,
                                     "knowledge_as_of": _AS_OF}, ("question",)),
    "query.report.read": _object({"report_id": _string()}, ("report_id",)),
    "memory.proposal.read": _object({"proposal_id": _string()}, ("proposal_id",)),
    "evidence.pages.list": _object({"project_ids": _PROJECT_IDS,
                                     "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                                     "offset": {"type": "integer", "minimum": 0,
                                                "description": "Offset in created_at/page_id descending order. Each call rechecks scope; pagination is not a fixed snapshot."}}),
    "memory.sample.search": _object({"project_id": _string(), "label": _string()}, ("project_id", "label")),
}

for _schema in READ_INPUT_SCHEMAS.values():
    _schema["properties"]["operation_id"] = {
        **_string(), "description": "Optional tracing identifier. Reads execute afresh and recheck current authorization."}

# JSON Schema clients see the same selector constraints enforced below. The
# repository's small validator does not implement oneOf/not, so validate them
# explicitly without adding a runtime dependency.
READ_INPUT_SCHEMAS["query.sample.context"]["oneOf"] = [
    {"required": ["sample_id"], "not": {"required": ["sample_label"]}},
    {"required": ["sample_label"], "not": {"required": ["sample_id"]}},
]
READ_INPUT_SCHEMAS["evidence.page.read"]["oneOf"] = [
    {"required": ["page_id"], "not": {"required": ["document_id"]}},
    {"required": ["document_id"], "not": {"required": ["page_id"]}},
]
READ_INPUT_SCHEMAS["query.sample.context"]["properties"]["snapshot"]["oneOf"] = [
    {"properties": {"mode": {"const": "current"}}, "not": {"required": ["knowledge_as_of"]}},
    {"properties": {"mode": {"const": "as_of"}}, "required": ["knowledge_as_of"]},
]
READ_INPUT_SCHEMAS["memory.event.read"]["allOf"] = [
    {"not": {"required": ["revision", "knowledge_as_of"],
             "properties": {"revision": {"type": "integer"}, "knowledge_as_of": {"type": "string"}}}},
    {"if": {"required": ["include_history"], "properties": {"include_history": {"const": True}}},
     "then": {"properties": {"revision": {"type": "null"}, "knowledge_as_of": {"type": "null"}}}},
]
READ_INPUT_SCHEMAS["evidence.page.read"]["allOf"] = [
    {"if": {"required": ["document_id"]},
     "then": {"not": {"anyOf": [{"required": ["transcript_revision"]}, {"required": ["block_ids"]}]}}},
]
READ_INPUT_SCHEMAS["query.samples.list"]["allOf"] = [
    {"if": {"required": [key]},
     "then": {"required": ["view"], "properties": {"view": {"const": "catalog"}}}}
    for key in ("q", "sample_id", "offset", "limit")
]

CAPABILITY_OUTPUT_SCHEMA = load_named_schema("capability_envelope_v1.json")
READ_OUTPUT_SCHEMAS = {key: deepcopy(CAPABILITY_OUTPUT_SCHEMA) for key in READ_INPUT_SCHEMAS}
READ_OUTPUT_SCHEMAS["evidence.pages.list"]["properties"]["result"] = {
    "type": ["object", "null"], "required": ["pages", "scope", "pagination"],
    "properties": {
        "scope": {"type": "array", "items": _string()},
        "pages": {"type": "array", "items": {
            "type": "object", "required": ["page_id", "document_id", "evidence_id", "source_state"],
            "properties": {
                "page_id": _string(), "document_id": _string(), "evidence_id": _string(),
                "source_state": {
                    "type": "object",
                    "description": "Persisted source/linkage state only; counts do not assert semantic coverage, task completion, or original file availability.",
                    "required": ["original_registered", "active_transcript_revision", "active_transcript_status", "proposal_count", "linked_current_event_count", "explicitly_linked_sample_count", "knowledge_complete"],
                    "properties": {
                        "original_registered": {"type": "boolean"},
                        "active_transcript_revision": {"type": ["integer", "null"], "minimum": 0},
                        "active_transcript_status": {"type": ["string", "null"]},
                        "proposal_count": {"type": "integer", "minimum": 0},
                        "linked_current_event_count": {"type": "integer", "minimum": 0,
                            "description": "Readable current event revisions linked by primary page ID or evidence refs; historical revisions are not counted."},
                        "explicitly_linked_sample_count": {"type": "integer", "minimum": 0,
                            "description": "Distinct stored sample IDs on the linked current events; no label or semantic inference."},
                        "knowledge_complete": {"type": "null"},
                    },
                },
            },
        }},
        "pagination": {"type": "object", "required": ["limit", "offset", "total", "has_more", "next_offset", "snapshot"],
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "offset": {"type": "integer", "minimum": 0},
                "total": {"type": "integer", "minimum": 0},
                "has_more": {"type": "boolean"},
                "next_offset": {"type": ["integer", "null"], "minimum": 0},
                "snapshot": {"type": "boolean", "enum": [False]},
            }},
    },
}
READ_OUTPUT_SCHEMAS["query.sample.context"]["properties"]["result"] = {
    "type": ["object", "null"],
    "required": ["schema_version", "status", "resolution", "sample", "snapshot", "events",
                 "evidence", "revision_index", "relations", "source_context", "current_state", "quality", "coverage"],
    "properties": {
        "schema_version": {"const": "sample_context_v1"},
        "status": {"enum": ["complete", "partial", "ambiguous", "not_found"]},
        "sample": {"type": ["object", "null"]},
        "resolution": {"type": "object", "required": ["status", "candidates"], "properties": {
            "status": {"enum": ["resolved", "ambiguous", "not_found"]},
            "candidates": {"type": "array", "items": {"type": "object"}},
        }},
        "snapshot": {"type": "object", "properties": {
            "mode": {"enum": ["current", "as_of"]},
            "fingerprint": {"type": ["string", "null"]},
            "identity_scope": {"const": "current"}, "source_status_scope": {"const": "current"},
        }},
        **{key: {"type": "array", "items": {"type": "object"}} for key in (
            "events", "evidence", "revision_index", "relations", "source_context", "current_state")},
        "quality": {"type": "object"},
        "coverage": {"type": "object", "required": ["retrieval_complete", "omitted", "totals", "returned"],
                     "properties": {"retrieval_complete": {"type": "boolean"},
                         "knowledge_complete": {"type": ["boolean", "null"]},
                         "omitted": {"type": "array", "items": {"type": "object"}},
                         "totals": {"type": "object"}, "returned": {"type": "object"},
                         "result_bytes": {"type": "integer", "minimum": 0}}},
    },
}


def _check_arrays_and_strings(value: Any, schema: dict[str, Any], path: str = "params") -> None:
    if isinstance(value, str) and not value.strip():
        raise SchemaViolation(f"{path} must not be blank.")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise SchemaViolation(f"{path} must contain at least {schema['minItems']} item(s).")
        if schema.get("uniqueItems") and len({repr(item) for item in value}) != len(value):
            raise SchemaViolation(f"{path} must contain unique items.")
        for index, item in enumerate(value):
            _check_arrays_and_strings(item, schema.get("items", {}), f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_arrays_and_strings(item, schema.get("properties", {}).get(key, {}), f"{path}.{key}")


def _timestamp(value: Any, field: str) -> None:
    if value is not None:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError) as exc:
            raise SchemaViolation(f"{field} must be an ISO-8601 timestamp.") from exc


def validate_read_params(capability_id: str, params: dict[str, Any]) -> None:
    """Reject unsupported fields/selectors; leave unknown capabilities to Core."""
    schema = READ_INPUT_SCHEMAS.get(capability_id)
    if schema is None:
        return
    validate_payload_against_schema(params, schema)
    _check_arrays_and_strings(params, schema)
    _timestamp(params.get("knowledge_as_of"), "params.knowledge_as_of")
    if capability_id == "memory.event.read":
        if sum((params.get("revision") is not None, params.get("knowledge_as_of") is not None,
                params.get("include_history") is True)) > 1:
            raise SchemaViolation("revision, knowledge_as_of and include_history=true are mutually exclusive.")
    elif capability_id == "query.sample.context":
        if ("sample_id" in params) == ("sample_label" in params):
            raise SchemaViolation("Exactly one of sample_id or sample_label is required.")
        snapshot = params.get("snapshot", {"mode": "current"})
        if (snapshot["mode"] == "as_of") != ("knowledge_as_of" in snapshot):
            raise SchemaViolation("snapshot.knowledge_as_of is required only for mode=as_of.")
        _timestamp(snapshot.get("knowledge_as_of"), "snapshot.knowledge_as_of")
        fingerprint = params.get("expected_fingerprint")
        if fingerprint is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint):
            raise SchemaViolation("expected_fingerprint must be 64 hexadecimal characters.")
        include = params.get("include", list(CONTEXT_SECTIONS))
        if "provenance" in include and not {"events", "source_context"}.intersection(include):
            raise SchemaViolation("provenance requires events or source_context in include.")
    elif capability_id == "evidence.page.read":
        if ("page_id" in params) == ("document_id" in params):
            raise SchemaViolation("Exactly one of page_id or document_id is required.")
        if "document_id" in params and any(key in params for key in ("transcript_revision", "block_ids")):
            raise SchemaViolation("transcript_revision and block_ids require page_id.")
    elif capability_id == "query.samples.list":
        if params.get("view", "expanded") != "catalog" and any(key in params for key in ("q", "sample_id", "offset", "limit")):
            raise SchemaViolation("q, sample_id, offset and limit require view=catalog.")
