"""S10: messaging channels (plan §16) — unified envelope over real platforms.

Design invariants (§16, all enforced here):

- The channel layer is an ORIGINAL LEDGER plus a mapping layer; message caches
  and channel state never replace Core idempotency (§16.7). Business facts
  flow through the same ``inbound_events`` queue as every other source.
- The raw event is persisted BEFORE any processing (§16.6 outage rule), so an
  outage never loses an original and a redelivery never double-applies.
- Dedupe root: ``UNIQUE(channel_account_id, platform_message_id)`` — the same
  external event seen via two channel accounts stays namespace-separated.
- Authors are only ever processed after an explicit, revocable
  ``channel_identities`` binding made by a trusted admin. Group/peer
  relations NEVER grant project access (§16.3); a bound author still only
  gets the projects their principal's memberships allow (Core re-checks).
- Recalls and edits APPEND review state (status change + review task);
  audited business history is never rewritten or deleted (§16.4).
- Delivery is decoupled from business results (S06/S08 semantics): send
  retries consume the EXISTING result and never re-trigger an agent.

Real platform adapters implement the same ``DeliveryAdapter`` protocol used
since S06 (deliveries.py). ``WebhookTestAdapter`` is the local test platform;
``QQBotAdapter`` is the QQ official-bot skeleton (credentials via env refs,
dry-run until an authorized test channel is configured).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import tasks

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


class ChannelError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


VALID_KINDS = ("message", "edited", "recalled", "receipt")


# ---------------------------------------------------------------------------
# Admin configuration (capabilities call these inside their transaction)
# ---------------------------------------------------------------------------

def register_channel_account(
    conn: sqlite3.Connection,
    *,
    platform: str,
    display_name: str | None = None,
    credential_ref: str | None = None,
    verify_secret_ref: str | None = None,
    allow_group_threads: bool = False,
    created_by: str | None = None,
    channel_account_id: str | None = None,
) -> dict[str, Any]:
    """Register a channel endpoint. Credentials are stored as REFERENCES
    (env-var / secret-store names) — plaintext secrets are never accepted."""
    if platform not in ("webhook_test", "qq_bot", "wecom_bot", "local"):
        raise ChannelError(
            "schema_violation",
            f"Unknown platform {platform!r}; expected webhook_test|qq_bot|wecom_bot|local.",
        )
    if credential_ref and ("=" in credential_ref or len(credential_ref) > 200):
        raise ChannelError(
            "schema_violation",
            "credential_ref must be a reference name (e.g. an env var), never a secret.",
        )
    account_id = channel_account_id or f"chacc_{os.urandom(6).hex()}"
    conn.execute(
        """
        INSERT INTO channel_accounts
          (channel_account_id, platform, display_name, credential_ref,
           verify_secret_ref, allow_group_threads, enabled, created_by)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            account_id, platform, display_name, credential_ref, verify_secret_ref,
            1 if allow_group_threads else 0, created_by,
        ),
    )
    row = conn.execute(
        "SELECT * FROM channel_accounts WHERE channel_account_id = ?", (account_id,)
    ).fetchone()
    return dict(row)


def get_channel_account(conn: sqlite3.Connection, channel_account_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM channel_accounts WHERE channel_account_id = ?",
        (channel_account_id,),
    ).fetchone()
    if row is None:
        raise ChannelError("not_found", f"Unknown channel account {channel_account_id!r}.", http_status=404)
    return dict(row)


