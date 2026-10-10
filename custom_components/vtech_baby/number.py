"""Numbers: temperature alert thresholds, °C in 0.1 steps (×10 on the wire) (HA_INTEGRATION.md §7)."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import VtechEntity
from .manager import CameraManager


@dataclass(frozen=True, kw_only=True)
class VtechNumberDescription(NumberEntityDescription):
    field: str                     # TempAlert field: max_c / min_c


NUMBERS = tuple(
    VtechNumberDescription(
        key=key, field=field, entity_category=EntityCategory.CONFIG, device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS, native_min_value=0.0, native_max_value=40.0,
        native_step=0.1, mode=NumberMode.BOX)
    for key, field in (("temperature_max", "max_c"), ("temperature_min", "min_c")))


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(VtechNumber(entry.runtime_data, d) for d in NUMBERS)


class VtechNumber(VtechEntity, NumberEntity):
    entity_description: VtechNumberDescription

    def __init__(self, manager: CameraManager, description: VtechNumberDescription) -> None:
        super().__init__(manager, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        st = self.coordinator.data
        return getattr(st.temp_alert, self.entity_description.field) if st and st.temp_alert else None

    async def async_set_native_value(self, value: float) -> None:
        field, value = self.entity_description.field, round(value, 1)
        # The camera stores whole °F, so it applies a quantised value (26.5 °C reads back 26.6): show that.
        def write(s):
            s.set_temp_alert(**{field: value})
            return s.get_temp_alert()
        self.coordinator.async_patch_state(temp_alert=await self.coordinator.async_command(write))
