"""Talk-back for one camera: play media through the camera's speaker.

Protocol: docs/vtech-lan-kit/docs/40-media-streaming.md ("Two-way audio"), 90-ha-runtime.md §8.

- One talk-back at a time per camera; `start()` replaces one in progress.
- `start()` opens the media and decodes its first frame before touching the camera, so bad media fails
  with no side effects. A dedicated thread (`vtech-talk-…`) then sets up the uplink, and `start()`
  returns once the camera has connected to it, or raises why it didn't.
- The uplink is the session's: `SdkUplink` runs an AV server through the SDK and encrypts and paces
  frames here; `LinkUplink` hands frames to `_vtech_link`, which encrypts and paces them itself.
- The thread sends a frame every 20 ms and always runs the stop sequence, including on error.
  `on_end(err)` reports how a started talk-back ended: None when the media played out or it was stopped.
- Half-duplex: camera audio is stopped while talking, and restarted afterwards if `audio_wanted()`
  (the live view is running), or if the live view started meanwhile and left its AUDIOSTART to us
  (`hold_audio_start()`).
"""
from __future__ import annotations

import itertools
import logging
import struct
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from . import ops
from .audio import alaw_frames
from .crypto import encrypt_frame
from .errors import AV_ER_EXCEED_MAX_SIZE, MediaError, ProtocolError, SessionGone, VtechError, check, error_for

_LOG = logging.getLogger(__name__)

FRAME_S = 0.02                 # 160 bytes of 8 kHz A-law
SERV_TIMEOUT_S = 10            # wait this long for the camera to connect to the uplink
LINK_SEND_S = 1.0              # LinkUplink: the longest a frame waits for room in the module's queue
MAX_LAG_S = 0.2                # further behind than this (the source stalled): restart the clock, don't burst
STOP_JOIN_S = 15.0             # a stop waits out a blocked media read (READ_TIMEOUT_S) before giving up
CODEC_ALAW = 0x8A
FLAGS = 0x02                   # mono, 8 kHz, 16-bit capture

OnEnd = Callable[[VtechError | None], None]


def frame_info(timestamp_ms: int) -> bytes:
    """The 16-byte uplink frameinfo: u16@0 codec, u8@2 flags, u32@12 timestamp in ms."""
    return struct.pack("<HBBB7xI", CODEC_ALAW, FLAGS, 0, 0, timestamp_ms & 0xFFFFFFFF)


class SdkUplink:
    """The uplink through the SDK: avServStart3 on a free channel; frames encrypted and paced here."""

    def __init__(self, sdk: Any, session: Any, frame_s: float):
        self._sdk, self._session, self._frame_s = sdk, session, frame_s
        self._sid: int = session.sid
        self._master = b""
        self.ch = self.idx = -1
        self._t0, self._n = 0.0, 0

    def prepare(self) -> None:
        """The live-view key, before camera audio is stopped."""
        self._master = self._session.live_view_master()

    def open(self) -> int:
        """A free channel to announce with SPEAKERSTART."""
        self.ch = check(self._sdk.free_channel(self._sid), "IOTC_Session_Get_Free_Channel")
        return self.ch

    def accept(self, timeout_s: int) -> None:
        """Wait for the camera to connect to the channel."""
        self.idx = check(self._sdk.serv_start(self._sid, self.ch, timeout_s), "avServStart3")
        self._t0, self._n = time.monotonic(), 0

    def send(self, body: bytes, stop: threading.Event) -> bool:
        """Send one frame, then wait out its 20 ms. Returns whether it went out."""
        rc = self._sdk.send_audio(self.idx, encrypt_frame(body, self._master), frame_info(int(time.time() * 1000)))
        if rc == AV_ER_EXCEED_MAX_SIZE:                         # send buffer full
            self._sdk.serv_reset(self.idx)
        elif rc < 0:
            raise error_for(rc, "avSendAudioData")
        self._n += 1
        delay = self._t0 + self._n * self._frame_s - time.monotonic()
        if delay < -MAX_LAG_S:
            self._t0, self._n = time.monotonic(), 0
        elif delay > 0:
            stop.wait(delay)
        return rc >= 0

    def close(self, drain: bool) -> None:
        """avServStop, IOTC_Session_Channel_OFF, avServExit. Frames already went out as they were sent."""
        if self.idx >= 0:
            self._sdk.serv_stop(self.idx)
        if self.ch >= 0:
            self._sdk.channel_off(self._sid, self.ch)
            self._sdk.serv_exit(self._sid, self.ch)


