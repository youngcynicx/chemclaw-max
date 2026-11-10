"""S04 offline proposal builder (plan §10.1: structured fixtures / offline
proposer; the real Agent B model integration is re-validated in S06).

Deterministic rules turn frozen transcript blocks + risk fragments into
structured proposal items. This module doubles as the executable contract
for Agent B's tool surface: whatever the offline proposer emits must satisfy
``experiments.create_proposal`` validation, and B's prompts (S06) must emit
the same item schema.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .profiles import get_profile

__all__ = ["build_proposal_items", "PLAN_PATTERN", "COMPUTED_PATTERN", "NEGATION_PATTERN"]

# Modality separation (plan §10.3): plans, negations and computed values are
# recognised lexically; everything else is a reported operation/observation.
PLAN_PATTERN = re.compile(
    r"(拟|计划|将|准备|下一步|plan(?:ned)?\s+to|will\s+|going\s+to)", re.IGNORECASE
)
NEGATION_PATTERN = re.compile(r"(不是|并未|没有|未|ignore|cancel(?:led)?|not\s+)", re.IGNORECASE)
COMPUTED_PATTERN = re.compile(r"(计算|推算|得|yield|计算得|computed|calculated|=)", re.IGNORECASE)
OBSERVATION_PATTERN = re.compile(r"(观察|观测|看到|出现|沉淀|变色|observed|appeared)", re.IGNORECASE)
MEASUREMENT_PATTERN = re.compile(r"(测得|测量|称取|量取|测定|measured|weighed)", re.IGNORECASE)

_AMOUNT_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?|\?\?(?:\.\?\?)?)\s*(?P<unit>g|mg|ug|µg|mL|mL|ul|µL|L|mol|mmol|°C|degC|h|min)\b",
    re.IGNORECASE,
)
_SAMPLE_RE = re.compile(r"\b([A-Z]{1,3}[-\s]?\d{1,4})\b")


def classify_event_kind(raw_text: str) -> str:
    """Lexical modality classification; order matters (negation first)."""
    text = raw_text.strip()
    if re.search(r"不是.{1,12}是", text):
        # "不是 5 g，是 0.5 g" style corrections
        return "negation"
    if NEGATION_PATTERN.search(text):
        return "negation"
    if PLAN_PATTERN.search(text):
        return "plan"
    if COMPUTED_PATTERN.search(text):
        return "computed"
    if OBSERVATION_PATTERN.search(text):
        return "observation"
    if MEASUREMENT_PATTERN.search(text):
        return "measurement"
    return "operation"


def _parse_amount(raw_text: str) -> dict[str, Any] | None:
    m = _AMOUNT_RE.search(raw_text)
    if m is None:
        return None
    raw_value, unit = m.group("value"), m.group("unit")
    unit_normalized = {
        "µg": "ug", "µL": "uL", "°C": "degC",
    }.get(unit, unit.lower() if unit in ("G", "ML", "MOL") else unit)
    if unit == "ml":
        unit_normalized = "mL"
    if unit == "°C" or unit.lower() == "degc":
        unit_normalized = "degC"
    uncertain = "??" in raw_value
    normalized = None if uncertain else float(raw_value)
    return {
        "raw": raw_value,
        "normalized": normalized,
        "unit": unit_normalized,
        "uncertain": uncertain,
    }


def _sample_label(raw_text: str) -> dict[str, Any] | None:
    m = _SAMPLE_RE.search(raw_text)
    if m is None:
        return None
    return {"raw": m.group(1), "normalized": m.group(1).replace(" ", "-"), "uncertain": False}


def _extract_fields(raw_text: str, profile_id: str, profile_version: str) -> dict[str, Any]:
    profile = get_profile(profile_id, profile_version)
    fields: dict[str, Any] = {}
    sample = _sample_label(raw_text)
    if sample and "sample_label" in profile.field_names:
        fields["sample_label"] = sample
    amount = _parse_amount(raw_text)
    if amount is not None:
        unit = amount.pop("unit")
        # Keep the unit attached to the quantity itself AND in the legacy
        # profile field. Readers must decode both shapes without guessing.
        amount["unit"] = unit
        if "amount" in profile.field_names:
            fields["amount"] = amount
        if "unit" in profile.field_names:
            fields["unit"] = {"raw": unit, "normalized": unit, "uncertain": False}
    return fields


def build_proposal_items(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    transcript_revision: int,
    profile_id: str = "chemistry_notebook",
    profile_version: str = "1.0",
) -> list[dict[str, Any]]:
    """Build structured proposal items from a page's frozen transcript.

    - One item per transcript block, with its evidence ref (page, revision,
      block).
    - Values the transcript left unknown keep ``uncertain: true`` and never
      get invented values (plan §10.2/§10.3).
    - Unwritten methods are NEVER inferred: fields simply stay absent.
    """
    get_profile(profile_id, profile_version)  # fail fast on unknown profile
    pt = conn.execute(
        "SELECT active_run_id, active_revision FROM page_transcripts WHERE page_id = ?",
        (page_id,),
    ).fetchone()
    if pt is None or pt["active_run_id"] is None:
        raise ValueError(f"Page {page_id!r} has no active transcript.")
    if pt["active_revision"] != transcript_revision:
        raise ValueError(
            f"Active revision {pt['active_revision']} != cited {transcript_revision}."
        )
    blocks = conn.execute(
        """
        SELECT tb.block_id, tb.block_kind, tb.reading_order, tb.block_json
        FROM transcript_blocks tb
        WHERE tb.run_id = ?
        ORDER BY tb.reading_order
        """,
        (pt["active_run_id"],),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for b in blocks:
        block = json.loads(b["block_json"])
        raw_text = str(block.get("text") or "").strip()
        if not raw_text:
            continue
        kind = classify_event_kind(raw_text)
        fields = _extract_fields(raw_text, profile_id, profile_version)
        uncertain_keys = [
            name for name, val in fields.items()
            if isinstance(val, dict) and val.get("uncertain")
        ]
        items.append(
            {
                "item_key": f"blk_{b['reading_order']:03d}",
                "event_kind": kind,
                "input_type": "handwritten_text",
                "raw_text": raw_text,
                "block_kind": b["block_kind"],
                "fields": fields,
                "uncertain_fields": uncertain_keys,
                "evidence_refs": [
                    {
                        "page_id": page_id,
                        "transcript_revision": transcript_revision,
                        "block_id": b["block_id"],
                    }
                ],
            }
        )
    return items
