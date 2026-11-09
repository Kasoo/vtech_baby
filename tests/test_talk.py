"""The talk card's websocket stream: microphone audio plays live on the speaker, against the fake link."""
import asyncio
import math
import struct
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant

from custom_components.vtech_baby import talk

from test_integration import (  # noqa: F401 - fixtures
    P, SPEAKER, auto_enable_custom_integrations, fake, module_patch, setup_entry, unload, wait_until,
)

pytest.importorskip("av")

RATE = 16000


def pcm(seconds: float, rate: int = RATE) -> bytes:
    n = int(seconds * rate)
    return struct.pack(f"<{n}h", *(int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(n)))


async def connect(hass, hass_ws_client, token=None):
    """A websocket client, once the entry (and so HTTP) is set up. HA's own URL points at its test server,
    so the talk-back can fetch the stream."""
    ws = await (hass_ws_client(access_token=token) if token else hass_ws_client())
    hass.config.internal_url = str(ws.client.make_url("")).rstrip("/")
    return ws


async def start(ws, entity_id=SPEAKER, rate=RATE):
    """Start a talk; returns (message id, binary handler id), or (message id, the error)."""
    await ws.send_json_auto_id({"type": "vtech_baby/talk", "entity_id": entity_id, "sample_rate": rate})
    r = await ws.receive_json()
    if not r["success"]:
        return r["id"], r["error"]
    ev = await ws.receive_json()
    assert ev["event"]["type"] == "ready"
    return r["id"], ev["event"]["handler_id"]


async def send_audio(ws, handler_id, audio: bytes, chunk_s=0.02, rate=RATE, pace=True):
    step = int(chunk_s * rate) * 2
    for i in range(0, len(audio), step):
        await ws.send_bytes(bytes([handler_id]) + audio[i:i + step])
        if pace:
            await asyncio.sleep(chunk_s)


async def last_event(ws, timeout=10):
    async with asyncio.timeout(timeout):
        return (await ws.receive_json())["event"]


async def live_view(hass, fake):
    from fake_camera import enable_live_view
    enable_live_view(fake.camera)
    return await setup_entry(hass)


async def test_talk_streams_while_recording(hass: HomeAssistant, fake, hass_ws_client):
    """Audio reaches the camera before the talk ends, and all of it plays."""
    entry = await live_view(hass, fake)
    ws = await connect(hass, hass_ws_client)
    _, hid = await start(ws)
    await send_audio(ws, hid, pcm(0.4))
    await wait_until(lambda: len(fake.uplink) > 0 and hass.states.get(SPEAKER).state == "playing", 10)
    await send_audio(ws, hid, pcm(0.2))
    await ws.send_bytes(bytes([hid]))                              # end of audio
    assert await last_event(ws) == {"type": "end"}
    assert 29 <= len(fake.uplink) <= 31 and fake.camera.speaker_ch is None
    assert hass.states.get(SPEAKER).state == "idle"
    await unload(hass, entry)


async def test_talk_resamples_other_rates(hass: HomeAssistant, fake, hass_ws_client):
    entry = await live_view(hass, fake)
    ws = await connect(hass, hass_ws_client)
    _, hid = await start(ws, rate=48000)
    await send_audio(ws, hid, pcm(0.6, 48000), rate=48000, pace=False)
    await ws.send_bytes(bytes([hid]))
    assert await last_event(ws) == {"type": "end"}
    assert 29 <= len(fake.uplink) <= 31
    await unload(hass, entry)


