"""Sliders: chime volume and ringtones of the doorbell and the indoor monitor."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import InstantBellConfigEntry
from .const import DOMAIN
from .coordinator import StatusCoordinator
from .entity import StatusEntity
from .hub import InstantBellHub


@dataclass(frozen=True)
class NumberDef:
    """One slider: where its value is read and which chime field it writes."""

    key: str
    data_key: str  # door station; monitors under data_key + "_monitors"
    field: str
    maximum: int
    monitor: bool = False


# The firmware gives the ringtone as an index; its upper limit depends on the
# firmware, so the slider is generous (0-15) and the device ignores a value it
# does not know.
DOOR_NUMBERS = (
    NumberDef("chime_volume", "chime_volume", "volume", 7),
    NumberDef("chime_ringtone", "chime_ringtone", "doorbell", 15),
    NumberDef("chime_pir_ringtone", "chime_pir_ringtone", "pir", 15),
)
MONITOR_NUMBERS = (
    NumberDef("chime_volume_monitor", "chime_volume", "volume", 7, monitor=True),
    NumberDef("chime_ringtone_monitor", "chime_ringtone", "doorbell", 15, monitor=True),
    NumberDef("chime_pir_ringtone_monitor", "chime_pir_ringtone", "pir", 15, monitor=True),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: InstantBellConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = entry.runtime_data
    entities: list[ChimeNumber] = []
    for serial in hub.serials:
        entities.extend(ChimeNumber(hub, hub.coordinator, serial, d) for d in DOOR_NUMBERS)
        for monitor in hub.monitors.get(serial, []):
            entities.extend(ChimeNumber(hub, hub.coordinator, serial, d, owner=monitor) for d in MONITOR_NUMBERS)
    async_add_entities(entities)


class ChimeNumber(StatusEntity, NumberEntity):
    """A chime slider."""

    _attr_native_min_value = 0
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        hub: InstantBellHub,
        coordinator: StatusCoordinator,
        serial: str,
        definition: NumberDef,
        *,
        owner: str | None = None,
    ) -> None:
        super().__init__(hub, coordinator, serial, definition.key, owner=owner)
        self._definition = definition
        self._attr_native_max_value = definition.maximum

    @property
    def native_value(self) -> float | None:
        data_key = self._definition.data_key
        if self._definition.monitor:
            values = self.status.get(f"{data_key}_monitors")
            value = values.get(self._owner) if isinstance(values, dict) else None
        else:
            value = self.status.get(data_key)
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        target = max(0, min(self._definition.maximum, int(round(value))))
        try:
            await self._hub.async_call(
                self._hub.api.set_chime_fields, self._owner, **{self._definition.field: target}
            )
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="setting_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self.coordinator.async_request_refresh()
