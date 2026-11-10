from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import AuditDecision, InventoryEvent
from .units import convert_amount, units_compatible


POLICY_VERSION = "inventory-policy-v0.4"
HIGH_HAZARD_LEVELS = {"high", "critical"}


def evaluate_inventory_event(
    event: InventoryEvent,
    chemical: Mapping[str, Any] | None,
    *,
    user_confirmed: bool = False,
) -> AuditDecision:
    """Evaluate an inventory proposal without mutating operational state."""
    if event.transaction_type == "unknown":
        return AuditDecision(
            "pending_confirmation",
            "medium",
            "Could not identify the inventory action.",
            "confirm_inventory_action",
        )
    if event.item_id is None:
        return AuditDecision(
            "pending_confirmation",
            "medium",
            "Could not match the message to exactly one chemical.",
            "confirm_chemical_identity",
        )
    if event.amount is None:
        return AuditDecision(
            "pending_confirmation",
            "medium",
            "Could not extract a numeric amount.",
            "confirm_amount",
        )
    if event.unit is None:
        return AuditDecision(
            "pending_confirmation",
            "medium",
            "The message is missing a supported unit.",
            "confirm_unit",
        )
    if event.amount <= 0:
        return AuditDecision(
            "reject",
            "high",
            "Inventory amounts must be greater than zero.",
            "provide_positive_amount",
        )
    if chemical is None:
        return AuditDecision(
            "reject",
            "high",
            "Matched chemical is no longer available.",
            "retry_lookup",
        )

    stored_unit = str(chemical["unit"])
    if not units_compatible(event.unit, stored_unit):
        return AuditDecision(
            "pending_confirmation",
            "medium",
            f'Unit "{event.unit}" is incompatible with stored unit "{stored_unit}".',
            "confirm_unit_conversion",
        )

    if event.requires_confirmation and not user_confirmed:
        return AuditDecision(
            "pending_confirmation",
            "medium",
            "Message semantics require explicit confirmation before a write.",
            "confirm_completed_inventory_action",
        )

    normalized_amount = convert_amount(event.amount, event.unit, stored_unit)
    current_amount = float(chemical["amount"])
    threshold = float(chemical["threshold"] or 0)

    if event.transaction_type == "use":
        amount_delta = -normalized_amount
    elif event.transaction_type == "add":
        amount_delta = normalized_amount
    elif event.transaction_type == "adjust":
        amount_delta = normalized_amount - current_amount
    elif event.transaction_type == "opening_balance":
        if abs(current_amount) > 1e-12:
            return AuditDecision(
                "reject",
                "high",
                "Opening balance is only valid for an item with zero current stock.",
                "use_adjust_instead",
            )
        amount_delta = normalized_amount
    else:
        return AuditDecision(
            "reject",
            "high",
            f'Unsupported transaction type "{event.transaction_type}".',
            "provide_supported_action",
        )

    if current_amount + amount_delta < 0:
        action = "correct_stock_or_amount" if user_confirmed else "confirm_stock_deduction"
        decision = "reject" if user_confirmed else "pending_confirmation"
        return AuditDecision(
            decision,
            "high",
            "Requested usage would make inventory negative.",
            action,
        )

    hazard_level = str(chemical["hazard_level"] or "low").lower()
    if hazard_level in HIGH_HAZARD_LEVELS and not user_confirmed:
        return AuditDecision(
            "pending_confirmation",
            "high",
            f'Chemical policy marks this item as {hazard_level} hazard.',
            "confirm_hazardous_inventory_change",
        )

    configured_limit = chemical["max_auto_delta"]
    if (
        configured_limit is not None
        and abs(amount_delta) > float(configured_limit)
        and not user_confirmed
    ):
        return AuditDecision(
            "pending_confirmation",
            "high",
            "Inventory change exceeds the configured automatic-change limit.",
            "confirm_large_inventory_change",
        )

    if event.transaction_type == "opening_balance":
        magnitude_limit = abs(amount_delta)
    elif event.transaction_type == "use":
        magnitude_limit = max(current_amount * 0.5, 1.0)
    elif event.transaction_type == "adjust":
        magnitude_limit = max(current_amount * 0.25, 1.0)
    else:
        magnitude_limit = max(current_amount * 0.5, threshold, 1.0)
    if abs(amount_delta) > magnitude_limit and not user_confirmed:
        return AuditDecision(
            "pending_confirmation",
            "high",
            "Inventory change is unusually large relative to current stock.",
            "confirm_large_inventory_change",
        )

    if user_confirmed:
        return AuditDecision(
            "approve",
            "medium",
            "User-confirmed inventory event passed all hard policy constraints.",
        )
    return AuditDecision(
        "approve",
        "low",
        "Inventory event is specific and within configured policy bounds.",
    )
