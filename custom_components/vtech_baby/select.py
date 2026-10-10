"""Selects: night-vision mode, resolution, motion sensitivity, notification interval,
lullaby timer (HA_INTEGRATION.md §7)."""
from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import VtechEntity
from .manager import CameraManager, CameraState
from .vtech_lan import ops
from .vtech_lan.session import Session


async def _night_vision(m: CameraManager, v: int) -> None:
    await m.async_command(Session.set_night_vision, ops.NightVisionMode(v))
    m.async_patch_state(night_vision=v)


async def _quality(m: CameraManager, v: int) -> None:
    # While the parent handset is on the camera runs at 720p, and a 1080p request then seems to replace
    # the stored preference with 720p (kit 40), so refuse it; the camera returns to 1080p by itself when
    # the handset goes off.
    if v == ops.Quality.FHD_1080P and m.data is not None and m.data.parent_active:
        raise HomeAssistantError("The parent unit is on, so the camera is limited to 720p; it returns to "
                                 "its 1080p setting when the parent unit is switched off")
    # Show what the camera actually applied.
    m.async_patch_state(quality=int(await m.async_command(Session.set_quality, ops.Quality(v))))


async def _motion(m: CameraManager, v: int) -> None:
    await m.async_command(Session.set_motion_sensitivity, ops.MotionSensitivity(v))
    st = m.data
    m.async_patch_state(
        motion=dataclasses.replace(st.motion, sensitivity=v) if st.motion else None,
        params=dataclasses.replace(st.params, motion_sensitivity=v) if st.params else None)


async def _notify(m: CameraManager, v: int) -> None:
    m.async_patch_state(notify_interval=int(await m.async_command(Session.set_notify_interval, ops.NotifyInterval(v))))


async def _lullaby_timer(m: CameraManager, v: int) -> None:
    await m.async_command(lambda s: s.update_params(lullaby_timer_s=v))
    if m.data.params:
        m.async_patch_state(params=dataclasses.replace(m.data.params, lullaby_timer_s=v))


@dataclass(frozen=True, kw_only=True)
class VtechSelectDescription(SelectEntityDescription):
    values: dict[str, int]                              # option -> wire value
    current: Callable[[CameraState], int | None]
    write: Callable[[CameraManager, int], Awaitable[None]]


SELECTS: tuple[VtechSelectDescription, ...] = (
    VtechSelectDescription(
        key="night_vision", entity_category=EntityCategory.CONFIG, values={"auto": 0, "on": 1, "off": 2},
        current=lambda s: s.night_vision, write=_night_vision),
    VtechSelectDescription(
        key="resolution", entity_category=EntityCategory.CONFIG, values={"1080p": 2, "720p": 1},
        current=lambda s: s.quality, write=_quality),
    VtechSelectDescription(
        key="motion_sensitivity", entity_category=EntityCategory.CONFIG,
        values={"off": 0, "low": 25, "medium": 50, "high": 75},
        current=lambda s: s.motion.sensitivity if s.motion else None, write=_motion),
    VtechSelectDescription(
        key="notify_interval", entity_category=EntityCategory.CONFIG,
        values={"default": 0, "1_min": 1, "3_min": 2, "5_min": 3, "10_min": 4, "30_min": 5, "never": 6},
        current=lambda s: s.notify_interval, write=_notify),
    VtechSelectDescription(
        key="lullaby_timer", entity_category=EntityCategory.CONFIG,
        values={"15_min": 900, "30_min": 1800, "60_min": 3600, "non_stop": 0},
        current=lambda s: s.params.lullaby_timer_s if s.params else None, write=_lullaby_timer),
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(VtechSelect(entry.runtime_data, d) for d in SELECTS)


class VtechSelect(VtechEntity, SelectEntity):
    entity_description: VtechSelectDescription

    def __init__(self, manager: CameraManager, description: VtechSelectDescription) -> None:
        super().__init__(manager, description.key)
        self.entity_description = description
        self._attr_options = list(description.values)

    @property
    def current_option(self) -> str | None:
        st = self.coordinator.data
        v = self.entity_description.current(st) if st else None
        return next((k for k, w in self.entity_description.values.items() if w == v), None)

    async def async_select_option(self, option: str) -> None:
        await self.entity_description.write(self.coordinator, self.entity_description.values[option])
