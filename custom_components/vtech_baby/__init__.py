"""The VTech RM5754HD baby-monitor integration: LAN-direct over ThroughTek's Kalay protocol.

Design notes: AGENTS.md. The protocol module (vtech-link) runs in one worker process shared by all
entries (`vtech_lan/worker.py`); each config entry owns one CameraManager (one camera session).
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import shutil

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType

from .const import CONF_SDK_DIR, CONF_UID, DOMAIN, ISSUE_RESTART_REQUIRED, SDK_DIRS
from .manager import CameraManager, SetupError, async_load

_LOG = logging.getLogger(__name__)

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
        client = await async_load(hass)
    except SetupError as err:
        raise ConfigEntryError(f"cannot load the VTech protocol module: {err}") from err
    # Restarts are no longer needed to switch SDKs (the worker restarts instead): drop the old issue.
    ir.async_delete_issue(hass, DOMAIN, ISSUE_RESTART_REQUIRED)

    # The night-light moved from a select to a light (2026-10-06); drop the stale select entry.
    reg = er.async_get(hass)
    if old := reg.async_get_entity_id("select", DOMAIN, f"{entry.data[CONF_UID]}_night_light"):
        reg.async_remove(old)

    manager = CameraManager(hass, entry, client)
    await manager.async_first_connect()
    entry.runtime_data = manager
    entry.async_on_unload(manager.async_close)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VtechConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: VtechConfigEntry) -> bool:
    """Version 1 → 2: the vendor's SDK is gone. Drop the entry's SDK directory, and the extracted and
    patched libraries under the HA config directory."""
    if entry.version > 2:
        return False                            # written by a newer version of the integration
    if entry.version == 1:
        data = {k: v for k, v in entry.data.items() if k != CONF_SDK_DIR}
        await hass.async_add_executor_job(_remove_sdk_dirs, hass.config.path())
        hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True


def _remove_sdk_dirs(config_dir: str) -> None:
    """Executor job: delete what version 1 wrote under the config directory, then its parent if empty."""
    root = os.path.join(config_dir, SDK_DIRS[0][0])
    for parts in SDK_DIRS:
        path = os.path.join(config_dir, *parts)
        if os.path.isdir(path):
            _LOG.info("removing %s: the vendor's SDK is no longer used", path)
            shutil.rmtree(path, ignore_errors=True)
    try:
        os.rmdir(root)
    except OSError:
        pass                                    # absent, or holds something else
