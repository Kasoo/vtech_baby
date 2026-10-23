"""Diagnostics: redacted credentials, the SDK worker, session state, cached camera state, media stats."""
from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import BACKEND, CONF_AUTHKEY, CONF_AV_PASSWORD, CONF_MAC, CONF_UID
from .vtech_lan.errors import VtechError

TO_REDACT = {CONF_UID, CONF_AV_PASSWORD, CONF_AUTHKEY, CONF_MAC, "unique_id", "title"}


def _link_diagnostics(session) -> dict[str, Any] | None:
    """The camera's vtech-link `info()` and `stats()`, if its session runs over vtech-link."""
    if session is None or session.backend != "link":
        return None
    try:
        return {"info": session.info(), "stats": session.stats()}
    except VtechError as err:
        return {"error": str(err)}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any]:
    manager = entry.runtime_data
    client = manager.client
    session = manager.session
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "backend": BACKEND,
        "sdk": await hass.async_add_executor_job(client.sdk_info),
        "worker": client.stats(),
        "connected": session is not None,
        "link": await hass.async_add_executor_job(_link_diagnostics, session),
        "consecutive_failures": manager.failures,
        "last_error": manager.last_error,
        "state": dataclasses.asdict(manager.data) if manager.data else None,
        "live_view": await hass.async_add_executor_job(manager.media.stats),
        "talk_back": await hass.async_add_executor_job(manager.talk.stats),
    }
