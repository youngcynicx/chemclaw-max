"""Versioned experiment profiles (plan §10.2).

A profile declares which fields a structured experiment record carries,
which units are acceptable, which fields are required for the record to be
"complete", and what question priority missing fields get. Profiles also
gate auto-apply: by default nothing auto-applies (plan §10.5: first round is
verify-then-apply; auto-apply only for input types explicitly configured
AND evaluated on the profile).

Profiles are versioned: proposals record the profile id + version they were
built against, so behaviour changes never silently reinterpret old evidence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ExperimentProfile",
    "ProfileError",
    "CHEMISTRY_NOTEBOOK_V1",
    "get_profile",
    "profile_to_contract",
]


class ProfileError(ValueError):
    """Raised when a profile definition is invalid."""


_ALLOWED_MODALITIES = frozenset(
    {"operation", "observation", "measurement", "plan", "negation",
     "interpretation", "computed"}
)


@dataclass(frozen=True)
class ExperimentProfile:
    profile_id: str
    version: str
    fields: tuple[dict[str, Any], ...]
    allowed_units: tuple[str, ...]
    question_priority: tuple[str, ...]
    # Input types allowed to auto-apply without per-item human confirmation.
    # Empty = verify-then-apply for everything (S04 first round default).
    auto_apply_inputs: frozenset[str] = field(default=frozenset())

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f["name"] for f in self.fields)

    def field_def(self, name: str) -> dict[str, Any] | None:
        for f in self.fields:
            if f["name"] == name:
                return f
        return None

    def required_fields(self) -> tuple[str, ...]:
        return tuple(f["name"] for f in self.fields if f.get("required"))

    def validate(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", self.profile_id):
            raise ProfileError(f"Invalid profile_id {self.profile_id!r}.")
        if not re.fullmatch(r"[0-9]+\.[0-9]+", self.version):
            raise ProfileError(f"Invalid version {self.version!r} (expect MAJOR.MINOR).")
        if not self.fields:
            raise ProfileError("Profile must declare at least one field.")
        seen: set[str] = set()
        for f in self.fields:
            name = f.get("name")
            if not name or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name):
                raise ProfileError(f"Invalid field name {name!r}.")
            if name in seen:
                raise ProfileError(f"Duplicate field {name!r}.")
            seen.add(name)
            if f.get("kind") not in ("text", "number", "enum", "timestamp", "reference"):
                raise ProfileError(f"Field {name!r} needs a valid kind.")
        if not self.allowed_units:
            raise ProfileError("Profile must declare allowed units.")
        for prio in self.question_priority:
            if prio not in ("blocker", "high", "normal", "low"):
                raise ProfileError(f"Invalid question priority {prio!r}.")

    def to_json(self) -> str:
        return json.dumps(profile_to_contract(self), sort_keys=True, ensure_ascii=False)


def profile_to_contract(profile: ExperimentProfile) -> dict[str, Any]:
    """The contract surfaced to Agent B and stored with proposals."""
    return {
        "profile_id": profile.profile_id,
        "version": profile.version,
        "fields": [dict(f) for f in profile.fields],
        "allowed_units": list(profile.allowed_units),
        "question_priority": list(profile.question_priority),
        "auto_apply_inputs": sorted(profile.auto_apply_inputs),
        "rules": {
            "unwritten_methods_never_inferred": (
                "Fields the source does not state (e.g. 'actual procedure') must "
                "NOT be filled from convention; they stay missing and become "
                "clarification questions at the declared priority."
            ),
            "plan_vs_reported": (
                "'plan' modality events never count as performed operations and "
                "never generate inventory consumption proposals."
            ),
            "computed_vs_observed": (
                "'computed' values are derived results; they are never stored as "
                "wet-lab observations."
            ),
            "verify_then_apply": (
                "Handwritten numbers, units and experiment status require explicit "
                "confirmation bound to the proposal revision unless the input type "
                "is listed in auto_apply_inputs."
            ),
        },
    }


CHEMISTRY_NOTEBOOK_V1 = ExperimentProfile(
    profile_id="chemistry_notebook",
    version="1.0",
    fields=(
        {"name": "sample_label", "kind": "reference", "required": True,
         "description": "Sample identifier as written on the page."},
        {"name": "run_name", "kind": "text", "required": False},
        {"name": "operation", "kind": "text", "required": False,
         "description": "What was actually done; never inferred from convention."},
        {"name": "amount", "kind": "number", "required": False},
        {"name": "unit", "kind": "enum", "required": False},
        {"name": "performed_at", "kind": "timestamp", "required": False},
        {"name": "observed_state", "kind": "text", "required": False},
        {"name": "measurement_value", "kind": "number", "required": False},
        {"name": "plan_action", "kind": "text", "required": False,
         "description": "Planned but not performed action."},
        {"name": "negates", "kind": "reference", "required": False,
         "description": "Identifier a negation/scratch-out applies to."},
    ),
    allowed_units=("g", "mg", "ug", "mL", "uL", "L", "mol", "mmol", "degC", "h", "min"),
    question_priority=("blocker", "high", "normal", "low"),
    # S04 first round: everything verify-then-apply. No auto-apply inputs.
    auto_apply_inputs=frozenset(),
)


_REGISTRY: dict[tuple[str, str], ExperimentProfile] = {
    (CHEMISTRY_NOTEBOOK_V1.profile_id, CHEMISTRY_NOTEBOOK_V1.version): CHEMISTRY_NOTEBOOK_V1,
}


def get_profile(profile_id: str, version: str) -> ExperimentProfile:
    key = (profile_id, version)
    if key not in _REGISTRY:
        raise ProfileError(
            f"Unknown profile {profile_id!r} version {version!r}. "
            f"Known: {sorted(k[0] + '@' + k[1] for k in _REGISTRY)}"
        )
    return _REGISTRY[key]
