"""Event entity: camera alerts from 0x1fff — motion, sound, too_hot, too_cold."""
from __future__ import annotations

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SIGNAL_EVENT
from .entity import VtechEntity
from .manager import CameraManager
from .vtech_lan import ops

EVENT_TYPES = {ops.EventCode.MOTION: "motion", ops.EventCode.SOUND: "sound",
               ops.EventCode.TEMP_HIGH: "too_hot", ops.EventCode.TEMP_LOW: "too_cold"}


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([VtechAlertEvent(entry.runtime_data)])


class VtechAlertEvent(VtechEntity, EventEntity):
    entity_description = EventEntityDescription(key="alert")
    _attr_event_types = [*EVENT_TYPES.values(), "unknown"]

    def __init__(self, manager: CameraManager) -> None:
        super().__init__(manager, "alert")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_EVENT.format(self.coordinator.config_entry.entry_id), self._on_event))

    @callback
    def _on_event(self, report: ops.EventReport, self_triggered: bool) -> None:
        self._trigger_event(EVENT_TYPES.get(report.code, "unknown"), {
            "code": report.code,
            "camera_time": report.timestamp.isoformat(),
            "self_triggered": self_triggered,
        })
        self.async_write_ha_state()