def bind_channel_identity(
    conn: sqlite3.Connection,
    *,
    channel_account_id: str,
    platform_user_id: str,
    principal_id: str,
    bound_by: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    """Explicit platform-author -> principal binding (revocable). This is the
    ONLY way a channel author gains processing access; group membership or a
    peer chat never auto-binds (§16.3)."""
    account = get_channel_account(conn, channel_account_id)
    principal = conn.execute(
        "SELECT principal_id FROM principals WHERE principal_id = ?", (principal_id,)
    ).fetchone()
    if principal is None:
        raise ChannelError(
            "invalid_state", f"Unknown principal {principal_id!r}; bind only real principals.", http_status=404
        )
    binding_id = f"chbind_{os.urandom(6).hex()}"
    conn.execute(
        """
        INSERT INTO channel_identities
          (binding_id, channel_account_id, platform_user_id, principal_id,
           display_name, bound_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (binding_id, account["channel_account_id"], platform_user_id,
         principal_id, display_name, bound_by),
    )
    row = conn.execute(
        "SELECT * FROM channel_identities WHERE binding_id = ?", (binding_id,)
    ).fetchone()
    return dict(row)


def revoke_channel_identity(conn: sqlite3.Connection, binding_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM channel_identities WHERE binding_id = ?", (binding_id,)
    ).fetchone()
    if row is None:
        raise ChannelError("not_found", f"Unknown binding {binding_id!r}.", http_status=404)
    conn.execute(
        "UPDATE channel_identities SET revoked_at = CURRENT_TIMESTAMP WHERE binding_id = ?",
        (binding_id,),
    )
    return dict(
        conn.execute("SELECT * FROM channel_identities WHERE binding_id = ?", (binding_id,)).fetchone()
    )


def _active_identity(
    conn: sqlite3.Connection, channel_account_id: str, platform_user_id: str | None
) -> dict[str, Any] | None:
    if not platform_user_id:
        return None
    row = conn.execute(
        """
        SELECT * FROM channel_identities
        WHERE channel_account_id = ? AND platform_user_id = ? AND revoked_at IS NULL
        """,
        (channel_account_id, platform_user_id),
    ).fetchone()
    return dict(row) if row is not None else None


def list_channel_messages(
    conn: sqlite3.Connection,
    *,
    channel_account_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Audit read over the raw channel ledger (§16.7: identities and origins
    stay lookable)."""
    sql = "SELECT * FROM channel_messages WHERE 1=1"
    args: list[Any] = []
    if channel_account_id is not None:
        sql += " AND channel_account_id = ?"
        args.append(channel_account_id)
    if status is not None:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY received_at, rowid LIMIT ?"
    args.append(limit)
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


# ---------------------------------------------------------------------------
# Attachments: originals hit disk BEFORE any processing (§16.6)
# ---------------------------------------------------------------------------

def _decode_attachment_bytes(item: dict[str, Any]) -> bytes | None:
    """Decode one attachment item without ever substituting b'' for bad input.

    Supported shapes:
    - ``data`` bytes (capability adapter / internal callers)
    - ``data_b64`` base64 text (HTTP webhook and capability API)
    - no bytes but ``platform_reference`` (deferred download: allowed, saved
      as pending metadata — never as an empty original)
    """
    data = item.get("data")
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, str):
        try:
            return base64.b64decode(data, validate=True)
        except Exception as exc:
            raise ChannelError(
                "invalid_attachment_encoding",
                f"attachment data is neither bytes nor valid base64: {exc}",
            ) from exc
    data_b64 = item.get("data_b64")
    if isinstance(data_b64, str):
        try:
            return base64.b64decode(data_b64, validate=True)
        except Exception as exc:
            raise ChannelError(
                "invalid_attachment_encoding",
                f"attachment.data_b64 is not valid base64: {exc}",
            ) from exc
    if item.get("platform_reference"):
        return None
    raise ChannelError(
        "missing_attachment_bytes",
        "attachment must carry data/data_b64 or a platform_reference.",
    )


def normalize_attachments(event: dict[str, Any]) -> dict[str, Any]:
    """One shared attachment gate for HTTP and capability ingress (§R08).

    Decodes bytes, rejects empty/oversized/type-conflicting payloads and
    returns a deep copy. Both entry points MUST call this before persisting
    the event, so the two paths cannot diverge.
    """
    if not isinstance(event, dict):
        raise ChannelError("schema_violation", "event must be an object.")
    attachments = event.get("attachments")
    if attachments is None:
        return event
    if not isinstance(attachments, list):
        raise ChannelError("schema_violation", "event.attachments must be an array.")
    normalized: list[dict[str, Any]] = []
    for item in attachments:
        if not isinstance(item, dict):
            raise ChannelError("schema_violation", "event.attachments items must be objects.")
        data = _decode_attachment_bytes(item)
        declared_mime = item.get("mime")
        if data is None:
            # Platform reference: keep the reference and its expiry; the
            # original is explicitly pending, not an empty file.
            meta = dict(item)
            meta["download_status"] = "pending"
            normalized.append(meta)
            continue
        if len(data) == 0:
            raise ChannelError(
                "empty_attachment",
                f"attachment {item.get('name', 'attachment')!r} has zero bytes.",
            )
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ChannelError(
                "attachment_too_large",
                f"attachment {item.get('name', 'attachment')!r} exceeds "
                f"{MAX_ATTACHMENT_BYTES} bytes.",
            )
        from .ingest import sniff_mime

        sniffed = sniff_mime(data, declared_mime, item.get("name"))
        if (
            isinstance(declared_mime, str)
            and declared_mime
            and declared_mime != "application/octet-stream"
            and sniffed != declared_mime
            and not (declared_mime == "image/jpg" and sniffed == "image/jpeg")
        ):
            raise ChannelError(
                "mime_conflict",
                f"attachment declares {declared_mime!r} but content looks like {sniffed!r}.",
            )
        normalized.append(
            {
                "name": item.get("name", "attachment"),
                "mime": sniffed or declared_mime,
                "data": data,
                "declared_mime": declared_mime,
                "platform_reference": item.get("platform_reference"),
            }
        )
    copied = dict(event)
    copied["attachments"] = normalized
    return copied


def save_attachment(
    storage_root: Path,
    channel_account_id: str,
    *,
    name: str,
    mime: str | None,
    data: bytes,
) -> dict[str, Any]:
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise ChannelError(
            "empty_attachment", f"attachment {name!r} has no verified bytes."
        )
    data = bytes(data)
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name or "attachment")[-120:]
    digest = hashlib.sha256(data).hexdigest()
    directory = (Path(storage_root) / "channel_attachments" / channel_account_id).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = (directory / f"{digest}_{safe_name}").resolve()
    if directory not in path.parents:
        raise ChannelError("schema_violation", "attachment path escaped the storage root.")
    if path.exists():
        # Sha256-named: redelivery reuses the verified original. If the bytes
        # on disk no longer match, that is a real integrity failure.
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ChannelError(
                "attachment_integrity_failure",
                f"stored attachment {path.name!r} no longer matches its digest.",
            )
    else:
        temp = directory / f".{digest}.{os.getpid()}.tmp"
        try:
            with open(temp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)
    return {
        "name": name,
        "mime": mime,
        "path": str(path),
        "bytes": len(data),
        "sha256": digest,
    }


