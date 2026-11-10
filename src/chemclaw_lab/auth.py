"""S02: principals, memberships, credentials, and session namespaces.

Prototype identity model per plan §8.2: principals are registered and
credentials are issued by server-side configuration (no SSO yet). Identity is
ALWAYS derived from the authenticated credential inside this module — the new
shared capability entry never accepts a self-reported identity.

Legacy ``users``/``gateway_identities`` tables remain untouched for the
compatibility API; they are not authority sources for the v2 entry.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .validation import SchemaViolation, load_named_schema, validate_payload_against_schema


PRINCIPAL_SCHEMA_NAME = "principal_v1.json"
ROLE_ORDER = {"viewer": 0, "member": 1, "owner": 2}
PRINCIPAL_KINDS = ("human", "agent", "service")


class AuthError(Exception):
    """Authentication or authorization failure with a stable error code."""

    def __init__(self, code: str, message: str, *, http_status: int = 403) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True)
class Membership:
    project_id: str
    role: str


@dataclass(frozen=True)
class PrincipalContext:
    """The authenticated subject attached to every capability invocation."""

    principal_id: str
    kind: str
    display_name: str | None
    memberships: dict[str, str] = field(default_factory=dict)

    def role_for(self, project_id: str | None) -> str | None:
        if project_id is None:
            return None
        return self.memberships.get(project_id)

    def has_any_membership(self) -> bool:
        return bool(self.memberships)

    def require_project(self, project_id: str | None, minimum_role: str) -> None:
        """Enforce project scope. Project-less objects are lab-shared reads
        handled by the capability layer; writes must always name a project."""
        if project_id is None:
            raise AuthError(
                "forbidden_scope",
                "A project scope is required for this capability.",
            )
        role = self.memberships.get(project_id)
        if role is None:
            raise AuthError(
                "forbidden_scope",
                f"No membership for project {project_id!r}.",
            )
        if ROLE_ORDER[role] < ROLE_ORDER[minimum_role]:
            raise AuthError(
                "forbidden_scope",
                f"Role {role!r} on project {project_id!r} is below "
                f"the required {minimum_role!r}.",
            )

    def to_contract(self) -> dict[str, Any]:
        payload = {
            "principal_id": self.principal_id,
            "kind": self.kind,
            "display_name": self.display_name,
            "memberships": [
                {"project_id": project_id, "role": role}
                for project_id, role in sorted(self.memberships.items())
            ],
        }
        validate_payload_against_schema(
            payload, load_named_schema(PRINCIPAL_SCHEMA_NAME)
        )
        return payload


def register_principal(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    kind: str,
    display_name: str | None = None,
) -> str:
    if kind not in PRINCIPAL_KINDS:
        raise ValueError(f"principal kind must be one of {PRINCIPAL_KINDS}")
    conn.execute(
        """
        INSERT INTO principals (principal_id, kind, display_name)
        VALUES (?, ?, ?)
        """,
        (principal_id, kind, display_name),
    )
    return principal_id


def grant_membership(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    project_id: str,
    role: str,
) -> None:
    if role not in ROLE_ORDER:
        raise ValueError(f"role must be one of {sorted(ROLE_ORDER)}")
    conn.execute(
        """
        INSERT INTO memberships (principal_id, project_id, role)
        VALUES (?, ?, ?)
        ON CONFLICT(principal_id, project_id)
        DO UPDATE SET role = excluded.role
        """,
        (principal_id, project_id, role),
    )


def register_credential(
    conn: sqlite3.Connection,
    *,
    credential_id: str,
    principal_id: str,
    secret: str,
) -> str:
    """Store a server-issued credential. Only the SHA-256 digest is kept."""
    secret_hash = _hash_secret(secret)
    try:
        conn.execute(
            """
            INSERT INTO auth_credentials (credential_id, principal_id, kind, secret_hash)
            VALUES (?, ?, 'api_key', ?)
            """,
            (credential_id, principal_id, secret_hash),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"Credential {credential_id!r} already exists") from exc
    return credential_id


def revoke_credential(conn: sqlite3.Connection, *, credential_id: str) -> None:
    conn.execute(
        "UPDATE auth_credentials SET status = 'revoked' WHERE credential_id = ?",
        (credential_id,),
    )


def authenticate_principal(
    conn: sqlite3.Connection,
    secret: str | None,
) -> PrincipalContext:
    """Derive the PrincipalContext from a presented credential.

    Returns a context or raises AuthError('unauthorized'). The comparison is
    constant-time over the digest, and revoked credentials never authenticate.
    """
    if not secret:
        raise AuthError("unauthorized", "Missing principal credential.", http_status=401)
    secret_hash = _hash_secret(secret)
    row = conn.execute(
        """
        SELECT credential_id, principal_id, secret_hash
        FROM auth_credentials
        WHERE secret_hash = ? AND status = 'active'
        """,
        (secret_hash,),
    ).fetchone()
    if row is None or not hmac.compare_digest(row["secret_hash"], secret_hash):
        raise AuthError("unauthorized", "Invalid principal credential.", http_status=401)
    return load_principal_context(conn, row["principal_id"])


def load_principal_context(conn: sqlite3.Connection, principal_id: str) -> PrincipalContext:
    row = conn.execute(
        """
        SELECT principal_id, kind, display_name, status
        FROM principals
        WHERE principal_id = ?
        """,
        (principal_id,),
    ).fetchone()
    if row is None:
        raise AuthError("unauthorized", f"Unknown principal {principal_id!r}.", http_status=401)
    if row["status"] != "active":
        raise AuthError("unauthorized", f"Principal {principal_id!r} is not active.", http_status=401)
    memberships = {
        membership["project_id"]: membership["role"]
        for membership in conn.execute(
            """
            SELECT project_id, role FROM memberships WHERE principal_id = ?
            """,
            (principal_id,),
        ).fetchall()
    }
    return PrincipalContext(
        principal_id=row["principal_id"],
        kind=row["kind"],
        display_name=row["display_name"],
        memberships=memberships,
    )


def visible_project_ids(context: PrincipalContext) -> list[str]:
    return sorted(context.memberships)


# ``user`` is the legacy spelling used by older tests/contexts; it denotes a
# human exactly like ``human``. Agents/services are never human-equivalent.
HUMAN_KINDS = frozenset({"human", "user"})


def require_human(context: PrincipalContext, action: str) -> None:
    """An approval/confirmation must come from an authenticated human.

    Agents and services may prepare proposals and apply already-approved
    actions, but they can never mint the human decision themselves (§R03).
    """
    if context.kind not in HUMAN_KINDS:
        raise AuthError(
            "human_approval_required",
            f"{action} requires an authenticated human actor; "
            f"principal {context.principal_id!r} is {context.kind!r}.",
        )


def bind_session_namespace(
    conn: sqlite3.Connection,
    *,
    session_key: str,
    context: PrincipalContext,
    project_id: str | None,
    mode: str,
    channel_source: str | None = None,
    external_account: str | None = None,
    peer: str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Create or verify a session namespace binding.

    The namespace binds (principal, project, mode) first; channel coordinates
    (source/account/peer/thread) are attributes of that binding. An existing
    session key can never silently move to another principal or project —
    that would let channel traffic cross project scopes.
    """
    existing = conn.execute(
        """
        SELECT principal_id, project_id, mode
        FROM session_namespaces
        WHERE session_key = ?
        """,
        (session_key,),
    ).fetchone()
    if existing is not None:
        if (
            existing["principal_id"] != context.principal_id
            or existing["project_id"] != project_id
            or existing["mode"] != mode
        ):
            raise AuthError(
                "forbidden_scope",
                "Session key is bound to a different principal/project/mode.",
            )
        return _session_payload(conn, session_key)
    if project_id is not None:
        context.require_project(project_id, "viewer")
    conn.execute(
        """
        INSERT INTO session_namespaces
          (session_key, principal_id, project_id, mode, role,
           channel_source, external_account, peer, thread_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_key,
            context.principal_id,
            project_id,
            mode,
            context.role_for(project_id),
            channel_source,
            external_account,
            peer,
            thread_id,
        ),
    )
    return _session_payload(conn, session_key)


def export_principal_audit(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
) -> dict[str, Any]:
    """Minimal provenance record of who granted what (kept for audits)."""
    row = conn.execute(
        "SELECT principal_id, kind, display_name, created_at FROM principals WHERE principal_id = ?",
        (principal_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown principal {principal_id!r}")
    memberships = [
        dict(member)
        for member in conn.execute(
            "SELECT project_id, role, created_at FROM memberships WHERE principal_id = ?",
            (principal_id,),
        ).fetchall()
    ]
    payload = {
        "principal": dict(row),
        "memberships": memberships,
        "memberships_json": json.dumps(memberships, sort_keys=True, ensure_ascii=False),
    }
    try:
        validate_payload_against_schema(
            {
                "principal_id": row["principal_id"],
                "kind": row["kind"],
                "display_name": row["display_name"],
                "memberships": memberships,
            },
            load_named_schema(PRINCIPAL_SCHEMA_NAME),
        )
    except SchemaViolation:
        payload["contract_warning"] = "principal record no longer matches principal_v1"
    return payload


def _session_payload(conn: sqlite3.Connection, session_key: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM session_namespaces WHERE session_key = ?",
        (session_key,),
    ).fetchone()
    return dict(row) if row is not None else {}


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def new_principal_id(kind: str) -> str:
    prefix = {"human": "hum", "agent": "agt", "service": "svc"}[kind]
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
