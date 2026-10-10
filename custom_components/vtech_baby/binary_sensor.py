"""Binary sensors: night vision active, parent unit active (pushes), and motion / sound with an
auto-off timer driven by 0x1fff alerts."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import SIGNAL_EVENT
from .entity import VtechEntity
from .manager import CameraManager, CameraState
from .vtech_lan import ops


@dataclass(frozen=True, kw_only=True)
class VtechBinaryDescription(BinarySensorEntityDescription):
    is_on: Callable[[CameraState], bool | None]


STATE_SENSORS = (
    VtechBinaryDescription(key="night_vision_active", entity_category=EntityCategory.DIAGNOSTIC,
                           is_on=lambda s: s.night_active),
    VtechBinaryDescription(key="parent_unit", device_class=BinarySensorDeviceClass.CONNECTIVITY,
                           entity_category=EntityCategory.DIAGNOSTIC, is_on=lambda s: s.parent_active),
)
ALERT_SENSORS = (
    (BinarySensorEntityDescription(key="motion", device_class=BinarySensorDeviceClass.MOTION), ops.EventCode.MOTION),
    (BinarySensorEntityDescription(key="sound", device_class=BinarySensorDeviceClass.SOUND), ops.EventCode.SOUND),
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    m: CameraManager = entry.runtime_data
    async_add_entities([*(VtechStateBinary(m, d) for d in STATE_SENSORS),
                        *(VtechAlertBinary(m, d, code) for d, code in ALERT_SENSORS)])


class VtechStateBinary(VtechEntity, BinarySensorEntity):
    entity_description: VtechBinaryDescription

    def __init__(self, manager: CameraManager, description: VtechBinaryDescription) -> None:
        super().__init__(manager, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        st = self.coordinator.data
        return self.entity_description.is_on(st) if st else None


class VtechAlertBinary(VtechEntity, BinarySensorEntity):
    """On when the camera reports the alert; off after the timeout. A new alert restarts the timer.
    Self-triggered sound (during/just after a lullaby) is ignored."""

    _attr_is_on = False

    def __init__(self, manager: CameraManager, description: BinarySensorEntityDescription, code: int) -> None:
        super().__init__(manager, description.key)
        self.entity_description = description
        self._code = code
        self._cancel: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_EVENT.format(self.coordinator.config_entry.entry_id), self._on_event))
        self.async_on_remove(self._cancel_timer)

    @callback
    def _cancel_timer(self) -> None:
        if self._cancel is not None:
            self._cancel()
            self._cancel = None

    @callback
    def _on_event(self, report: ops.EventReport, self_triggered: bool) -> None:
        if report.code != self._code or self_triggered:
            return
        self._cancel_timer()
        self._attr_is_on = True
        self._cancel = async_call_later(self.hass, self.coordinator.event_timeout_s(), self._off)
        self.async_write_ha_state()

    @callback
    def _off(self, _now) -> None:
        self._cancel = None
        self._attr_is_on = False
        self.async_write_ha_state()
