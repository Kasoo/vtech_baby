"""Sensors: temperature, temperature state, sound sensitivity (read-only on this camera)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import VtechEntity
from .manager import CameraManager, CameraState
from .vtech_lan import ops

TEMP_STATES = {ops.TempState.IN_RANGE: "ok", ops.TempState.TOO_HOT: "too_hot", ops.TempState.TOO_COLD: "too_cold"}
SOUND_SENS = {ops.SoundSensitivity.LOW: "low", ops.SoundSensitivity.MEDIUM: "medium", ops.SoundSensitivity.HIGH: "high"}


@dataclass(frozen=True, kw_only=True)
class VtechSensorDescription(SensorEntityDescription):
    value: Callable[[CameraState], Any]


SENSORS: tuple[VtechSensorDescription, ...] = (
    VtechSensorDescription(
        key="temperature", device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value=lambda s: s.params.temp_c if s.params else None),
    VtechSensorDescription(
        key="temperature_state", device_class=SensorDeviceClass.ENUM, options=list(TEMP_STATES.values()),
        value=lambda s: TEMP_STATES.get(s.temp_state)),
    VtechSensorDescription(
        key="sound_sensitivity", device_class=SensorDeviceClass.ENUM, options=list(SOUND_SENS.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda s: SOUND_SENS.get(s.params.sound_sensitivity) if s.params else None),
    VtechSensorDescription(
        key="connection", device_class=SensorDeviceClass.ENUM, options=["lan", "p2p", "relay"],
        entity_category=EntityCategory.DIAGNOSTIC, value=lambda s: s.connection_mode),
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    manager: CameraManager = entry.runtime_data
    async_add_entities(VtechSensor(manager, d) for d in SENSORS)


class VtechSensor(VtechEntity, SensorEntity):
    entity_description: VtechSensorDescription

    def __init__(self, manager: CameraManager, description: VtechSensorDescription) -> None:
        super().__init__(manager, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.coordinator.data) if self.coordinator.data else None