# ---------------------------------------------------------------------------
# Inbound webhook: unified envelope ingest (the §16.2 mapping)
# ---------------------------------------------------------------------------

def verify_webhook_signature(
    account: dict[str, Any],
    *,
    body: bytes,
    signature: str | None,
) -> None:
    """HMAC-SHA256 over the raw body with the account's verify secret.

    The secret comes from the env var named by ``verify_secret_ref`` — it is
    never stored in the database. Accounts without a verify_secret_ref (local
    test accounts) skip verification deliberately and are not for production.
    """
    platform = account.get("platform")
    if platform in ("qq_bot", "wecom_bot"):
        # A custom HMAC is NOT the official QQ/WeCom signature contract.
        # Until the official receive route is implemented and verified, do
        # not accept a lookalike scheme.
        raise ChannelError(
            "not_implemented",
            f"Official {platform} webhook verification/receive route is not implemented.",
            http_status=501,
        )
    ref = account.get("verify_secret_ref")
    if not ref:
        # A production platform without a verification secret is never
        # enabled: silently skipping signature checks is a security hole.
        # Only the explicitly local/test webhook may run unsigned.
        if account.get("platform") in ("webhook_test", "local"):
            return
        raise ChannelError(
            "unauthorized",
            f"Channel account {account.get('channel_account_id')!r} has no "
            "verify_secret_ref; production platforms require signature verification.",
            http_status=401,
        )
    secret = os.environ.get(ref, "")
    if not secret:
        raise ChannelError("unauthorized", f"Verify secret {ref!r} is not configured.", http_status=401)
    if not signature:
        raise ChannelError("unauthorized", "Missing X-Channel-Signature header.", http_status=401)
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise ChannelError("unauthorized", "Webhook signature mismatch.", http_status=401)


def _ledger_safe_event(event: dict[str, Any]) -> dict[str, Any]:
    """Copy of the event safe for the raw ledger: decoded attachment bytes are
    replaced by their size (the original bytes live on disk, sha256-named)."""
    safe = dict(event)
    attachments = safe.get("attachments")
    if isinstance(attachments, list):
        stripped = []
        for item in attachments:
            if isinstance(item, dict) and "data" in item:
                meta = {k: v for k, v in item.items() if k != "data"}
                meta["data_bytes"] = len(item["data"])
                stripped.append(meta)
            else:
                stripped.append(item)
        safe["attachments"] = stripped
    return safe


