"""Hold-to-talk from a dashboard: the talk card streams the microphone, which plays live on a speaker.

- Websocket command `vtech_baby/talk` (`entity_id`, `sample_rate`), from any user allowed to control that
  speaker, is a subscription. Its first event, `ready`, carries a binary handler id; the card then sends
  the microphone as binary messages, that id followed by 16-bit mono PCM at `sample_rate`. An empty
  message ends the audio; so does unsubscribing, closing the websocket, IDLE_S without audio, or MAX_S of
  audio. The last event is `end` once the audio has played, or `error`.
- The audio plays through `media_player.play_media` (announce), so a lullaby stops and resumes as for any
  announcement. The talk-back fetches it from `/api/vtech_baby/clip/<token>`, a signed URL that serves the
  audio as it arrives, as raw `audio/L16` (a container would make the decoder read seconds ahead), and
  exists only for that talk. Nothing is written to disk.
- One talk at a time per speaker: another while one plays is refused, since replacing an announcement
  loses the lullaby it would resume.
- The card itself (`frontend/vtech-talk-card.js`) is served by the integration and loaded on every
  dashboard, so it needs no resource entry.
"""
from __future__ import annotations

import array
import asyncio
import hashlib
import secrets
import sys
from collections.abc import AsyncIterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import voluptuous as vol
from aiohttp import web

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
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

CLIP_URL = "/api/vtech_baby/clip/{token}"
CARD_URL = "/vtech_baby/vtech-talk-card.js"
CARD_FILE = Path(__file__).parent / "frontend" / "vtech-talk-card.js"
IDLE_S = 3.0                       # no audio for this long ends the talk
MAX_S = 120.0                      # audio longer than this is cut off
DATA = f"{DOMAIN}_talk"


class LiveClip:
    """Audio arriving from the card (16-bit little-endian), read once by the talk-back as `audio/L16`."""

    def __init__(self, rate: int):
        self.rate = rate
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._left = int(rate * MAX_S) * 2
        self._ended = self._taken = False

    def feed(self, data: bytes):
        if self._ended:
            return
        data = data[:self._left - self._left % 2]
        self._left -= len(data)
        if data:
            if sys.byteorder == "little":           # L16 is big-endian
                samples = array.array("h", data)
                samples.byteswap()
                data = samples.tobytes()
            self._queue.put_nowait(data)
        if self._left < 2:
            self.end()

    def end(self):
        if not self._ended:
            self._ended = True
            self._queue.put_nowait(None)

    def take(self) -> bool:
        """Claim the one read; False if it was already claimed."""
        taken, self._taken = self._taken, True
        return not taken

    @property
    def content_type(self) -> str:
        return f"audio/L16;rate={self.rate};channels=1"

    async def read(self) -> AsyncIterator[bytes]:
        while True:
            try:
                async with asyncio.timeout(IDLE_S):
                    chunk = await self._queue.get()
            except TimeoutError:
                self.end()
                return
            if chunk is None:
                return
            yield chunk


class _Talks:
    def __init__(self):
        self.clips: dict[str, LiveClip] = {}
        self.busy: set[str] = set()


async def async_setup(hass: HomeAssistant):
    talks = hass.data[DATA] = _Talks()
    websocket_api.async_register_command(hass, ws_talk)
    hass.http.register_view(ClipView(talks))
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(CARD_FILE), False)])
    if "frontend" in hass.config.components:
        from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415

        digest = await hass.async_add_executor_job(lambda: hashlib.sha256(CARD_FILE.read_bytes()).hexdigest())
        add_extra_js_url(hass, f"{CARD_URL}?v={digest[:12]}")      # a new card busts the browser's cache


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

    clip, token = LiveClip(msg["sample_rate"]), secrets.token_urlsafe(32)
    talks.clips[token] = clip
    talks.busy.add(entity_id)

    def on_audio(_hass: HomeAssistant, _conn: websocket_api.ActiveConnection, data: bytes):
        if data:
            clip.feed(data)
        else:
            clip.end()

    handler_id, unregister = connection.async_register_binary_handler(on_audio)

    async def play():
        result: dict[str, Any] = {"type": "end"}
        try:
            await hass.services.async_call(MP_DOMAIN, SERVICE_PLAY_MEDIA, {
                ATTR_ENTITY_ID: entity_id,
                ATTR_MEDIA_CONTENT_ID: CLIP_URL.format(token=token),
                ATTR_MEDIA_CONTENT_TYPE: MediaType.MUSIC,
                ATTR_MEDIA_ANNOUNCE: True,
            }, blocking=True, context=connection.context(msg))
        except HomeAssistantError as err:
            result = {"type": "error", "message": str(err)}
        finally:
            unregister()
            clip.end()
            talks.clips.pop(token, None)
            talks.busy.discard(entity_id)
        if connection.subscriptions.pop(msg_id, None) is not None:     # else the card has gone
            connection.send_event(msg_id, result)

    hass.async_create_task(play(), f"vtech_baby talk {entity_id}")
    connection.subscriptions[msg_id] = clip.end          # unsubscribing or disconnecting ends the audio
    connection.send_result(msg_id)
    connection.send_event(msg_id, {"type": "ready", "handler_id": handler_id})


class ClipView(HomeAssistantView):
    """A talk's audio, for the talk-back to fetch. Signed by `play_media`, so only HA itself has the URL."""

    url = CLIP_URL
    name = "api:vtech_baby:clip"

    def __init__(self, talks: _Talks):
        self._talks = talks

    async def get(self, request: web.Request, token: str) -> web.StreamResponse:
        if (clip := self._talks.clips.get(token)) is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        if not clip.take():
            return web.Response(status=HTTPStatus.GONE)
        resp = web.StreamResponse(headers={"Content-Type": clip.content_type, "Cache-Control": "no-store"})
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        async for chunk in clip.read():
            await resp.write(chunk)
        await resp.write_eof()
        return resp
