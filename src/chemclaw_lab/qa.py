from __future__ import annotations

import re
from pathlib import Path
from sqlite3 import Row
from typing import Any

from .db import connect_read_only
from .units import UNIT_ALIASES, convert_amount, normalize_unit, units_compatible


_UNIT_TOKEN_PATTERN = "|".join(
    re.escape(token) for token in sorted(UNIT_ALIASES, key=len, reverse=True)
)
_LOCATION_PATTERN = re.compile(
    r"\b(where|location|located|find|cabinet|shelf|stored)\b|"
    r"在哪|哪里|位置|放哪",
    re.IGNORECASE,
)
_LOW_STOCK_PATTERN = re.compile(
    r"\b(low stock|restock|reorder|running low)\b|低库存|库存不足|补货",
    re.IGNORECASE,
)
_SUFFICIENCY_PATTERN = re.compile(
    r"\b(enough|sufficient)\b|够不够|是否够|足够",
    re.IGNORECASE,
)
_AVAILABILITY_PATTERN = re.compile(
    r"\b(available|do we have|in stock|have any)\b|有吗|还有吗|有没有",
    re.IGNORECASE,
)


def answer_inventory_question(
    db_path: str | Path,
    question: str,
) -> dict[str, Any]:
    """Answer a read-only inventory question from the SQLite projection."""
    if not question.strip():
        raise ValueError("question must not be empty")
    conn = connect_read_only(db_path)
    try:
        chemicals = conn.execute(
            """
            SELECT chemical_id, name, formula, location, amount, unit, threshold
            FROM chemicals
            ORDER BY name
            """
        ).fetchall()
    finally:
        conn.close()

    matches = _match_chemicals(question, chemicals)
    if not matches:
        if _LOW_STOCK_PATTERN.search(question):
            items = [
                _chemical_summary(row)
                for row in chemicals
                if float(row["amount"]) <= float(row["threshold"] or 0)
            ]
            return {
                "answer_type": "low_stock_list",
                "items": items,
                "needs_clarification": False,
            }
        return _clarification("item_not_found")
    if len(matches) > 1:
        return {
            **_clarification("ambiguous_item"),
            "candidates": [
                {
                    "chemical_id": row["chemical_id"],
                    "item_name": row["name"],
                }
                for row in matches
            ],
        }

    chemical = matches[0]
    base = {
        "chemical_id": chemical["chemical_id"],
        "item_name": chemical["name"],
        "needs_clarification": False,
    }
    if _LOCATION_PATTERN.search(question):
        return {
            **base,
            "answer_type": "location",
            "location": chemical["location"],
        }
    if _SUFFICIENCY_PATTERN.search(question):
        requested_amount, requested_unit = _extract_amount_and_unit(question)
        if requested_amount is None or requested_unit is None:
            return {
                **base,
                "answer_type": "clarification",
                "needs_clarification": True,
                "reason": "missing_required_amount",
            }
        if not units_compatible(requested_unit, chemical["unit"]):
            return {
                **base,
                "answer_type": "clarification",
                "needs_clarification": True,
                "reason": "incompatible_unit",
                "requested_amount": requested_amount,
                "requested_unit": requested_unit,
            }
        available = convert_amount(
            float(chemical["amount"]),
            chemical["unit"],
            requested_unit,
        )
        return {
            **base,
            "answer_type": "sufficiency",
            "available": round(available, 12),
            "requested_amount": requested_amount,
            "unit": requested_unit,
            "sufficient": available >= requested_amount,
        }
    if _LOW_STOCK_PATTERN.search(question):
        return {
            **base,
            "answer_type": "low_stock",
            "low_stock": float(chemical["amount"])
            <= float(chemical["threshold"] or 0),
            "threshold": float(chemical["threshold"] or 0),
            "value": float(chemical["amount"]),
            "unit": chemical["unit"],
        }
    if _AVAILABILITY_PATTERN.search(question):
        return {
            **base,
            "answer_type": "availability",
            "available": float(chemical["amount"]) > 0,
            "value": float(chemical["amount"]),
            "unit": chemical["unit"],
        }
    return {
        **base,
        "answer_type": "amount",
        "value": float(chemical["amount"]),
        "unit": chemical["unit"],
    }


def _clarification(reason: str) -> dict[str, Any]:
    return {
        "answer_type": "clarification",
        "chemical_id": None,
        "item_name": None,
        "needs_clarification": True,
        "reason": reason,
    }


def _chemical_summary(chemical: Row) -> dict[str, Any]:
    return {
        "chemical_id": chemical["chemical_id"],
        "item_name": chemical["name"],
        "value": float(chemical["amount"]),
        "unit": chemical["unit"],
        "threshold": float(chemical["threshold"] or 0),
        "location": chemical["location"],
    }


def _extract_amount_and_unit(text: str) -> tuple[float | None, str | None]:
    match = re.search(
        rf"(?<![a-z0-9])(?P<amount>\d[\d,]*(?:\.\d+)?)\s*"
        rf"(?P<unit>{_UNIT_TOKEN_PATTERN})",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None, None
    return (
        float(match.group("amount").replace(",", "")),
        normalize_unit(match.group("unit")),
    )


def _match_chemicals(question: str, chemicals: list[Row]) -> list[Row]:
    text = question.casefold()
    matches: list[Row] = []
    for chemical in chemicals:
        names = [chemical["name"], chemical["formula"]]
        if any(
            name
            and re.search(
                rf"(?<![a-z0-9]){re.escape(str(name).casefold())}(?![a-z0-9])",
                text,
            )
            for name in names
        ):
            matches.append(chemical)
    return matches
