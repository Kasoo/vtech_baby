"""Decode media from a URL to the talk-back uplink format, with PyAV, as it plays.

`alaw_frames()` yields 20 ms frames of G.711 A-law, 8 kHz mono (160 bytes each). It reads the URL only
as frames are taken, so a file isn't downloaded ahead of playback and an endless stream plays until the
caller stops taking frames. Blocking: run it on a worker thread.

The URL is fetched with urllib and PyAV reads the response as a file: the FFmpeg that PyAV 19 bundles
fails with EIO on the first read of a chunked response, which is how Home Assistant serves TTS while it
is still being generated. Playlists (HLS, m3u, pls) are handed to FFmpeg by URL instead, since their
entries are relative to it. urllib checks TLS certificates; FFmpeg doesn't by default.
"""
from __future__ import annotations

import contextlib
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


def _fetch(url: str):
    """The response for `url` to hand to PyAV, or None for a playlist (FFmpeg opens it by URL)."""
    try:
        resp = _opener.open(urllib.request.Request(url, headers={"Icy-MetaData": "0"}), timeout=OPEN_TIMEOUT_S)
    except (OSError, ValueError) as err:          # URLError, HTTPError and timeouts are OSErrors
        raise MediaError(f"can't open the media: {err}", call="talk-back") from err
    if resp.headers.get_content_type() in PLAYLIST_TYPES or urlsplit(resp.url).path.endswith((".m3u8", ".m3u", ".pls")):
        resp.close()
        return None
    return resp


def alaw_frames(url: str) -> Iterator[bytes]:
    """160-byte A-law frames of the first audio stream at `url`. A short last frame is padded with silence.

    Raises MediaError for a URL that isn't http(s), can't be opened or decoded, or has no audio."""
    import av                      # noqa: PLC0415 - PyAV only when used

    if urlsplit(url).scheme not in SCHEMES:
        raise MediaError(f"unsupported media URL {url!r}: only http and https are played", call="talk-back")
    resp = _fetch(url)
    try:
        container = av.open(resp if resp is not None else url, timeout=(OPEN_TIMEOUT_S, READ_TIMEOUT_S),
                            options={"protocol_whitelist": PROTOCOLS})
    except av.FFmpegError as err:
        if resp is not None:
            resp.close()
        raise MediaError(f"can't open the media: {err}", call="talk-back") from err
    with container, contextlib.closing(resp) if resp is not None else contextlib.nullcontext():
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
