"""Camera: the live view (H.264 + AAC) and snapshots.

`stream_source()` points at the worker's loopback HTTP server (`vtech_lan/worker.py`), which serves
the camera's MPEG-TS; HA's go2rtc serves it as WebRTC and its stream component as HLS. Media runs only
while something watches (`vtech_lan/media.py`).
Snapshots decode the newest keyframe; with nothing watching, the stream starts briefly and a snapshot is
reused for up to 60 s, so dashboard thumbnails don't keep the camera streaming. The entity's state is
`streaming` while the camera sends video, else `idle`.
"""
from __future__ import annotations

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import VtechEntity
from .manager import CameraManager
from .vtech_lan.errors import VtechError


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([VtechCamera(entry.runtime_data)])


class VtechCamera(VtechEntity, Camera):
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, manager: CameraManager) -> None:
        VtechEntity.__init__(self, manager, "live_view")
        Camera.__init__(self)

    @property
    def is_streaming(self) -> bool:
        """The camera is sending video (someone is watching, or a snapshot is being taken)."""
        return self.coordinator.media.running

    async def stream_source(self) -> str | None:
        if self.coordinator.session is None:
            return None
        return self.coordinator.client.url(self.coordinator.uid)

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        try:
            return await self.hass.async_add_executor_job(self.coordinator.media.snapshot, width, height)
        except VtechError as err:
            raise HomeAssistantError(f"camera snapshot failed: {err}") from err
