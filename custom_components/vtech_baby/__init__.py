"""The VTech RM5754HD baby-monitor integration: LAN-direct over the vendor's Kalay SDK.

Design notes: AGENTS.md. The SDK runs in one worker process shared by all entries
(`vtech_lan/worker.py`); each config entry owns one CameraManager (one camera session).
"""
from __future__ import annotations

import datetime as dt

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType

from .const import CONF_SDK_DIR, CONF_UID, DOMAIN, ISSUE_RESTART_REQUIRED
from .manager import CameraManager, SdkSetupError, async_load_sdk

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR, Platform.CAMERA, Platform.EVENT, Platform.LIGHT, Platform.MEDIA_PLAYER,
    Platform.NUMBER, Platform.SELECT,
    Platform.SENSOR, Platform.SWITCH,
]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICE_GET_MOTION_EVENTS = "get_motion_events"
GET_MOTION_EVENTS_SCHEMA = vol.Schema({
    vol.Required("config_entry_id"): cv.string,
    vol.Optional("hours", default=24): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=24 * 31)),
})

type VtechConfigEntry = ConfigEntry[CameraManager]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def get_motion_events(call: ServiceCall) -> ServiceResponse:
        """The camera's stored motion history (it stores only motion), newest first."""
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError("not a loaded VTech baby monitor config entry")
        end = dt.datetime.now(dt.UTC)
        start = end - dt.timedelta(hours=call.data["hours"])
        records = await entry.runtime_data.async_command(lambda s: s.list_events(start, end))
        return {"events": [{"time": r.time.isoformat() if r.time else None, "event": r.event, "status": r.status}
                           for r in records]}

    hass.services.async_register(DOMAIN, SERVICE_GET_MOTION_EVENTS, get_motion_events,
                                 schema=GET_MOTION_EVENTS_SCHEMA, supports_response=SupportsResponse.ONLY)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: VtechConfigEntry) -> bool:
    try:
        handle = await async_load_sdk(hass, entry.data[CONF_SDK_DIR])
    except SdkSetupError as err:
        raise ConfigEntryError(f"cannot prepare the VTech SDK: {err}") from err
    # Restarts are no longer needed to switch SDKs (the worker restarts instead): drop the old issue.
    ir.async_delete_issue(hass, DOMAIN, ISSUE_RESTART_REQUIRED)

    # The night-light moved from a select to a light (2026-10-06); drop the stale select entry.
    reg = er.async_get(hass)
    if old := reg.async_get_entity_id("select", DOMAIN, f"{entry.data[CONF_UID]}_night_light"):
        reg.async_remove(old)

    manager = CameraManager(hass, entry, handle.client)
    await manager.async_first_connect()
    entry.runtime_data = manager
    entry.async_on_unload(manager.async_close)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VtechConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
