"""One-time entity/device registry migration from the legacy pyhydros
(Cognito + MQTT) integration to the pyhydros2 (public REST API) integration.

Home Assistant keys history/statistics off ``entity_id``, which the entity
registry leaves untouched once assigned -- only ``unique_id`` needs to change
to point an existing entity at the new data source. This module performs
that one-time remap during the reauth flow, *before* the new platforms are
set up, so existing entities keep their history instead of being recreated
from scratch.

Matching is best-effort and name-based: the public API has no equivalent of
the old config blob, so there's no exact key to join old and new entities
on. Every old entity whose (normalized) name matches a new override/log
series entry is migrated; anything that doesn't match (e.g. the old "MQTT
Health" or "Reservoir Remaining" sensors, which have no new-API equivalent)
is left alone and reported back so the caller can tell the user.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from pyhydros2 import LogSeriesInfo, OverrideMetadataEntry

from .const import DOMAIN
from .coordinator import is_manual_flow_rate, override_unique_key

_LOGGER = logging.getLogger(__name__)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize(value: str) -> str:
    return _NON_ALNUM.sub("", value.lower())


@dataclass
class MigrationResult:
    migrated: list[str]
    unmatched: list[str]


@dataclass
class _Candidate:
    unique_id: str
    domain: str


# (accessor, metric label) pairs mirroring sensor.py's HydrosOutputMetricSensor
# naming, kept in sync manually since importing sensor.py here isn't worth the
# coupling just for four strings.
_OUTPUT_METRIC_ACCESSORS: tuple[tuple[str, str], ...] = (
    ("output_voltage_volts", "Voltage"),
    ("output_current_amps", "Current"),
    ("output_power_watts", "Power"),
    ("output_frequency_hz", "Frequency"),
)


def _build_candidates(
    new_device_id: str,
    override_metadata: list[OverrideMetadataEntry],
    log_series: list[LogSeriesInfo],
) -> dict[tuple[str, str], _Candidate]:
    """Build (normalized name, domain) -> unique_id candidates that mirror
    *exactly* what each platform's ``async_setup_entry`` will construct for
    ``new_device_id``, so a migrated unique_id is always one a platform will
    actually pick up instead of landing on an entity nothing ever creates.
    """
    candidates: dict[tuple[str, str], _Candidate] = {}

    def _add(name: str, unique_id: str, domain: str) -> None:
        candidates.setdefault((_normalize(name), domain), _Candidate(unique_id, domain))

    for item in override_metadata:
        if is_manual_flow_rate(item):
            continue
        labels = [label for label in (item.name, item.label) if label]
        key = override_unique_key(override_metadata, item.key, item.name)

        # select.py / number.py all name the primary entity after the
        # output itself, with unique_id "{device_id}-{key}".
        if item.type in ("bool", "flag"):
            for label in labels:
                _add(label, f"{new_device_id}-{key}", "select")
        elif item.type == "level":
            for label in labels:
                _add(label, f"{new_device_id}-{key}", "number")
        if item.is_mode:
            for label in labels:
                _add(label, f"{new_device_id}-{key}", "select")

        # sensor.py: HydrosOutputMetricSensor, one per metric field, named
        # "{output_name} {Metric Label}".
        for accessor, metric_label in _OUTPUT_METRIC_ACCESSORS:
            for label in labels:
                # Mirrors sensor.py: a label already ending the name isn't repeated.
                full_name = (
                    label
                    if label.lower().endswith(metric_label.lower())
                    else f"{label} {metric_label}"
                )
                _add(full_name, f"{new_device_id}-{key}-{accessor}", "sensor")

        # Legacy integration exposed only a single "{name} Power" sensor per
        # output (reading the same "powerI" field as the new
        # "output_power_watts" accessor) -- no separate voltage/current/
        # frequency entities existed before, so only this one needs an
        # old-name alias to keep its history.
        for label in labels:
            _add(f"{label} Power", f"{new_device_id}-{key}-output_power_watts", "sensor")

        # sensor.py: HydrosDosedTodaySensor, named "{output_name} Dosed Today".
        # sensor.py: HydrosReservoirSensor, named "{output_name} Reservoir Remaining".
        if "dose" in item.commands:
            for label in labels:
                _add(f"{label} Dosed Today", f"{new_device_id}-{key}-dosed-today", "sensor")
                _add(
                    f"{label} Reservoir Remaining",
                    f"{new_device_id}-{key}-reservoir",
                    "sensor",
                )

        # number.py: HydrosCommandNumber, named "{output_name} {command label}".
        if not item.is_mode:
            for command_name, spec in item.commands.items():
                if spec.is_parameterized:
                    command_label = spec.label or command_name
                    for label in labels:
                        _add(
                            f"{label} {command_label}",
                            f"{new_device_id}-{key}-{command_name}",
                            "number",
                        )

    # Legacy integration exposed one hardware-reported "XP8 Total Power"
    # aggregate per power-monitoring strip; the public API has no equivalent
    # per-strip grouping, so the new integration instead sums every output
    # currently reporting a power reading (see sensor.HydrosTotalPowerSensor).
    # Alias both plausible old names on a best-effort basis -- this is a
    # different computed value, not a guaranteed hardware-equivalent one.
    for old_name in ("Total Power", "XP8 Total Power"):
        _add(old_name, f"{new_device_id}-total-power", "sensor")

    # sensor.py: HydrosAlertsSensor, named "Alerts".
    _add("Alerts", f"{new_device_id}-alerts", "sensor")

    for series in log_series:
        # sensor.py (analog/enum) and binary_sensor.py (bool) all use
        # unique_id "{device_id}-{input_name}", split by sensor_type's domain.
        domain = "binary_sensor" if series.sensor_type == "bool" else "sensor"
        _add(series.name, f"{new_device_id}-{series.name}", domain)

    return candidates


async def async_migrate_device_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    old_thing_id: str,
    new_device_id: str,
    override_metadata: list[OverrideMetadataEntry],
    log_series: list[LogSeriesInfo],
) -> MigrationResult:
    """Remap one device's identifiers from its old ``thing_id`` to its new
    ``device_id``, and best-effort rename its entities' ``unique_id``\\ s to
    the new override/log-series keys.

    Must run before the new platforms create any entities for
    ``new_device_id``, otherwise the freshly created entities would collide
    with (or be ignored in favor of) the migrated registry rows.
    """
    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get_device_by_identifier(
        (DOMAIN, old_thing_id), entry.entry_id
    )
    old_device_name: str | None = None
    if device_entry is not None:
        old_device_name = device_entry.name_by_user or device_entry.name
        device_registry.async_update_device(
            device_entry.id, new_identifiers={(DOMAIN, new_device_id)}
        )
        _LOGGER.info(
            "Hydros migration: device %s -> %s", old_thing_id, new_device_id
        )

    candidates = _build_candidates(new_device_id, override_metadata, log_series)

    entity_registry = er.async_get(hass)
    old_prefix = f"{entry.entry_id}-{old_thing_id}-"
    migrated: list[str] = []
    unmatched: list[str] = []

    for registry_entry in list(entity_registry.entities.values()):
        if registry_entry.config_entry_id != entry.entry_id:
            continue
        if not (registry_entry.unique_id or "").startswith(old_prefix):
            continue

        label = registry_entry.original_name or registry_entry.name or ""
        old_domain = registry_entry.entity_id.partition(".")[0]

        # The legacy integration baked the device name directly into each
        # entity's flat name (e.g. "100G Sump Temp"), while the new API's
        # names are unprefixed ("Sump Temp"). Try the de-prefixed label
        # first, falling back to the raw label for anything that wasn't
        # actually prefixed.
        unprefixed_label = label
        if old_device_name and label.startswith(f"{old_device_name} "):
            unprefixed_label = label[len(old_device_name) + 1 :]
        candidate = candidates.get((_normalize(unprefixed_label), old_domain)) or candidates.get(
            (_normalize(label), old_domain)
        )

        # No same-domain match: HA ties history to entity_id, and an entity's
        # domain is fixed at creation time, so a cross-domain match can't be used.
        if candidate is None:
            unmatched.append(label or registry_entry.entity_id)
            continue

        try:
            entity_registry.async_update_entity(
                registry_entry.entity_id, new_unique_id=candidate.unique_id
            )
        except ValueError:
            # The target unique_id is already taken by another registry
            # entry -- most likely the new platform already created it (if
            # migration is being re-run after a prior partial attempt) or
            # two old entities coincidentally map to the same candidate.
            # Leave this entity alone rather than aborting the whole
            # migration for every other device.
            _LOGGER.warning(
                "Hydros migration: could not retarget %s to unique_id %s "
                "(already in use); leaving it unmigrated",
                registry_entry.entity_id,
                candidate.unique_id,
            )
            unmatched.append(label or registry_entry.entity_id)
            continue
        migrated.append(label or registry_entry.entity_id)

    if unmatched:
        _LOGGER.warning(
            "Hydros migration: %d entities for %s have no same-domain "
            "equivalent in the new API and will stop updating (history is "
            "preserved, but they can be deleted manually): %s",
            len(unmatched),
            old_thing_id,
            ", ".join(unmatched),
        )

    return MigrationResult(migrated=migrated, unmatched=unmatched)
