from __future__ import annotations

from typing import Any

BINARY_OUTPUT_TYPES: set[str] = {
    "smartdoser",
    "simpledoser",
    "heater",
    "chiller",
    "returnpump",
    "return_pump",
    "calciumreactor",
    "calcium_reactor",
    "kalkreactor",
    "kalk_reactor",
    "rodifilter",
    "rodi_filter",
    "protein_skimmer",
    "proteinskimmer",
    "ozonegenerator",
    "ozone_generator",
    "feeder",
    "ato",
    "automaticwaterchange",
    "automatic_water_change",
}

BINARY_OUTPUT_FAMILIES: set[str] = {
    "doser",
    "dose",
    "heater",
    "chiller",
    "return",
    "returnpump",
    "filter",
    "skimmer",
    "reactor",
    "feeder",
    "ato",
    "awc",
}


def is_doser_output(output_meta: dict[str, Any] | None) -> bool:
    if not isinstance(output_meta, dict):
        return False
    type_value = str(output_meta.get("type") or "").strip().lower()
    family_value = str(output_meta.get("family") or "").strip().lower()
    if "doser" in type_value:
        return True
    if family_value in {"dose", "doser"}:
        return True
    return False


def is_binary_output(output_meta: dict[str, Any] | None) -> bool:
    if not isinstance(output_meta, dict):
        return False
    type_value = str(output_meta.get("type") or "").strip().lower()
    family_value = str(output_meta.get("family") or "").strip().lower()
    if type_value and type_value in BINARY_OUTPUT_TYPES:
        return True
    if family_value and family_value in BINARY_OUTPUT_FAMILIES:
        return True
    if "doser" in type_value:
        return True
    return False
