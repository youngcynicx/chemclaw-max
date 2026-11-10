from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .db import connect


def upsert_gateway_identity(
    db_path: str | Path,
    *,
    source: str,
    external_user_id: str,
    user_id: str,
    display_name: str | None = None,
) -> dict[str, Any]:
    if not source.strip() or not external_user_id.strip():
        raise ValueError("source and external_user_id must not be empty")
    conn = connect(db_path)
    try:
        user = conn.execute(
            "SELECT user_id, name FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if user is None:
            raise ValueError(f"Unknown user_id: {user_id}")
        identity_id = f"gid_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO gateway_identities
              (identity_id, source, external_user_id, user_id, display_name)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source, external_user_id) DO UPDATE SET
              user_id = excluded.user_id,
              display_name = excluded.display_name,
              updated_at = CURRENT_TIMESTAMP
            """,
            (
                identity_id,
                source.strip(),
                external_user_id.strip(),
                user_id,
                display_name,
            ),
        )
        conn.commit()
        return resolve_gateway_identity(
            db_path,
            source=source,
            external_user_id=external_user_id,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def resolve_gateway_identity(
    db_path: str | Path,
    *,
    source: str,
    external_user_id: str,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT gi.identity_id, gi.source, gi.external_user_id,
                   gi.user_id, gi.display_name, u.name AS user_name
            FROM gateway_identities gi
            JOIN users u ON u.user_id = gi.user_id
            WHERE gi.source = ? AND gi.external_user_id = ?
            """,
            (source.strip(), external_user_id.strip()),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"Unmapped gateway identity: {source}/{external_user_id}"
            )
        return dict(row)
    finally:
        conn.close()


def list_gateway_identities(db_path: str | Path) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT gi.identity_id, gi.source, gi.external_user_id,
                       gi.user_id, gi.display_name, u.name AS user_name
                FROM gateway_identities gi
                JOIN users u ON u.user_id = gi.user_id
                ORDER BY gi.source, gi.external_user_id
                """
            ).fetchall()
        ]
    finally:
        conn.close()


class ChemClawClient:
    """Small standard-library client for gateway and integration tests."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def map_identity(
        self,
        *,
        source: str,
        external_user_id: str,
        user_id: str,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/identities",
            {
                "source": source,
                "external_user_id": external_user_id,
                "user_id": user_id,
                "display_name": display_name,
            },
        )

    def send_message(
        self,
        *,
        source: str,
        source_message_id: str,
        raw_text: str,
        external_user_id: str | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/inventory/messages",
            {
                "source": source,
                "source_message_id": source_message_id,
                "raw_text": raw_text,
                "external_user_id": external_user_id,
                "user_id": user_id,
                "project_id": project_id,
                "dry_run": dry_run,
            },
        )

    def ask_inventory(self, question: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/inventory/questions",
            {"question": question},
        )

    def confirm_event(
        self,
        event_id: str,
        *,
        approve: bool = True,
        corrections: dict[str, Any] | None = None,
        confirmed_by: str = "gateway_user",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/inventory/events/{event_id}/confirm",
            {
                "approve": approve,
                "corrections": corrections or {},
                "confirmed_by": confirmed_by,
            },
        )

    def list_events(self, *, status: str | None = None) -> list[dict[str, Any]]:
        path = "/v1/inventory/events"
        if status is not None:
            path += f"?status={status}"
        return self._request("GET", path)

    def list_chemicals(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/inventory/chemicals")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key is not None:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{self.base_url}{path}",
            method=method,
            data=data,
            headers=headers,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("error", body)
            except json.JSONDecodeError:
                detail = body
            raise ValueError(f"ChemClaw API {exc.code}: {detail}") from exc
