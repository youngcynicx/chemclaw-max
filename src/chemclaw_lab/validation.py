from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from .models import InventoryEvent


INVENTORY_EVENT_SCHEMA_VERSION = "0.3.0"


class SchemaViolation(ValueError):
    """Raised when a payload fails a versioned JSON Schema check."""


def load_named_schema(name: str) -> dict[str, Any]:
    """Load a checked-in versioned schema file from chemclaw_lab/schemas/."""
    schema_path = files("chemclaw_lab").joinpath(f"schemas/{name}")
    return json.loads(schema_path.read_text(encoding="utf-8"))


def validate_payload_against_schema(payload: Any, schema: dict[str, Any]) -> None:
    """Validate a payload against the JSON Schema subset used by this package.

    Supports: type, enum, const, required, additionalProperties, properties,
    minLength, minimum/maximum, allOf with if/then branches. This is the same
    subset the v0.2 inventory validator relied on, generalized so every S02
    contract can stay zero-dependency while checked-in schemas stay
    authoritative.
    """
    _validate_node("$", payload, schema)


def _validate_node(path: str, value: Any, rules: dict[str, Any]) -> None:
    if "const" in rules and value != rules["const"]:
        raise SchemaViolation(f"{path} must equal {rules['const']!r}")
    if "enum" in rules and value not in rules["enum"]:
        raise SchemaViolation(f"{path} must be one of {rules['enum']!r}")

    expected_types = rules.get("type")
    if expected_types is not None:
        if isinstance(expected_types, str):
            expected_types = [expected_types]
        if not any(_matches_json_type(value, item) for item in expected_types):
            raise SchemaViolation(f"{path} has invalid type")

    if isinstance(value, dict):
        properties = rules.get("properties", {})
        required = set(rules.get("required", []))
        missing = required - set(value)
        if missing:
            raise SchemaViolation(f"{path} is missing required fields: {sorted(missing)}")
        if rules.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                raise SchemaViolation(f"{path} has unsupported fields: {sorted(extra)}")
        for key, item in value.items():
            if key in properties:
                _validate_node(f"{path}.{key}", item, properties[key])

    if isinstance(value, list):
        item_rules = rules.get("items")
        if isinstance(item_rules, dict):
            for index, item in enumerate(value):
                _validate_node(f"{path}[{index}]", item, item_rules)

    if isinstance(value, str) and len(value) < rules.get("minLength", 0):
        raise SchemaViolation(f"{path} is too short")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in rules and value < rules["minimum"]:
            raise SchemaViolation(f"{path} must be >= {rules['minimum']}")
        if "maximum" in rules and value > rules["maximum"]:
            raise SchemaViolation(f"{path} must be <= {rules['maximum']}")

    for branch in rules.get("allOf", []):
        if_branch = branch.get("if")
        if isinstance(if_branch, dict) and _matches_branch(value, if_branch):
            then_branch = branch.get("then")
            if isinstance(then_branch, dict):
                _validate_node(path, value, then_branch)


def _matches_branch(value: Any, branch: dict[str, Any]) -> bool:
    try:
        _validate_node("$", value, branch)
        return True
    except SchemaViolation:
        return False


def _matches_json_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return False


@lru_cache(maxsize=1)
def load_inventory_event_schema() -> dict[str, Any]:
    schema_path = files("chemclaw_lab").joinpath("schemas/inventory_event.schema.json")
    return json.loads(schema_path.read_text(encoding="utf-8"))


def validate_inventory_event(event: InventoryEvent) -> None:
    """Execute the subset of JSON Schema used by the canonical event schema.

    Keeping this validator in the package preserves a zero-dependency phone
    runtime while still making the checked-in JSON Schema authoritative.
    """
    schema = load_inventory_event_schema()
    payload = event.to_payload()

    required = set(schema.get("required", []))
    missing = required - set(payload)
    if missing:
        raise ValueError(f"Inventory event is missing required fields: {sorted(missing)}")

    properties = schema["properties"]
    if schema.get("additionalProperties") is False:
        extra = set(payload) - set(properties)
        if extra:
            raise ValueError(f"Inventory event has unsupported fields: {sorted(extra)}")

    for field, value in payload.items():
        rules = properties[field]
        _validate_value(field, value, rules)


def _validate_value(field: str, value: Any, rules: dict[str, Any]) -> None:
    try:
        _validate_node(field, value, rules)
    except SchemaViolation as exc:
        raise ValueError(str(exc)) from exc