def ingest_channel_event(
    conn: sqlite3.Connection,
    *,
    channel_account_id: str,
    event: dict[str, Any],
    storage_root: Path,
) -> dict[str, Any]:
    """One platform event -> the unified envelope. Idempotent per
    (channel_account_id, platform_message_id); originals persist first."""
    account = get_channel_account(conn, channel_account_id)
    if not account["enabled"]:
        raise ChannelError("invalid_state", f"Channel account {channel_account_id!r} is disabled.", http_status=409)

    # One shared decode/validation path for HTTP, capability and adapter
    # ingress (§R08/F10). Never fall back to b'' for missing bytes.
    event = normalize_attachments(event)

    platform_message_id = event.get("platform_message_id")
    kind = event.get("kind", "message")
    if not platform_message_id or not isinstance(platform_message_id, str):
        raise ChannelError("schema_violation", "event.platform_message_id is required.")
    if kind not in VALID_KINDS:
        raise ChannelError("schema_violation", f"event.kind must be one of {VALID_KINDS}.")

    # --- dedupe first: a redelivered webhook is ACKed with the original row.
    existing = conn.execute(
        "SELECT * FROM channel_messages WHERE channel_account_id = ? AND platform_message_id = ?",
        (channel_account_id, platform_message_id),
    ).fetchone()
    if existing is not None:
        result = dict(existing)
        result["dedupe"] = "duplicate"
        return result

    channel_message_id = f"chmsg_{os.urandom(6).hex()}"
    raw_json = json.dumps(_ledger_safe_event(event), sort_keys=True, ensure_ascii=False)

    # --- receipt: transport ACK for something we sent. Mapped onto the
    # delivery by its transport event id; never a business fact (§16.7).
    if kind == "receipt":
        conn.execute(
            """
            INSERT INTO channel_messages
              (channel_message_id, channel_account_id, platform_message_id,
               platform_thread_id, platform_user_id, kind, status, raw_json)
            VALUES (?, ?, ?, ?, ?, 'receipt', 'processed', ?)
            """,
            (channel_message_id, channel_account_id, platform_message_id,
             event.get("platform_thread_id"), event.get("platform_user_id"), raw_json),
        )
        delivery = _map_receipt(conn, channel_account_id, event)
        row = dict(conn.execute(
            "SELECT * FROM channel_messages WHERE channel_message_id = ?", (channel_message_id,)
        ).fetchone())
        row["delivery"] = delivery
        return row

    # --- recall: the sender withdrew a message. We mark the ORIGINAL row
    # recalled, keep every column, and open a human review task (§16.4).
    if kind == "recalled":
        recall_of = event.get("recall_of_message_id")
        original = conn.execute(
            "SELECT * FROM channel_messages WHERE channel_account_id = ? AND platform_message_id = ?",
            (channel_account_id, recall_of),
        ).fetchone() if recall_of else None
        actor = _active_identity(conn, channel_account_id, event.get("platform_user_id"))
        if original is None:
            conn.execute(
                """
                INSERT INTO channel_messages
                  (channel_message_id, channel_account_id, platform_message_id,
                   platform_thread_id, platform_user_id, kind, status, raw_json,
                   recall_of_message_id, error_code)
                VALUES (?, ?, ?, ?, ?, 'recalled', 'rejected', ?, ?, 'unknown_recall_target')
                """,
                (channel_message_id, channel_account_id, platform_message_id,
                 event.get("platform_thread_id"), event.get("platform_user_id"),
                 raw_json, recall_of),
            )
        elif actor is None or actor["principal_id"] != original["author_principal_id"]:
            # A sender can only recall their OWN message. The victim's author
            # is never rewritten; the forged event is kept for audit.
            conn.execute(
                """
                INSERT INTO channel_messages
                  (channel_message_id, channel_account_id, platform_message_id,
                   platform_thread_id, platform_user_id, actor_principal_id,
                   kind, status, raw_json, recall_of_message_id, error_code)
                VALUES (?, ?, ?, ?, ?, ?, 'recalled', 'rejected', ?, ?, ?)
                """,
                (channel_message_id, channel_account_id, platform_message_id,
                 event.get("platform_thread_id"), event.get("platform_user_id"),
                 actor["principal_id"] if actor else None, raw_json, recall_of,
                 "author_mismatch" if actor else "sender_unbound"),
            )
        else:
            conn.execute(
                "UPDATE channel_messages SET status = 'recalled' WHERE channel_message_id = ?",
                (original["channel_message_id"],),
            )
            conn.execute(
                """
                INSERT INTO channel_messages
                  (channel_message_id, channel_account_id, platform_message_id,
                   platform_thread_id, platform_user_id, actor_principal_id,
                   kind, status, raw_json, recall_of_message_id)
                VALUES (?, ?, ?, ?, ?, ?, 'recalled', 'processed', ?, ?)
                """,
                (channel_message_id, channel_account_id, platform_message_id,
                 event.get("platform_thread_id"), event.get("platform_user_id"),
                 actor["principal_id"], raw_json, recall_of),
            )
            task = tasks.create_task(
                conn,
                task_type="channel_recall_review",
                principal_id=account.get("created_by") or account["channel_account_id"],
                title=f"Channel message recalled: {recall_of}",
            )
            conn.execute(
                "UPDATE channel_messages SET inbound_id = ? WHERE channel_message_id = ?",
                (task["task_id"], channel_message_id),
            )
        row = dict(conn.execute(
            "SELECT * FROM channel_messages WHERE channel_message_id = ?", (channel_message_id,)
        ).fetchone())
        row["review_task_id"] = row.get("inbound_id") if row["status"] == "processed" else None
        return row

    # --- edited: a NEW event row linked to the original; the original text
    # stays untouched (audit history is append-only) and a review task is
    # opened because earlier processing may now be stale.
    if kind == "edited":
        edits_original = event.get("edits_original_id")
        original = conn.execute(
            "SELECT * FROM channel_messages WHERE channel_account_id = ? AND platform_message_id = ?",
            (channel_account_id, edits_original),
        ).fetchone() if edits_original else None
        actor = _active_identity(conn, channel_account_id, event.get("platform_user_id"))
        if original is None:
            conn.execute(
                """
                INSERT INTO channel_messages
                  (channel_message_id, channel_account_id, platform_message_id,
                   platform_thread_id, platform_user_id, kind, status, raw_json,
                   error_code)
                VALUES (?, ?, ?, ?, ?, 'edited', 'rejected', ?, 'unknown_edit_target')
                """,
                (channel_message_id, channel_account_id, platform_message_id,
                 event.get("platform_thread_id"), event.get("platform_user_id"), raw_json),
            )
            row = dict(conn.execute(
                "SELECT * FROM channel_messages WHERE channel_message_id = ?", (channel_message_id,)
            ).fetchone())
            return row
        if actor is None or actor["principal_id"] != original["author_principal_id"]:
            conn.execute(
                """
                INSERT INTO channel_messages
                  (channel_message_id, channel_account_id, platform_message_id,
                   platform_thread_id, platform_user_id, actor_principal_id,
                   kind, status, raw_json, edits_original_id, error_code)
                VALUES (?, ?, ?, ?, ?, ?, 'edited', 'rejected', ?, ?, ?)
                """,
                (channel_message_id, channel_account_id, platform_message_id,
                 event.get("platform_thread_id"), event.get("platform_user_id"),
                 actor["principal_id"] if actor else None, raw_json,
                 original["channel_message_id"],
                 "author_mismatch" if actor else "sender_unbound"),
            )
            row = dict(conn.execute(
                "SELECT * FROM channel_messages WHERE channel_message_id = ?", (channel_message_id,)
            ).fetchone())
            return row
        conn.execute(
            """
            INSERT INTO channel_messages
              (channel_message_id, channel_account_id, platform_message_id,
               platform_thread_id, platform_user_id, author_principal_id,
               actor_principal_id, kind, text_content, status, raw_json,
               edits_original_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'edited', ?, 'processed', ?, ?)
            """,
            (channel_message_id, channel_account_id, platform_message_id,
             event.get("platform_thread_id"), event.get("platform_user_id"),
             actor["principal_id"], actor["principal_id"], event.get("text"), raw_json,
             original["channel_message_id"]),
        )
        task = tasks.create_task(
            conn,
            task_type="channel_edit_review",
            principal_id=account.get("created_by") or account["channel_account_id"],
            title=f"Channel message edited: {edits_original}",
        )
        conn.execute(
            "UPDATE channel_messages SET inbound_id = ? WHERE channel_message_id = ?",
            (task["task_id"], channel_message_id),
        )
        row = dict(conn.execute(
            "SELECT * FROM channel_messages WHERE channel_message_id = ?", (channel_message_id,)
        ).fetchone())
        row["review_task_id"] = task["task_id"]
        return row

    # --- plain message: identity gate FIRST, then attachments, then the
    # durable business ingress (inbound_events) — all in the caller's
    # transaction so an outage rolls back cleanly with the original intact.
    is_group = bool(event.get("is_group"))
    if is_group and not account["allow_group_threads"]:
        conn.execute(
            """
            INSERT INTO channel_messages
              (channel_message_id, channel_account_id, platform_message_id,
               platform_thread_id, platform_user_id, kind, status, raw_json,
               error_code)
            VALUES (?, ?, ?, ?, ?, 'message', 'rejected', ?, 'group_not_allowed')
            """,
            (channel_message_id, channel_account_id, platform_message_id,
             event.get("platform_thread_id"), event.get("platform_user_id"), raw_json),
        )
        return dict(conn.execute(
            "SELECT * FROM channel_messages WHERE channel_message_id = ?", (channel_message_id,)
        ).fetchone())

    binding = conn.execute(
        """
        SELECT * FROM channel_identities
        WHERE channel_account_id = ? AND platform_user_id = ? AND revoked_at IS NULL
        """,
        (channel_account_id, event.get("platform_user_id")),
    ).fetchone()
    if binding is None:
        # Unmapped authors are archived but NEVER processed (§16.3) — no
        # guessed identity, no guest execution, no auto-binding.
        conn.execute(
            """
            INSERT INTO channel_messages
              (channel_message_id, channel_account_id, platform_message_id,
               platform_thread_id, platform_user_id, kind, status, raw_json,
               error_code)
            VALUES (?, ?, ?, ?, ?, 'message', 'unmapped', ?, 'sender_unbound')
            """,
            (channel_message_id, channel_account_id, platform_message_id,
             event.get("platform_thread_id"), event.get("platform_user_id"), raw_json),
        )
        return dict(conn.execute(
            "SELECT * FROM channel_messages WHERE channel_message_id = ?", (channel_message_id,)
        ).fetchone())

    principal_id = binding["principal_id"]

    attachments_json = None
    attachments = event.get("attachments") or []
    if attachments:
        saved = []
        for item in attachments:
            data = item.get("data")
            if data is None:
                saved.append(
                    {
                        "name": item.get("name", "attachment"),
                        "mime": item.get("mime"),
                        "platform_reference": item.get("platform_reference"),
                        "download_status": "pending",
                    }
                )
                continue
            meta = save_attachment(
                storage_root, channel_account_id,
                name=item.get("name", "attachment"),
                mime=item.get("mime"),
                data=data,
            )
            meta["download_status"] = "ready"
            saved.append(meta)
        attachments_json = json.dumps(saved, ensure_ascii=False)

    inbound = tasks.enqueue_inbound(
        conn,
        source=f"channel:{channel_account_id}",
        transport_event_id=platform_message_id,
        external_thread=event.get("platform_thread_id"),
        principal_id=principal_id,
        payload={
            "channel": account["platform"],
            "channel_account_id": channel_account_id,
            "channel_message_id": channel_message_id,
            "platform_message_id": platform_message_id,
            "platform_user_id": event.get("platform_user_id"),
            "platform_thread_id": event.get("platform_thread_id"),
            "is_group": is_group,
            "text": event.get("text"),
            "attachments": json.loads(attachments_json) if attachments_json else [],
        },
    )
    deduped = inbound.get("dedupe") == "duplicate"
    conn.execute(
        """
        INSERT INTO channel_messages
          (channel_message_id, channel_account_id, platform_message_id,
           platform_thread_id, platform_user_id, author_principal_id, kind,
           text_content, attachments_json, status, raw_json, inbound_id)
        VALUES (?, ?, ?, ?, ?, ?, 'message', ?, ?, ?, ?, ?)
        """,
        (
            channel_message_id, channel_account_id, platform_message_id,
            event.get("platform_thread_id"), event.get("platform_user_id"),
            principal_id, event.get("text"), attachments_json,
            "deduplicated" if deduped else "processed", raw_json,
            inbound["inbound_id"],
        ),
    )
    row = dict(conn.execute(
        "SELECT * FROM channel_messages WHERE channel_message_id = ?", (channel_message_id,)
    ).fetchone())
    row["dedupe"] = inbound.get("dedupe", "queued")
    return row


