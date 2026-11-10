"""S05 external read-only HTTP client (plan §11.7).

Reference client for external research agents: speaks the frozen /v2
capability protocol over stdlib urllib and exposes ONLY read-only query
capabilities. Write capabilities are not reachable through this class —
not by convention, by whitelist.

Validates in this stage: real protocol behaviour, sample history,
provenance and unknown handling. Real research agents consume it in S06.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import Any

__all__ = ["ReadOnlyQueryClient", "ClientError"]

# The complete surface of this client. Write capabilities can never be
# invoked here because invoke() refuses anything not in this set.
_READ_ONLY_CAPABILITIES = frozenset(
    {
        "query.events.search",
        "query.sample.timeline",
        "query.answer.render",
        "query.report.read",
        "query.answer.build",
        "query.samples.list",
        "query.sample.context",
        "memory.event.read",
        "memory.proposal.read",
        "memory.sample.search",
        "evidence.page.read",
        "evidence.pages.list",
    }
)


class ClientError(Exception):
    def __init__(self, status: int, code: str | None, message: str) -> None:
        super().__init__(f"HTTP {status}: {code or ''} {message}".strip())
        self.status = status
        self.code = code
        self.message = message


class ReadOnlyQueryClient:
    """HTTP client bound to one principal credential (API key)."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _operation_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = None
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
                error = parsed.get("error") or {}
                code = error.get("code") if isinstance(error, dict) else None
                message = error.get("message") if isinstance(error, dict) else body
            except json.JSONDecodeError:
                code, message = None, body
            raise ClientError(exc.code, code, message) from exc

    # -- protocol surface ---------------------------------------------------

    def list_capabilities(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/v2/capabilities")
        return result.get("capabilities", [])

    def invoke(self, capability_id: str, params: dict[str, Any], *, operation_id: str | None = None) -> Any:
        if capability_id not in _READ_ONLY_CAPABILITIES:
            raise ClientError(
                400, "read_only_client",
                f"{capability_id!r} is not a read-only capability; "
                "this client exposes queries only.",
            )
        return self._request(
            "POST",
            f"/v2/capabilities/{capability_id}/invoke",
            {"params": params, "operation_id": operation_id if operation_id is not None else self._operation_id("ext-read")},
        )

    # -- convenience use cases (§11.7) ---------------------------------------

    def list_samples(
        self, *, project_ids: list[str] | None = None, q: str | None = None,
        sample_id: str | None = None, limit: int = 100, offset: int = 0,
    ) -> dict[str, Any]:
        """Compact discovery, in sample ID order (not a fixed snapshot page)."""
        params = {"view": "catalog", "limit": limit, "offset": offset}
        params.update({key: value for key, value in {
            "project_ids": project_ids, "q": q, "sample_id": sample_id,
        }.items() if value is not None})
        return self.invoke("query.samples.list", params).get("result", {})

    def get_sample_context(
        self, project_id: str, sample_id: str | None = None, *, sample_label: str | None = None,
        include: list[str] | None = None, snapshot: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None, expected_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"project_id": project_id}
        params.update({key: value for key, value in {
            "sample_id": sample_id, "sample_label": sample_label, "include": include,
            "snapshot": snapshot, "budget": budget,
            "expected_fingerprint": expected_fingerprint,
        }.items() if value is not None})
        return self.invoke("query.sample.context", params).get("result", {})

    def read_event(
        self, event_id: str, *, revision: int | None = None,
        knowledge_as_of: str | None = None, include_history: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"event_id": event_id, "include_history": include_history}
        params.update({key: value for key, value in {
            "revision": revision, "knowledge_as_of": knowledge_as_of,
        }.items() if value is not None})
        return self.invoke("memory.event.read", params).get("result", {})

    def read_page(
        self, page_id: str | None = None, *, document_id: str | None = None,
        transcript_revision: int | None = None, block_ids: list[str] | None = None,
        view: str = "agent",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"view": view}
        params.update({key: value for key, value in {
            "page_id": page_id, "document_id": document_id,
            "transcript_revision": transcript_revision, "block_ids": block_ids,
        }.items() if value is not None})
        return self.invoke("evidence.page.read", params).get("result", {})

    def list_pages(
        self, *, project_ids: list[str] | None = None, limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        """Discover sources even when they have no sample or knowledge links.

        Source state describes persisted records, not semantic completeness.
        Pagination is over the current authorized state, not a fixed snapshot.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if project_ids is not None:
            params["project_ids"] = project_ids
        return self.invoke("evidence.pages.list", params).get("result", {})

    def search_events(
        self,
        *,
        project_ids: list[str] | None = None,
        text: str | None = None,
        sample_label: str | None = None,
        knowledge_as_of: str | None = None,
    ) -> dict[str, Any]:
        envelope = self.invoke(
            "query.events.search",
            {
                "project_ids": project_ids,
                "text": text,
                "sample_label": sample_label,
                "knowledge_as_of": knowledge_as_of,
            },
            operation_id=self._operation_id("ext-search"),
        )
        return envelope.get("result", {})

    def sample_history(
        self, project_id: str, sample_id: str, *, knowledge_as_of: str | None = None
    ) -> dict[str, Any]:
        envelope = self.invoke(
            "query.sample.timeline",
            {"sample_id": sample_id, "knowledge_as_of": knowledge_as_of},
            operation_id=self._operation_id("ext-history"),
        )
        result = envelope.get("result", {})
        if result.get("sample", {}).get("project_id") != project_id:
            raise ClientError(403, "forbidden_scope", "Sample does not belong to the requested project.")
        return result

    def render_answer(self, question: str, *, project_ids: list[str] | None = None) -> dict[str, Any]:
        envelope = self.invoke(
            "query.answer.render",
            {"question": question, "project_ids": project_ids},
            operation_id=self._operation_id("ext-answer"),
        )
        return envelope.get("result", {})

    def read_report(self, report_id: str) -> dict[str, Any]:
        envelope = self.invoke(
            "query.report.read", {"report_id": report_id},
            operation_id=self._operation_id("ext-report"),
        )
        return envelope.get("result", {})
