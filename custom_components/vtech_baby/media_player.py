"""Speaker: the camera's lullabies, and media (TTS, media sources, URLs) played through it as talk-back.

- Lullabies are the sources; one plays until stopped or until the camera's lullaby timer runs out.
- Media is fetched and decoded by the SDK worker as it plays (`vtech_lan/talkback.py`), so streams of any
  length play until stopped. `play_media` returns when the media has played, so automations can sequence
  on it; new media replaces what is playing.
- Media stops a playing lullaby first. An announcement restarts that lullaby afterwards (from the start,
  with a fresh timer); other media doesn't.
- Volume is the camera's speaker volume, 0-5 mapped onto 0-1; each step is one camera level.
All of it is audible in the nursery.
"""
from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    ATTR_MEDIA_ANNOUNCE,
    BrowseMedia,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import VtechEntity
from .manager import CameraManager
from .vtech_lan import ops

SOURCES = {name: track for track, name in ops.LULLABY_NAMES.items()}
TALK_BACK = "Talk-back"


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([VtechSpeaker(entry.runtime_data)])


class VtechSpeaker(VtechEntity, MediaPlayerEntity):
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
        | MediaPlayerEntityFeature.STOP | MediaPlayerEntityFeature.BROWSE_MEDIA
        | MediaPlayerEntityFeature.SELECT_SOURCE | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP)
    _attr_source_list = list(SOURCES)
    _attr_volume_step = 1 / ops.VOLUME_MAX

    def __init__(self, manager: CameraManager) -> None:
        super().__init__(manager, "speaker")
        self._task: asyncio.Task | None = None      # the media playing or about to
        self._lock = asyncio.Lock()                 # one talk-back at a time

    @property
    def state(self) -> MediaPlayerState:
        m = self.coordinator
        return MediaPlayerState.PLAYING if m.talking or m.lullaby_playing else MediaPlayerState.IDLE

    @property
    def source(self) -> str | None:
        track = self.coordinator.lullaby_track
        return ops.LULLABY_NAMES[track] if track is not None else None

    @property
    def media_title(self) -> str | None:
        return TALK_BACK if self.coordinator.talking else self.source

    @property
    def volume_level(self) -> float | None:
        p = self.coordinator.data.params if self.coordinator.data else None
        return min(max(p.volume / ops.VOLUME_MAX, 0.0), 1.0) if p is not None else None

    @property
    def media_content_type(self) -> MediaType | None:
        return MediaType.MUSIC if self.state == MediaPlayerState.PLAYING else None

    # ── Lullabies and volume ─────────────────────────────────────────────────
    async def async_select_source(self, source: str) -> None:
        if (track := SOURCES.get(source)) is None:
            raise ServiceValidationError(f"unknown lullaby {source!r}; choose one of {', '.join(SOURCES)}")
        await self._stop_media()
        await self.coordinator.async_play_lullaby(track)

    async def async_media_stop(self) -> None:
        await self._stop_media()
        if self.coordinator.lullaby_playing:
            await self.coordinator.async_stop_lullaby()

    async def async_set_volume_level(self, volume: float) -> None:
        await self.coordinator.async_set_volume(int(volume * ops.VOLUME_MAX + 0.5))

    # ── Media (talk-back) ────────────────────────────────────────────────────
    async def async_play_media(self, media_type: MediaType | str, media_id: str, **kwargs: Any) -> None:
        if media_source.is_media_source_id(media_id):
            media_id = (await media_source.async_resolve_media(self.hass, media_id, self.entity_id)).url
        url = async_process_play_media_url(self.hass, media_id)
        old, task = self._task, self.hass.async_create_task(self._play(url, bool(kwargs.get(ATTR_MEDIA_ANNOUNCE))))
        self._task = task
        if old is not None:
            old.cancel()                # it stops its talk-back before releasing the lock
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and not (current and current.cancelling()):
                return                  # replaced by other media, or stopped
            raise

    async def async_browse_media(self, media_content_type: MediaType | str | None = None,
                                 media_content_id: str | None = None) -> BrowseMedia:
        return await media_source.async_browse_media(
            self.hass, media_content_id, content_filter=lambda item: item.media_content_type.startswith("audio/"))

    async def async_will_remove_from_hass(self) -> None:
        await self._stop_media()
        await super().async_will_remove_from_hass()

    async def _stop_media(self) -> None:
        """Stop the media playing, if any, and wait until it has stopped."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait([task])

    async def _play(self, url: str, announce: bool) -> None:
        async with self._lock:
            m = self.coordinator
            lullaby = m.lullaby_track
            if lullaby is not None:
                await m.async_stop_lullaby()
            try:
                await m.async_talk(url)
            finally:
                # Resume after an announcement that played out or failed; not after a stop or replacement.
                if announce and lullaby is not None and not _cancelling() and m.session is not None:
                    await m.async_play_lullaby(lullaby)


def _cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0
