from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class InventoryEvent:
    event_type: str
    event_subtype: str
    item_type: str
    item_id: str | None
    item_name: str | None
    amount: float | None
    unit: str | None
    transaction_type: str
    confidence: float
    requires_confirmation: bool
    raw_text: str
    project_id: str | None = None
    user_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "InventoryEvent":
        return cls(
            event_type=payload["event_type"],
            event_subtype=payload["event_subtype"],
            item_type=payload["item_type"],
            item_id=payload.get("item_id"),
            item_name=payload.get("item_name"),
            amount=payload.get("amount"),
            unit=payload.get("unit"),
            transaction_type=payload["transaction_type"],
            confidence=float(payload["confidence"]),
            requires_confirmation=bool(payload["requires_confirmation"]),
            raw_text=payload["raw_text"],
            project_id=payload.get("project_id"),
            user_id=payload.get("user_id"),
        )


@dataclass(frozen=True)
class AuditDecision:
    decision: str
    risk_level: str
    reason: str
    required_action: str | None = None

