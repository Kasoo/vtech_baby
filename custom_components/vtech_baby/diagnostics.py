"""Diagnostics: redacted credentials, the module, session state, cached camera state, media stats."""
from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_AUTHKEY, CONF_AV_PASSWORD, CONF_MAC, CONF_UID
from .vtech_lan.errors import VtechError

TO_REDACT = {CONF_UID, CONF_AV_PASSWORD, CONF_AUTHKEY, CONF_MAC, "unique_id", "title"}


def _link_diagnostics(session) -> dict[str, Any] | None:
    """The camera's link: its `info()` and `stats()`."""
    if session is None:
        return None
    try:
        return {"info": session.info(), "stats": session.stats()}
    except VtechError as err:
        return {"error": str(err)}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any]:
    manager = entry.runtime_data
    session = manager.session
    media, talk = manager.media, manager.talk
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "module": await hass.async_add_executor_job(manager.hub.module_info),
        "connected": session is not None,
        "link": await hass.async_add_executor_job(_link_diagnostics, session),
        "consecutive_failures": manager.failures,
        "last_error": manager.last_error,
        "state": dataclasses.asdict(manager.data) if manager.data else None,
        "live_view": {**media.stats, "subscribers": media.subscribers},
        "talk_back": {**talk.stats, "running": talk.running},
    }
