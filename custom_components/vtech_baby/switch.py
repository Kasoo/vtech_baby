"""Switches: status LED, temperature alerts, sound detection."""
from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import VtechEntity
from .manager import CameraManager, CameraState
from .vtech_lan.session import Session


async def _led(m: CameraManager, on: bool) -> None:
    await m.async_command(Session.set_status_led, on)
    m.async_patch_state(status_led=on)


async def _temp_alert(m: CameraManager, on: bool) -> None:
    await m.async_command(lambda s: s.set_temp_alert(enabled=on))
    if m.data.temp_alert:
        m.async_patch_state(temp_alert=dataclasses.replace(m.data.temp_alert, enabled=on))


async def _sound(m: CameraManager, on: bool) -> None:
    await m.async_command(Session.set_sound_detect, on)
    m.async_patch_state(sound_detect=on)


@dataclass(frozen=True, kw_only=True)
class VtechSwitchDescription(SwitchEntityDescription):
    is_on: Callable[[CameraState], bool | None]
    write: Callable[[CameraManager, bool], Awaitable[None]]


SWITCHES: tuple[VtechSwitchDescription, ...] = (
    VtechSwitchDescription(key="status_led", entity_category=EntityCategory.CONFIG,
                           is_on=lambda s: s.status_led, write=_led),
    VtechSwitchDescription(key="temperature_alerts", entity_category=EntityCategory.CONFIG,
                           is_on=lambda s: s.temp_alert.enabled if s.temp_alert else None, write=_temp_alert),
    VtechSwitchDescription(key="sound_detection", entity_category=EntityCategory.CONFIG,
                           is_on=lambda s: s.sound_detect, write=_sound),
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(VtechSwitch(entry.runtime_data, d) for d in SWITCHES)


class VtechSwitch(VtechEntity, SwitchEntity):
    entity_description: VtechSwitchDescription

    def __init__(self, manager: CameraManager, description: VtechSwitchDescription) -> None:
        super().__init__(manager, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        st = self.coordinator.data
        return self.entity_description.is_on(st) if st else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.entity_description.write(self.coordinator, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.entity_description.write(self.coordinator, False)