def _map_receipt(
    conn: sqlite3.Connection, channel_account_id: str, event: dict[str, Any]
) -> dict[str, Any] | None:
    """Receipt -> delivery mapping with monotonic state rules (§R09/F12).

    ``receipt_for`` carries the transport event id the adapter returned when
    sending. A receipt never re-runs business logic. Late/failed receipts
    cannot downgrade an already delivered message: that conflict is recorded
    for reconciliation instead of blindly assigning ``status=payload.state``.
    """
    receipt_for = event.get("receipt_for")
    if not receipt_for:
        return None
    row = conn.execute(
        """
        SELECT * FROM deliveries
        WHERE transport_event_id = ? AND channel_account_id = ?
        ORDER BY rowid DESC LIMIT 1
        """,
        (receipt_for, channel_account_id),
    ).fetchone()
    if row is None:
        return None
    receipt_state = str(event.get("state") or "delivered").lower()
    current_state = row["state"] or ("delivered" if row["status"] == "delivered" else "queued")
    if receipt_state in ("delivered", "read", "received"):
        conn.execute(
            """
            UPDATE deliveries
            SET status = 'delivered', state = 'delivered',
                delivered_at = COALESCE(delivered_at, CURRENT_TIMESTAMP),
                receipt_json = ?
            WHERE delivery_id = ?
            """,
            (json.dumps(event, sort_keys=True, ensure_ascii=False), row["delivery_id"]),
        )
    elif receipt_state in ("failed", "rejected", "bounced"):
        if current_state in ("delivered", "accepted"):
            # The platform says failed after it already accepted the message.
            # Do not overwrite the success evidence; park the conflict.
            conn.execute(
                """
                UPDATE deliveries
                SET state = 'delivery_unknown', error_code = 'receipt_conflict',
                    last_error = 'late failure receipt after accepted/delivered',
                    receipt_json = ?
                WHERE delivery_id = ?
                """,
                (json.dumps(event, sort_keys=True, ensure_ascii=False), row["delivery_id"]),
            )
        else:
            conn.execute(
                """
                UPDATE deliveries
                SET status = 'failed', state = 'delivery_unknown',
                    error_code = 'platform_reported_failure',
                    next_retry_at = NULL, receipt_json = ?
                WHERE delivery_id = ?
                """,
                (json.dumps(event, sort_keys=True, ensure_ascii=False), row["delivery_id"]),
            )
    else:
        conn.execute(
            "UPDATE deliveries SET receipt_json = ? WHERE delivery_id = ?",
            (json.dumps(event, sort_keys=True, ensure_ascii=False), row["delivery_id"]),
        )
    return dict(
        conn.execute(
            "SELECT * FROM deliveries WHERE delivery_id = ?", (row["delivery_id"],)
        ).fetchone()
    )


