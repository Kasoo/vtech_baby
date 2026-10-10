"""Diagnostics: redacted credentials, SDK preparation, session state, cached camera state."""
from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_AUTHKEY, CONF_AV_PASSWORD, CONF_MAC, CONF_UID
from .vtech_lan import sdk as vsdk

TO_REDACT = {CONF_UID, CONF_AV_PASSWORD, CONF_AUTHKEY, CONF_MAC, "unique_id", "title"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any]:
    manager = entry.runtime_data
    loaded = vsdk.loaded()
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "sdk": {"libdir": loaded.libdir, "arch": loaded.arch, "lan_only": loaded.lan_only_stats()}
        if loaded else None,
        "connected": manager.session is not None,
        "consecutive_failures": manager.failures,
        "last_error": manager.last_error,
        "state": dataclasses.asdict(manager.data) if manager.data else None,
        "live_view": {**manager.media.stats, "subscribers": manager.media.subscribers},
    }
