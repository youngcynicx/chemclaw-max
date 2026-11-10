from __future__ import annotations

import hmac
import json
import re
import tempfile
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import __version__
from . import agent_gateway, agents, auth, capabilities, evidence, ingest, mcp, ocr_port, tasks, transcribe
from .db import connect, init_db
from .gateway import (
    list_gateway_identities,
    resolve_gateway_identity,
    upsert_gateway_identity,
)
from .inventory import (
    list_inventory_events,
    process_inventory_message,
    resolve_inventory_event,
)
from .service_config import ServiceSettings


MAX_JSON_BODY_BYTES = 64 * 1024
# The unified task capability may carry several base64 research files.
MAX_CAPABILITY_BODY_BYTES = 64 * 1024 * 1024
MAX_WEBHOOK_BODY_BYTES = 5 * 1024 * 1024  # channel webhooks may carry base64 attachments
CONFIRM_PATH = re.compile(r"^/v1/inventory/events/([^/]+)/confirm$")
CAPABILITY_INVOKE_PATH = re.compile(r"^/v2/capabilities/([^/]+)/invoke$")
AGENT_TOOL_INVOKE_PATH = re.compile(r"^/v2/agent/tools/([^/]+)$")
MCP_PATH = "/v2/mcp"
CHANNEL_WEBHOOK_PATH = re.compile(r"^/v2/channels/([^/]+)/webhook$")


class ChemClawHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # Parallel agent reads open a burst of connections (HTTP/1.0 closes each
    # response). The inherited queue of five can refuse connections before a
    # handler runs when the accept thread is briefly busy. Queue the burst;
    # clients must not need to retry an operation whose outcome is uncertain.
    request_queue_size = 128

    def __init__(
        self,
        server_address: tuple[str, int],
        db_path: str | Path,
        *,
        api_key: str | None = None,
        service_settings: ServiceSettings | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.api_key = api_key
        self.service_settings = service_settings or ServiceSettings.from_env()
        super().__init__(server_address, ChemClawRequestHandler)


class ChemClawRequestHandler(BaseHTTPRequestHandler):
    server: ChemClawHTTPServer

    def do_GET(self) -> None:
        try:
            path = urlsplit(self.path)
            if path.path == "/health":
                self._handle_health()
                return
            if path.path == "/v2/capabilities":
                self._handle_capabilities_list()
                return
            if path.path == "/v2/agent/tools":
                self._handle_agent_tools_describe()
                return
            if path.path == "/v2/agent/status":
                self._handle_agent_runtime("status", {})
                return
            if path.path == "/v2/agent/roles":
                self._handle_agent_delegation("roles", {})
                return
            if path.path == "/v2/tasks":
                self._handle_tasks_list()
                return
            self._require_authorization()
            if path.path == "/v1/inventory/events":
                status = parse_qs(path.query).get("status", [None])[0]
                self._send_json(list_inventory_events(self.server.db_path, status=status))
                return
            if path.path == "/v1/inventory/chemicals":
                self._send_json(_list_chemicals(self.server.db_path))
                return
            if path.path == "/v1/identities":
                self._send_json(list_gateway_identities(self.server.db_path))
                return
            if path.path == "/v1/inventory/export.xlsx":
                self._handle_excel_export()
                return
            self._send_error(HTTPStatus.NOT_FOUND, "Route not found")
        except Exception as exc:
            self._handle_exception(exc)

    def do_POST(self) -> None:
        try:
            path = urlsplit(self.path).path
            if path == "/v2/tasks/interactions/answer":
                body = self._read_json_body()
                self._handle_interaction_answer(body)
                return
            # S10 platform webhook: authenticated by the channel account's
            # HMAC signature (never by our API key — platforms don't have it).
            match = CHANNEL_WEBHOOK_PATH.match(path)
            if match:
                self._handle_channel_webhook(match.group(1))
                return
            match = CAPABILITY_INVOKE_PATH.match(path)
            if match:
                body = self._read_json_body(max_bytes=MAX_CAPABILITY_BODY_BYTES)
                self._handle_capability_invoke(match.group(1), body)
                return
            match = AGENT_TOOL_INVOKE_PATH.match(path)
            if match:
                body = self._read_json_body()
                self._handle_agent_tool_invoke(match.group(1), body)
                return
            if path in ("/v2/agent/checkpoint", "/v2/agent/progress"):
                self._handle_agent_runtime(path.rsplit("/", 1)[1], self._read_json_body(max_bytes=2 * 1024 * 1024))
                return
            if path in ("/v2/agent/delegate", "/v2/agent/delegate/complete"):
                self._handle_agent_delegation("complete" if path.endswith("/complete") else "delegate", self._read_json_body())
                return
            if path == MCP_PATH:
                self._handle_mcp_request()
                return
            self._require_authorization()
            body = self._read_json_body()
            if path == "/v1/identities":
                result = upsert_gateway_identity(
                    self.server.db_path,
                    source=_required_text(body, "source"),
                    external_user_id=_required_text(body, "external_user_id"),
                    user_id=_required_text(body, "user_id"),
                    display_name=_optional_text(body, "display_name"),
                )
                self._send_json(result, status=HTTPStatus.CREATED)
                return
            if path == "/v1/inventory/messages":
                result = self._handle_message(body)
                self._send_json(result)
                return
            if path == "/v1/inventory/questions":
                from .qa import answer_inventory_question

                result = answer_inventory_question(
                    self.server.db_path,
                    _required_text(body, "question"),
                )
                self._send_json(result)
                return
            match = CONFIRM_PATH.match(path)
            if match:
                result = resolve_inventory_event(
                    self.server.db_path,
                    match.group(1),
                    approve=_optional_bool(body, "approve", default=True),
                    corrections=_optional_object(body, "corrections"),
                    confirmed_by=_optional_text(body, "confirmed_by")
                    or "gateway_user",
                )
                self._send_json(result)
                return
            self._send_error(HTTPStatus.NOT_FOUND, "Route not found")
        except Exception as exc:
            self._handle_exception(exc)

    def _handle_capabilities_list(self) -> None:
        context = self._principal_context()
        self._send_json(
            {
                "capability_version": capabilities.CAPABILITY_VERSION,
                "capabilities": capabilities.list_capabilities(context),
            }
        )

    def _handle_capability_invoke(self, capability_id: str, body: dict[str, Any]) -> None:
        context = self._principal_context()
        operation_id = (
            self.headers.get("X-Operation-Id", "").strip()
            or _optional_text(body, "operation_id")
            or ""
        )
        if not operation_id:
            spec = capabilities.CAPABILITY_REGISTRY.get(capability_id)
            if spec is not None and not spec.mutates:
                operation_id = f"read_{uuid.uuid4().hex}"
        if not operation_id:
            self._send_json(
                {
                    "error": {
                        "code": "schema_violation",
                        "message": "X-Operation-Id header (or body.operation_id) is required.",
                    }
                },
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        namespace = self.headers.get("X-Namespace", "").strip() or "default"
        params = body.get("params")
        if params is None:
            params = {k: v for k, v in body.items() if k != "operation_id"}
        if not isinstance(params, dict):
            self._send_json(
                {"error": {"code": "schema_violation", "message": "params must be an object."}},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        envelope = capabilities.invoke_capability(
            self.server.db_path,
            context,
            capability_id,
            params,
            namespace=namespace,
            operation_id=operation_id,
            services=self.server.service_settings,
        )
        self._send_json(envelope)

    def _handle_tasks_list(self) -> None:
        context = self._principal_context()
        conn = connect(self.server.db_path)
        try:
            rows = tasks.list_tasks_for_principal(
                conn,
                principal_id=context.principal_id,
                project_ids=auth.visible_project_ids(context),
            )
        finally:
            conn.close()
        self._send_json({"tasks": rows})

    # ---------- S06: agent tool gateway (run-token authenticated) ----------

    def _agent_run_token(self) -> str:
        authorization = self.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            token = self.headers.get("X-Agent-Run-Token", "").strip()
        return token

    def _resolve_agent_token_row(self) -> dict[str, Any]:
        token = self._agent_run_token()
        conn = connect(self.server.db_path)
        try:
            row = agents.resolve_run_token(conn, token)
            conn.commit()  # persist expired/exhausted marker transitions
            return row
        finally:
            conn.close()

    def _handle_agent_tools_describe(self) -> None:
        token_row = self._resolve_agent_token_row()
        conn = connect(self.server.db_path)
        try:
            tools = agent_gateway.describe_agent_tools(conn, token_row)
        finally:
            conn.close()
        self._send_json(
            {
                "agent_id": token_row["agent_id"],
                "manifest_version": token_row["manifest_version"],
                "task_id": token_row["task_id"],
                "run_id": token_row["run_id"],
                "budget_tool_calls": token_row["budget_tool_calls"],
                "used_tool_calls": token_row["used_tool_calls"],
                "tools": tools,
            }
        )

    def _handle_agent_runtime(self, action: str, body: dict[str, Any]) -> None:
        from . import agent_jobs
        conn = connect(self.server.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            token_row = agents.resolve_run_token(conn, self._agent_run_token(), allow_exhausted=True)
            result = agent_jobs.runtime_control(conn, token_row, action, body)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._send_json(result)

    def _handle_agent_delegation(self, action: str, body: dict[str, Any]) -> None:
        from . import orchestration
        conn = connect(self.server.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Finishing children remains possible after the final tool used
            # the budget. Exhaustion never authorizes additional execution.
            token_row = agents.resolve_run_token(conn, self._agent_run_token(), allow_exhausted=action == "complete")
            if action == "roles":
                result = {"roles": orchestration.roles(conn, token_row)}
            elif action == "delegate":
                result = orchestration.delegate(conn, token_row, body)
            else:
                result = orchestration.complete_child(conn, token_row, body)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._send_json(result)

    def _handle_agent_tool_invoke(self, capability_id: str, body: dict[str, Any]) -> None:
        token_row = self._resolve_agent_token_row()
        operation_id = (
            self.headers.get("X-Operation-Id", "").strip()
            or _optional_text(body, "operation_id")
            or ""
        )
        params = body.get("params")
        if params is None:
            params = {k: v for k, v in body.items() if k != "operation_id"}
        if not isinstance(params, dict):
            self._send_json(
                {"error": {"code": "schema_violation", "message": "params must be an object."}},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        conn = connect(self.server.db_path)
        try:
            fresh_token_row = agents.resolve_run_token(conn, self._agent_run_token())
            envelope = agent_gateway.invoke_agent_tool(
                str(self.server.db_path),
                conn,
                fresh_token_row,
                capability_id=capability_id,
                params=params,
                operation_id=operation_id or None,
                services=self.server.service_settings,
            )
            conn.commit()
        except agents.AgentHostError as exc:
            conn.rollback()
            self._send_json(
                {"error": {"code": exc.code, "message": exc.message}},
                status=HTTPStatus(exc.http_status),
            )
            return
        finally:
            conn.close()
        self._send_json(envelope)

    def _handle_interaction_answer(self, body: dict[str, Any]) -> None:
        context = self._principal_context()
        envelope = capabilities.invoke_capability(
            self.server.db_path,
            context,
            "task.interaction.answer",
            body,
            namespace="default",
            operation_id=_required_text(body, "operation_id"),
        )
        self._send_json(envelope)

    def _handle_mcp_request(self) -> None:
        context = self._principal_context()
        body = self._read_json_body()
        response = mcp.handle_mcp_request(self.server.db_path, context, body)
        if response is None:
            self._send_json({}, status=HTTPStatus.ACCEPTED)
        else:
            self._send_json(response)

    # ---------- S10: channel webhook (platform-signature authenticated) ----------

    def _handle_channel_webhook(self, channel_account_id: str) -> None:
        raw = self._read_raw_body(max_bytes=MAX_WEBHOOK_BODY_BYTES)
        from . import channels

        conn = connect(self.server.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            account = channels.get_channel_account(conn, channel_account_id)
            channels.verify_webhook_signature(
                account,
                body=raw,
                signature=self.headers.get("X-Channel-Signature"),
            )
            event = json.loads(raw.decode("utf-8"))
            if not isinstance(event, dict):
                raise ValueError("webhook body must be a JSON object")
            result = channels.ingest_channel_event(
                conn,
                channel_account_id=channel_account_id,
                event=event,
                storage_root=Path(self.server.service_settings.storage_root),
            )
            conn.commit()
        except channels.ChannelError as exc:
            conn.rollback()
            self._send_json(
                {"error": {"code": exc.code, "message": exc.message}},
                status=HTTPStatus(exc.http_status),
            )
            return
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._send_json(result)

    def _read_raw_body(self, *, max_bytes: int = MAX_JSON_BODY_BYTES) -> bytes:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if content_length <= 0 or content_length > max_bytes:
            raise ValueError(f"Body must be between 1 and {max_bytes} bytes")
        return self.rfile.read(content_length)

    def _principal_context(self) -> auth.PrincipalContext:
        authorization = self.headers.get("Authorization", "")
        secret = authorization.removeprefix("Bearer ").strip()
        conn = connect(self.server.db_path)
        try:
            return auth.authenticate_principal(conn, secret or None)
        finally:
            conn.close()

    def _handle_health(self) -> None:
        conn = connect(self.server.db_path)
        try:
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            conn.close()
        self._send_json(
            {
                "status": "ok" if integrity == "ok" else "degraded",
                "version": __version__,
                "database": integrity,
            }
        )

    def _handle_message(self, body: dict[str, Any]) -> dict[str, Any]:
        source = _required_text(body, "source")
        source_message_id = _required_text(body, "source_message_id")
        raw_text = _required_text(body, "raw_text")
        user_id = _optional_text(body, "user_id")
        external_user_id = _optional_text(body, "external_user_id")
        if external_user_id is not None:
            identity = resolve_gateway_identity(
                self.server.db_path,
                source=source,
                external_user_id=external_user_id,
            )
            if user_id is not None and user_id != identity["user_id"]:
                raise ValueError("user_id conflicts with mapped gateway identity")
            user_id = identity["user_id"]
        return process_inventory_message(
            self.server.db_path,
            raw_text,
            source=source,
            source_message_id=source_message_id,
            user_id=user_id,
            project_id=_optional_text(body, "project_id"),
            apply_approved=not _optional_bool(body, "dry_run", default=False),
        )

    def _handle_excel_export(self) -> None:
        from .excel import export_inventory_workbook

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "chemclaw_inventory.xlsx"
            export_inventory_workbook(self.server.db_path, output_path)
            content = output_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.send_header(
            "Content-Disposition",
            'attachment; filename="chemclaw_inventory.xlsx"',
        )
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _read_json_body(self, *, max_bytes: int = MAX_JSON_BODY_BYTES) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type.lower():
            raise ValueError("Content-Type must be application/json")
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if content_length <= 0 or content_length > max_bytes:
            raise ValueError(
                f"JSON body must be between 1 and {max_bytes} bytes"
            )
        raw = self.rfile.read(content_length)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _require_authorization(self) -> None:
        api_key = self.server.api_key
        if api_key is None:
            return
        authorization = self.headers.get("Authorization", "")
        bearer = authorization.removeprefix("Bearer ").strip()
        header_key = self.headers.get("X-API-Key", "")
        candidate = bearer or header_key
        if not candidate or not hmac.compare_digest(candidate, api_key):
            self._send_error(HTTPStatus.UNAUTHORIZED, "Invalid or missing API key")
            raise _ResponseAlreadySent

    def _send_json(
        self,
        payload: Any,
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _handle_exception(self, exc: Exception) -> None:
        if isinstance(exc, _ResponseAlreadySent):
            return
        if isinstance(exc, auth.AuthError):
            self._send_json(
                {"error": {"code": exc.code, "message": exc.message}},
                status=HTTPStatus(exc.http_status),
            )
            return
        if isinstance(
            exc,
            (
                capabilities.CapabilityError,
                tasks.TaskError,
                evidence.EvidenceError,
                ingest.IngestError,
                transcribe.TranscriptionError,
                ocr_port.TranscriberError,
            ),
        ):
            self._send_json(
                {"error": {"code": exc.code, "message": exc.message}},
                status=HTTPStatus(exc.http_status),
            )
            return
        if isinstance(exc, agents.AgentHostError):
            self._send_json(
                {"error": {"code": exc.code, "message": exc.message}},
                status=HTTPStatus(exc.http_status),
            )
            return
        if isinstance(exc, (ValueError, json.JSONDecodeError)):
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Internal server error")

    def log_message(self, format: str, *args: Any) -> None:
        return


class _ResponseAlreadySent(Exception):
    pass


def create_api_server(
    db_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    api_key: str | None = None,
    service_settings: ServiceSettings | None = None,
) -> ChemClawHTTPServer:
    normalized_api_key = api_key.strip() if api_key else None
    if not _is_loopback(host) and not normalized_api_key:
        raise ValueError("Remote binding requires an API key")
    init_db(db_path)
    return ChemClawHTTPServer(
        (host, port),
        db_path,
        api_key=normalized_api_key,
        service_settings=service_settings,
    )


def serve_api(
    db_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    api_key: str | None = None,
    service_settings: ServiceSettings | None = None,
) -> None:
    server = create_api_server(
        db_path, host=host, port=port, api_key=api_key, service_settings=service_settings
    )
    try:
        print(f"ChemClaw API listening on http://{host}:{server.server_port}")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _is_loopback(host: str) -> bool:
    return host.lower() in {"127.0.0.1", "::1", "localhost"}


def _required_text(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(payload: dict[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string when provided")
    return value.strip()


def _optional_object(payload: dict[str, Any], field: str) -> dict[str, Any]:
    value = payload.get(field)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _optional_bool(
    payload: dict[str, Any],
    field: str,
    *,
    default: bool,
) -> bool:
    value = payload.get(field, default)
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _list_chemicals(db_path: str | Path) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT chemical_id, name, formula, location, amount, unit,
                       threshold, hazard_level, max_auto_delta, updated_at
                FROM chemicals
                ORDER BY name
                """
            ).fetchall()
        ]
    finally:
        conn.close()