# ---------------------------------------------------------------------------
# Outbound: content review gate + platform adapters
# ---------------------------------------------------------------------------

def payload_content_gate(payload: dict[str, Any]) -> dict[str, Any]:
    """Worker-facing gate: the delivery worker hands this the full payload;
    review runs over the outgoing text."""
    return review_outbound_content(payload.get("text") if isinstance(payload, dict) else None)


_DEFAULT_REVIEW_PATTERNS = (
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"disregard\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"出题|绕过|忽略(所有|之前|上述)?(安全|指令|检查)",
)

REVIEW_PATTERNS: tuple[str, ...] = _DEFAULT_REVIEW_PATTERNS


def review_outbound_content(
    text: str | None,
    *,
    extra_patterns: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Content gate before the FIRST bot reply (§16.5). A held reply is a
    permanent delivery failure for that attempt — a human reviews it; the
    worker never silently drops or auto-retries it."""
    patterns = REVIEW_PATTERNS + tuple(extra_patterns)
    if not text:
        return {"held": False}
    for pattern in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return {"held": True, "reason": f"matched content review pattern: {pattern}"}
    return {"held": False}


@dataclass
class WebhookTestAdapter:
    """Local test platform behind the DeliveryAdapter protocol. Records every
    send with a platform-style message id; pair with FaultInjectionAdapter
    for outage drills (S08 pattern)."""

    name: str = "webhook_test"
    sent: list[dict[str, Any]] = field(default_factory=list)
    _counter: int = 0

    def send(self, *, message_type: str, payload: dict[str, Any], target: str) -> str:
        self._counter += 1
        platform_message_id = f"wt-{self._counter:06d}"
        self.sent.append(
            {"platform_message_id": platform_message_id, "message_type": message_type,
             "target": target, "payload": payload}
        )
        return platform_message_id


def _delivery_send_error(
    message: str, *, transient: bool, uncertain: bool = False
) -> Exception:
    """Deferred DeliverySendError construction (import cycle: deliveries ->
    agents -> capabilities -> channels)."""
    from .deliveries import DeliverySendError

    return DeliverySendError(message, transient=transient, uncertain=uncertain)


@dataclass
class QQBotAdapter:
    """QQ official open-platform bot skeleton (plan §16.1: verify the real
    authorized path at implementation time; never assume a production
    connection exists).

    Credentials are read from the env var named by ``credential_ref`` at
    runtime — ``<REF>_APP_ID`` and ``<REF>`` (secret). ``dry_run=True``
    (default) records sends locally and NEVER touches the network, so tests
    stay offline; flipping it on requires a configured, authorized test
    channel (operator decision, §16.5)."""

    name: str = "qq_bot"
    credential_ref: str = "CHEMCLAW_QQBOT"
    dry_run: bool = True
    api_base: str = "https://api.sgroup.qq.com"
    sent: list[dict[str, Any]] = field(default_factory=list)
    _counter: int = 0

    def _credentials(self) -> tuple[str, str]:
        app_id = os.environ.get(f"{self.credential_ref}_APP_ID", "")
        secret = os.environ.get(self.credential_ref, "")
        if not app_id or not secret:
            raise _delivery_send_error(
                f"QQ bot credentials not configured ({self.credential_ref}_APP_ID/{self.credential_ref}).",
                transient=False,
            )
        return app_id, secret

    def send(self, *, message_type: str, payload: dict[str, Any], target: str) -> str:
        gate = review_outbound_content(payload.get("text"))
        if gate.get("held"):
            raise _delivery_send_error(
                f"content review held: {gate.get('reason')}", transient=False)
        self._counter += 1
        if self.dry_run:
            platform_message_id = f"qqdry-{self._counter:06d}"
            self.sent.append(
                {"platform_message_id": platform_message_id, "message_type": message_type,
                 "target": target, "payload": payload}
            )
            return platform_message_id
        app_id, secret = self._credentials()
        recipient = str(payload.get("recipient_id") or payload.get("reply_to") or target)
        if recipient.startswith("group:"):
            path = f"/v2/groups/{recipient.split(':', 1)[1]}/messages"
        elif recipient.startswith("user:"):
            path = f"/v2/users/{recipient.split(':', 1)[1]}/messages"
        else:
            raise _delivery_send_error(
                "qq recipient must be an explicit user:<openid> or group:<group_openid> "
                "route; refusing to guess",
                transient=False,
            )
        body = {
            "content": payload.get("text", ""),
            "msg_type": 0,
            "msg_id": payload.get("platform_message_id") or payload.get("reply_to_message_id"),
        }
        request = urllib.request.Request(
            f"{self.api_base}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"QQBot {self._access_token(app_id, secret)}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # A 5xx may have been produced after accepting the message; a 4xx
            # is a definite rejection.
            raise _delivery_send_error(
                f"qq send failed: HTTP {exc.code}",
                transient=500 <= exc.code < 600 or exc.code == 429,
                uncertain=500 <= exc.code < 600,
            ) from exc
        except urllib.error.URLError as exc:
            raise _delivery_send_error(
                f"qq unreachable: {exc.reason}", transient=True, uncertain=True
            ) from exc
        platform_message_id = str(result.get("id") or result.get("data", {}).get("message_id") or "")
        if not platform_message_id:
            # Never fabricate a qq-* receipt. The request may have succeeded.
            raise _delivery_send_error(
                "qq response missing a real message id; delivery outcome unknown",
                transient=True,
                uncertain=True,
            )
        self.sent.append(
            {"platform_message_id": platform_message_id, "message_type": message_type,
             "target": target, "payload": payload}
        )
        return platform_message_id

    def _access_token(self, app_id: str, secret: str) -> str:
        # Official QQ bot token endpoint (separate host from the API base).
        request = urllib.request.Request(
            "https://bots.qq.com/app/getAppAccessToken",
            data=json.dumps({"appId": app_id, "clientSecret": secret}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise _delivery_send_error(f"qq token request failed: {exc}", transient=True) from exc
        token = result.get("access_token")
        if not token:
            raise _delivery_send_error("qq token response missing access_token", transient=True)
        return token


def adapter_for_platform(platform: str, account: dict[str, Any]) -> Any:
    """Adapter factory: platform -> sender. webhook_test stays in-process;
    qq_bot/wecom_bot start in dry-run until an operator enables live sends."""
    if platform == "webhook_test":
        return WebhookTestAdapter()
    if platform == "qq_bot":
        return QQBotAdapter(credential_ref=account.get("credential_ref") or "CHEMCLAW_QQBOT")
    raise ChannelError("not_implemented", f"No adapter wired for platform {platform!r}.", http_status=501)
