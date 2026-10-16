"""Talk-back for one camera: play media through the camera's speaker.

Protocol: docs/vtech-lan-kit/docs/40-media-streaming.md ("Two-way audio"), 90-ha-runtime.md §8.

- One talk-back at a time per camera; `start()` replaces one in progress.
- `start()` opens the media and decodes its first frame before touching the camera, so bad media fails
  with no side effects. A dedicated thread (`vtech-talk-…`) then sets up the uplink, and `start()`
  returns once the camera has connected to it, or raises why it didn't.
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
SERV_TIMEOUT_S = 10            # avServStart3: wait this long for the camera to connect
MAX_LAG_S = 0.2                # further behind than this (the source stalled): restart the clock, don't burst
STOP_JOIN_S = 15.0             # a stop waits out a blocked media read (READ_TIMEOUT_S) before giving up
CODEC_ALAW = 0x8A
FLAGS = 0x02                   # mono, 8 kHz, 16-bit capture

OnEnd = Callable[[VtechError | None], None]


def frame_info(timestamp_ms: int) -> bytes:
    """The 16-byte uplink frameinfo: u16@0 codec, u8@2 flags, u32@12 timestamp in ms."""
    return struct.pack("<HBBB7xI", CODEC_ALAW, FLAGS, 0, 0, timestamp_ms & 0xFFFFFFFF)


class TalkBack:
    """Talk-back for one camera. `get_session` returns its current session (or None)."""

    def __init__(self, sdk: Any, get_session: Callable[[], Any], name: str = "",
                 audio_wanted: Callable[[], bool] = lambda: False,
                 open_media: Callable[[str], Iterator[bytes]] = alaw_frames, frame_s: float = FRAME_S):
        self._sdk, self._get_session, self.name = sdk, get_session, name
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
        sdk, sid = self._sdk, session.sid
        ch = idx = -1
        announced = started = False
        err: VtechError | None = None
        try:
            fmt = session.get_audio_out_format()
            if fmt.codec != CODEC_ALAW or (fmt.sample_rate_idx or 0) != 0:
                raise ProtocolError(f"unsupported uplink format {fmt}: only G.711 A-law 8 kHz is sent",
                                    call="talk-back")
            master = session.live_view_master()
            with self._audio_lock:
                self._holding_audio, self._resume_audio = True, False
            session.send(ops.AUDIO_STOP)
            ch = check(sdk.free_channel(sid), "IOTC_Session_Get_Free_Channel")
            session.send(ops.SPEAKER_START, ch)
            announced = True
            idx = check(sdk.serv_start(sid, ch, SERV_TIMEOUT_S), "avServStart3")
            started = True
            self.stats["talks"] += 1
            ready.set()
            if stop.is_set():
                return
            _LOG.debug("%s: talk-back started (ch=%s idx=%s)", self.name, ch, idx)
            self._send(idx, master, frames, stop)
        except VtechError as e:
            err = e
        except Exception as e:                                  # noqa: BLE001 - reported, not raised
            err = VtechError(f"talk-back failed: {type(e).__name__}: {e}", call="talk-back")
        finally:
            _close(source)
            self._teardown(session, sid, ch, idx, announced)
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

    def _send(self, idx: int, master: bytes, frames: Iterator[bytes], stop: threading.Event) -> None:
        sdk = self._sdk
        t0, n = time.monotonic(), 0
        for body in frames:
            if stop.is_set():
                return
            rc = sdk.send_audio(idx, encrypt_frame(body, master), frame_info(int(time.time() * 1000)))
            if rc == AV_ER_EXCEED_MAX_SIZE:                     # send buffer full
                sdk.serv_reset(idx)
            elif rc < 0:
                raise error_for(rc, "avSendAudioData")
            else:
                self.stats["frames"] += 1
            n += 1
            delay = t0 + n * self._frame_s - time.monotonic()
            if delay < -MAX_LAG_S:
                t0, n = time.monotonic(), 0
            elif delay > 0 and stop.wait(delay):
                return

    def _teardown(self, session: Any, sid: int, ch: int, idx: int, announced: bool) -> None:
        """The stop sequence: avServStop, IOTC_Session_Channel_OFF, avServExit, SPEAKERSTOP, then camera
        audio back on if the live view wants it. Each step runs even if an earlier one fails."""
        sdk = self._sdk
        if idx >= 0:
            sdk.serv_stop(idx)
        if ch >= 0:
            sdk.channel_off(sid, ch)
            sdk.serv_exit(sid, ch)
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
