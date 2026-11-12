"""Hold-to-talk from a dashboard: the talk card streams the microphone, which plays live on a speaker.

- Websocket command `vtech_baby/talk` (`entity_id`, `sample_rate`), from any user allowed to control that
  speaker, is a subscription. Its first event, `ready`, carries a binary handler id; the card then sends
  the microphone as binary messages, that id followed by 16-bit mono PCM at `sample_rate`. An empty
  message ends the audio; so does unsubscribing, closing the websocket, IDLE_S without audio, or MAX_S of
  audio. The last event is `end` once the audio has played, or `error`.
- The audio plays through `media_player.play_media` (announce), so a lullaby stops and resumes as for any
  announcement. Its content id, `vtech_baby_talk:<id>`, names the talk's audio in memory (`LiveAudio`),
  which the speaker takes once and decodes as it arrives. No HTTP is involved, so reverse proxies and
  HA's own URLs don't matter. Nothing is written to disk.
- One talk at a time per speaker: another while one plays is refused, since replacing an announcement
  loses the lullaby it would resume.
- The card itself (`frontend/vtech-talk-card.js`) is served by the integration, which keeps a dashboard
  resource for it (storage mode), so users add none. It isn't loaded through the page's extra modules: the
  mobile app's start page comes from a service-worker cache that can predate the card, so it never loads.
"""
from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from typing import Any

import logging

import voluptuous as vol

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components import websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.media_player import (
    ATTR_MEDIA_ANNOUNCE,
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    SERVICE_PLAY_MEDIA,
    MediaType,
)
from homeassistant.components.media_player import DOMAIN as MP_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .vtech_lan.audio import LiveAudio

AUDIO_ID = "vtech_baby_talk:"      # + the id: a play_media content id for a talk's audio
CARD_URL = "/vtech_baby/vtech-talk-card.js"
CARD_FILE = Path(__file__).parent / "frontend" / "vtech-talk-card.js"
IDLE_S = 3.0                       # no audio for this long ends the talk
MAX_S = 120.0                      # audio longer than this is cut off
DATA = f"{DOMAIN}_talk"
_LOG = logging.getLogger(__name__)


class _Talks:
    def __init__(self):
        self.audio: dict[str, LiveAudio] = {}
        self.busy: set[str] = set()


async def async_setup(hass: HomeAssistant):
    talks = hass.data[DATA] = _Talks()
    websocket_api.async_register_command(hass, ws_talk)
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(CARD_FILE), False)])
    digest = await hass.async_add_executor_job(lambda: hashlib.sha256(CARD_FILE.read_bytes()).hexdigest())
    await _async_register_card(hass, f"{CARD_URL}?v={digest[:12]}")     # a new card busts browsers' caches


def take_audio(hass: HomeAssistant, media_id: str) -> LiveAudio | None:
    """The talk audio that `media_id` names, once; None for any other content id."""
    if not media_id.startswith(AUDIO_ID) or (talks := hass.data.get(DATA)) is None:
        return None
    return talks.audio.pop(media_id[len(AUDIO_ID):], None)


async def _async_register_card(hass: HomeAssistant, url: str):
    """Keep one dashboard resource for the card at `url`. Dashboards in YAML resource mode can't be
    changed from here: the card then loads through the page's extra modules, and the log says to add it."""
    from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE  # noqa: PLC0415

    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None or lovelace.resource_mode != MODE_STORAGE:
        if "frontend" in hass.config.components:
            from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415

            add_extra_js_url(hass, url)
        _LOG.info("dashboard resources are in YAML: add %s as a module resource for the talk card", url)
        return
    resources = lovelace.resources
    await resources.async_get_info()                           # loads the stored resources
    ours = [r for r in resources.async_items() if r["url"].split("?")[0] == CARD_URL]
    if not ours:
        await resources.async_create_item({"res_type": "module", "url": url})
        return
    if ours[0]["url"] != url:
        await resources.async_update_item(ours[0]["id"], {"res_type": "module", "url": url})
    for extra in ours[1:]:
        await resources.async_delete_item(extra["id"])


@websocket_api.websocket_command({
    vol.Required("type"): "vtech_baby/talk",
    vol.Required("entity_id"): cv.entity_id,
    vol.Required("sample_rate"): vol.All(int, vol.Range(min=8000, max=96000)),
})
@callback
def ws_talk(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]):
    talks: _Talks = hass.data[DATA]
    entity_id, msg_id = msg["entity_id"], msg["id"]
    reg = er.async_get(hass).async_get(entity_id)
    if reg is None or reg.platform != DOMAIN or reg.domain != MP_DOMAIN:
        connection.send_error(msg_id, websocket_api.ERR_NOT_FOUND, f"{entity_id} is not a VTech speaker")
        return
    if not connection.user.permissions.check_entity(entity_id, POLICY_CONTROL):
        raise Unauthorized(entity_id=entity_id, permission=POLICY_CONTROL)
    if entity_id in talks.busy:
        connection.send_error(msg_id, "busy", "already talking on this speaker")
        return

    audio, token = LiveAudio(msg["sample_rate"], IDLE_S, MAX_S), secrets.token_urlsafe(32)
    talks.audio[token] = audio
    talks.busy.add(entity_id)

    def on_audio(_hass: HomeAssistant, _conn: websocket_api.ActiveConnection, data: bytes):
        if data:
            audio.feed(data)
        else:
            audio.end()

    handler_id, unregister = connection.async_register_binary_handler(on_audio)

    async def play():
        result: dict[str, Any] = {"type": "end"}
        try:
            await hass.services.async_call(MP_DOMAIN, SERVICE_PLAY_MEDIA, {
                ATTR_ENTITY_ID: entity_id,
                ATTR_MEDIA_CONTENT_ID: AUDIO_ID + token,
                ATTR_MEDIA_CONTENT_TYPE: MediaType.MUSIC,
                ATTR_MEDIA_ANNOUNCE: True,
            }, blocking=True, context=connection.context(msg))
        except HomeAssistantError as err:
            _LOG.warning("talk on %s failed: %s", entity_id, err)
            result = {"type": "error", "message": str(err)}
        finally:
            unregister()
            audio.end()
            talks.audio.pop(token, None)
            talks.busy.discard(entity_id)
        if connection.subscriptions.pop(msg_id, None) is not None:     # else the card has gone
            connection.send_event(msg_id, result)

    hass.async_create_task(play(), f"vtech_baby talk {entity_id}")
    connection.subscriptions[msg_id] = audio.end          # unsubscribing or disconnecting ends the audio
    connection.send_result(msg_id)
    connection.send_event(msg_id, {"type": "ready", "handler_id": handler_id})

