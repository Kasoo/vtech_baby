"""Buttons: pan and tilt, one step per press."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import MOVE_STEP_S, MOVEMENT_MODELS
from .entity import VtechEntity
from .manager import CameraManager
from .vtech_lan import ops

MOVES: tuple[tuple[str, ops.Move], ...] = (
    ("pan_left", ops.Move.LEFT), ("pan_right", ops.Move.RIGHT),
    ("tilt_up", ops.Move.UP), ("tilt_down", ops.Move.DOWN),
)


def moves(model: str | None) -> bool:
    """Whether a camera reporting this DEVINFO model is known to pan and tilt."""
    return (model or "").strip().upper() in MOVEMENT_MODELS


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(VtechMoveButton(entry.runtime_data, key, direction) for key, direction in MOVES)


class VtechMoveButton(VtechEntity, ButtonEntity):
    def __init__(self, manager: CameraManager, key: str, direction: ops.Move) -> None:
        super().__init__(manager, key)
        self._direction = direction
        dev = manager.data.devinfo if manager.data else None
        self._attr_entity_registry_enabled_default = moves(dev.model if dev else None)

    async def async_press(self) -> None:
        await self.coordinator.async_command(lambda s: s.move(self._direction, MOVE_STEP_S))
