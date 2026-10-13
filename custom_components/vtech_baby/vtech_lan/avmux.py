"""Live-view MPEG-TS via PyAV (ffmpeg's libraries, in-process): H.264 passthrough + A-law → AAC.

PyAV ships with Home Assistant (stream component). The camera's frames are handed to ffmpeg's MPEG-TS
muxer as packets with our own timestamps (arrival time, 90 kHz), so nothing is probed and there are no
pipes. Audio is AAC because go2rtc's MPEG-TS reader and HA's HLS path both take it (PCMA they do not).
Audio time counts samples from the first block's arrival, so it is monotonic even though blocks arrive
in bursts. If the sample clock falls more than REBASE (0.5 s) behind arrival time (dropped frames, a
gap), it jumps forward to arrival time; it never moves back. That keeps audio aligned with the
arrival-stamped video however long the stream runs.

The video stream's parameters come from the first keyframe: it is parsed by PyAV's raw-H.264 demuxer and
used as a template (no H.264 encoder is opened).
"""
from __future__ import annotations

import io
from collections import deque
from fractions import Fraction

import av

RATE = 8000
TB = Fraction(1, 90_000)
REBASE = 90_000 // 2       # 0.5 s, in 90 kHz
VIDEO_PID = 0x100        # ffmpeg's mpegts numbers streams from 0x100; video is added first


class _Sink(io.RawIOBase):
    """File-like target that just collects what the muxer writes."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        self.buf += b
        return len(b)

    def take(self) -> bytes:
        out, self.buf = bytes(self.buf), bytearray()
        return out


class AvTsMuxer:
    """Feed `video()` from the first keyframe on and `audio()` at will; each returns the TS bytes produced."""

    def __init__(self, first_keyframe: bytes, audio_bit_rate: int = 24000) -> None:
        self._sink = _Sink()
        self._out = av.open(self._sink, mode="w", format="mpegts",
                            # Write each packet as soon as it is muxed; hold at most 100 ms to interleave
                            # (the default waits up to 10 s for the other stream, i.e. no live output).
                            container_options={"flush_packets": "1", "max_interleave_delta": "100000"})
        with av.open(io.BytesIO(first_keyframe), format="h264") as probe:
            self._v = self._out.add_stream_from_template(probe.streams.video[0], opaque=True)
        self._v.time_base = TB
        self._a = self._out.add_stream("aac", rate=RATE)
        self._a.layout = "mono"
        self._a.bit_rate = audio_bit_rate
        self._alaw = av.CodecContext.create("pcm_alaw", "r")
        self._alaw.sample_rate = RATE
        self._alaw.layout = "mono"
        self._resample = av.AudioResampler(format="fltp", layout="mono", rate=RATE)
        self._samples = 0                                   # A-law samples fed so far
        self._blocks: deque[tuple[int, int]] = deque()      # (first sample index, arrival PTS)
        self._base: int | None = None                       # 90 kHz time of sample 0
        self._last = -1                                     # last audio PTS muxed

    def video(self, access_unit: bytes, pts: int, keyframe: bool) -> bytes:
        p = av.Packet(access_unit)
        p.stream = self._v
        p.time_base = TB
        p.pts = p.dts = pts
        p.is_keyframe = keyframe
        self._out.mux(p)
        return self._sink.take()

    def audio(self, alaw: bytes, pts: int) -> bytes:
        if not alaw:
            return b""
        self._blocks.append((self._samples, pts))
        for frame in self._alaw.decode(av.Packet(alaw)):
            for f in self._resample.resample(frame):
                f.pts = self._samples
                f.time_base = Fraction(1, RATE)
                self._samples += f.samples
                for p in self._a.encode(f):                  # p.pts counts samples (1/8000)
                    p.pts = p.dts = self._arrival(p.pts or 0)
                    p.time_base = TB
                    self._out.mux(p)
        return self._sink.take()

    def _arrival(self, sample: int) -> int:
        while len(self._blocks) > 1 and self._blocks[1][0] <= sample:
            self._blocks.popleft()
        start, arrived = self._blocks[0]
        arrived += (sample - start) * 90_000 // RATE         # when this sample arrived, roughly
        if self._base is None:
            self._base = arrived - sample * 90_000 // RATE
        pts = self._base + sample * 90_000 // RATE
        if arrived - pts > REBASE:                          # fell behind: catch up, never go back
            self._base += arrived - pts
            pts = arrived
        pts = max(pts, self._last + 1)
        self._last = pts
        return pts

    def close(self) -> bytes:
        try:
            for p in self._a.encode(None):
                p.pts = p.dts = self._arrival(p.pts or 0)
                p.time_base = TB
                self._out.mux(p)
            self._out.close()
        except av.FFmpegError:
            pass
        return self._sink.take()


def join_offset(ts: bytes) -> int | None:
    """Where a new reader can start in a chunk of muxer output: the PAT that ffmpeg writes just before a
    video keyframe (whose first packet carries the random-access flag), or None if the chunk has none."""
    pat = None
    for off in range(0, len(ts) - 187, 188):
        pid = (ts[off + 1] & 0x1F) << 8 | ts[off + 2]
        if pid == 0:
            pat = off if pat is None else pat
        elif pid == VIDEO_PID and ts[off + 1] & 0x40:
            if pat is not None and ts[off + 3] & 0x20 and ts[off + 4] and ts[off + 5] & 0x40:
                return pat                                 # PES start + adaptation field + random access
            pat = None                                     # a non-key frame: wait for the next PAT
    return None


def keyframe_to_jpeg(access_unit: bytes, width: int | None = None, height: int | None = None) -> bytes:
    """Decode one H.264 keyframe (Annex-B, with SPS/PPS) and encode it as JPEG, optionally scaled down to
    fit `width` x `height` (HA's size hints) with the aspect ratio kept. Never scales up."""
    dec = av.CodecContext.create("h264", "r")
    frames = dec.decode(av.Packet(access_unit)) + dec.decode(None)
    if not frames:
        raise ValueError("keyframe did not decode")
    f = frames[0]
    scale = min([1.0] + [want / have for want, have in ((width, f.width), (height, f.height)) if want])
    w, h = max(2, round(f.width * scale)), max(2, round(f.height * scale))
    w, h = w - w % 2, h - h % 2                         # 4:2:0 wants even sizes
    enc = av.CodecContext.create("mjpeg", "w")
    enc.width, enc.height, enc.pix_fmt = w, h, "yuvj420p"
    enc.time_base = Fraction(1, 25)
    enc.options = {"q:v": "4"}                          # high quality; ~100–200 kB at 1080p
    packets = enc.encode(f.reformat(width=w, height=h, format="yuvj420p")) + enc.encode(None)
    return bytes(packets[0])
