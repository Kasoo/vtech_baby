"""Loopback-only HTTP server for the live view (HA_INTEGRATION.md §6 Phase 5).

`camera.stream_source()` returns `http://127.0.0.1:<port>/<token>/<uid>.ts`. HA's go2rtc (WebRTC) and
stream component (HLS) open it; each GET subscribes to the camera's LiveStream and is served MPEG-TS
until the client goes away or the stream ends.

- Bound to 127.0.0.1 only: HA runs with host networking, and backends must not listen beyond what needs
  them (root AGENTS.md). The random per-process token keeps other local processes off the feed.
- One server per HA process, shared by all cameras. It only listens once something asks for a stream
  (the first `stream_source()`) and then keeps its port and token until HA stops, so the URL HA's
  go2rtc has registered stays valid across entry reloads. (Stopping it on unload made a reload wait
  for aiohttp's graceful shutdown of go2rtc's open request, and gave go2rtc a stale URL; 2026-10-06.)
- On HA stop, open requests are ended first, then the server is cleaned up.
- The media thread hands chunks over with `call_soon_threadsafe`; a client that falls `QUEUE_CHUNKS`
  behind is cut off (it reconnects at a keyframe) rather than buffering without bound.
"""
from __future__ import annotations

import asyncio
import logging
import secrets

from aiohttp import web

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant

from .const import DOMAIN
from .vtech_lan.errors import SessionGone
from .vtech_lan.media import LiveStream

_LOG = logging.getLogger(__name__)

DATA_SERVER = f"{DOMAIN}_media_server"
QUEUE_CHUNKS = 600            # ~15–30 s of stream; beyond that the client is too slow


class MediaServer:
    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(24)
        self.port: int | None = None
        self._streams: dict[str, LiveStream] = {}
        self._runner: web.AppRunner | None = None
        self._lock = asyncio.Lock()
        self._open: set[asyncio.Queue] = set()               # one per request being served

    async def async_url(self, uid: str) -> str:
        async with self._lock:
            if self._runner is None:
                await self._async_start()
        return f"http://127.0.0.1:{self.port}/{self.token}/{uid}.ts"

    async def _async_start(self) -> None:
        app = web.Application()
        app.router.add_get("/{token}/{uid}.ts", self._handle)
        self._runner = web.AppRunner(app, access_log=None, handle_signals=False)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]   # noqa: SLF001 - aiohttp has no public getter
        _LOG.debug("live-view server on 127.0.0.1:%s", self.port)

    async def async_stop(self) -> None:
        for q in list(self._open):
            q.put_nowait(None)                                  # let every open request finish now
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def add(self, uid: str, stream: LiveStream) -> None:
        self._streams[uid] = stream

    def remove(self, uid: str) -> None:
        """Forget a camera (entry unload). Its open requests end when its LiveStream closes."""
        self._streams.pop(uid, None)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if not secrets.compare_digest(request.match_info["token"], self.token):
            raise web.HTTPNotFound
        stream = self._streams.get(request.match_info["uid"])
        if stream is None:
            raise web.HTTPNotFound
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def put(data: bytes | None) -> None:                    # event loop
            if data is not None and queue.qsize() >= QUEUE_CHUNKS:
                _LOG.debug("%s: live-view client too slow; disconnecting it", stream.name)
                data = None
            queue.put_nowait(data)

        try:
            sub = stream.subscribe(lambda data: loop.call_soon_threadsafe(put, data))
        except SessionGone as err:
            raise web.HTTPServiceUnavailable(text=str(err)) from err
        resp = web.StreamResponse(headers={"Content-Type": "video/mp2t", "Cache-Control": "no-store"})
        self._open.add(queue)
        try:
            await resp.prepare(request)
            while (data := await queue.get()) is not None:
                await resp.write(data)
        except (ConnectionResetError, ConnectionError):
            pass
        finally:
            self._open.discard(queue)
            sub.close()
        return resp


def get_server(hass: HomeAssistant) -> MediaServer:
    """The process-wide server (listening only once a stream is asked for), stopped with HA."""
    server: MediaServer | None = hass.data.get(DATA_SERVER)
    if server is None:
        server = hass.data[DATA_SERVER] = MediaServer()

        async def _stop(_event: Event) -> None:
            await server.async_stop()
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)
    return server


def release_camera(hass: HomeAssistant, uid: str) -> None:
    server: MediaServer | None = hass.data.get(DATA_SERVER)
    if server is not None:
        server.remove(uid)
