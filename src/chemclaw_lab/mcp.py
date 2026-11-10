"""S09: MCP same-origin adapter (plan §15).

The MCP surface is a thin, homogeneous mapping over the SAME capability layer
used by HTTP/Web/agent tools:

- authorization comes from the authenticated ``PrincipalContext`` (never
  self-reported), and ``tools/list`` only exposes capabilities the principal
  may actually invoke;
- ``tools/call`` goes through ``capabilities.invoke_capability`` — identical
  schema validation, idempotency keyed by (namespace="mcp", operation_id),
  versioned envelopes with ``action_status`` and ``task_id``;
- nothing is implemented twice: an unimplemented capability stays unexposed
  and uncallable.

JSON-RPC 2.0 over HTTP POST (Streamable HTTP without SSE). A notification
yields no body (HTTP 202). Unsupported methods map to JSON-RPC -32601.
"""
from __future__ import annotations

import json
import uuid
from copy import deepcopy
from typing import Any

from . import auth, capabilities
from .agent_gateway import AGENT_TOOL_PARAM_SCHEMAS
from .read_contracts import CAPABILITY_OUTPUT_SCHEMA, READ_INPUT_SCHEMAS, READ_OUTPUT_SCHEMAS

PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "chemclaw-lab-mcp", "version": "1.0.0"}
MCP_NAMESPACE = "mcp"

_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_REQUEST = -32600


def _rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def list_mcp_tools(context: auth.PrincipalContext) -> list[dict[str, Any]]:
    """Only implemented AND authorized capabilities become MCP tools."""
    tools: list[dict[str, Any]] = []
    for item in capabilities.list_capabilities(context):
        schema = READ_INPUT_SCHEMAS.get(item["capability_id"], AGENT_TOOL_PARAM_SCHEMAS.get(item["capability_id"]))
        if schema is None:
            schema = {
                "type": "object",
                "properties": {
                    "operation_id": {
                        "type": "string",
                        "description": (
                            "Optional client-supplied idempotency key; retrying "
                            "with the same key and payload replays the stored "
                            "envelope instead of re-applying."
                        ),
                    },
                },
                "additionalProperties": True,
            }
        schema = deepcopy(schema)
        schema.setdefault("properties", {}).setdefault("operation_id", {
            "type": "string", "minLength": 1,
            "description": "Optional request identifier; for writes this is the idempotency key.",
        })
        tools.append(
            {
                "name": item["capability_id"],
                "description": item["summary"],
                "inputSchema": schema,
                "outputSchema": READ_OUTPUT_SCHEMAS.get(item["capability_id"], CAPABILITY_OUTPUT_SCHEMA),
                "annotations": {"readOnlyHint": not item["mutates"]},
            }
        )
    return tools


def _tool_call(db_path: str, context: auth.PrincipalContext, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str) or not name.strip():
        raise capabilities.CapabilityError(
            "schema_violation", "params.name must be a capability id."
        )
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise capabilities.CapabilityError(
            "schema_violation", "params.arguments must be an object."
        )
    operation_id = arguments.get("operation_id")
    if operation_id is not None and (
        not isinstance(operation_id, str) or not operation_id.strip()
    ):
        raise capabilities.CapabilityError(
            "schema_violation", "arguments.operation_id must be a non-empty string."
        )
    if operation_id is None:
        operation_id = f"mcp_{uuid.uuid4().hex}"
    envelope = capabilities.invoke_capability(
        db_path,
        context,
        name,
        arguments,
        namespace=MCP_NAMESPACE,
        operation_id=operation_id,
    )
    is_error = envelope.get("action_status") in ("failed", "rejected")
    return {
        "structuredContent": envelope,
        "content": [
            {
                "type": "text",
                "text": json.dumps(envelope, ensure_ascii=False, sort_keys=True),
            }
        ],
        "isError": is_error,
    }


def handle_mcp_request(
    db_path: str, context: auth.PrincipalContext, message: Any
) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC message. Returns None for notifications."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" \
            or not isinstance(message.get("method"), str):
        request_id = message.get("id") if isinstance(message, dict) else None
        return _rpc_error(request_id, _JSONRPC_INVALID_REQUEST, "not a JSON-RPC 2.0 message")
    method = message["method"]
    request_id = message.get("id")
    params = message.get("params", {})
    if not isinstance(params, dict):
        if request_id is None:
            return None
        return _rpc_error(request_id, _JSONRPC_INVALID_REQUEST, "params must be an object")

    try:
        if method == "initialize":
            return _rpc_result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                },
            )
        if method.startswith("notifications/"):
            return None
        if method == "tools/list":
            return _rpc_result(request_id, {"tools": list_mcp_tools(context)})
        if method == "tools/call":
            if request_id is None:
                return None
            return _rpc_result(request_id, _tool_call(db_path, context, params))
        if request_id is None:
            return None
        return _rpc_error(
            request_id, _JSONRPC_METHOD_NOT_FOUND, f"method {method!r} is not supported"
        )
    except capabilities.CapabilityError as exc:
        # A capability that is NOT IMPLEMENTED is a protocol-level error
        # (http 404); everything else — authorization (403), schema (400),
        # phase conflicts — keeps its stable code as a tool-level failure
        # (result.isError), preserving the /v2 envelope semantics.
        if exc.code == "forbidden_capability" and exc.http_status == 404:
            return _rpc_error(request_id, _JSONRPC_METHOD_NOT_FOUND, exc.message)
        arguments = params.get("arguments")
        supplied_id = arguments.get("operation_id") if isinstance(arguments, dict) else None
        failure = {
            "schema_version": "capability_envelope_v1",
            "capability_id": params.get("name") if isinstance(params.get("name"), str) and params["name"] else "unknown",
            "operation_id": supplied_id if isinstance(supplied_id, str) and supplied_id.strip() else f"mcp_{uuid.uuid4().hex}",
            "namespace": MCP_NAMESPACE, "action_status": "failed", "result": None,
            "error": {"code": exc.code, "message": exc.message},
        }
        return _rpc_result(request_id, {
            "structuredContent": failure,
            # Keep the two legacy text aliases for existing MCP consumers;
            # structuredContent follows the declared envelope schema.
            "content": [{"type": "text", "text": json.dumps(
                {**failure, "error_code": exc.code, "message": exc.message}, ensure_ascii=False)}],
            "isError": True,
        })
