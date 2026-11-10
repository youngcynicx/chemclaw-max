from __future__ import annotations


UNIT_ALIASES = {
    "μg": "ug",
    "µg": "ug",
    "ug": "ug",
    "毫克": "mg",
    "mg": "mg",
    "克": "g",
    "g": "g",
    "公斤": "kg",
    "千克": "kg",
    "kg": "kg",
    "μl": "ul",
    "µl": "ul",
    "ul": "ul",
    "微升": "ul",
    "毫升": "ml",
    "ml": "ml",
    "升": "l",
    "l": "l",
    "摩尔": "mol",
    "mol": "mol",
    "毫摩尔": "mmol",
    "mmol": "mmol",
}

UNIT_DIMENSIONS = {
    "ug": ("mass", 0.000001),
    "mg": ("mass", 0.001),
    "g": ("mass", 1.0),
    "kg": ("mass", 1000.0),
    "ul": ("volume", 0.000001),
    "ml": ("volume", 0.001),
    "l": ("volume", 1.0),
    "mmol": ("amount_of_substance", 0.001),
    "mol": ("amount_of_substance", 1.0),
}

DISPLAY_UNITS = {
    "ug": "µg",
    "mg": "mg",
    "g": "g",
    "kg": "kg",
    "ul": "µL",
    "ml": "mL",
    "l": "L",
    "mmol": "mmol",
    "mol": "mol",
}


def display_unit(unit: str | None) -> str | None:
    normalized = normalize_unit(unit)
    if normalized is None:
        return unit
    return DISPLAY_UNITS.get(normalized, normalized)

SUPPORTED_UNITS = frozenset(UNIT_DIMENSIONS)


def normalize_unit(unit: str | None) -> str | None:
    if unit is None or not isinstance(unit, str):
        return None
    return UNIT_ALIASES.get(unit.strip().lower())


def units_compatible(source_unit: str, target_unit: str) -> bool:
    source = normalize_unit(source_unit)
    target = normalize_unit(target_unit)
    if source is None or target is None:
        return False
    return UNIT_DIMENSIONS[source][0] == UNIT_DIMENSIONS[target][0]


def convert_amount(amount: float, source_unit: str, target_unit: str) -> float:
    source = normalize_unit(source_unit)
    target = normalize_unit(target_unit)
    if source is None or target is None or not units_compatible(source, target):
        raise ValueError(f'Cannot convert unit "{source_unit}" to "{target_unit}"')
    source_factor = UNIT_DIMENSIONS[source][1]
    target_factor = UNIT_DIMENSIONS[target][1]
    return amount * source_factor / target_factor

