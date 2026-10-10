"""Light: the night-light, as a colour-temperature light limited to the camera's warm presets.

The camera takes a preset (0x7a2: off / yellow / orange / red) and has no brightness. The light spans
500-2900 K; a requested temperature snaps to the nearest preset and the state reports that preset's
temperature. Brightness is fixed at 255.

Range maths: the frontend's four default colour presets sit at min + i*(max-min)/3, here 500, 1300, 2100
and 2900 K, which snap to red, orange, orange, yellow, so every colour is one tap away. The frontend draws
a temperature with Helland's formula without clamping, so 500 K shows as pure red (255, 0, 0), 1700 K as
orange (255, 120, 0) and 2900 K as the warmest near-white; HA core's rgb_color attribute clamps at 1000 K.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_COLOR_TEMP_KELVIN, ColorMode, LightEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import VtechEntity
from .manager import CameraManager
from .vtech_lan import ops
from .vtech_lan.session import Session

# Preset -> the colour temperature reported for it (band edges fall halfway: 1100 K and 2300 K).
KELVIN = {ops.NightLight.RED: 500, ops.NightLight.ORANGE: 1700, ops.NightLight.YELLOW: 2900}
MIN_KELVIN, MAX_KELVIN = 500, 2900


def preset_for(kelvin: int) -> ops.NightLight:
    """The preset nearest to a requested colour temperature (anything outside the range clamps)."""
    return min(KELVIN, key=lambda p: abs(KELVIN[p] - kelvin))


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([VtechNightLight(entry.runtime_data)])


class VtechNightLight(VtechEntity, LightEntity):
    _attr_color_mode = ColorMode.COLOR_TEMP
    _attr_supported_color_modes = {ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = MIN_KELVIN
    _attr_max_color_temp_kelvin = MAX_KELVIN

    def __init__(self, manager: CameraManager) -> None:
        super().__init__(manager, "night_light")
        self._last = ops.NightLight.YELLOW          # colour restored by a plain turn_on
        self._remember()

    def _preset(self) -> ops.NightLight | None:
        st = self.coordinator.data
        v = st.night_light if st else None
        return ops.NightLight(v) if v in KELVIN or v == ops.NightLight.OFF else None

    def _remember(self) -> None:
        if (p := self._preset()) in KELVIN:
            self._last = p

    @callback
    def _handle_coordinator_update(self) -> None:
        self._remember()                            # handset and push changes count as the last colour
        super()._handle_coordinator_update()

    @property
    def is_on(self) -> bool | None:
        st = self.coordinator.data
        v = st.night_light if st else None
        return None if v is None else v != ops.NightLight.OFF

    @property
    def brightness(self) -> int | None:
        return 255 if self.is_on else None

    @property
    def color_temp_kelvin(self) -> int | None:
        p = self._preset()
        return KELVIN[p] if p in KELVIN else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            preset = preset_for(kwargs[ATTR_COLOR_TEMP_KELVIN])
        elif self.is_on:
            self._snap_back()                       # brightness-only change: nothing to dim
            return
        else:
            preset = self._last
        await self._write(preset)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(ops.NightLight.OFF)

    async def _write(self, preset: ops.NightLight) -> None:
        before = self._preset()
        await self.coordinator.async_command(Session.set_night_light, preset)
        self.coordinator.async_patch_state(night_light=int(preset))
        if preset == before:
            self._snap_back()

    def _snap_back(self) -> None:
        """Re-send an unchanged state so the frontend's sliders return to the real value.

        HA drops a state write that changes nothing, and the dialog only resets its sliders on a new
        state; a forced write produces one.
        """
        self._attr_force_update = True
        try:
            self.async_write_ha_state()
        finally:
            self._attr_force_update = False
