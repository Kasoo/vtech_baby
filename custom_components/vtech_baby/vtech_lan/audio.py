"""Decode media from a URL, or live audio held in memory, to the talk-back uplink format, with PyAV, as
it plays.

`alaw_frames()` yields 20 ms frames of G.711 A-law, 8 kHz mono (160 bytes each). It reads the media only
as frames are taken, so a file isn't downloaded ahead of playback and an endless stream plays until the
caller stops taking frames. Blocking: run it on a worker thread.

`LiveAudio` is 16-bit mono PCM fed in as it arrives (the talk card's microphone). It is read straight
from memory, with no HTTP, and ends when `end()` is called, after `idle_s` without audio, or once
`max_s` of audio has arrived.

The URL is fetched with urllib and PyAV reads the response as a file: the FFmpeg that PyAV 19 bundles
fails with EIO on the first read of a chunked response, which is how Home Assistant serves TTS while it
is still being generated. Playlists (HLS, m3u, pls) are handed to FFmpeg by URL instead, since their
entries are relative to it. urllib checks TLS certificates; FFmpeg doesn't by default.

Raw PCM (`audio/L16` with `rate` and `channels` parameters: 16-bit big-endian, RFC 2586) starts decoding
as soon as data arrives. A container makes FFmpeg read ahead before the first frame (WAV's demuxer reads
64 KiB), which holds back live audio by seconds.
"""
from __future__ import annotations

import contextlib
import queue
import urllib.error
import urllib.request
from collections.abc import Iterator
from urllib.parse import urlsplit

from .errors import MediaError

RATE = 8000
FRAME_BYTES = 160                  # 20 ms of 8 kHz A-law
OPEN_TIMEOUT_S = 10.0             # also each read of a fetched response
READ_TIMEOUT_S = 10.0             # reads by FFmpeg (playlists)
SCHEMES = ("http", "https")
# Network protocols only, so a URL (or a playlist inside one) can't read files on the host.
PROTOCOLS = "http,https,tcp,tls,crypto"
PLAYLIST_TYPES = ("application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl",
                  "audio/x-mpegurl", "audio/x-scpls")


class _HttpOnly(urllib.request.HTTPRedirectHandler):
    """Follow redirects to http(s) only (urllib's default also follows ftp)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme not in SCHEMES:
            raise urllib.error.HTTPError(newurl, code, f"redirect to {newurl!r} refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpOnly)


class _Arrived:
    """A response for PyAV to read as a file. A read returns what has arrived, where the response's own
    read waits for the whole buffer PyAV asks for, which holds back the start of a slow stream."""

    def __init__(self, resp):
        self._resp = resp
        self.headers = resp.headers

    def read(self, size: int = -1) -> bytes:
        return self._resp.read1(size)

    def close(self):
        self._resp.close()


def _fetch(url: str):
    """The response for `url` to hand to PyAV, or None for a playlist (FFmpeg opens it by URL)."""
    try:
        resp = _opener.open(urllib.request.Request(url, headers={"Icy-MetaData": "0"}), timeout=OPEN_TIMEOUT_S)
    except (OSError, ValueError) as err:          # URLError, HTTPError and timeouts are OSErrors
        raise MediaError(f"can't open the media: {err}", call="talk-back") from err
    if resp.headers.get_content_type() in PLAYLIST_TYPES or urlsplit(resp.url).path.endswith((".m3u8", ".m3u", ".pls")):
        resp.close()
        return None
    return _Arrived(resp)


class LiveAudio:
    """16-bit little-endian mono PCM at `rate`, fed from any thread and read once, as a file, by PyAV."""

    def __init__(self, rate: int, idle_s: float, max_s: float):
        self.rate, self._idle_s = rate, idle_s
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._left = int(rate * max_s) * 2
        self._ended = False
        self._pending = b""

    def feed(self, data: bytes):
        """Add audio; what arrives after `max_s` of it is dropped and ends the audio."""
        if self._ended:
            return
        data = data[:self._left - self._left % 2]
        self._left -= len(data)
        if data:
            self._queue.put_nowait(data)
        if self._left < 2:
            self.end()

    def end(self):
        if not self._ended:
            self._ended = True
            self._queue.put_nowait(None)

    def read(self, size: int = -1) -> bytes:
        """What has arrived, up to `size` bytes; waits up to `idle_s` for more, then b"" for the end."""
        if not self._pending:
            try:
                chunk = self._queue.get(timeout=self._idle_s)
            except queue.Empty:
                chunk = None
            if chunk is None:
                self._ended = True
                self._queue.put_nowait(None)               # later reads end at once
                return b""
            self._pending = chunk
        n = len(self._pending) if size is None or size < 0 else size
        data, self._pending = self._pending[:n], self._pending[n:]
        return data

    def close(self):
        self.end()

    def demuxer(self) -> tuple[str, dict[str, str]]:
        return "s16le", {"sample_rate": str(self.rate), "ch_layout": "mono",
                         "probesize": "32", "analyzeduration": "1"}


def _raw_pcm(resp: _Arrived) -> tuple[str | None, dict[str, str]]:
    """The demuxer and its options for an `audio/L16` response, else (None, {}) for FFmpeg to probe."""
    if resp.headers.get_content_type() != "audio/l16":
        return None, {}
    try:
        rate, channels = int(resp.headers.get_param("rate")), int(resp.headers.get_param("channels", "1"))
    except (TypeError, ValueError):
        rate = channels = 0
    if not 1000 <= rate <= 192000 or channels not in (1, 2):
        resp.close()
        raise MediaError("audio/L16 needs a rate and 1 or 2 channels", call="talk-back")
    # Nothing to probe: the format is all in the content type.
    return "s16be", {"sample_rate": str(rate), "ch_layout": "mono" if channels == 1 else "stereo",
                     "probesize": "32", "analyzeduration": "1"}


def alaw_frames(media: str | LiveAudio) -> Iterator[bytes]:
    """160-byte A-law frames of the first audio stream at a URL, or of live audio. A short last frame is
    padded with silence.

    Raises MediaError for a URL that isn't http(s), media that can't be opened or decoded, or no audio."""
    import av                      # noqa: PLC0415 - PyAV only when used

    if isinstance(media, LiveAudio):
        fmt, opts = media.demuxer()
        try:
            container = av.open(media, format=fmt, options=opts)
        except av.FFmpegError as err:
            media.close()
            raise MediaError(f"can't open the audio: {err}", call="talk-back") from err
        with container, contextlib.closing(media):
            yield from _encode(av, container)
        return
    url = media
    if urlsplit(url).scheme not in SCHEMES:
        raise MediaError(f"unsupported media URL {url!r}: only http and https are played", call="talk-back")
    resp = _fetch(url)
    fmt, opts = _raw_pcm(resp) if resp is not None else (None, {})
    try:
        container = av.open(resp if resp is not None else url, format=fmt, timeout=(OPEN_TIMEOUT_S, READ_TIMEOUT_S),
                            options={"protocol_whitelist": PROTOCOLS, **opts})
    except av.FFmpegError as err:
        if resp is not None:
            resp.close()
        raise MediaError(f"can't open the media: {err}", call="talk-back") from err
    with container, contextlib.closing(resp) if resp is not None else contextlib.nullcontext():
        yield from _encode(av, container)


def _encode(av, container) -> Iterator[bytes]:
    """The container's first audio stream as 160-byte A-law frames, the last padded with silence."""
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
