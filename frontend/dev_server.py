"""Local ChemClaw frontend, scoped Core bridge and Pi runtime adapter (Python 3.10+).

Run: py -3.12 frontend/dev_server.py --port 4173
No Core package, API key, or project data is bundled into the static frontend.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import gzip
import ipaddress
import json
import math
import mimetypes
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_BODY = 8192
MAX_SESSION_BODY = 64 * 1024 * 1024
MAX_RESPONSE = 32 * 1024 * 1024
SESSION_TTL = 1800
COOKIE_NAME = "chemclaw_local_session"
WRITE_CAPABILITIES = frozenset({"assistant.task.process"})
READ_CAPABILITIES = frozenset({
    "inventory.query.list",
    "query.events.search",
    "query.samples.list",
    "query.sample.timeline",
    "query.answer.build",
    "memory.event.read",
    "memory.sample.search",
    "memory.proposal.read",
    "evidence.page.read", "evidence.pages.list",
    "evidence.page.original",
    "evidence.page.preview",
    "evidence.pages.list",
    "evidence.provider.list",
    "resource.list",
    "resource.booking.status",
})
# Base connection only needs the original pair; the rest are surfaced as
# feature flags so an older Core still connects in degraded read mode.
BASE_READ_CAPABILITIES = frozenset({"inventory.query.list", "query.events.search"})
OPTIONAL_READ_CAPABILITIES = READ_CAPABILITIES - BASE_READ_CAPABILITIES
# Windows registry associations may label .js as text/plain. Keep browser-critical
# assets deterministic because nosniff and ES modules require the correct MIME.
STATIC_MIME_TYPES = {
    ".html": "text/html", ".htm": "text/html",
    ".js": "text/javascript", ".mjs": "text/javascript",
    ".css": "text/css", ".svg": "image/svg+xml",
    ".json": "application/json", ".webmanifest": "application/manifest+json",
    ".ttf": "font/ttf", ".otf": "font/otf", ".woff": "font/woff", ".woff2": "font/woff2",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".avif": "image/avif", ".gif": "image/gif", ".ico": "image/x-icon",
}


class BridgeError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class TerminalRuntimeError(BridgeError):
    """A typed deterministic host refusal, never an uncertain transport loss."""
    retryable = False


_TERMINAL_RUNTIME_ERRORS = {
    "checkpoint_size_limit": 422,
    "checkpoint_invalid": 422,
    "runtime_request_size_limit": 413,
}


def _terminal_runtime_fallback(code, started, resume, *, status="failed"):
    # Reference authoritative persisted checkpoints instead of duplicating a
    # possibly oversized journal into another Core payload. No success inferred.
    checkpoints = resume.get("checkpoints", {}) if isinstance(resume, dict) else {}
    # Only bounded, generated Core IDs may survive a rejected host summary.
    # Never carry host text, references, model metadata or checkpoint content.
    root_id = started.get("run_id")

    def valid_id(value):
        return isinstance(value, str) and re.fullmatch(r"run_[0-9a-f]{12,64}", value) is not None

    run_ids = sorted(key for key in checkpoints if valid_id(key)) if isinstance(checkpoints, dict) else []
    run_ids = ([root_id] if valid_id(root_id) else []) + [key for key in run_ids if key != root_id]
    run_ids = run_ids[:16]
    return {"status": "partial" if status == "partial" else "failed", "text": "运行遇到不可重试的持久化或传输限制，已停止；已保存的写入和回执保留，未重新执行任务。",
        "error_code": code, "classification": "non_retryable_runtime_failure",
        "runtime_error": {"code": code, "retryable": False, "result_receipt_unavailable": True,
            "retained_checkpoint_run_ids": run_ids},
        "data": {"retained_checkpoints": {"run_ids": run_ids, "source": "Core assistant_runtime_checkpoints"}},
        "object_refs": [{"kind": "agent_run", "id": root_id}] if valid_id(root_id) else [],
        "warnings": [code]}


@dataclass(frozen=True)
class CoreConnection:
    endpoint: str
    api_key: str
    project_id: str


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    protocol: str = "openai-chat"
    vision_model: str | None = None
    reasoning_effort: str | None = None
    max_tokens: int = 16384
    context_window: int = 128000
    reasoning_format: str = "auto"
    vision_base_url: str | None = None
    vision_api_key: str | None = field(default=None, repr=False)
    vision_protocol: str = "openai-chat"
    vision_test: str = "not_tested"


@dataclass
class BrowserSession:
    csrf: str
    expires: float
    connection: CoreConnection | None = None
    model: ModelConfig | None = None
    generation: int = 0


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _native_glm53(base_url: str, model: str) -> bool:
    return urlsplit(base_url).hostname in {"open.bigmodel.cn", "api.z.ai"} and bool(
        re.fullmatch(r"glm-5\.3(?:-[a-z0-9][a-z0-9._-]*)?", model, re.IGNORECASE))


def validate_connection(body: dict, frontend_port: int) -> CoreConnection:
    if set(body) != {"endpoint", "apiKey", "projectId"}:
        raise BridgeError("invalid_config", "请填写 Core 地址、API key 和明确的项目 ID。")
    if not all(isinstance(v, str) for v in body.values()):
        raise BridgeError("invalid_config", "连接配置必须为文本。")
    endpoint, api_key, project_id = (body[k].strip() for k in ("endpoint", "apiKey", "projectId"))
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise BridgeError("invalid_endpoint", "Core 地址格式不正确。") from None
    if (parsed.scheme not in {"http", "https"} or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not 1 <= port <= 65535):
        raise BridgeError("invalid_endpoint", "只接受本机 127.0.0.1、localhost 或 [::1] 的 Core 根地址。")
    if port == frontend_port:
        raise BridgeError("invalid_endpoint", "Core 端口不能与前端端口相同。")
    if not api_key or len(api_key) > 1024 or any(ord(c) < 32 or ord(c) > 126 for c in api_key):
        raise BridgeError("invalid_key", "API key 为空或格式不正确。")
    if not project_id or len(project_id) > 128 or any(ord(c) < 32 for c in project_id):
        raise BridgeError("invalid_project", "请提供有效的项目 ID。")
    # Pin localhost rather than trust environment proxies or DNS resolution.
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    return CoreConnection(f"{parsed.scheme}://{host}:{port}", api_key, project_id)


class ModelError(Exception):
    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


MODEL_PROTOCOLS = {"openai-chat", "openai-responses"}
REASONING_EFFORTS = {"auto", "off", "minimal", "low", "medium", "high", "xhigh", "max"}
REASONING_FORMATS = {"auto", "none", "openai", "zai", "deepseek", "qwen", "openrouter"}


def _model_protocol(value: str) -> str:
    value = "openai-chat" if value == "openai-completions" else value
    if value not in MODEL_PROTOCOLS:
        raise BridgeError("invalid_model_config", "protocol 仅支持 openai-chat 或 openai-responses。")
    return value


def _normalize_model_base(value: str, protocol: str, frontend_port: int = 0) -> str:
    value = value.strip()
    try:
        parsed = urlsplit(value.strip().rstrip("/"))
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise BridgeError("invalid_model_config", "模型地址格式不正确。") from None
    if (parsed.scheme not in {"http", "https"} or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or not parsed.hostname or not 1 <= port <= 65535
            or any(ord(c) < 33 for c in value)):
        raise BridgeError("invalid_model_config", "模型地址必须是 http(s) 接口前缀，不得包含账号、查询、片段或空白。")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise BridgeError("invalid_model_config", "非本机模型必须使用 https。")
    if port == frontend_port:
        raise BridgeError("invalid_model_config", "模型端口不能与前端端口相同。")
    path = parsed.path.rstrip("/")
    for suffix, expected in (("/chat/completions", "openai-chat"), ("/responses", "openai-responses")):
        if path.endswith(suffix):
            if protocol != expected:
                raise BridgeError("invalid_model_config", "模型完整接口路径与所选 protocol 不一致。")
            path = path[:-len(suffix)]
            break
    host = "[::1]" if parsed.hostname == "::1" else parsed.hostname
    netloc = host if port == (443 if parsed.scheme == "https" else 80) else f"{host}:{port}"
    return f"{parsed.scheme}://{netloc}{path}"


def _reasoning_settings(config: ModelConfig, model: str | None = None) -> tuple[str, str | None]:
    effort, fmt = config.reasoning_effort or "auto", config.reasoning_format
    if effort not in REASONING_EFFORTS or fmt not in REASONING_FORMATS:
        raise BridgeError("invalid_model_config", "reasoningEffort / reasoningFormat 不受支持。")
    native = _native_glm53(config.base_url, model or config.model)
    if native:
        if config.protocol != "openai-chat" or fmt not in {"auto", "zai"}:
            raise BridgeError("invalid_model_config", "原生 GLM-5.3 需要 Chat 协议及 auto/zai 格式。")
        if effort not in {"auto", "low", "high", "max"}:
            raise BridgeError("invalid_model_config", "原生 GLM-5.3 仅支持 low/high/max，不能关闭思考。")
        return "zai", "low" if effort == "auto" else effort
    if config.protocol == "openai-responses" and fmt not in {"auto", "none", "openai"}:
        raise BridgeError("invalid_model_config", "Responses 协议仅支持 auto/none/openai 思考格式。")
    if effort == "auto":
        return fmt, None  # Provider defaults: never inject guessed reasoning controls.
    if fmt == "none":
        raise BridgeError("invalid_model_config", "none 格式不发送思考参数，请将 reasoningEffort 设为 auto。")
    if fmt == "auto":
        known_openai = (urlsplit(config.base_url).hostname == "api.openai.com"
                        and bool(re.match(r"(?:o[134](?:-|$)|gpt-5(?:[.-]|$))", model or config.model)))
        if config.protocol == "openai-responses" or known_openai:
            fmt = "openai"
        else:
            raise BridgeError("invalid_model_config", "当前模型无自动思考强度映射；请选择明确 reasoningFormat 或使用 auto 强度。")
    if fmt == "qwen" and effort != "off":
        raise BridgeError("invalid_model_config", "qwen 格式仅映射思考开关，不支持强度；请使用 auto 或 off。")
    return fmt, effort


def validate_model_config(body: dict, frontend_port: int, previous: ModelConfig | None = None) -> ModelConfig:
    allowed = {"baseUrl", "model", "apiKey", "protocol", "visionModel", "visionBaseUrl", "visionApiKey",
               "visionProtocol", "reasoningEffort", "reasoningFormat", "maxTokens", "contextWindow",
               "reuseApiKey", "reuseVisionApiKey"}
    if set(body) - allowed or not {"baseUrl", "model", "apiKey"} <= set(body):
        raise BridgeError("invalid_model_config", "请提供 baseUrl/model/apiKey；模型配置包含未支持或缺少的字段。")
    for key, value in body.items():
        expected = bool if key in {"reuseApiKey", "reuseVisionApiKey"} else int if key in {"maxTokens", "contextWindow"} else str
        if type(value) is not expected:
            raise BridgeError("invalid_model_config", "模型配置字段类型不正确。")
    protocol = _model_protocol(body.get("protocol", "openai-chat"))
    if body.get("visionProtocol", "openai-chat") not in {"openai-chat", "openai-completions"}:
        raise BridgeError("invalid_model_config", "视觉端点目前仅支持 openai-chat 协议。")
    base = _normalize_model_base(body["baseUrl"], protocol, frontend_port)
    model, vision_model = body["model"].strip(), body.get("visionModel", "").strip() or None
    for name in (model, vision_model):
        if name is not None and (not name or len(name) > 128 or any(ord(c) < 32 for c in name)):
            raise BridgeError("invalid_model_config", "模型名称为空或格式不正确。")
    explicit_vision_base = body.get("visionBaseUrl", "").strip()
    vision_base = _normalize_model_base(explicit_vision_base, "openai-chat", frontend_port) if explicit_vision_base else None
    effective_vision_base = vision_base or base
    key = body["apiKey"].strip()
    if not key and body.get("reuseApiKey", True) and previous is not None:
        if _normalize_model_base(previous.base_url, _model_protocol(previous.protocol)) == base:
            key = previous.api_key
    vision_key = body.get("visionApiKey", "").strip()
    inherited_vision_key = False
    if not vision_key and body.get("reuseVisionApiKey", True):
        if previous is not None and _normalize_model_base(previous.vision_base_url or previous.base_url, "openai-chat" if previous.vision_base_url else _model_protocol(previous.protocol)) == effective_vision_base:
            if previous.vision_api_key is not None:
                vision_key = previous.vision_api_key
            elif effective_vision_base == base:
                vision_key, inherited_vision_key = key, True
            else:
                vision_key = previous.api_key
        elif effective_vision_base == base:
            vision_key, inherited_vision_key = key, True
    for endpoint, secret in ((base, key), (effective_vision_base, vision_key)):
        if ((not secret and urlsplit(endpoint).hostname not in {"localhost", "127.0.0.1", "::1"})
                or len(secret) > 2048 or any(ord(c) < 32 or ord(c) > 126 for c in secret)):
            raise BridgeError("invalid_model_config", "远程端点需要 API key；留空只复用同端点已配置密钥，本机端点可免密钥。")
    max_tokens, context_window = body.get("maxTokens", 16384), body.get("contextWindow", 128000)
    if (not 256 <= max_tokens <= 131072 or not 4096 <= context_window <= 2000000
            or max_tokens > context_window - 2048):
        raise BridgeError("invalid_model_config", "模型输出/上下文预算不正确，或未保留足够的输入空间。")
    effort = body.get("reasoningEffort", "auto")
    config = ModelConfig(base, model, key, protocol=protocol, vision_model=vision_model,
        reasoning_effort=None if effort == "auto" else effort, reasoning_format=body.get("reasoningFormat", "auto"),
        max_tokens=max_tokens, context_window=context_window, vision_base_url=vision_base,
        vision_api_key=None if inherited_vision_key else vision_key, vision_protocol="openai-chat")
    _reasoning_settings(config)
    return config


def public_model_config(config: ModelConfig | None) -> dict:
    if config is None:
        return {"configured": False, "model": None, "baseUrl": None, "visionModel": None,
                "visionBaseUrl": None, "protocol": "openai-chat", "visionProtocol": "openai-chat",
                "reasoningEffort": "auto", "reasoningFormat": "auto", "maxTokens": 16384,
                "contextWindow": 128000, "keyConfigured": False, "visionKeyConfigured": False,
                "visionTest": "not_configured"}
    effective_format, effective_effort = _reasoning_settings(config)
    return {"configured": True, "model": config.model, "baseUrl": config.base_url,
            "protocol": _model_protocol(config.protocol), "reasoningEffort": config.reasoning_effort or "auto",
            "reasoningFormat": config.reasoning_format, "maxTokens": config.max_tokens,
            "effectiveReasoningFormat": effective_format,
            "effectiveReasoningEffort": effective_effort or "provider_default",
            "contextWindow": config.context_window, "visionModel": config.vision_model,
            "visionBaseUrl": config.vision_base_url, "visionProtocol": "openai-chat",
            "visionTest": config.vision_test if config.vision_model or config.vision_base_url else "not_configured",
            "keyConfigured": bool(config.api_key), "visionKeyConfigured": bool(
                config.vision_api_key if config.vision_api_key is not None else config.api_key)}


def _vision_config(config: ModelConfig) -> ModelConfig:
    vision = replace(config, base_url=config.vision_base_url or config.base_url,
                   model=config.vision_model or config.model,
                   api_key=config.vision_api_key if config.vision_api_key is not None else config.api_key,
                   protocol="openai-chat", reasoning_effort=None, reasoning_format="auto")
    if (_native_glm53(config.base_url, config.model) and _native_glm53(vision.base_url, vision.model)
            and config.model.casefold() == vision.model.casefold()
            and _normalize_model_base(config.base_url, _model_protocol(config.protocol))
                == _normalize_model_base(vision.base_url, "openai-chat")):
        vision = replace(vision, reasoning_effort=config.reasoning_effort, reasoning_format=config.reasoning_format)
    return vision


def _vision_output_budget(config: ModelConfig) -> int:
    return config.max_tokens if _native_glm53(config.vision_base_url or config.base_url,
        config.vision_model or config.model) else 1024


def _model_endpoint(base_url: str, protocol: str = "openai-chat") -> str:
    return _normalize_model_base(base_url, protocol) + ("/responses" if protocol == "openai-responses" else "/chat/completions")


def _reasoning_payload(config: ModelConfig, model: str | None = None) -> dict:
    fmt, effort = _reasoning_settings(config, model)
    if effort is None:
        return {}
    value = "none" if effort == "off" else effort
    if config.protocol == "openai-responses":
        return {"reasoning": {"effort": value}}
    if fmt == "openai":
        return {"reasoning_effort": value}
    if fmt == "openrouter":
        return {"reasoning": {"effort": value}}
    if fmt == "qwen":
        return {"enable_thinking": False}
    return {"thinking": {"type": "disabled" if effort == "off" else "enabled"},
            **({"reasoning_effort": value} if effort != "off" else {})}


_PROVIDER_AUDIT_LOCK = threading.Lock()
_PROVIDER_AUDIT_FAILURES = 0


def _provider_number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def _provider_usage(raw):
    usage = raw if isinstance(raw, dict) else {}
    def detail(*names):
        for name in names:
            value = usage.get(name)
            if isinstance(value, dict):
                return value
        return {}
    return {
        "input_tokens": _provider_number(usage.get("prompt_tokens", usage.get("input_tokens"))),
        "output_tokens": _provider_number(usage.get("completion_tokens", usage.get("output_tokens"))),
        "total_tokens": _provider_number(usage.get("total_tokens")),
        "cache_read_tokens": _provider_number(detail("prompt_tokens_details", "input_tokens_details").get("cached_tokens", usage.get("cached_tokens"))),
        "cache_write_tokens": _provider_number(usage.get("cache_creation_input_tokens")),
        "reasoning_tokens": _provider_number(detail("completion_tokens_details", "output_tokens_details").get("reasoning_tokens")),
    }


def _provider_error_code(payload):
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    code = str(error.get("code", "")) if isinstance(error, dict) else ""
    return code if code in {"1113", "insufficient_quota", "rate_limit_exceeded", "invalid_api_key"} else None


def _persist_provider_observation(record):
    """Append only the constructed allowlist. Never serialize request/response objects."""
    global _PROVIDER_AUDIT_FAILURES
    path = Path(os.environ.get("CHEMCLAW_PROVIDER_AUDIT_PATH") or (
        Path(__file__).resolve().parents[1] / "work" / "provider-observability" / "provider-requests.jsonl"))
    with _PROVIDER_AUDIT_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as audit:
                audit.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                audit.flush()
        except (OSError, ValueError, TypeError):
            # Observability failures must not change or replay a paid request.
            _PROVIDER_AUDIT_FAILURES += 1


def call_model(config: ModelConfig, messages: list[dict], *, timeout: int = 90,
               max_tokens: int | None = None, model: str | None = None, vision: bool = False,
               audit_context: dict | None = None) -> str:
    """One row per actual urllib request; this path has no automatic HTTP retry."""
    context = audit_context or {}
    def identifier(name):
        value = context.get(name)
        return value if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,192}", value) else None
    request_id = str(uuid.uuid4())
    role = context.get("agent_id")
    role = role if role in {"model_probe", "vision_probe", "vision_ocr"} else "vision_ocr" if vision else "model_probe"
    record = {"schema_version": 1, "request_id": request_id, "call_id": request_id, "phase": "started",
        "run_id": identifier("run_id"), "agent_id": role,
        "page_id": identifier("page_id"), "model": model or (config.vision_model or config.model if vision else config.model),
        "retry_index": 0, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_ms": None, "http_status": None, "stop_reason": None,
        "error_class": None, "provider_error_code": None, "usage": _provider_usage(None),
        "cost": None, "cost_currency": None}
    started = time.monotonic()
    try:
        return _call_model(config, messages, timeout=timeout, max_tokens=max_tokens,
            model=model, vision=vision, observation=record)
    except ModelError as exc:
        record["error_class"] = exc.code
        exc.provider_observation = record
        raise
    finally:
        # A validation failure before constructing the transport is not a call.
        if record.pop("_request_started", False):
            record["phase"] = "finished"
            record["duration_ms"] = max(0, round((time.monotonic() - started) * 1000, 3))
            _persist_provider_observation(record)
            if isinstance(context.get("records"), list):
                context["records"].append(dict(record))


def _provider_rows(*groups):
    return list({row["request_id"]: row for group in groups if isinstance(group, list)
        for row in group if isinstance(row, dict) and row.get("request_id")}.values())


def _provider_summary(rows):
    intents = _provider_rows(rows)
    rows = [row for row in intents if row.get("phase") != "not_sent"]
    def total(field):
        values = [row.get("usage", {}).get(field) for row in rows]
        return sum(values) if values and all(_provider_number(value) is not None for value in values) else None
    currencies = {row.get("cost_currency") for row in rows}
    costs = [row.get("cost") for row in rows]
    cost_known = bool(rows) and len(currencies) == 1 and None not in currencies and all(_provider_number(value) is not None for value in costs)
    return {"request_count": len(rows), "retry_count": sum(row.get("retry_index", 0) > 0 for row in rows),
        "request_intent_count": len(intents), "unfinished_requests": sum(row.get("phase") == "started" for row in rows),
        "not_sent_requests": sum(row.get("phase") == "not_sent" for row in intents),
        "usage": {field: total(field) for field in _provider_usage(None)},
        "usage_observed_requests": sum(any(value is not None for value in row.get("usage", {}).values()) for row in rows),
        "quota_exhausted_requests": sum(row.get("error_class") == "provider_quota_exhausted" for row in rows),
        "cost": sum(costs) if cost_known else None, "cost_currency": next(iter(currencies)) if cost_known else None}


def _call_model(config: ModelConfig, messages: list[dict], *, timeout: int = 90,
               max_tokens: int | None = None, model: str | None = None, vision: bool = False,
               observation: dict) -> str:
    """Probe and OCR use the same normalized protocol and reasoning policy as Pi."""
    credentials = tuple(x for x in (config.api_key, config.vision_api_key) if x)
    if vision:
        config = _vision_config(config)
    config = replace(config, protocol=_model_protocol(config.protocol))
    reasoning = _reasoning_payload(config, model)
    limit = max_tokens if max_tokens is not None else config.max_tokens
    if config.protocol == "openai-responses":
        request_body = {"model": model or config.model, "input": messages,
                        "max_output_tokens": limit, "stream": False, **reasoning}
    else:
        # Generic Chat endpoints usually implement max_tokens; explicit OpenAI
        # reasoning models use max_completion_tokens. Pi uses the same rule.
        openai_tokens = (urlsplit(config.base_url).hostname == "api.openai.com"
                         and bool(re.match(r"(?:o[134](?:-|$)|gpt-5(?:[.-]|$))", model or config.model)))
        request_body = {"model": model or config.model, "messages": messages, "stream": False,
                        "max_completion_tokens" if openai_tokens else "max_tokens": limit, **reasoning}
    request = Request(_model_endpoint(config.base_url, config.protocol),
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 **({"Authorization": f"Bearer {config.api_key}"} if config.api_key else {})}, method="POST")
    def clean(value):
        text = str(value)
        for secret in credentials:
            text = text.replace(secret, "[redacted]")
        return text[:2000]
    try:
        _persist_provider_observation(observation)
        observation["_request_started"] = True
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            observation["http_status"] = status if type(status) is int and 100 <= status <= 599 else None
            raw = response.read(4 * 1024 * 1024 + 1)
    except HTTPError as exc:
        observation["http_status"] = exc.code
        observation["stop_reason"] = "http_error"
        try:
            detail = json.loads(exc.read(65536).decode("utf-8"))
            observation["provider_error_code"] = _provider_error_code(detail)
            error = detail.get("error", {})
            message = error.get("message", "") if isinstance(error, dict) else ""
        except Exception:
            message = ""
        code = ("provider_quota_exhausted" if observation["provider_error_code"] in {"1113", "insufficient_quota"}
            else "provider_rate_limited" if exc.code == 429
            else "model_unauthorized" if exc.code in (401, 403) else "model_rejected")
        raise ModelError(code,
            f"模型服务返回 HTTP {exc.code}（{config.protocol}）。" + (f" 详情：{clean(message)}" if message else "")) from None
    except (URLError, TimeoutError, OSError) as exc:
        raise ModelError("model_unreachable", "无法连接模型服务：" + clean(exc)) from None
    if len(raw) > 4 * 1024 * 1024:
        raise ModelError("model_response_too_large", "模型响应过大。")
    try:
        payload = json.loads(raw.decode("utf-8"))
        observation["usage"] = _provider_usage(payload.get("usage"))
        usage = payload.get("usage")
        if isinstance(usage, dict):
            observation["cost"] = _provider_number(usage.get("cost"))
            if isinstance(usage.get("cost_currency"), str) and re.fullmatch(r"[A-Z]{3}", usage["cost_currency"]):
                observation["cost_currency"] = usage["cost_currency"]
        observation["provider_error_code"] = _provider_error_code(payload)
        if observation["provider_error_code"] in {"1113", "insufficient_quota"}:
            raise ModelError("provider_quota_exhausted", "模型服务额度不足；已保留原件和已完成步骤，请恢复额度后继续。")
        if config.protocol == "openai-responses":
            observation["stop_reason"] = payload.get("status") if payload.get("status") in {"completed", "incomplete", "failed", "cancelled"} else None
            if payload.get("status") == "incomplete":
                raise ModelError("model_output_truncated", "Responses 输出未完成；请核对输出预算/协议。")
            if payload.get("error") or payload.get("status") in {"failed", "cancelled"}:
                raise ModelError("model_rejected", "Responses 模型未成功完成请求。")
            content = "".join(part.get("text", "") for item in payload["output"]
                if item.get("type") == "message" and item.get("role", "assistant") == "assistant"
                for part in item.get("content", []) if part.get("type") == "output_text")
        else:
            choice = payload["choices"][0]
            observation["stop_reason"] = choice.get("finish_reason") if choice.get("finish_reason") in {"stop", "length", "tool_calls", "function_call", "content_filter"} else None
            if choice.get("finish_reason") == "length":
                raise ModelError("model_output_truncated", "模型输出触及长度上限，未保存截断内容；请提高输出预算后重试。")
            content = choice["message"]["content"]
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    except ModelError:
        raise
    except Exception:
        raise ModelError("model_invalid_response", f"模型返回格式与所选 {config.protocol} 协议不兼容。") from None
    if not isinstance(content, str) or not content.strip():
        raise ModelError("model_empty_response", "模型未返回正文文本。")
    return content.strip()


def probe_model(config: ModelConfig) -> dict:
    call_model(config, [{"role": "user", "content": "只回复 OK"}], timeout=30)
    visual = bool(config.vision_model or config.vision_base_url)
    if visual:
        image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKElEQVR4nO3NsQ0AAAzCMP5/un0CNkuZ41wybXsHAAAAAAAAAAAAxR4yw/wuPL6QkAAAAABJRU5ErkJggg=="
        call_model(config, [{"role": "user", "content": [
            {"type": "text", "text": "识别图片的主要颜色，只回复颜色。"},
            {"type": "image_url", "image_url": {"url": image}}]}], timeout=30, vision=True, max_tokens=_vision_output_budget(config),
            audit_context={"agent_id": "vision_probe"})
    return {"test": "ok", "visionTest": "ok" if visual else "not_configured"}


def core_request(connection: CoreConnection, path: str, *, body: dict | None = None,
                 operation_id: str | None = None, namespace: str = "frontend_read_bridge") -> dict:
    # Routes are constants owned by this module, never accepted from the browser.
    headers = {"Accept": "application/json", "Authorization": f"Bearer {connection.api_key}"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["X-Operation-Id"] = operation_id or "frontend_read_" + secrets.token_hex(20)
        headers["X-Namespace"] = namespace
    request = Request(connection.endpoint + path, data=data, headers=headers,
                      method="POST" if body is not None else "GET")
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=8) as response:
            if response.status != 200:
                raise BridgeError("core_rejected", "Core 拒绝了读取请求。", 502)
            raw = response.read(MAX_RESPONSE + 1)
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        detail = (
            error.get("message") if isinstance(error, dict)
            else error if isinstance(error, str) else None
        )
        core_code = error.get("code") if isinstance(error, dict) else None
        if exc.code in (401, 403):
            code, message = "core_unauthorized", "Core 身份验证或项目权限检查未通过。"
        elif exc.code == 400:
            code, message = "core_bad_request", "Core 拒绝了请求（HTTP 400）；请检查任务内容、字段格式或附件。"
        elif exc.code == 404:
            code, message = "core_route_not_found", "Core 缺少该能力或路由（HTTP 404）；请确认服务已更新到包含该能力的版本。"
        elif exc.code == 415:
            code, message = "core_unsupported_media", "Core 不支持该文件类型（HTTP 415）；请上传 PNG/JPEG/PDF 或文本文件。"
        elif exc.code == 409:
            code, message = "core_conflict", "Core 返回冲突（HTTP 409）；请刷新后重试。"
        elif 300 <= exc.code < 400:
            code, message = "core_redirect", "Core 返回重定向；本地桥拒绝跟随，请检查地址是否指向正确的 Core 根路径。"
        else:
            code, message = "core_rejected", f"Core 返回 HTTP {exc.code}。"
        if detail:
            message = f"{message} 详情：{detail}"
        if core_code:
            message = f"{message}（{core_code}）"
        error = BridgeError(code, message, 502)
        error.core_status, error.core_code = exc.code, core_code
        raise error from None
    except (URLError, TimeoutError, OSError):
        raise BridgeError("core_unreachable", "无法连接本机 Core；请检查地址、端口和运行状态。", 502) from None
    if len(raw) > MAX_RESPONSE:
        raise BridgeError("core_response_too_large", "Core 响应超过本地桥的读取上限。", 502)
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise BridgeError("core_invalid_response", "Core 未返回有效的 JSON。", 502) from None
    if not isinstance(result, dict):
        raise BridgeError("core_invalid_response", "Core 响应格式不兼容。", 502)
    if result.get("error") or result.get("action_status") in {"failed", "rejected", "denied"}:
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        detail = error.get("message") or (result.get("result") or {}).get("text")
        message = "Core 未通过权限或能力检查；请核对项目 ID 和成员权限。"
        if detail:
            message = f"{message} 详情：{detail}"
        if error.get("code"):
            message = f"{message}（{error['code']}）"
        raise BridgeError("core_capability_error", message, 502)
    return result


def describe_connection(connection: CoreConnection) -> dict:
    health = core_request(connection, "/health")
    capability_payload = core_request(connection, "/v2/capabilities")
    raw_capabilities = capability_payload.get("capabilities", [])
    if not isinstance(raw_capabilities, list):
        raise BridgeError("core_invalid_response", "Core 能力列表格式不兼容。", 502)
    capabilities = [
        item for item in raw_capabilities
        if isinstance(item, dict) and item.get("mutates") is False
    ]
    if "inventory.query.list" not in {
        item.get("capability_id") for item in capabilities
    }:
        raise BridgeError(
            "core_version_mismatch",
            "Core 未提供 inventory.query.list；该服务版本不能作为工作台连接。",
            502,
        )
    invoke_read(
        connection, "inventory.query.list",
        {"kind": "transactions", "project_id": connection.project_id},
    )
    features = available_features(raw_capabilities)
    missing = sorted(
        capability for capability in OPTIONAL_READ_CAPABILITIES
        if capability not in {
            item.get("capability_id") for item in capabilities
        }
    )
    return {
        "health": health,
        "projectId": connection.project_id,
        "capabilities": capabilities,
        "features": features,
        "missingReadCapabilities": missing,
        "coreVersion": health.get("version") if isinstance(health, dict) else None,
    }


def invoke_read(connection: CoreConnection, capability: str, params: dict) -> dict:
    if capability not in READ_CAPABILITIES:
        raise BridgeError("write_disabled", "本地桥仅允许已列明的读取能力。", 403)
    if capability == "inventory.query.list":
        if params.get("kind") not in {"chemicals", "transactions"} or params.get("project_id") != connection.project_id:
            raise BridgeError("invalid_scope", "库存读取必须携带已连接的项目 ID。", 403)
    if capability == "query.events.search" and params.get("project_ids") != [connection.project_id]:
        raise BridgeError("invalid_scope", "搜索只能读取已连接的项目。", 403)
    if capability in {
        "query.samples.list", "query.answer.build", "evidence.pages.list",
        "resource.list", "resource.booking.status",
    } and params.get("project_ids") != [connection.project_id]:
        raise BridgeError("invalid_scope", "读取只能限定在已连接的项目。", 403)
    if capability == "memory.sample.search" and params.get("project_id") != connection.project_id:
        raise BridgeError("invalid_scope", "样品检索必须限定在已连接的项目。", 403)
    envelope = core_request(connection, f"/v2/capabilities/{capability}/invoke",
                            body={"params": params})
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise BridgeError("core_invalid_response", "Core 能力响应缺少 result 对象。", 502)
    return result


def _one_query(
    query: dict[str, list[str]], name: str, *, max_length: int = 512
) -> str:
    if set(query) - {name}:
        raise BridgeError("invalid_search", "请求包含未支持的参数。")
    values = query.get(name)
    if values is None or len(values) != 1:
        raise BridgeError("invalid_search", f"缺少参数 {name}。")
    text = values[0].strip()
    if not text or len(text) > max_length:
        raise BridgeError("invalid_search", f"{name} 长度不正确。")
    return text


def invoke_write(connection: CoreConnection, capability: str, params: dict) -> dict:
    """One explicitly allowlisted business route for the unified task box."""
    if capability not in WRITE_CAPABILITIES:
        raise BridgeError("write_disabled", "本地桥未开放该写入能力。", 403)
    if params.get("project_id") != connection.project_id:
        raise BridgeError("invalid_scope", "写入只能作用于已连接的项目。", 403)
    envelope = core_request(
        connection, f"/v2/capabilities/{capability}/invoke", body={"params": params}
    )
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise BridgeError("core_invalid_response", "Core 能力响应缺少 result 对象。", 502)
    return result


def validate_session_payload(body: dict) -> dict:
    if set(body) - {"text", "attachments", "sessionId"}:
        raise BridgeError("invalid_body", "会话请求包含未支持的字段。")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise BridgeError("invalid_body", "请输入 1 至 4000 字符的任务描述。")
    attachments = body.get("attachments") or []
    if not isinstance(attachments, list) or len(attachments) > 5:
        raise BridgeError("invalid_body", "最多上传 5 个附件。")
    clean = []
    for index, item in enumerate(attachments):
        if not isinstance(item, dict):
            raise BridgeError("invalid_body", "附件格式不正确。")
        if set(item) - {"name", "mime", "data_b64"}:
            raise BridgeError("invalid_body", "附件包含未支持的字段。")
        data_b64 = item.get("data_b64")
        if not isinstance(data_b64, str) or not data_b64 or len(data_b64) > 30_000_000:
            raise BridgeError("invalid_body", "附件数据为空或超过大小限制。")
        clean.append({
            "name": str(item.get("name") or f"upload-{index + 1}"),
            "mime": str(item.get("mime") or "application/octet-stream"),
            "data_b64": data_b64,
        })
    session_id = body.get("sessionId")
    if session_id is not None and (
        not isinstance(session_id, str) or not session_id or len(session_id) > 128
    ):
        raise BridgeError("invalid_body", "sessionId 格式不正确。")
    return {"text": text.strip(), "attachments": clean, "session_id": session_id}


def invoke_internal_write(connection: CoreConnection, capability: str, params: dict,
                          *, operation_id: str | None = None) -> dict:
    """Server-side chained write; never exposed as a browser route by name."""
    envelope = core_request(
        connection, f"/v2/capabilities/{capability}/invoke", body={"params": params},
        operation_id=operation_id,
        namespace="frontend_agent_" + hashlib.sha256(connection.project_id.encode()).hexdigest()[:24],
    )
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise BridgeError("core_invalid_response", "Core 能力响应缺少 result 对象。", 502)
    return result


def _agent_public(value, credentials: tuple[str, ...]):
    """Keep host credentials and transport-only fields out of persisted/UI receipts."""
    if isinstance(value, dict):
        return {
            key: _agent_public(item, credentials) for key, item in value.items()
            if key.lower() not in {
                "run_token", "token", "api_key", "apikey", "authorization", "data_b64",
            }
        }
    if isinstance(value, list):
        return [_agent_public(item, credentials) for item in value]
    if isinstance(value, str):
        for credential in credentials:
            if credential:
                value = value.replace(credential, "[redacted]")
    return value


def _pi_node() -> str:
    node = os.environ.get("CHEMCLAW_NODE_EXE") or shutil.which("node")
    host = Path(__file__).resolve().parents[1] / "runtime" / "pi" / "server.mjs"
    if not node or not Path(node).is_file() or not host.is_file():
        raise BridgeError(
            "agent_runtime_unavailable",
            "Agent 运行环境不可用。请安装 Node.js 20+，或设置 CHEMCLAW_NODE_EXE。", 503,
        )
    if not (host.parent / "node_modules" / "@earendil-works" / "pi-agent-core").is_dir():
        raise BridgeError(
            "agent_runtime_unavailable", "Pi 依赖尚未安装，请在 runtime/pi 执行 npm ci。", 503,
        )
    return node


def session_source_context(connection: CoreConnection, started: dict, context: dict,
                           *, max_pages: int = 8, max_bytes: int = 96000,
                           cancelled: threading.Event | None = None) -> dict:
    """Carry authorized original text across turns, without semantic extraction.

    Whole pages only: omitted pages remain available through ordinary tools.
    Context is rebuilt from Core, never trusted from an old assistant summary.
    """
    identifiers = {}
    for entry in [*(started.get("history") or []), {"context": context}]:
        pages = (entry.get("context") or {}).get("pages", []) if isinstance(entry, dict) else []
        for page in pages if isinstance(pages, list) else []:
            page_id = page.get("page_id") if isinstance(page, dict) else None
            if isinstance(page_id, str) and page_id:
                identifiers.pop(page_id, None)
                identifiers[page_id] = None
    all_ids = list(identifiers)
    selected, omitted, size = [], all_ids[:-max_pages], 0
    for page_id in all_ids[-max_pages:]:
        if cancelled is not None and cancelled.is_set():
            omitted.append(page_id)
            continue
        try:
            head = invoke_read(connection, "evidence.page.read", {"page_id": page_id})
            page = head.get("page", head)
            transcript = page.get("transcript") or {}
            if page.get("project_id") != connection.project_id or not transcript.get("blocks"):
                omitted.append(page_id)
                continue
            source = {"page_id": page_id,
                "transcript_revision": transcript.get("active_revision", page.get("active_revision")),
                "blocks": [{key: block[key] for key in ("block_id", "text", "reading_order", "block_kind", "quality_flags")
                            if key in block} for block in transcript["blocks"]]}
            cost = len(json.dumps(source, ensure_ascii=False).encode("utf-8"))
            if size + cost > max_bytes:
                omitted.append(page_id)
                continue
            selected.append(source)
            size += cost
        except BridgeError:
            omitted.append(page_id)
    return {"source_pages": selected, "source_omitted_pages": omitted}


def run_pi_agent(
    connection: CoreConnection, config: ModelConfig, started: dict,
    message: str, context: dict, *, mock: bool = False,
    resume: dict | None = None, cancel_event: threading.Event | None = None,
    durable: bool = False,
) -> dict:
    """One scoped Pi process; keys travel in process memory, never argv or files."""
    node = _pi_node()
    host = Path(__file__).resolve().parents[1] / "runtime" / "pi" / "server.mjs"
    # Core provides the role manifest for both new and resumed runs.
    manifest = started.get("manifest") or {}
    if not isinstance(manifest, dict):
        raise BridgeError("invalid_agent_timeout", "Core returned an invalid Agent manifest.", 502)
    timeout_s = manifest.get("timeout_s", 240)
    if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s) or timeout_s <= 0):
        raise BridgeError("invalid_agent_timeout", "Core returned an invalid Agent timeout.", 502)
    run_timeout_ms = math.ceil(timeout_s * 1000)
    http_timeout_s = run_timeout_ms / 1000 + 15
    context = {**context, **session_source_context(connection, started, context, cancelled=cancel_event)}
    env = {
        **os.environ,
        "GATEWAY_URL": connection.endpoint,
        "AGENT_TOKEN": started["run_token"],
        "AGENT_ROLE": "orchestrator",
        "PI_BASE_URL": _normalize_model_base(config.base_url, _model_protocol(config.protocol)),
        "PI_API_KEY": config.api_key,
        "PI_MODEL": config.model,
        "PI_PROTOCOL": "openai-responses" if config.protocol == "openai-responses" else "openai-completions",
        "PI_REASONING_FORMAT": config.reasoning_format,
        "PI_MAX_TOKENS": str(config.max_tokens),
        "PI_CONTEXT_WINDOW": str(config.context_window),
        "PI_RUN_TIMEOUT_MS": str(run_timeout_ms),
        "PI_MOCK": "1" if mock else "0",
    }
    # Explicit values travel with this connection; don't inherit another
    # process/session's effort setting when the user selects model defaults.
    env.pop("PI_REASONING_EFFORT", None)
    if config.reasoning_effort is not None:
        env["PI_REASONING_EFFORT"] = config.reasoning_effort
    process = subprocess.Popen(
        [node, str(host)], cwd=host.parent, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8",
    )
    ready: queue.Queue = queue.Queue()

    def read_port():
        try:
            for line in process.stdout:
                if line.startswith("PI_SERVER_PORT="):
                    ready.put(int(line.strip().split("=", 1)[1]))
                    return
        finally:
            ready.put(None)

    threading.Thread(target=read_port, daemon=True).start()
    try:
        try:
            port = ready.get(timeout=20)
        except queue.Empty:
            port = None
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise BridgeError("agent_runtime_start_failed", "Pi 未能启动，请检查 Node 与 runtime/pi 依赖。", 503)
        done = threading.Event()

        def cancel_host():
            while not done.wait(0.2):
                if cancel_event is not None and cancel_event.is_set():
                    try:
                        request = Request(f"http://127.0.0.1:{port}/cancel", data=b"{}",
                                          headers={"Content-Type": "application/json"}, method="POST")
                        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=3) as response:
                            response.read()
                    except (OSError, URLError):
                        pass
                    return

        if cancel_event is not None:
            threading.Thread(target=cancel_host, daemon=True).start()
        body = json.dumps({
            "session_id": started["session_id"], "message": message,
            "history": started.get("history", []), "context": context,
            "durable": durable,
            **({"resume": resume} if resume else {}),
        }, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if len(body) > 256 * 1024:
            # Lossless local IPC only: the model receives the same decoded
            # context and receipts. Credentials/payload never go to disk.
            body = gzip.compress(body, mtime=0)
            headers["Content-Encoding"] = "gzip"
        request = Request(
            f"http://127.0.0.1:{port}/run", data=body,
            headers=headers, method="POST",
        )
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=http_timeout_s) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError("response too large")
            result = json.loads(raw)
            if not isinstance(result, dict) or not isinstance(result.get("text", ""), str):
                raise ValueError("invalid result")
            return result
        except HTTPError as exc:
            try:
                failure = json.loads(exc.read(MAX_RESPONSE))
                error = failure.get("error", {})
                code = error.get("code")
            except (ValueError, AttributeError, OSError):
                failure, error, code = {}, {}, None
            if isinstance(code, str) and _TERMINAL_RUNTIME_ERRORS.get(code) == exc.code and error.get("retryable") is False:
                result = failure.get("result")
                if (isinstance(result, dict) and result.get("status") in ("failed", "partial")
                        and isinstance(result.get("text"), str) and result["text"].strip()
                        and result.get("error_code") == code
                        and all(isinstance(result[key], list) for key in ("toolCalls", "events", "delegations", "object_refs", "warnings") if key in result)
                        and all(isinstance(result[key], dict) for key in ("data", "runtime_error", "completion", "knowledge_delivery") if key in result)):
                    return {**result, "classification": "non_retryable_runtime_failure",
                        "runtime_error": {**result.get("runtime_error", {}), "code": code, "retryable": False}}
                raise TerminalRuntimeError(code,
                    "Pi 已确认不可重试的持久化或传输错误；保留原检查点并结束本次运行。", exc.code) from None
            if exc.code == 503 and code == "checkpoint_unavailable":
                raise BridgeError("checkpoint_unavailable",
                    "运行检查点未能保存，任务将保留原运行和回执，等待后台恢复。", 503) from None
            raise BridgeError("agent_runtime_failed", "Agent 运行未正常返回。已保存的记录会保留，请查看任务状态后继续。", 502) from None
        except (URLError, OSError, ValueError) as exc:
            raise BridgeError(
                "agent_runtime_failed",
                "Agent 运行未正常返回。已保存的记录会保留，请查看任务状态后继续。", 502,
            ) from None
    finally:
        if "done" in locals():
            done.set()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


def stage_agent_attachments(connection: CoreConnection, config: ModelConfig, attachments: list,
                            *, preprocess: bool = True, request_id: str | None = None) -> dict:
    """Persist originals and decode text/images; the agent decides what facts to propose."""
    staged = {"pages": [], "object_refs": [], "warnings": [], "failed_attachments": [], "provider_requests": []}
    if not attachments:
        return staged
    doc_params = {
        "project_id": connection.project_id,
        "notebook_code": "WEB-" + (hashlib.sha256(request_id.encode()).hexdigest()[:16].upper()
                                   if request_id else secrets.token_hex(4).upper()),
        "title": "Web Agent upload", "source_channel": "web_assistant",
    }
    document = invoke_internal_write(connection, "evidence.document.create", doc_params,
        **({"operation_id": f"jobupload:{request_id}:document"} if request_id else {}))
    document_id = document["document_id"]
    staged["object_refs"].append({"kind": "evidence_document", "id": document_id})
    quota_exhausted = False
    for index, item in enumerate(attachments):
        saved = False
        try:
            result = invoke_internal_write(connection, "evidence.page.ingest", {
                "document_id": document_id, "page_order": index + 1,
                "filename": item["name"],
                "content_data_url": f"data:{item['mime']};base64,{item['data_b64']}",
            }, **({"operation_id": f"jobupload:{request_id}:page:{index}"} if request_id else {}))
            page = result["page"]
            saved = True
            info = {"page_id": page["page_id"], "filename": item["name"], "mime": page["mime"]}
            staged["pages"].append(info)
            staged["object_refs"].append({"kind": "evidence_page", "id": page["page_id"]})
            if not preprocess or quota_exhausted:
                continue
            if page["mime"] in ("text/plain", "text/markdown"):
                invoke_internal_write(connection, "evidence.transcribe.run", {
                    "page_id": page["page_id"], "provider": "text_input",
                })
            elif page["mime"].startswith("image/"):
                ocr = vision_ocr_page(connection, config, page["page_id"])
                staged["provider_requests"].extend(ocr.get("provider_requests", []))
            else:
                staged["warnings"].append(f"parser_not_configured:{page['page_id']}")
        except (BridgeError, ModelError) as exc:
            staged["warnings"].append(f"attachment_processing_failed:{item['name']}:{exc.code}")
            if exc.code == "provider_quota_exhausted":
                quota_exhausted = True
            if getattr(exc, "provider_observation", None):
                staged["provider_requests"].append(exc.provider_observation)
            if not saved:
                staged["failed_attachments"].append({"index": index, "name": item["name"], "code": exc.code})
    return staged


def _provider_quota_result(staged: dict, observation: dict | None = None) -> dict:
    """A resource refusal ends this attempt without pretending the job can resume."""
    requests = _provider_rows(staged.get("provider_requests", []), [observation] if observation else [])
    return {"status": "partial" if staged.get("object_refs") else "failed",
        "text": "模型服务额度不足，已停止后续模型调用。原件和已保存成果保留；恢复额度后可基于现有页面继续未完成整理。",
        "error_code": "provider_quota_exhausted", "classification": "requires_resource_recovery",
        "needs_provider_quota": True, "runtime": "pi", "modelUsed": False,
        "object_refs": staged.get("object_refs", []), "warnings": staged.get("warnings", []) + ["provider_quota_exhausted"],
        "data": {"pages": staged.get("pages", []), "provider_requests": requests, "provider_usage": _provider_summary(requests),
            "needs_provider_quota": True, "classification": "requires_resource_recovery"}}


def process_agent_session(connection: CoreConnection, config: ModelConfig, payload: dict) -> dict:
    """LLM owns planning/delegation. Failures never replay through the legacy writer."""
    _pi_node()  # Fail before creating tasks or saving uploads if the host is missing.
    advertised = core_request(connection, "/v2/capabilities").get("capabilities", [])
    if not available_features(advertised)["agentRuntime"]:
        raise BridgeError("agent_runtime_unavailable", "当前 Core 尚未提供 Agent 会话能力，请更新 Core。", 409)
    # OCR can take longer than one run lease. Finish preprocessing before
    # minting the execution token, then persist page references with the turn.
    staged = stage_agent_attachments(connection, config, payload["attachments"])
    if any(warning.endswith(":provider_quota_exhausted") for warning in staged["warnings"]):
        return _provider_quota_result(staged)
    try:
        started = invoke_internal_write(connection, "assistant.agent.start", {
            "project_id": connection.project_id, "text": payload["text"],
            "session_id": payload.get("session_id"),
            "context": {"pages": staged["pages"], "object_refs": staged["object_refs"]},
        })
    except BridgeError as exc:
        if not staged["object_refs"]:
            raise BridgeError(exc.code, exc.message + " 请先运行 scripts/configure_agent_runtime.py 启用角色。", exc.status) from None
        return {
            "status": "partial", "text": "原件已保存，但 Agent 未能启动。请启用角色后在证据工作区继续处理。",
            "agent": "orchestrator", "runtime": "pi", "modelUsed": False,
            "object_refs": staged["object_refs"], "data": staged,
            "warnings": staged["warnings"] + [exc.code],
        }
    credentials = (connection.api_key, config.api_key, config.vision_api_key or "", started["run_token"])
    try:
        runtime = run_pi_agent(connection, config, started, payload["text"], {
            "project_id": connection.project_id, "task_id": started["task_id"],
            "run_id": started["run_id"], "pages": staged["pages"],
            "object_refs": staged["object_refs"],
        })
    except (BridgeError, ModelError, OSError) as exc:
        runtime = {
            "status": "failed", "text": "Agent 任务未完成，已保存的原件和操作回执会保留。",
            "error": getattr(exc, "code", "agent_runtime_failed"),
            "events": [], "delegations": [], "object_refs": [],
        }
    runtime = _agent_public(runtime, credentials)
    requests = _provider_rows(staged.get("provider_requests", []), runtime.get("provider_requests", []),
        runtime.get("data", {}).get("provider_requests", []))
    refs = staged["object_refs"] + runtime.get("object_refs", [])
    result = {
        **runtime, "status": runtime.get("status") or ("failed" if runtime.get("error") else "completed"),
        "object_refs": list({(r.get("kind"), r.get("id")): r for r in refs if isinstance(r, dict) and r.get("id")}.values()),
        "warnings": staged["warnings"] + runtime.get("warnings", [])
                    + ([str(runtime["error"])] if runtime.get("error") else []),
        "data": {**runtime.get("data", {}), "pages": staged["pages"], "failed_attachments": staged["failed_attachments"],
                 "provider_requests": requests, "provider_usage": _provider_summary(requests),
                 "toolCalls": runtime.get("toolCalls", [])},
    }
    if staged["warnings"] and result["status"] == "completed":
        result["status"] = "partial"
    # `error` is reserved for HTTP/bridge transport failures in CoreClient.
    # Runtime failures must still deliver partial receipts and saved file refs.
    result.pop("error", None)
    if runtime.get("error"):
        result["runtime_error"] = str(runtime["error"])
    # The completion capability also revokes descendants and stores the public reply.
    finished = invoke_internal_write(connection, "assistant.agent.finish", {
        "project_id": connection.project_id, "run_id": started["run_id"],
        "claim_epoch": started["claim_epoch"], "result": result,
    })
    return _agent_public({
        **result, **finished, "runtime": "pi", "agent": "orchestrator",
        "session_id": started["session_id"], "run_id": started["run_id"],
        "task_id": started["task_id"], "trace_id": started.get("trace_id"),
        "classification": "agent", "modelUsed": True,
        "model": {"name": config.model, "baseUrl": config.base_url},
    }, credentials)


def invoke_review_action(connection: CoreConnection, body: dict) -> dict:
    """Human review actions on a proposal; project is checked server-side."""
    action = body.get("action")
    proposal_id = body.get("proposal_id")
    if action not in {"update", "confirm", "apply"}:
        raise BridgeError("invalid_review", "未知的核对操作。", 400)
    if not isinstance(proposal_id, str) or not proposal_id:
        raise BridgeError("invalid_review", "缺少 proposal_id。", 400)
    head = invoke_read(connection, "memory.proposal.read", {"proposal_id": proposal_id})
    proposal = head.get("proposal") or {}
    if proposal.get("project_id") != connection.project_id:
        raise BridgeError("invalid_scope", "提案不属于当前项目。", 403)
    if action == "update":
        item_key = body.get("item_key")
        changes = body.get("changes")
        expected = body.get("expected_revision")
        if not isinstance(item_key, str) or not isinstance(changes, dict) or not isinstance(expected, int):
            raise BridgeError("invalid_review", "编辑需要 item_key、changes 和 expected_revision。", 400)
        return invoke_internal_write(connection, "memory.proposal.item.update", {
            "proposal_id": proposal_id, "item_key": item_key,
            "changes": changes, "expected_proposal_revision": expected,
        })
    if action == "confirm":
        item_keys = body.get("item_keys")
        decision = body.get("decision")
        expected = body.get("expected_revision")
        if not isinstance(item_keys, list) or decision not in {"confirmed", "declined"} or not isinstance(expected, int):
            raise BridgeError("invalid_review", "确认需要 item_keys、decision 和 expected_revision。", 400)
        return invoke_internal_write(connection, "memory.proposal.confirm_items", {
            "proposal_id": proposal_id, "item_keys": item_keys,
            "decision": decision, "expected_proposal_revision": expected,
        })
    return invoke_internal_write(connection, "memory.proposal.apply", {
        "proposal_id": proposal_id,
        "operation_id": f"review_apply_{secrets.token_hex(8)}",
    })


def invoke_knowledge_correction(connection: CoreConnection, body: dict, *, transcript: bool = False) -> dict:
    """Human correction of an adopted record; immutable Core revisions remain."""
    identity = "pageId" if transcript else "eventId"
    content = "corrections" if transcript else "changes"
    if set(body) != {identity, "expectedRevision", "reason", content}:
        raise BridgeError("invalid_correction", "纠错请求字段不完整或包含未支持字段。")
    object_id, revision, reason = body[identity], body["expectedRevision"], body["reason"]
    if (not isinstance(object_id, str) or not object_id or len(object_id) > 256
            or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000):
        raise BridgeError("invalid_correction", "纠错需要对象ID、当前版本和修改原因。")
    if transcript:
        result = invoke_read(connection, "evidence.page.read", {"page_id": object_id})
        page = result.get("page", result)
    else:
        event = invoke_read(connection, "memory.event.read", {"event_id": object_id})
        payload = event.get("field_json") or {}
        page_id = payload.get("page_id")
        if not page_id:
            raise BridgeError("invalid_scope", "该记录缺少可核对的项目来源。", 403)
        result = invoke_read(connection, "evidence.page.read", {"page_id": page_id})
        page = result.get("page", result)
    if page.get("project_id") != connection.project_id:
        raise BridgeError("invalid_scope", "只能修改当前连接项目中的记录。", 403)
    if not transcript:
        changes = body["changes"]
        if not isinstance(changes, dict) or not changes:
            raise BridgeError("invalid_correction", "changes 必须是非空对象。")
        return invoke_internal_write(connection, "memory.event.correct", {
            "event_id": object_id, "expected_revision": revision, "reason": reason.strip(), "changes": changes,
        })
    corrections = body["corrections"]
    if (not isinstance(corrections, list) or not corrections or any(not isinstance(c, dict)
            or set(c) != {"block_id", "text"} or not isinstance(c["block_id"], str)
            or not isinstance(c["text"], str) for c in corrections)
            or len({c["block_id"] for c in corrections}) != len(corrections)):
        raise BridgeError("invalid_correction", "corrections 必须列出不重复的 block_id 与修正正文。")
    current = page.get("transcript") or {}
    if current.get("active_revision") != revision:
        raise BridgeError("revision_conflict", "转写已更新，请刷新后再纠错。", 409)
    replacements = {c["block_id"]: c["text"] for c in corrections}
    original = current.get("blocks") or []
    if set(replacements) - {b["block_id"] for b in original}:
        raise BridgeError("invalid_correction", "纠错块不属于该页当前版本。")
    keys = ("block_id", "block_kind", "reading_order", "text", "table_cell", "geometry",
            "coordinate_system", "source_page", "extractor", "quality_flags", "competing_readings")
    # Block IDs identify immutable stored versions, including unchanged blocks.
    ids = {b["block_id"]: "b_correct_" + hashlib.sha256(
        f"{object_id}|{revision + 1}|{b['block_id']}".encode()).hexdigest()[:24] for b in original}
    blocks = [{**{k: b[k] for k in keys if k in b},
               "block_id": ids[b["block_id"]],
               "text": replacements.get(b["block_id"], b.get("text", ""))} for b in original]
    for block in blocks:
        cell = block.get("table_cell")
        if isinstance(cell, dict) and cell.get("table_ref") in ids:
            block["table_cell"] = {**cell, "table_ref": ids[cell["table_ref"]]}
    return invoke_internal_write(connection, "evidence.transcript.correct", {
        "page_id": object_id, "expected_revision": revision, "reason": reason.strip(), "blocks": blocks,
    })


def _vision_text_blocks(config: ModelConfig, page: dict, *, audit_context: dict | None = None) -> tuple[list[dict], str]:
    model = config.vision_model or config.model
    data_url = f"data:{page['mime']};base64,{page['data_b64']}"
    messages = [{
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "你是实验室手写记录 OCR。读取图片中的所有文字，按阅读顺序逐行输出，"
                    "不要解释、不要换算、不要补造看不清的内容；无法辨认的片段写 [unclear]。"
                    "删除线用~~旧内容~~保留，修正值与箭头关联；批注注明它对应的条目，"
                    "并把批注和所属样品/段落写在同一块；无法确定归属就标记[unclear:批注归属]，"
                    "不要拼接到相邻条目或猜测图像坐标。"
                ),
            },
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }]
    raw = call_model(
        config, messages, timeout=240, max_tokens=_vision_output_budget(config), model=model, vision=True,
        audit_context=audit_context,
    )
    raw_blocks = [
        {"text": line.strip(), "block_kind": "handwriting"}
        for line in raw.splitlines() if line.strip()
    ]
    allowed_kinds = {
        "paragraph", "heading", "table", "table_cell",
        "handwriting", "stamp", "annotation",
    }
    blocks = []
    for index, item in enumerate(raw_blocks):
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            kind = item.get("block_kind") if item.get("block_kind") in allowed_kinds else "handwriting"
        else:
            text, kind = str(item).strip(), "handwriting"
        if not text:
            continue
        page_key = str(page.get("page_id") or "")
        block_id = (
            f"b_ocr_{hashlib.sha256(f'{page_key}|{index}|{text[:40]}'.encode()).hexdigest()[:16]}"
        )
        blocks.append({
            "block_id": block_id,
            "block_kind": kind,
            "reading_order": len(blocks),
            "text": text,
            "table_cell": None,
            "geometry": None,
            "coordinate_system": None,
            "source_page": 1,
            "extractor": {
                "provider": "vision_model",
                "model": model,
                "version": "1.0",
                "config": {"transport": "openai-chat"},
            },
            "quality_flags": ["geometry_untrusted"],
        })
    if not blocks:
        raise BridgeError("ocr_empty", "视觉模型未返回可用的转写块。", 502)
    return blocks, raw


def vision_ocr_page(connection: CoreConnection, config: ModelConfig, page_id: str,
                    *, operation_id: str | None = None, job_fence: dict | None = None,
                    cancelled: threading.Event | None = None, audit_run_id: str | None = None) -> dict:
    original = invoke_read(connection, "evidence.page.original", {"page_id": page_id})
    mime = str(original.get("mime") or "")
    if not mime.startswith("image/"):
        raise BridgeError("unsupported_media", f"视觉 OCR 需要图片，当前是 {mime or 'unknown'}。", 415)
    requests = []
    blocks, raw = _vision_text_blocks(config, original, audit_context={"page_id": page_id,
        "run_id": audit_run_id, "records": requests})
    if cancelled is not None and cancelled.is_set():
        raise BridgeError("job_cancelled", "任务已停止，转写结果未提交。", 409)
    params = {
        "page_id": page_id,
        "blocks": blocks,
        "provider": "vision_model",
        "model": config.vision_model or config.model,
        "version": "1.0",
        "raw_output": raw[:20000],
        "needs_human": False,
    }
    if job_fence:
        run = invoke_job(connection, "preprocess", **job_fence,
            capability_id="evidence.transcript.import", params=params, operation_id=operation_id)
    else:
        run = invoke_internal_write(connection, "evidence.transcript.import", params,
                                    operation_id=operation_id)
    return {"run": run, "mime": mime, "blocks": len(blocks), "provider_requests": requests,
        "provider_usage": _provider_summary(requests)}


def available_features(capabilities: list) -> dict[str, bool]:
    ids = {
        item.get("capability_id")
        for item in capabilities
        if isinstance(item, dict)
    }
    return {
        "inventory": "inventory.query.list" in ids,
        "search": "query.events.search" in ids,
        "samples": "query.samples.list" in ids,
        "timeline": "query.sample.timeline" in ids,
        "answer": "query.answer.build" in ids,
        "event": "memory.event.read" in ids,
        "page": "evidence.page.read" in ids,
        "original": "evidence.page.original" in ids,
        "preview": "evidence.page.preview" in ids,
        "evidence": "evidence.pages.list" in ids,
        "proposal": "memory.proposal.read" in ids,
        "providers": "evidence.provider.list" in ids,
        "resources": "resource.list" in ids,
        "bookings": "resource.booking.status" in ids,
        "task": "assistant.task.process" in ids,
        "agentRuntime": {"assistant.agent.start", "assistant.agent.finish"} <= ids,
        "agentJobs": {f"assistant.job.{name}" for name in (
            "submit", "list", "read", "claim", "heartbeat", "complete", "cancel")} <= ids,
    }


def invoke_job(connection: CoreConnection, operation: str, **params) -> dict:
    try:
        return invoke_internal_write(connection, f"assistant.job.{operation}", {
            **params, "project_id": connection.project_id,
        })
    except BridgeError as exc:
        if getattr(exc, "core_status", None) in (400, 401, 403, 404, 409):
            exc.status = exc.core_status
            exc.code = exc.core_code or exc.code
        raise


def queue_page_knowledge(connection: CoreConnection, page_id: str) -> dict:
    """Let the existing agent organize the active transcript after manual OCR."""
    result = invoke_read(connection, "evidence.page.read", {"page_id": page_id})
    page = result.get("page", result)
    if page.get("project_id") != connection.project_id:
        raise BridgeError("invalid_scope", "只能整理当前项目的记录。", 403)
    revision = (page.get("transcript") or {}).get("active_revision")
    if not revision:
        raise BridgeError("transcript_missing", "请先完成原页转写。", 409)
    return invoke_job(connection, "submit", request_id=f"pageknowledge:{page_id}:r{revision}",
        text="整理这页当前转写中的实验与样品信息，保留来源、计划与实测区别和未知项。明确的样品标签形成知识记录，默认采纳并关联样品。不要重复OCR；如果已有知识，追加纠错版本或保留，避免重复记录。界面说明不当作实验事实。",
        attachments_manifest=[], context={"pages": [{"page_id": page_id, "mime": page.get("mime", "image/jpeg")}],
            "object_refs": [{"kind": "evidence_page", "id": page_id}]})


_CONTINUATION_TERMINAL = frozenset({"completed", "partial", "failed", "cancelled", "needs_review", "clarification_needed"})


def _reference_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) is not None


def _continuation_list(value):
    return value if isinstance(value, list) else []


def _continuation_requires_review(result: dict) -> bool:
    """Unknown effects are a hard boundary, not another extraction attempt."""
    if not isinstance(result, dict):
        return False
    if result.get("status") == "cancelled":
        # Core cancellation deliberately has a small public receipt, not the
        # full interrupted journal. Absence of that journal cannot prove that
        # no mutation was issued before cancellation.
        return True
    if result.get("error_code") in {"ambiguous_tool_outcome", "pending_tool_outcome_requires_review"}:
        return True
    progress = result.get("capture_progress") or {}
    if isinstance(progress, dict) and (progress.get("requires_review") or progress.get("unresolved_writes")):
        return True
    delivery = result.get("knowledge_delivery") or {}
    if isinstance(delivery, dict) and any(p.get("reason") == "write_outcome_unconfirmed"
            for p in _continuation_list(delivery.get("pending")) if isinstance(p, dict)):
        return True
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if data.get("retained_checkpoints") or data.get("checkpoint_failure"):
        return True
    calls = [*_continuation_list(result.get("toolCalls")), *_continuation_list(data.get("toolCalls"))]
    for call in calls:
        if not isinstance(call, dict):
            continue
        receipt = call.get("result") if isinstance(call.get("result"), dict) else {}
        if call.get("name") not in READ_CAPABILITIES | {"agent.respond"} and (
                call.get("state") == "pending" or receipt.get("action_status") == "unknown"):
            return True
        if call.get("name") == "agent.delegate" and _continuation_requires_review(call.get("result") or {}):
            return True
    return any(_continuation_requires_review(child) for child in _continuation_list(result.get("delegations")) if isinstance(child, dict))


def capture_continuation_context(connection: CoreConnection, source: dict) -> dict:
    """Rebuild a bounded cursor from authorized current sources and readbacks.

    Old block lists are hints only. A current event reference does not establish
    semantic completeness of its block or page. No source text is interpreted.
    """
    result, original = source.get("result") or {}, source.get("context") or {}
    if not isinstance(result, dict) or not isinstance(original, dict):
        raise BridgeError("continuation_sources_missing", "原任务缺少可核查的来源回执。", 409)
    if source.get("status") == "cancelled" or _continuation_requires_review(result):
        raise BridgeError("continuation_requires_review", "原任务存在效果未知的写入，请先核查实际对象；尚未创建续作任务。", 409)
    previous = result.get("capture_progress") or {}
    if not isinstance(previous, dict):
        previous = {}
    target_ids = [p.get("page_id") for p in _continuation_list(original.get("pages")) if isinstance(p, dict)]
    if not target_ids and previous.get("target_page_ids_complete") is False:
        raise BridgeError("continuation_scope_too_large", "原任务的来源范围索引有省略，请先明确选择需要继续的来源。", 409)
    if not target_ids:
        target_ids = _continuation_list(previous.get("target_page_ids"))
    target_ids = list(dict.fromkeys(p for p in target_ids if _reference_id(p)))
    if not target_ids or len(target_ids) > 100:
        raise BridgeError("continuation_sources_missing", "原任务没有可继续整理的来源引用，请选择原记录任务。", 409)
    old_pages = {p.get("page_id"): p for p in _continuation_list(previous.get("pages")) if isinstance(p, dict)}
    for p in _continuation_list(original.get("pages")):
        if isinstance(p, dict) and p.get("page_id") not in old_pages:
            old_pages[p.get("page_id")] = p
    pages, cursors, changed, page_map = [], [], [], {}
    for page_id in target_ids:
        head = invoke_read(connection, "evidence.page.read", {"page_id": page_id})
        page = head.get("page", head)
        if page.get("page_id") != page_id:
            raise BridgeError("core_invalid_response", "来源回执与请求对象不一致。", 502)
        transcript = page.get("transcript") or {}
        revision = transcript.get("active_revision")
        if page.get("project_id") != connection.project_id:
            raise BridgeError("invalid_scope", "续作来源不属于当前项目。", 403)
        if (not isinstance(revision, int) or isinstance(revision, bool) or revision < 1
                or not isinstance(transcript.get("blocks"), list) or not transcript["blocks"]):
            raise BridgeError("continuation_transcript_missing", "原转写不可用；本次不会重新上传或OCR，请先检查来源。", 409)
        ids = [b.get("block_id") for b in transcript["blocks"] if isinstance(b, dict) and _reference_id(b.get("block_id"))]
        old_revision = old_pages.get(page_id, {}).get("transcript_revision")
        if not isinstance(old_revision, int) or isinstance(old_revision, bool) or old_revision < 1:
            old_revision = None
        source_changed = old_revision is not None and old_revision != revision
        if source_changed:
            changed.append(page_id)
        cursor = {"page_id": page_id, "transcript_revision": revision,
            "source_block_ids": ids, "source_inventory_complete": len(ids) == len(transcript["blocks"]),
            "linked_block_ids": [], "unlinked_block_ids": list(ids), "verified_event_refs": [],
            "source_changed": source_changed, "previous_transcript_revision": old_revision}
        pages.append({"page_id": page_id, "mime": page.get("mime") or "image/jpeg", "transcript_revision": revision})
        cursors.append(cursor)
        page_map[page_id] = cursor
    candidates = {}
    if isinstance(previous, dict):
        for field in ("saved_event_refs", "verified_event_refs"):
            for ref in _continuation_list(previous.get(field)):
                if isinstance(ref, dict) and _reference_id(ref.get("event_id")):
                    candidates[ref["event_id"]] = ref.get("revision")
    delivery = result.get("knowledge_delivery") or {}
    if isinstance(delivery, dict):
        for field in ("event_revisions", "read_event_revisions"):
            values = delivery.get(field) or {}
            for event_id, revision in values.items() if isinstance(values, dict) else []:
                if _reference_id(event_id):
                    candidates[event_id] = revision
    candidate_omitted = max(0, len(candidates) - 100)
    candidates = dict(list(candidates.items())[:100])
    revalidated = []
    for event_id in candidates:
        try:
            event = invoke_read(connection, "memory.event.read", {"event_id": event_id})
        except BridgeError:
            raise BridgeError("continuation_requires_review", "原任务的部分知识对象目前不可读，请先核查；尚未创建续作任务。", 409) from None
        fields = event.get("field_json") or {}
        if (event.get("entity_id") or event.get("event_id")) != event_id:
            raise BridgeError("core_invalid_response", "知识回执与请求对象不一致。", 502)
        owner = fields.get("project_id")
        if owner is not None and owner != connection.project_id:
            raise BridgeError("invalid_scope", "续作知识对象不属于当前项目。", 403)
        refs = _continuation_list(event.get("evidence_refs"))
        source_pages = {r.get("page_id") for r in refs if isinstance(r, dict)} | {fields.get("page_id")}
        if not source_pages.intersection(target_ids):
            continue
        revision = event.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise BridgeError("continuation_requires_review", "原任务知识对象缺少有效版本，请先核查。", 409)
        ref = {"event_id": event_id, "revision": revision}
        revalidated.append(ref)
        for page_id in source_pages.intersection(target_ids):
            page_map[page_id]["verified_event_refs"].append(ref)
        for evidence in refs:
            if not isinstance(evidence, dict):
                continue
            cursor = page_map.get(evidence.get("page_id"))
            block_id = evidence.get("block_id")
            if (cursor and evidence.get("transcript_revision") == cursor["transcript_revision"]
                    and block_id in cursor["source_block_ids"] and block_id not in cursor["linked_block_ids"]):
                cursor["linked_block_ids"].append(block_id)
    for cursor in cursors:
        cursor["unlinked_block_ids"] = [b for b in cursor["source_block_ids"] if b not in cursor["linked_block_ids"]]
    seed = {"version": 1, "knowledge_complete": None, "target_page_ids": target_ids, "pages": cursors,
        "previous_event_refs": revalidated, "requires_review": False,
        "truncated": bool(previous.get("truncated")) or bool(candidate_omitted),
        "previous_progress_truncated": bool(previous.get("truncated")),
        "candidate_event_refs_omitted_count": candidate_omitted,
        "interpretation": "这些只是不完整的已有对象读回候选；事件引用不证明块/页事实完整。先读回当前对象与原块，并按来源检索其余已有知识，再由LLM判断缺口。"}
    context = {"pages": pages, "object_refs": [{"kind": "evidence_page", "id": p} for p in target_ids],
        "capture_continuation": {"source_job_id": source["job_id"], "source_changed_page_ids": changed,
            "previous_progress": seed, "revalidated_event_refs": revalidated,
            "no_upload_or_ocr": True}}
    # Keep references available even when a huge source inventory cannot fit in
    # Core's context envelope. Omitted blocks remain readable through normal tools.
    for cursor in reversed(cursors):
        if len(json.dumps(context, ensure_ascii=False).encode("utf-8")) < 60000:
            break
        cursor.update(source_inventory_complete=False, inventory_omitted=True,
            omitted_block_count=len(cursor["source_block_ids"]), source_block_ids=[], linked_block_ids=[], unlinked_block_ids=[])
        seed["truncated"] = True
    if len(json.dumps(context, ensure_ascii=False).encode("utf-8")) >= 60000:
        for cursor in cursors:
            if cursor["verified_event_refs"]:
                cursor["event_ref_inventory_omitted"] = True
                cursor["verified_event_refs"] = []
        seed["truncated"] = True
    if len(json.dumps(context, ensure_ascii=False).encode("utf-8")) >= 60000:
        context["capture_continuation"]["revalidated_event_refs"] = []
        context["capture_continuation"]["revalidated_refs_in_previous_progress"] = True
    while seed["pages"] and len(json.dumps(context, ensure_ascii=False).encode("utf-8")) >= 60000:
        seed["pages"].pop()
        seed["truncated"] = True
        seed["page_cursor_omitted_count"] = seed.get("page_cursor_omitted_count", 0) + 1
    while seed["previous_event_refs"] and len(json.dumps(context, ensure_ascii=False).encode("utf-8")) >= 60000:
        seed["previous_event_refs"].pop()
        seed["truncated"] = True
        seed["candidate_event_refs_omitted_count"] += 1
    if len(json.dumps(context, ensure_ascii=False).encode("utf-8")) >= 60000:
        raise BridgeError("continuation_scope_too_large", "续作引用超过安全范围，请分组处理。", 409)
    return context


def submit_capture_continuation(connection: CoreConnection, body: dict, source_job_id: str) -> dict:
    """One explicit human-requested child job per terminal source job."""
    request_id = f"continuation:{source_job_id}"
    if (not _reference_id(source_job_id) or set(body) != {"requestId", "sessionId"}
            or body.get("requestId") != request_id or not _reference_id(body.get("sessionId"))):
        raise BridgeError("invalid_continuation_request", "续作请求标识与原任务不一致，请刷新后重试。")
    source = invoke_job(connection, "read", job_id=source_job_id)["job"]
    if source.get("project_id") != connection.project_id or source.get("session_id") != body["sessionId"]:
        raise BridgeError("invalid_scope", "只能继续当前项目中所选会话的原任务。", 403)
    if source.get("status") not in _CONTINUATION_TERMINAL:
        raise BridgeError("continuation_source_active", "原任务尚在运行，请等待或先停止该任务。", 409)
    existing = invoke_job(connection, "list", request_id=request_id)["jobs"]
    if existing:
        job = existing[0]
        prior = (job.get("context") or {}).get("capture_continuation") or {}
        if (prior.get("source_job_id") != source_job_id or job.get("session_id") != body["sessionId"]
                or job.get("attachments_manifest") != []):
            raise BridgeError("idempotency_conflict", "该续作标识已被其他任务使用。", 409)
        return {"job": job, "duplicate": True}
    context = capture_continuation_context(connection, source)
    context["client_session_id"] = body["sessionId"]
    text = ("继续整理原任务尚未完成的来源。这是用户明确发起的新任务，原任务和原回执保持不变。"
        "复用已保存转写，不重新上传或OCR。先读取当前已有对象和原文块，再按来源补缺；"
        "事件引用过的块仍可能含未整理事实，不能仅凭条数、旧游标或旧答复判断完整。"
        "来源版本有变时重新核对，保留已有纠错。小批保存后读回，交付真实对象及未完成范围。\n\n"
        "原任务目标：\n" + str(source.get("text") or "")[:8000])
    return invoke_job(connection, "submit", request_id=request_id, text=text,
        session_id=body["sessionId"], attachments_manifest=[], context=context)


def submit_agent_job(connection: CoreConnection, config: ModelConfig, body: dict) -> dict:
    request_id = body.get("requestId")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
        raise BridgeError("invalid_request_id", "任务需要稳定的 requestId（最多 128 字符）。")
    payload = validate_session_payload({k: v for k, v in body.items() if k != "requestId"})
    manifest = []
    for item in payload["attachments"]:
        try:
            raw = base64.b64decode(item["data_b64"], validate=True)
        except (ValueError, TypeError):
            raise BridgeError("invalid_attachment", "附件不是有效的 base64 数据。") from None
        if len(raw) > 20 * 1024 * 1024:
            raise BridgeError("attachment_too_large", "每个附件最多 20 MB。", 413)
        manifest.append({"name": item["name"], "mime": item["mime"], "size": len(raw),
                         "sha256": hashlib.sha256(raw).hexdigest()})
    _pi_node()
    previous = invoke_job(connection, "list", request_id=request_id)["jobs"]
    if previous:
        job = previous[0]
        if (job["text"] != payload["text"] or job.get("attachments_manifest") != manifest
                or job.get("context", {}).get("client_session_id") != payload["session_id"]):
            raise BridgeError("idempotency_conflict", "同一 requestId 已用于不同内容，请创建新任务。", 409)
        return {"job": job, "duplicate": True}
    # Original bytes reach Core before acknowledgement; parsing belongs to the
    # worker. Stable upload operations also cover a lost submission response.
    staged = stage_agent_attachments(connection, config, payload["attachments"],
                                     preprocess=False, request_id=request_id)
    if staged["failed_attachments"]:
        raise BridgeError("attachment_save_failed", "部分原件未能保存，请保留附件并重试；已保存的原件会复用。", 502)
    return invoke_job(connection, "submit", request_id=request_id, text=payload["text"],
        session_id=payload["session_id"], attachments_manifest=manifest,
        context={"pages": staged["pages"], "object_refs": staged["object_refs"],
                 "client_session_id": payload["session_id"]})


def public_job_read(result: dict) -> dict:
    job = dict(result["job"])
    job["events"] = result.get("events", [])
    job["last_event_seq"] = result.get("last_seq", 0)
    return {"job": job}


class AgentJobWorker:
    """Browser-independent scheduler. Core owns durable state and lease fencing."""

    def __init__(self):
        self.worker_id = "bridge_" + secrets.token_hex(16)
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.contexts: dict[CoreConnection, ModelConfig] = {}
        self.active: dict[tuple[CoreConnection, str], threading.Event] = {}
        self.thread = threading.Thread(target=self._loop, daemon=True, name="chemclaw-job-worker")
        self.thread.start()

    def register(self, connection, model):
        if connection is None or model is None:
            return
        with self.lock:
            self.contexts[connection] = model
        self.wake.set()

    def cancel(self, connection, job_id):
        with self.lock:
            event = self.active.get((connection, job_id))
        if event:
            event.set()

    def unregister(self, connection):
        with self.lock:
            self.contexts.pop(connection, None)
            for (bound, _), event in self.active.items():
                if bound == connection:
                    event.set()

    def close(self):
        self.stopping.set()
        self.wake.set()
        with self.lock:
            for event in self.active.values():
                event.set()
        self.thread.join(timeout=6)

    def _loop(self):
        while not self.stopping.is_set():
            self.wake.clear()
            with self.lock:
                contexts = list(self.contexts.items())
            for connection, model in contexts:
                if self.stopping.is_set():
                    return
                try:
                    jobs = invoke_job(connection, "list", statuses=["queued", "running"], limit=200)["jobs"]
                    # Preserve submission order when several turns are queued.
                    for job in reversed(jobs):
                        if self.stopping.is_set():
                            return
                        with self.lock:
                            if self.contexts.get(connection) is not model:
                                break
                        try:
                            claim = invoke_job(connection, "claim", job_id=job["job_id"],
                                worker_id=self.worker_id, lease_seconds=60)
                        except BridgeError:
                            continue
                        try:
                            self._execute(connection, model, claim)
                        except Exception:
                            # A malformed job must never stop the scheduler for
                            # every other user. Its lease remains recoverable.
                            continue
                except (BridgeError, KeyError, TypeError, ValueError):
                    # No credentials or queue state are copied to disk. Once
                    # Core/configuration returns, the same jobs can be claimed.
                    continue
            self.wake.wait(2)

    def _execute(self, connection, model, claim):
        job, started = claim["job"], claim["started"]
        job_id = job["job_id"]
        cancelled, finished = threading.Event(), threading.Event()
        with self.lock:
            if self.contexts.get(connection) is not model or self.stopping.is_set():
                return
            self.active[(connection, job_id)] = cancelled
        fence = {"job_id": job_id, "worker_id": self.worker_id,
                 "claim_epoch": started["claim_epoch"]}

        def heartbeat():
            while not finished.wait(15):
                if cancelled.is_set() or self.stopping.is_set():
                    return
                try:
                    invoke_job(connection, "heartbeat", **fence, lease_seconds=60)
                except BridgeError:
                    # Stop the model on uncertain ownership; an expired lease
                    # is recovered through Core with the existing checkpoints.
                    cancelled.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            job_context = job.get("context") or {}
            if job_context.get("capture_continuation"):
                try:
                    source_id = job_context["capture_continuation"].get("source_job_id")
                    source = invoke_job(connection, "read", job_id=source_id)["job"]
                    if (source.get("project_id") != connection.project_id
                            or source.get("session_id") != job.get("session_id")
                            or source.get("status") not in _CONTINUATION_TERMINAL
                            or job.get("request_id") != f"continuation:{source_id}"):
                        raise BridgeError("invalid_scope", "续作任务与原会话不一致。", 403)
                    # Sources/events may change while this job waits in the
                    # queue. Rebuild again; old cursor claims never authorize a
                    # write and a missing transcript never triggers another OCR.
                    refreshed = capture_continuation_context(connection, source)
                    job_context = {**job_context, **refreshed}
                    job["context"] = job_context
                except BridgeError as exc:
                    invoke_job(connection, "complete", **fence, result={"status": "partial",
                        "text": "续作来源需要核查，本轮未调用模型或重新OCR。", "error_code": exc.code,
                        "warnings": [exc.code]})
                    return
            pages, refs = job_context.get("pages", []), job_context.get("object_refs", [])
            if (not isinstance(pages, list) or not isinstance(refs, list)
                    or any(not isinstance(page, dict) or not isinstance(page.get("page_id"), str)
                           or not isinstance(page.get("mime"), str) for page in pages)):
                invoke_job(connection, "complete", **fence, result={"status": "failed",
                    "text": "任务附件引用格式不正确，请重新提交。", "warnings": ["invalid_job_context"]})
                return
            staged = {"pages": pages, "object_refs": refs,
                "warnings": [], "failed_attachments": [], "provider_requests": []}
            for page in staged["pages"]:
                if cancelled.is_set() or self.stopping.is_set():
                    return
                try:
                    head = invoke_read(connection, "evidence.page.read", {"page_id": page["page_id"]})
                    saved_page = head.get("page", head)
                    if job_context.get("capture_continuation"):
                        if (saved_page.get("project_id") != connection.project_id
                                or not (saved_page.get("transcript") or {}).get("active_revision")):
                            raise BridgeError("continuation_transcript_missing", "续作原转写不可用。", 409)
                        continue
                    if (saved_page.get("transcript") or {}).get("active_run_id"):
                        continue
                    if page["mime"] in ("text/plain", "text/markdown"):
                        invoke_job(connection, "preprocess", **fence,
                            capability_id="evidence.transcribe.run",
                            params={"page_id": page["page_id"], "provider": "text_input"},
                            operation_id=f"jobparse:{job_id}:{page['page_id']}")
                    elif page["mime"].startswith("image/"):
                        ocr = vision_ocr_page(connection, model, page["page_id"],
                            operation_id=f"jobparse:{job_id}:{page['page_id']}",
                            job_fence=fence, cancelled=cancelled, audit_run_id=started.get("run_id"))
                        staged["provider_requests"].extend(ocr.get("provider_requests", []))
                    else:
                        staged["warnings"].append(f"parser_not_configured:{page['page_id']}")
                except (BridgeError, ModelError) as exc:
                    if job_context.get("capture_continuation"):
                        invoke_job(connection, "complete", **fence, result={"status": "partial",
                            "text": "续作来源需要核查，本轮未调用模型或重新OCR。", "error_code": exc.code,
                            "warnings": [exc.code]})
                        return
                    staged["warnings"].append(f"attachment_processing_failed:{page['page_id']}:{exc.code}")
                    if getattr(exc, "provider_observation", None):
                        staged["provider_requests"].append(exc.provider_observation)
                    if exc.code == "provider_quota_exhausted":
                        invoke_job(connection, "complete", **fence,
                            result=_provider_quota_result(staged, getattr(exc, "provider_observation", None)))
                        return
            if cancelled.is_set() or self.stopping.is_set():
                return
            # run tokens are transport-only, including resumed child tokens.
            credentials = [connection.api_key, model.api_key, model.vision_api_key or "", started["run_token"]]
            credentials.extend(run.get("run_token", "") for run in claim.get("resume", {}).get("runs", {}).values())
            try:
                runtime = run_pi_agent(connection, model, started, job["text"], {
                    "project_id": connection.project_id, "task_id": started["task_id"],
                    "run_id": started["run_id"], **job.get("context", {}),
                }, durable=True, resume=claim.get("resume"), cancel_event=cancelled)
            except TerminalRuntimeError as exc:
                runtime = _terminal_runtime_fallback(exc.code, started, claim.get("resume"))
            if cancelled.is_set() or self.stopping.is_set():
                return
            runtime = _agent_public(runtime, tuple(credentials))
            requests = _provider_rows(staged["provider_requests"], runtime.get("provider_requests", []),
                runtime.get("data", {}).get("provider_requests", []))
            refs = staged["object_refs"] + runtime.get("object_refs", [])
            result = {**runtime, "status": runtime.get("status", "completed"),
                "object_refs": list({(r.get("kind"), r.get("id")): r for r in refs
                    if isinstance(r, dict) and r.get("id")}.values()),
                "warnings": staged["warnings"] + runtime.get("warnings", []),
                "data": {**runtime.get("data", {}), "pages": staged["pages"],
                         "provider_requests": requests, "provider_usage": _provider_summary(requests),
                         "toolCalls": runtime.get("toolCalls", [])},
                "runtime": "pi", "modelUsed": True,
                "model": {"name": model.model, "baseUrl": model.base_url}}
            if runtime.get("error"):
                result["warnings"].append(str(runtime["error"]))
                result.pop("error", None)
            if staged["warnings"] and result["status"] == "completed":
                result["status"] = "partial"
            try:
                invoke_job(connection, "complete", **fence, result=result)
            except BridgeError as exc:
                terminal = runtime.get("runtime_error")
                code = terminal.get("code") if isinstance(terminal, dict) else None
                if not (isinstance(code, str) and code in _TERMINAL_RUNTIME_ERRORS
                        and terminal.get("retryable") is False
                        and getattr(exc, "core_status", None) == 400
                        and getattr(exc, "core_code", None) == "schema_violation"):
                    raise
                # Core explicitly rejected this result before committing it.
                # One small completion under the same fence is safe; uncertain
                # transport losses and ownership conflicts must remain resumable.
                fallback = _terminal_runtime_fallback(code, started, claim.get("resume"),
                    status=result.get("status"))
                invoke_job(connection, "complete", **fence, result=fallback)
        except (BridgeError, ModelError, OSError):
            # A lost host/HTTP response may hide successful writes. Keep the
            # lease recoverable; only a resumed journal decides what to replay.
            pass
        finally:
            finished.set()
            with self.lock:
                self.active.pop((connection, job_id), None)


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        directory: Path,
        *,
        default_connection: CoreConnection | None = None,
        default_model: ModelConfig | None = None,
    ):
        if not ipaddress.ip_address(address[0]).is_loopback:
            raise ValueError("Frontend bridge must bind to loopback")
        self.directory = directory.resolve()
        self.sessions: dict[str, BrowserSession] = {}
        self.sessions_lock = threading.Lock()
        self.default_connection = default_connection
        self.default_model = default_model
        super().__init__(address, BridgeHandler)
        self.jobs = AgentJobWorker()
        self.jobs.register(default_connection, default_model)

    def server_close(self):
        self.jobs.close()
        super().server_close()


class BridgeHandler(BaseHTTPRequestHandler):
    server: BridgeServer
    server_version = "ChemClawLocal/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, format, *args):
        # Do not log URLs, query text, cookies, headers, credentials, or bodies.
        pass

    def _headers(self, status: int, content_type: str, size: int):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def _json(self, value: dict, status: int = 200, cookie: str | None = None):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(raw))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def _guard_origin(self):
        host = self.headers.get("Host", "")
        port = self.server.server_port
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        if host not in allowed_hosts:
            raise BridgeError("invalid_host", "本地服务仅接受本机地址。", 403)
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{host}":
            raise BridgeError("invalid_origin", "跨站请求被拒绝。", 403)
        if self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
            raise BridgeError("invalid_origin", "跨站请求被拒绝。", 403)
        if self.command == "POST" and origin != f"http://{host}":
            raise BridgeError("invalid_origin", "修改本地连接需要同源请求。", 403)

    def _cookie_id(self) -> str | None:
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get(COOKIE_NAME)
            return morsel.value if morsel else None
        except Exception:
            return None

    def _session(self, *, create: bool = False) -> tuple[str, BrowserSession, bool]:
        now = time.monotonic()
        session_id = self._cookie_id()
        with self.server.sessions_lock:
            for key in [key for key, item in self.server.sessions.items() if item.expires < now]:
                del self.server.sessions[key]
            session = self.server.sessions.get(session_id or "")
            created = False
            if session is None and create:
                if len(self.server.sessions) >= 128:
                    raise BridgeError("session_limit", "本地会话已满；请稍后重试。", 503)
                session_id = secrets.token_urlsafe(32)
                session = BrowserSession(secrets.token_urlsafe(32), now + SESSION_TTL)
                if self.server.default_connection is not None:
                    session.connection = self.server.default_connection
                if self.server.default_model is not None:
                    session.model = self.server.default_model
                self.server.sessions[session_id] = session
                created = True
            if session is None:
                raise BridgeError("session_expired", "本地连接会话已过期；请重新连接。", 401)
            if not create and not secrets.compare_digest(self.headers.get("X-ChemClaw-Session", ""), session.csrf):
                raise BridgeError("session_required", "缺少本地会话校验。", 403)
            session.expires = now + SESSION_TTL
        return session_id, session, created

    def _connection(self) -> CoreConnection:
        _, session, _ = self._session()
        if session.connection is None:
            raise BridgeError("not_connected", "请先连接本机 Core。", 409)
        return session.connection

    def _read_body(self, max_bytes: int = MAX_BODY) -> dict:
        if self.headers.get("Transfer-Encoding"):
            raise BridgeError("invalid_body", "不接受分块请求。", 400)
        if self.headers.get_content_type() != "application/json":
            raise BridgeError("invalid_content_type", "请求必须使用 JSON。", 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise BridgeError("invalid_body", "请求长度不正确。") from None
        if not 1 <= length <= max_bytes:
            raise BridgeError("body_too_large", "请求体超过大小限制。", 413)
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            raise BridgeError("invalid_json", "请求必须是有效 JSON。") from None
        if not isinstance(body, dict):
            raise BridgeError("invalid_json", "请求内容必须是 JSON 对象。")
        return body

    def do_GET(self):
        try:
            self._guard_origin()
            path = urlsplit(self.path)
            if path.path == "/api/session":
                session_id, session, created = self._session(create=True)
                cookie = None
                if created:
                    cookie = f"{COOKIE_NAME}={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}"
                self._json({
                    "bridge": "chemclaw_read_bridge_v1",
                    "csrfToken": session.csrf,
                    "connected": session.connection is not None,
                    "projectId": session.connection.project_id if session.connection else None,
                    "modelConfigured": session.model is not None,
                    "modelName": session.model.model if session.model else None,
                    "visionModel": session.model.vision_model if session.model else None,
                }, cookie=cookie)
                return
            if path.path == "/api/bootstrap" and not path.query:
                _, session, _ = self._session()
                if session.connection is None:
                    self._json({
                        "connected": False,
                        "modelConfigured": session.model is not None,
                        "modelName": session.model.model if session.model else None,
                    })
                    return
                info = describe_connection(session.connection)
                self._json({
                    "connected": True,
                    **info,
                    "modelConfigured": session.model is not None,
                    "modelName": session.model.model if session.model else None,
                    "visionModel": session.model.vision_model if session.model else None,
                    "modelBaseUrl": session.model.base_url if session.model else None,
                    "readOnly": True,
                    "inventoryScope": "lab_shared",
                })
                return
            if path.path == "/api/model/status" and not path.query:
                _, session, _ = self._session()
                self._json(public_model_config(session.model))
                return
            if path.path.startswith("/api/"):
                connection = self._connection()
                if path.path == "/api/core/jobs" and not path.query:
                    self._json(invoke_job(connection, "list"))
                elif re.fullmatch(r"/api/core/jobs/[A-Za-z0-9_-]+", path.path):
                    query = parse_qs(path.query, keep_blank_values=True)
                    if set(query) - {"after"} or len(query.get("after", ["0"])) != 1:
                        raise BridgeError("invalid_query", "只接受 after 事件序号。")
                    raw_after = query.get("after", ["0"])[0]
                    if not re.fullmatch(r"[0-9]{1,12}", raw_after):
                        raise BridgeError("invalid_query", "after 必须是非负整数。")
                    self._json(public_job_read(invoke_job(connection, "read",
                        job_id=path.path.rsplit("/", 1)[1], after_seq=int(raw_after))))
                elif path.path == "/api/core/health" and not path.query:
                    self._json({"health": core_request(connection, "/health"), "projectId": connection.project_id, "readOnly": True})
                elif path.path == "/api/core/inventory" and not path.query:
                    result = invoke_read(connection, "inventory.query.list", {"kind": "chemicals", "project_id": connection.project_id})
                    self._json({**result, "projectId": connection.project_id, "scope": "lab_shared", "readOnly": True})
                elif path.path == "/api/core/tasks" and not path.query:
                    result = core_request(connection, "/v2/tasks")
                    rows = result.get("tasks")
                    if not isinstance(rows, list):
                        raise BridgeError("core_invalid_response", "Core 任务列表格式不兼容。", 502)
                    self._json({"tasks": [row for row in rows if isinstance(row, dict) and row.get("project_id") == connection.project_id],
                                "projectId": connection.project_id, "readOnly": True})
                elif path.path == "/api/core/search":
                    query = parse_qs(path.query, keep_blank_values=True)
                    text = _one_query(query, "q")
                    result = invoke_read(connection, "query.events.search", {"project_ids": [connection.project_id], "text": text, "limit": 50})
                    self._json({**result, "projectId": connection.project_id, "readOnly": True})
                elif path.path == "/api/core/evidence" and not path.query:
                    result = invoke_read(connection, "evidence.pages.list", {"project_ids": [connection.project_id]})
                    self._json({**result, "projectId": connection.project_id, "readOnly": True})
                elif path.path == "/api/core/page/preview":
                    query = parse_qs(path.query, keep_blank_values=True)
                    page_id = _one_query(query, "page_id", max_length=128)
                    result = invoke_read(connection, "evidence.page.preview", {"page_id": page_id})
                    self._json({**result, "projectId": connection.project_id, "readOnly": True})
                elif path.path == "/api/core/proposal":
                    query = parse_qs(path.query, keep_blank_values=True)
                    proposal_id = _one_query(query, "proposal_id", max_length=128)
                    result = invoke_read(connection, "memory.proposal.read", {"proposal_id": proposal_id})
                    self._json({**result, "projectId": connection.project_id, "readOnly": True})
                elif path.path == "/api/core/samples" and not path.query:
                    result = invoke_read(connection, "query.samples.list", {"project_ids": [connection.project_id]})
                    self._json({**result, "projectId": connection.project_id, "readOnly": True})
                elif path.path == "/api/core/timeline":
                    query = parse_qs(path.query, keep_blank_values=True)
                    sample_id = _one_query(query, "sample_id", max_length=128)
                    result = invoke_read(connection, "query.sample.timeline", {"sample_id": sample_id})
                    self._json({**result, "projectId": connection.project_id, "readOnly": True})
                elif path.path == "/api/core/event":
                    query = parse_qs(path.query, keep_blank_values=True)
                    if set(query) - {"event_id", "history"}:
                        raise BridgeError("invalid_search", "请求包含未支持的参数。")
                    values = query.get("event_id")
                    if values is None or len(values) != 1:
                        raise BridgeError("invalid_search", "缺少参数 event_id。")
                    event_id = values[0].strip()
                    if not event_id or len(event_id) > 128:
                        raise BridgeError("invalid_search", "event_id 长度不正确。")
                    history = query.get("history", ["0"])[0]
                    if history not in {"0", "1"}:
                        raise BridgeError("invalid_search", "history 只能是 0 或 1。")
                    result = invoke_read(connection, "memory.event.read", {
                        "event_id": event_id,
                        "include_history": history == "1",
                    })
                    self._json({**result, "projectId": connection.project_id, "readOnly": True})
                elif path.path == "/api/core/page":
                    query = parse_qs(path.query, keep_blank_values=True)
                    page_id = _one_query(query, "page_id", max_length=128)
                    result = invoke_read(connection, "evidence.page.read", {"page_id": page_id})
                    self._json({**result, "projectId": connection.project_id, "readOnly": True})
                elif path.path == "/api/core/answer":
                    query = parse_qs(path.query, keep_blank_values=True)
                    question = _one_query(query, "q")
                    result = invoke_read(connection, "query.answer.build", {
                        "project_ids": [connection.project_id],
                        "question": question,
                    })
                    self._json({**result, "projectId": connection.project_id, "readOnly": True})
                elif path.path == "/api/core/resources" and not path.query:
                    result = invoke_read(connection, "resource.list", {"project_ids": [connection.project_id]})
                    self._json({**result, "projectId": connection.project_id, "readOnly": True})
                elif path.path == "/api/core/bookings":
                    query = parse_qs(path.query, keep_blank_values=True)
                    status = None
                    if "status" in query:
                        status = _one_query(query, "status", max_length=32)
                    elif query:
                        raise BridgeError("invalid_search", "请求包含未支持的参数。")
                    params = {"project_ids": [connection.project_id]}
                    if status:
                        params["status"] = status
                    result = invoke_read(connection, "resource.booking.status", params)
                    self._json({**result, "projectId": connection.project_id, "readOnly": True})
                elif path.path == "/api/core/providers" and not path.query:
                    result = invoke_read(connection, "evidence.provider.list", {})
                    self._json({**result, "projectId": connection.project_id, "readOnly": True})
                else:
                    raise BridgeError("route_not_found", "本地桥未开放此接口。", 404)
                return
            self._serve_static(path.path)
        except BridgeError as exc:
            self._json({"error": {"code": exc.code, "message": exc.message}}, exc.status)
        except Exception:
            self._json({"error": {"code": "bridge_error", "message": "本地桥处理失败。"}}, 500)

    def do_POST(self):
        try:
            self._guard_origin()
            path = urlsplit(self.path)
            allowed_posts = {
                "/api/connect", "/api/disconnect", "/api/core/session/message",
                "/api/core/ocr", "/api/core/review", "/api/core/event/correct", "/api/core/transcript/correct",
                "/api/model/configure", "/api/model/clear",
                "/api/core/jobs",
            }
            cancel_match = re.fullmatch(r"/api/core/jobs/([A-Za-z0-9_-]+)/cancel", path.path)
            continue_match = re.fullmatch(r"/api/core/jobs/([A-Za-z0-9_-]+)/continue", path.path)
            if path.query or (path.path not in allowed_posts and not cancel_match and not continue_match):
                raise BridgeError("write_disabled", "本地桥未开放此写入接口。", 403)
            session_id, session, _ = self._session()
            body = self._read_body(
                MAX_SESSION_BODY if path.path in {"/api/core/session/message", "/api/core/jobs"}
                else 256 * 1024 if path.path in {"/api/core/event/correct", "/api/core/transcript/correct"} else MAX_BODY
            )
            if path.path == "/api/core/jobs":
                connection = self._connection()
                if session.model is None:
                    raise BridgeError("model_not_configured", "请先配置模型。", 409)
                generation = session.generation
                result = submit_agent_job(connection, session.model, body)
                self.server.jobs.register(connection, session.model)
                if session.generation != generation or session.connection is not connection:
                    raise BridgeError("connection_superseded", "任务已保存在原项目，请返回原项目查看。", 409)
                self._json(result, 202)
                return
            if continue_match:
                connection = self._connection()
                if session.model is None:
                    raise BridgeError("model_not_configured", "请先配置模型。", 409)
                _pi_node()
                generation = session.generation
                result = submit_capture_continuation(connection, body, continue_match[1])
                self.server.jobs.register(connection, session.model)
                if session.generation != generation or session.connection is not connection:
                    raise BridgeError("connection_superseded", "续作已保存在原项目，请返回原项目查看。", 409)
                self._json(result, 202)
                return
            if cancel_match:
                if body:
                    raise BridgeError("invalid_body", "取消任务不需要参数。")
                connection = self._connection()
                result = invoke_job(connection, "cancel", job_id=cancel_match[1])
                self.server.jobs.cancel(connection, cancel_match[1])
                self._json(result)
                return
            if path.path == "/api/model/clear":
                if body:
                    raise BridgeError("invalid_body", "清除模型不需要参数。")
                self.server.jobs.unregister(session.connection)
                session.model = None
                session.generation += 1
                self._json({"configured": False})
                return
            if path.path == "/api/model/configure":
                generation, previous = session.generation, session.model
                config = validate_model_config(body, self.server.server_port, previous)
                try:
                    tests = probe_model(config)
                except ModelError as exc:
                    raise BridgeError(exc.code, exc.message, exc.status) from None
                if session.generation != generation or session.model is not previous:
                    raise BridgeError("connection_superseded", "模型测试期间配置已改变，请重新提交。", 409)
                config = replace(config, vision_test=tests["visionTest"])
                session.model = config
                session.generation += 1
                self.server.jobs.register(session.connection, config)
                self._json({**public_model_config(config), **tests})
                return
            if path.path == "/api/core/review":
                connection = self._connection()
                result = invoke_review_action(connection, body)
                self._json({**result, "projectId": connection.project_id})
                return
            if path.path in {"/api/core/event/correct", "/api/core/transcript/correct"}:
                connection = self._connection()
                result = invoke_knowledge_correction(connection, body, transcript=path.path.endswith("/transcript/correct"))
                self._json({**result, "projectId": connection.project_id})
                return
            if path.path == "/api/core/ocr":
                connection = self._connection()
                if session.model is None:
                    raise BridgeError(
                        "model_not_configured",
                        "请先在设置中配置支持视觉的模型。", 409,
                    )
                if set(body) != {"pageId"} or not isinstance(body.get("pageId"), str):
                    raise BridgeError("invalid_body", "OCR 请求只接受 pageId。")
                ocr = vision_ocr_page(connection, session.model, body["pageId"])
                try:
                    knowledge = queue_page_knowledge(connection, body["pageId"])
                except BridgeError:
                    self._json({**ocr, "pageId": body["pageId"], "knowledgeQueued": False,
                        "warning": "转写已保存；后台整理未能提交，可在任务会话中继续整理该页。"})
                    return
                self._json({
                    **ocr,
                    "knowledgeQueued": True, "knowledgeJob": knowledge.get("job"),
                    "pageId": body["pageId"],
                    "model": session.model.vision_model or session.model.model,
                })
                return
            if path.path == "/api/core/session/message":
                connection = self._connection()
                payload = validate_session_payload(body)
                generation = session.generation
                if session.model is not None:
                    result = process_agent_session(connection, session.model, payload)
                else:
                    result = invoke_write(
                        connection, "assistant.task.process",
                        {"project_id": connection.project_id, **payload},
                    )
                    result["modelUsed"] = False
                    result["runtime"] = "core"
                if generation != session.generation or session.connection is not connection:
                    raise BridgeError(
                        "connection_superseded",
                        "连接已切换。原任务的结果保存在原项目中，请返回该项目查看。", 409,
                    )
                self._json({
                    **result,
                    "projectId": connection.project_id,
                    "readOnly": False,
                    "writeScope": "controlled_session",
                })
                return
            if path.path == "/api/disconnect":
                if body:
                    raise BridgeError("invalid_body", "断开请求不需要参数。")
                with self.server.sessions_lock:
                    session.connection = None
                    session.generation += 1
                self._json({"connected": False})
                return
            connection = validate_connection(body, self.server.server_port)
            with self.server.sessions_lock:
                session.generation += 1
                generation = session.generation
            info = describe_connection(connection)
            with self.server.sessions_lock:
                if self.server.sessions.get(session_id) is not session:
                    raise BridgeError("session_expired", "会话已过期。", 401)
                if generation != session.generation:
                    raise BridgeError("connection_superseded", "连接请求已被新的连接或断开操作替代。", 409)
                session.connection = connection
            self.server.jobs.register(connection, session.model)
            self._json({
                "connected": True,
                **info,
                "modelConfigured": session.model is not None,
                "modelName": session.model.model if session.model else None,
                "visionModel": session.model.vision_model if session.model else None,
                "readOnly": True,
                "inventoryScope": "lab_shared",
            })
        except BridgeError as exc:
            self._json({"error": {"code": exc.code, "message": exc.message}}, exc.status)
        except Exception:
            self._json({"error": {"code": "bridge_error", "message": "本地桥处理失败。"}}, 500)

    def _serve_static(self, request_path: str):
        path = unquote(request_path)
        parts = Path(path.lstrip("/")).parts
        if any(part.startswith(".") for part in parts) or "\\" in path or "\x00" in path:
            raise BridgeError("file_not_found", "文件不存在。", 404)
        target = (self.server.directory / (path.lstrip("/") or "index.html")).resolve()
        if not target.is_relative_to(self.server.directory) or not target.is_file():
            raise BridgeError("file_not_found", "文件不存在。", 404)
        raw = target.read_bytes()
        mime = STATIC_MIME_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if mime in {"text/html", "text/css", "text/javascript", "application/javascript", "image/svg+xml"}:
            mime += "; charset=utf-8"
        self._headers(200, mime, len(raw))
        self.end_headers()
        self.wfile.write(raw)


def main():
    parser = argparse.ArgumentParser(description="ChemClaw local frontend and read-only Core bridge")
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--directory", type=Path, default=Path(__file__).parent / "dist")
    args = parser.parse_args()
    server = BridgeServer(("127.0.0.1", args.port), args.directory)
    print(f"ChemClaw frontend: http://127.0.0.1:{server.server_port} (local read bridge)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.sessions.clear()


if __name__ == "__main__":
    main()