async def test_talk_ends_when_unsubscribed_or_idle(hass: HomeAssistant, fake, hass_ws_client):
    entry = await live_view(hass, fake)
    ws = await connect(hass, hass_ws_client)
    msg_id, hid = await start(ws)
    await send_audio(ws, hid, pcm(0.2))
    await ws.send_json_auto_id({"type": "unsubscribe_events", "subscription": msg_id})
    assert (await ws.receive_json())["success"]
    await wait_until(lambda: not hass.data[talk.DATA].busy, 10)
    assert hass.states.get(SPEAKER).state == "idle" and fake.camera.speaker_ch is None

    with patch.object(talk, "IDLE_S", 0.3):
        _, hid = await start(ws)
        await send_audio(ws, hid, pcm(0.2))                        # then nothing
        await wait_until(lambda: not hass.data[talk.DATA].busy, 10)
    assert hass.states.get(SPEAKER).state == "idle"
    await unload(hass, entry)


async def test_talk_one_at_a_time(hass: HomeAssistant, fake, hass_ws_client):
    entry = await live_view(hass, fake)
    ws = await connect(hass, hass_ws_client)
    _, hid = await start(ws)
    await send_audio(ws, hid, pcm(0.2))
    _, err = await start(ws)
    assert err["code"] == "busy"
    await ws.send_bytes(bytes([hid]))
    assert await last_event(ws) == {"type": "end"}
    _, hid = await start(ws)                                       # free again
    assert isinstance(hid, int)
    await ws.send_bytes(bytes([hid]))
    await wait_until(lambda: not hass.data[talk.DATA].busy, 10)
    await unload(hass, entry)


async def test_talk_refuses(hass: HomeAssistant, fake, hass_ws_client, hass_read_only_access_token):
    entry = await setup_entry(hass)
    ws = await connect(hass, hass_ws_client)
    _, err = await start(ws, entity_id=f"sensor.{P}_temperature")
    assert err["code"] == "not_found"
    reader = await connect(hass, hass_ws_client, hass_read_only_access_token)
    _, err = await start(reader)
    assert err["code"] == "unauthorized"
    await unload(hass, entry)


async def test_talk_without_audio_is_an_error(hass: HomeAssistant, fake, hass_ws_client):
    entry = await setup_entry(hass)
    ws = await connect(hass, hass_ws_client)
    sent = len(fake.sent)
    _, hid = await start(ws)
    await ws.send_bytes(bytes([hid]))
    ev = await last_event(ws)
    assert ev["type"] == "error" and "no audio" in ev["message"]
    assert 0x350 not in [c for c, _ in fake.sent[sent:]]
    await unload(hass, entry)


async def test_clip_url_needs_auth(hass: HomeAssistant, fake, hass_ws_client):
    entry = await setup_entry(hass)
    ws = await connect(hass, hass_ws_client)
    url = ws.client.make_url(talk.CLIP_URL.format(token="t" * 43))
    async with aiohttp.ClientSession() as anon, anon.get(url) as r:
        assert r.status == 401
    await unload(hass, entry)


async def test_card_is_served(hass: HomeAssistant, fake, hass_ws_client):
    entry = await setup_entry(hass)
    ws = await connect(hass, hass_ws_client)
    r = await ws.client.get(talk.CARD_URL)
    assert r.status == 200 and 'customElements.define("vtech-talk-card"' in await r.text()
    await unload(hass, entry)


async def test_card_is_a_dashboard_resource(hass: HomeAssistant, fake):
    """One module resource for the card, at the current version: a stale one is updated, a duplicate removed."""
    from homeassistant.components.lovelace.const import LOVELACE_DATA
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "lovelace", {})
    resources = hass.data[LOVELACE_DATA].resources
    await resources.async_get_info()
    await resources.async_create_item({"res_type": "module", "url": f"{talk.CARD_URL}?v=old"})
    await resources.async_create_item({"res_type": "module", "url": f"{talk.CARD_URL}?v=older"})
    await resources.async_create_item({"res_type": "module", "url": "/hacsfiles/other.js"})
    entry = await setup_entry(hass)
    urls = [r["url"] for r in resources.async_items()]
    ours = [u for u in urls if u.startswith(talk.CARD_URL)]
    assert len(ours) == 1 and "?v=old" not in ours[0] and "/hacsfiles/other.js" in urls
    await unload(hass, entry)
