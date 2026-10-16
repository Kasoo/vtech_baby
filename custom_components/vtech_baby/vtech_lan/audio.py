"""Decode media from a URL to the talk-back uplink format, with PyAV, as it plays.

`alaw_frames()` yields 20 ms frames of G.711 A-law, 8 kHz mono (160 bytes each). It reads the URL only
as frames are taken, so a file isn't downloaded ahead of playback and an endless stream plays until the
caller stops taking frames. Blocking: run it on a worker thread.
"""
from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlsplit

from .errors import MediaError

RATE = 8000
FRAME_BYTES = 160                  # 20 ms of 8 kHz A-law
OPEN_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 10.0
SCHEMES = ("http", "https")
# Network protocols only, so a URL (or a playlist inside one) can't read files on the host.
PROTOCOLS = "http,https,tcp,tls,crypto"


def alaw_frames(url: str) -> Iterator[bytes]:
    """160-byte A-law frames of the first audio stream at `url`. A short last frame is padded with silence.

    Raises MediaError for a URL that isn't http(s), can't be opened or decoded, or has no audio."""
    import av                      # noqa: PLC0415 - PyAV only when used

    if urlsplit(url).scheme not in SCHEMES:
        raise MediaError(f"unsupported media URL {url!r}: only http and https are played", call="talk-back")
    try:
        container = av.open(url, timeout=(OPEN_TIMEOUT_S, READ_TIMEOUT_S),
                            options={"protocol_whitelist": PROTOCOLS})
    except av.FFmpegError as err:
        raise MediaError(f"can't open the media: {err}", call="talk-back") from err
    with container:
        if not container.streams.audio:
            raise MediaError("the media has no audio", call="talk-back")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        encoder = av.CodecContext.create("pcm_alaw", "w")
        encoder.sample_rate, encoder.layout, encoder.format = RATE, "mono", "s16"
        buf = bytearray()
        try:
            for frame in _frames(container):
                for chunk in resampler.resample(frame):
                    for packet in encoder.encode(chunk):
                        buf += bytes(packet)
                while len(buf) >= FRAME_BYTES:
                    yield bytes(buf[:FRAME_BYTES])
                    del buf[:FRAME_BYTES]
        except av.FFmpegError as err:
            raise MediaError(f"can't decode the media: {err}", call="talk-back") from err
        if buf:
            yield bytes(buf) + b"\xd5" * (FRAME_BYTES - len(buf))       # 0xd5 = A-law silence


def _frames(container) -> Iterator:
    yield from container.decode(audio=0)
    yield None                     # flushes the resampler
