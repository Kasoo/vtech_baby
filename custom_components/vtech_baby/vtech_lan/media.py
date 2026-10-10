"""On-demand live view for one camera: START → receive → decrypt → MPEG-TS → subscribers, and snapshots.

Design: HA_INTEGRATION.md §6 Phase 5 (2026-10-06 spike). Protocol: vtech-lan-kit/docs/40, 50.

- One dedicated thread per camera (`vtech-media-…`) owns the media path. It starts when the first
  subscriber arrives and stops `grace_s` after the last one leaves (go2rtc drops and reopens its
  connection; snapshots come and go), so short gaps don't restart the camera's stream.
- Start: live-view key (once per session), clean the SDK buffers (so the first frame is a keyframe),
  AUDIOSTART, full-rate, START. Stop: STOP, AUDIOSTOP. The control session is untouched; its reader
  thread keeps owning avRecvIOCtrl while this thread reads frames.
- Subscribers get MPEG-TS bytes (`avmux`) through a callback run on the media thread, which must not
  block. A new subscriber starts at the next keyframe. `None` means end of stream: the session went
  away or the stream is shut down; reconnecting is the subscriber's business.
- A subscriber arriving while the thread is tearing down is picked up by the same thread, which starts
  again, so start/stop never overlap on the channel.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from . import ops
from .crypto import decrypt_frame
from .errors import BAD_FRAME, NO_DATA, SessionGone, VtechError, error_for

_LOG = logging.getLogger(__name__)

GRACE_S = 5.0                 # keep streaming this long after the last subscriber leaves
FIRST_KEYFRAME_S = 10.0       # give up a start that produces no keyframe in this time
SNAPSHOT_MAX_AGE_S = 60.0     # when idle, reuse a snapshot this recent instead of starting the stream
IDLE_SLEEP_S = 0.005          # back-off when no frame is ready (avRecvFrameData2 never blocks)

OnData = Callable[[bytes | None], None]


def is_keyframe(access_unit: bytes) -> bool:
    """True if an Annex-B access unit holds an IDR slice (NAL type 5)."""
    i = 0
    while (i := access_unit.find(b"\x00\x00\x01", i)) != -1 and i + 3 < len(access_unit):
        if access_unit[i + 3] & 0x1F == 5:
            return True
        i += 3
    return False


class Subscription:
    def __init__(self, stream: LiveStream, on_data: OnData):
        self._stream, self.on_data = stream, on_data
        self.started = False                       # has received its first (keyframe) chunk

    def close(self) -> None:
        self._stream._remove(self)


class LiveStream:
    """The live view of one camera. `get_session` returns the manager's current session (or None)."""

    def __init__(self, sdk: Any, get_session: Callable[[], Any], name: str = "",
                 grace_s: float = GRACE_S, first_keyframe_s: float = FIRST_KEYFRAME_S,
                 on_running: Callable[[bool], None] | None = None):
        """`on_running(bool)` is called on the media thread when the camera's stream starts and stops."""
        self._sdk, self._get_session, self.name = sdk, get_session, name
        self._on_running = on_running
        self._grace_s, self._first_keyframe_s = grace_s, first_keyframe_s
        self._lock = threading.Condition()
        self._subs: list[Subscription] = []
        self._thread: threading.Thread | None = None
        self._closed = False
        self._keyframe: tuple[float, bytes] | None = None      # (time, access unit) of the newest
        self._jpeg: tuple[float, bytes, tuple] | None = None    # (time, JPEG, size args) of the last snapshot
        self.stats: dict[str, Any] = {"starts": 0, "running": False, "last_error": None}

    # ── Subscribers ──────────────────────────────────────────────────────────
    def subscribe(self, on_data: OnData) -> Subscription:
        """Start receiving MPEG-TS. Raises SessionGone if the camera isn't connected."""
        if self._get_session() is None:
            raise SessionGone(f"{self.name}: camera not connected", call="live view")
        sub = Subscription(self, on_data)
        with self._lock:
            if self._closed:
                raise SessionGone(f"{self.name}: live view closed", call="live view")
            self._subs.append(sub)
            self._lock.notify_all()
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=f"vtech-media-{self.name}",
                                                daemon=True)
                self._thread.start()
        return sub

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    @property
    def running(self) -> bool:
        return bool(self.stats["running"])

    def _set_running(self, running: bool) -> None:
        self.stats["running"] = running
        if self._on_running is not None:
            try:
                self._on_running(running)
            except Exception:                                   # noqa: BLE001
                _LOG.exception("%s: on_running callback failed", self.name)

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subs)

    def close(self, timeout: float = 10.0) -> None:
        """Stop for good (entry unload): end every subscriber and wait for the media thread."""
        with self._lock:
            self._closed = True
            self._lock.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._end_all()

    def _end_all(self) -> None:
        with self._lock:
            subs, self._subs = self._subs, []
        for s in subs:
            self._call(s, None)

    def _call(self, sub: Subscription, data: bytes | None) -> None:
        try:
            sub.on_data(data)
        except Exception:                                       # noqa: BLE001
            _LOG.exception("%s: live-view subscriber failed", self.name)

    # ── Snapshots ────────────────────────────────────────────────────────────
    def snapshot(self, width: int | None = None, height: int | None = None, timeout: float = 10.0) -> bytes:
        """A JPEG of the newest keyframe. Starts the stream briefly when no recent frame is at hand."""
        from .avmux import keyframe_to_jpeg                     # noqa: PLC0415 - PyAV only when used

        now = time.monotonic()
        size = (width, height)
        with self._lock:
            jpeg, key = self._jpeg, self._keyframe
        if jpeg and jpeg[2] == size and now - jpeg[0] < (2.5 if self.running else SNAPSHOT_MAX_AGE_S):
            return jpeg[1]
        if not (self.running and key and now - key[0] < 2.5):
            sub = self.subscribe(lambda _d: None)               # keeps the stream up while we wait
            try:
                with self._lock:
                    ok = self._lock.wait_for(lambda: self._keyframe and self._keyframe[0] > now
                                             or self._closed or self._thread is None, timeout)
                    key = self._keyframe
                if not ok or key is None or key[0] <= now:
                    raise VtechError(f"{self.name}: no keyframe within {timeout:.0f} s", call="snapshot")
            finally:
                sub.close()
        out = keyframe_to_jpeg(key[1], width, height)
        with self._lock:
            self._jpeg = (time.monotonic(), out, size)
        return out

    # ── The media thread ─────────────────────────────────────────────────────
    def _run(self) -> None:
        try:
            while True:
                with self._lock:
                    if self._closed or not self._subs:
                        self._thread = None
                        self._lock.notify_all()
                        return
                session = self._get_session()
                if session is None:
                    self._end_all()
                    continue
                try:
                    self._stream(session)
                except Exception as err:                        # noqa: BLE001
                    self.stats["last_error"] = f"{type(err).__name__}: {err}"
                    _LOG.warning("%s: live view stopped: %s", self.name, err)
                    self._end_all()                             # subscribers reconnect on their own
        finally:
            with self._lock:
                self._thread = None
                self._lock.notify_all()

    def _stream(self, session: Any) -> None:
        from .avmux import AvTsMuxer, join_offset               # noqa: PLC0415

        master = session.live_view_master()
        av = session.av
        self._sdk.clean_media_buffers(av)
        for op in (ops.AUDIO_START, ops.VIDEO_FULLRATE, ops.START):
            session.send(op)
        st = self.stats
        st.update(starts=st["starts"] + 1, video=0, audio=0, bad=0, keyframes=0, bytes=0)
        self._set_running(True)
        started = t0 = time.monotonic()
        idle_since: float | None = None
        reader = self._sdk.frame_reader(av)
        mux: AvTsMuxer | None = None
        _LOG.debug("%s: live view started", self.name)

        def pts() -> int:
            return int((time.monotonic() - t0) * 90_000)

        def emit(chunk: bytes) -> None:
            if not chunk:
                return
            st["bytes"] += len(chunk)
            join: int | None = None
            with self._lock:
                subs = list(self._subs)
            for s in subs:
                data = chunk
                if not s.started:
                    if join is None:
                        join = join_offset(chunk)
                        join = -1 if join is None else join
                    if join < 0:
                        continue
                    s.started, data = True, chunk[join:]
                self._call(s, data)

        try:
            while True:
                with self._lock:
                    if self._closed:
                        return
                    if self._subs:
                        idle_since = None
                    elif idle_since is None:
                        idle_since = time.monotonic()
                    elif time.monotonic() - idle_since > self._grace_s:
                        return
                if mux is None and time.monotonic() - started > self._first_keyframe_s:
                    raise VtechError(f"no keyframe within {self._first_keyframe_s:.0f} s of START",
                                     call="live view")
                rc, frame = reader.video()
                if rc > 0:
                    try:
                        au = decrypt_frame(frame, master)
                    except Exception:                           # noqa: BLE001 - bad tag / short frame
                        st["bad"] += 1
                        au = None
                    if au is not None:
                        st["video"] += 1
                        key = is_keyframe(au)
                        if key:
                            st["keyframes"] += 1
                            with self._lock:
                                self._keyframe = (time.monotonic(), au)
                                self._lock.notify_all()
                        if mux is None and key:
                            mux = AvTsMuxer(au)
                        if mux is not None:
                            emit(mux.video(au, pts(), key))
                elif rc in BAD_FRAME:
                    st["bad"] += 1
                elif rc in NO_DATA or rc == 0:
                    time.sleep(IDLE_SLEEP_S)
                else:
                    raise error_for(rc, "avRecvFrameData2")
                for _ in range(64):                             # drain audio (~25 frames/s)
                    rc, frame = reader.audio()
                    if rc <= 0:
                        if rc < 0 and rc not in NO_DATA and rc not in BAD_FRAME:
                            raise error_for(rc, "avRecvAudioData")
                        break
                    try:
                        alaw = decrypt_frame(frame, master)
                    except Exception:                           # noqa: BLE001
                        st["bad"] += 1
                        continue
                    st["audio"] += 1
                    if mux is not None:
                        emit(mux.audio(alaw, pts()))
        finally:
            for op in (ops.STOP, ops.AUDIO_STOP):
                try:
                    session.send(op)
                except VtechError as err:
                    _LOG.debug("%s: %s on stop: %s", self.name, op.name, err)
            if mux is not None:
                mux.close()
            self._set_running(False)
            _LOG.debug("%s: live view stopped (%s)", self.name,
                       {k: st[k] for k in ("video", "audio", "keyframes", "bad", "bytes")})