class LinkUplink:
    """The uplink through `_vtech_link`, which encrypts and paces the frames; `send` blocks while its
    queue is full."""

    def __init__(self, session: Any):
        self._session = session
        self._tb: Any = None

    def prepare(self) -> None:
        pass

    def open(self) -> int:
        self._tb = self._session._call("talk-back", self._session.link.talkback, float(SERV_TIMEOUT_S))  # noqa: SLF001
        return self._tb.channel

    def accept(self, timeout_s: int) -> None:
        self._session._call("talk-back", self._tb.accept, float(timeout_s))                  # noqa: SLF001

    def send(self, body: bytes, stop: threading.Event) -> bool:
        self._session._call("talk-back", self._tb.send, body, timeout=LINK_SEND_S)          # noqa: SLF001
        return True

    def close(self, drain: bool) -> None:
        if self._tb is None:
            return
        try:
            self._tb.close(drain=drain, timeout=2.0)
        except self._session._module.LinkError as e:                                         # noqa: SLF001
            _LOG.debug("talk-back close: %s %s", e.kind, e.detail)


class TalkBack:
    """Talk-back for one camera. `get_session` returns its current session (or None)."""

    def __init__(self, get_session: Callable[[], Any], name: str = "",
                 audio_wanted: Callable[[], bool] = lambda: False,
                 open_media: Callable[[str], Iterator[bytes]] = alaw_frames, frame_s: float = FRAME_S):
        self._get_session, self.name = get_session, name
        self._audio_wanted, self._open_media, self._frame_s = audio_wanted, open_media, frame_s
        self._lock = threading.Lock()              # serialises start and stop
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.session: Any = None                   # the session the current talk-back uses
        self._audio_lock = threading.Lock()        # guards the two flags below
        self._holding_audio = False                # camera audio is stopped for a talk-back
        self._resume_audio = False                 # a live view asked for it back meanwhile
        self.stats: dict[str, Any] = {"talks": 0, "frames": 0, "last_error": None}

    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def start(self, url: str, on_end: OnEnd | None = None) -> None:
        """Play `url` through the camera's speaker. Returns once the camera is receiving it.

        Raises MediaError for media that can't be played, SessionGone if the camera isn't connected,
        and VtechError if the camera doesn't take the uplink."""
        first, frames = self._open(url)
        with self._lock:
            self._stop_locked()
            session = self._get_session()
            if session is None:
                _close(frames)
                raise SessionGone(f"{self.name}: camera not connected", call="talk-back")
            stop, ready, result = threading.Event(), threading.Event(), {}
            self._stop, self.session = stop, session
            self._thread = threading.Thread(
                target=self._run, args=(session, itertools.chain([first], frames), frames, stop, ready, result,
                                        on_end),
                name=f"vtech-talk-{self.name}", daemon=True)
            self._thread.start()
            ready.wait()
            if (err := result.get("error")) is not None:
                self._thread.join()
                self._thread = self.session = None
                raise err

    def stop(self, timeout: float = STOP_JOIN_S) -> None:
        """Stop the talk-back in progress, if any, and wait for its stop sequence."""
        self._stop.set()                           # seen by a talk-back still setting up, before any audio
        with self._lock:
            self._stop_locked(timeout)

    def hold_audio_start(self) -> bool:
        """For a live view about to send AUDIOSTART: True if a talk-back has camera audio stopped. The
        talk-back then sends AUDIOSTART when it ends, and the caller must not send it now."""
        with self._audio_lock:
            if self._holding_audio:
                self._resume_audio = True
            return self._holding_audio

    def _stop_locked(self, timeout: float = STOP_JOIN_S) -> None:
        t = self._thread
        if t is None:
            return
        self._stop.set()
        t.join(timeout)
        if t.is_alive():
            _LOG.warning("%s: talk-back did not stop within %.0f s", self.name, timeout)
        self._thread = self.session = None

    def _open(self, url: str) -> tuple[bytes, Iterator[bytes]]:
        frames = self._open_media(url)
        try:
            return next(frames), frames
        except StopIteration:
            raise MediaError("the media has no audio", call="talk-back") from None
        except BaseException:
            _close(frames)
            raise

    # ── The talk-back thread ─────────────────────────────────────────────────
    def _run(self, session: Any, frames: Iterator[bytes], source: Iterator[bytes], stop: threading.Event,
             ready: threading.Event, result: dict[str, VtechError], on_end: OnEnd | None) -> None:
        up: Any = None
        announced = started = stopped = False
        ch = -1
        err: VtechError | None = None
        try:
            fmt = session.get_audio_out_format()
            if fmt.codec != CODEC_ALAW or (fmt.sample_rate_idx or 0) != 0:
                raise ProtocolError(f"unsupported uplink format {fmt}: only G.711 A-law 8 kHz is sent",
                                    call="talk-back")
            up = session.uplink(self._frame_s)
            up.prepare()
            with self._audio_lock:
                self._holding_audio, self._resume_audio = True, False
            session.send(ops.AUDIO_STOP)
            ch = up.open()
            session.send(ops.SPEAKER_START, ch)
            announced = True
            up.accept(SERV_TIMEOUT_S)
            started = True
            self.stats["talks"] += 1
            ready.set()
            if stop.is_set():
                stopped = True
                return
            _LOG.debug("%s: talk-back started (ch=%s)", self.name, ch)
            for body in frames:
                if stop.is_set():
                    stopped = True
                    return
                if up.send(body, stop):
                    self.stats["frames"] += 1
            stopped = stop.is_set()
        except VtechError as e:
            err = e
        except Exception as e:                                  # noqa: BLE001 - reported, not raised
            err = VtechError(f"talk-back failed: {type(e).__name__}: {e}", call="talk-back")
        finally:
            _close(source)
            self._teardown(session, up, ch, announced, drain=started and not stopped and err is None)
            if err is not None:
                self.stats["last_error"] = str(err)
                _LOG.debug("%s: talk-back ended: %s", self.name, err)
            if not started:
                result["error"] = err or VtechError("talk-back did not start", call="talk-back")
                ready.set()
            elif on_end is not None:
                try:
                    on_end(err)
                except Exception:                               # noqa: BLE001
                    _LOG.exception("%s: on_end callback failed", self.name)

    def _teardown(self, session: Any, up: Any, ch: int, announced: bool, drain: bool) -> None:
        """The stop sequence: close the uplink (sending what is queued when the media played out), then
        SPEAKERSTOP, then camera audio back on if the live view wants it. Each step runs even if an
        earlier one fails."""
        if up is not None:
            try:
                up.close(drain)
            except VtechError as e:
                _LOG.debug("%s: uplink close on talk-back stop: %s", self.name, e)
        steps = [(ops.SPEAKER_STOP, ch)] if announced else []
        with self._audio_lock:
            resume, self._holding_audio = self._resume_audio, False
        if resume or self._audio_wanted():
            steps.append((ops.AUDIO_START,))
        for op, *args in steps:
            try:
                session.send(op, *args)
            except VtechError as e:
                _LOG.debug("%s: %s on talk-back stop: %s", self.name, op.name, e)


def _close(frames: Iterator[bytes]) -> None:
    """Close the media (a generator from `alaw_frames`); plain iterators have nothing to close."""
    if (close := getattr(frames, "close", None)) is not None:
        close()
