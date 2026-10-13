"""Base entity: device info from DEVINFO, availability from the manager's session."""
from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MAC, DOMAIN
from .manager import CameraManager


class VtechEntity(CoordinatorEntity[CameraManager]):
    _attr_has_entity_name = True

    def __init__(self, manager: CameraManager, key: str) -> None:
        super().__init__(manager)
        entry = manager.config_entry
        self._attr_unique_id = f"{manager.uid}_{key}"
        self._attr_translation_key = key
        dev = manager.data.devinfo if manager.data else None
        mac = entry.data.get(CONF_MAC)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, manager.uid)},
            connections={(CONNECTION_NETWORK_MAC, mac)} if mac else set(),
            manufacturer="VTech",
            model=dev.model if dev else "RM5754HD",
            sw_version=dev.firmware if dev else None,
            name=entry.title,
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.session is not None and self.coordinator.data is not None
