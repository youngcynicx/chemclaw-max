"""Production-shaped local worker (repair round R11/E01/E02, offline level).

This module is the missing consumer between durable ingress and the existing
Core/outbox machinery:

    channel webhook / capability ingress
        -> inbound_events (queued)
        -> worker.process_inbound_once (this module)
        -> Core capability (inventory.message.process / query answer)
        -> outbox row with the exact channel account + reply thread
        -> worker.process_outbox_once
        -> platform adapter (webhook_test / QQ dry-run)

It is deliberately deterministic and model-free. It is an L1 local worker,
not a substitute for the Pi/B/Alice live runner: no OCR, no LLM, no real
platform credentials. Every business write still goes through
``capabilities.invoke_capability``; the worker never writes fact tables
directly.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import answers, auth, capabilities, channels, deliveries, tasks
from .db import connect
from .service_config import ServiceSettings, default_service_settings

INVENTORY_PATTERN = re.compile(
    r"(取用|用了|使用|称取|加入|加了|添加|消耗|used|use\s|consume|add\s)",
    re.IGNORECASE,
)
QUERY_PATTERN = re.compile(
    r"(多少|库存|查询|还剩|剩余|用量|情况|是否|\?|？|how\s+much|stock|remaining)",
    re.IGNORECASE,
)


class WorkerError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass
class WorkerStats:
    inbound_seen: int = 0
    inbound_processed: int = 0
    inbound_clarified: int = 0
    outbox: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "inbound_seen": self.inbound_seen,
            "inbound_processed": self.inbound_processed,
            "inbound_clarified": self.inbound_clarified,
            "outbox": self.outbox,
        }


def _principal_context(conn: sqlite3.Connection, principal_id: str) -> auth.PrincipalContext:
    return auth.load_principal_context(conn, principal_id)


def _choose_project(context: auth.PrincipalContext, payload: dict[str, Any]) -> str | None:
    explicit = payload.get("project_id")
    if isinstance(explicit, str) and explicit and explicit in context.memberships:
        return explicit
    if explicit:
        return None
    projects = auth.visible_project_ids(context)
    # Only an unambiguous single-membership sender may omit the project. We
    # never infer a project from recent chat history.
    return projects[0] if len(projects) == 1 else None


def _identity_still_valid(
    conn: sqlite3.Connection,
    *,
    channel_account_id: str | None,
    platform_user_id: str | None,
    principal_id: str,
) -> bool:
    """Revoked/remapped channel identities cannot continue queued work."""
    if not channel_account_id or not platform_user_id:
        # Non-channel inbound (CLI/API) has no channel identity to re-check.
        return True
    row = conn.execute(
        """
        SELECT principal_id FROM channel_identities
        WHERE channel_account_id = ? AND platform_user_id = ? AND revoked_at IS NULL
        """,
        (channel_account_id, platform_user_id),
    ).fetchone()
    return row is not None and row["principal_id"] == principal_id


def _channel_account_id(source: str, payload: dict[str, Any]) -> str | None:
    explicit = payload.get("channel_account_id")
    if isinstance(explicit, str) and explicit:
        return explicit
    if source.startswith("channel:"):
        return source.split(":", 1)[1] or None
    return None


def _reply_target(payload: dict[str, Any]) -> dict[str, Any]:
    thread = payload.get("platform_thread_id") or payload.get("thread_id")
    return {
        "channel_account_id": payload.get("channel_account_id"),
        "reply_to_thread": thread,
        "platform_thread_id": thread,
        "recipient_id": payload.get("recipient_id") or payload.get("platform_user_id"),
        "reply_to_message_id": payload.get("platform_message_id"),
    }


def _enqueue_reply(
    conn: sqlite3.Connection,
    *,
    inbound_id: str,
    payload: dict[str, Any],
    text: str,
    task_id: str | None = None,
) -> str:
    # One inbound event has at most ONE logical reply. A restart after the
    # business commit must not mint a second reply (§R11).
    existing = conn.execute(
        "SELECT outbox_id FROM outbox "
        "WHERE aggregate_type = 'inbound_event' AND aggregate_id = ? "
        "AND message_type = 'channel_reply' ORDER BY rowid LIMIT 1",
        (inbound_id,),
    ).fetchone()
    if existing is not None:
        return existing["outbox_id"]
    target = _reply_target(payload)
    reply_payload = {
        "text": text,
        "source_inbound_id": inbound_id,
        **{key: value for key, value in target.items() if value is not None},
    }
    if task_id:
        reply_payload["task_id"] = task_id
    outbox = tasks.enqueue_outbox(
        conn,
        aggregate_type="inbound_event",
        aggregate_id=inbound_id,
        message_type="channel_reply",
        payload=reply_payload,
    )
    conn.execute(
        "UPDATE outbox SET trace_id = ? WHERE outbox_id = ?",
        (f"inbound:{inbound_id}", outbox["outbox_id"]),
    )
    return outbox["outbox_id"]


def _text_result(result: dict[str, Any]) -> str:
    status = result.get("action_status")
    event = (result.get("result") or {}).get("event") or {}
    audit = (result.get("result") or {}).get("audit") or {}
    if status == "applied":
        return "记录已保存。"
    if status in ("clarification_needed", "proposed"):
        reason = audit.get("required_action") or "需要人工核对"
        return f"记录已保存，但{reason}。"
    if status == "rejected":
        return "记录未应用：策略拒绝。"
    return f"记录状态：{status or 'unknown'}。"


def process_inbound_once(
    db_path: str,
    *,
    services: ServiceSettings | None = None,
    limit: int = 20,
) -> WorkerStats:
    """Consume queued inbound events through the SAME Core entry.

    Each inbound row is processed with a stable logical operation id
    (``inbound:<inbound_id>``); a crash between the business commit and
    ``mark_inbound_processed`` therefore replays the receipt instead of
    applying the usage twice.
    """
    settings = services or default_service_settings()
    stats = WorkerStats()
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM inbound_events WHERE status = 'queued' "
            "ORDER BY rowid LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        stats.inbound_seen += 1
        inbound = dict(row)
        payload = json.loads(inbound["payload_json"] or "{}")
        account_id = _channel_account_id(inbound["source"], payload)
        if account_id:
            payload.setdefault("channel_account_id", account_id)

        conn = connect(db_path)
        try:
            principal_id = inbound.get("principal_id")
            try:
                context = _principal_context(conn, principal_id) if principal_id else None
            except auth.AuthError:
                context = None
            if context is None:
                _enqueue_reply(
                    conn, inbound_id=inbound["inbound_id"], payload=payload,
                    text="消息作者未绑定到有效主体，已归档，未处理。",
                )
                tasks.mark_inbound_processed(conn, inbound["inbound_id"], failed=True)
                conn.execute(
                    "UPDATE inbound_events SET payload_json = ? WHERE inbound_id = ?",
                    (
                        json.dumps({**payload, "worker_error": "sender_unbound"},
                                   ensure_ascii=False),
                        inbound["inbound_id"],
                    ),
                )
                conn.commit()
                stats.inbound_clarified += 1
                continue

            if not _identity_still_valid(
                conn,
                channel_account_id=account_id,
                platform_user_id=payload.get("platform_user_id"),
                principal_id=context.principal_id,
            ):
                _enqueue_reply(
                    conn, inbound_id=inbound["inbound_id"], payload=payload,
                    text="消息作者的通道绑定已撤销，消息不再处理；请重新绑定后发送。",
                )
                tasks.mark_inbound_processed(conn, inbound["inbound_id"], failed=True)
                conn.execute(
                    "UPDATE inbound_events SET payload_json = ? WHERE inbound_id = ?",
                    (
                        json.dumps({**payload, "worker_error": "identity_revoked"},
                                   ensure_ascii=False),
                        inbound["inbound_id"],
                    ),
                )
                conn.commit()
                stats.inbound_clarified += 1
                continue

            text = str(payload.get("text") or "").strip()
            project_id = _choose_project(context, payload)
            if not text:
                _enqueue_reply(
                    conn, inbound_id=inbound["inbound_id"], payload=payload,
                    text="消息没有可处理的文字内容；图片/文件需要纸页流程（待验证）。",
                )
                tasks.mark_inbound_processed(conn, inbound["inbound_id"], failed=True)
                conn.commit()
                stats.inbound_clarified += 1
                continue
            if project_id is None:
                _enqueue_reply(
                    conn, inbound_id=inbound["inbound_id"], payload=payload,
                    text="无法确定这条消息属于哪个项目，请明确项目后再发送。",
                )
                tasks.mark_inbound_processed(conn, inbound["inbound_id"], failed=True)
                conn.commit()
                stats.inbound_clarified += 1
                continue

            operation_id = f"inbound:{inbound['inbound_id']}"
            if QUERY_PATTERN.search(text) and not INVENTORY_PATTERN.search(text):
                answer = answers.build_answer(
                    conn, context, text, project_ids=[project_id]
                )
                reply = answer.get("rendered_text") or "没有可用回答。"
                if answer.get("status") in ("needs_review", "unknown"):
                    reply = f"[需要核对] {reply}"
                _enqueue_reply(
                    conn, inbound_id=inbound["inbound_id"], payload=payload, text=reply
                )
                tasks.mark_inbound_processed(conn, inbound["inbound_id"])
                conn.commit()
                stats.inbound_processed += 1
                continue

            if INVENTORY_PATTERN.search(text):
                try:
                    envelope = capabilities.invoke_capability(
                        db_path,
                        context,
                        "inventory.message.process",
                        {"project_id": project_id, "raw_text": text},
                        operation_id=operation_id,
                        services=settings,
                    )
                except (capabilities.CapabilityError, auth.AuthError) as exc:
                    code = getattr(exc, "code", "capability_error")
                    _enqueue_reply(
                        conn, inbound_id=inbound["inbound_id"], payload=payload,
                        text=f"记录未应用：{code}。请人工核对。",
                    )
                    tasks.mark_inbound_processed(conn, inbound["inbound_id"], failed=True)
                    conn.commit()
                    stats.inbound_clarified += 1
                    continue
                _enqueue_reply(
                    conn, inbound_id=inbound["inbound_id"], payload=payload,
                    text=_text_result(envelope),
                )
                tasks.mark_inbound_processed(conn, inbound["inbound_id"])
                conn.commit()
                stats.inbound_processed += 1
                continue

            _enqueue_reply(
                conn, inbound_id=inbound["inbound_id"], payload=payload,
                text="这条消息不是可识别的库存操作或查询；已保留原文等待人工处理。",
            )
            tasks.mark_inbound_processed(conn, inbound["inbound_id"], failed=True)
            conn.commit()
            stats.inbound_clarified += 1
        finally:
            conn.close()
    return stats


def _adapter_for_account(db_path: str) -> Callable[[str], Any]:
    cache: dict[str, Any] = {}

    def factory(channel_account_id: str) -> Any:
        if channel_account_id in cache:
            return cache[channel_account_id]
        conn = connect(db_path)
        try:
            account = channels.get_channel_account(conn, channel_account_id)
        finally:
            conn.close()
        adapter = channels.adapter_for_platform(account["platform"], account)
        cache[channel_account_id] = adapter
        return adapter

    return factory


def process_outbox_once(
    db_path: str,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        stats = deliveries.process_outbox_once(
            conn,
            adapter=deliveries.EchoAdapter(),
            adapter_factory=_adapter_for_account(db_path),
            target_from_payload="reply_to_thread",
            content_gate=channels.payload_content_gate,
            limit=limit,
        )
        conn.commit()
        return stats
    finally:
        conn.close()


def write_health(path: str | Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)


def run_worker(
    db_path: str,
    *,
    services: ServiceSettings | None = None,
    poll_seconds: float = 1.0,
    once: bool = False,
    stop_file: str | Path | None = None,
    health_file: str | Path | None = None,
) -> dict[str, Any]:
    """Run the resident worker. ``once=True`` is used by smoke tests."""
    stop_path = Path(stop_file) if stop_file else None
    total = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "passes": 0,
        "inbound_seen": 0,
        "inbound_processed": 0,
        "inbound_clarified": 0,
        "outbox": {},
    }
    while True:
        if stop_path is not None and stop_path.exists():
            break
        inbound = process_inbound_once(db_path, services=services)
        outbox = process_outbox_once(db_path)
        total["passes"] += 1
        total["inbound_seen"] += inbound.inbound_seen
        total["inbound_processed"] += inbound.inbound_processed
        total["inbound_clarified"] += inbound.inbound_clarified
        total["outbox"] = outbox
        write_health(
            health_file,
            {
                **total,
                "db": str(db_path),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "mode": "once" if once else "continuous",
            },
        )
        if once:
            break
        time.sleep(max(0.1, poll_seconds))
    total["stopped_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return total
