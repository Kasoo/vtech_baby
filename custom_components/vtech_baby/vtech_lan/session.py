"""One authenticated session to one camera: connect, request/reply, pushes, liveness, teardown.

Authority: vtech-lan-kit/docs/90-ha-runtime.md §2–5 (sessions, threading, errors, teardown),
20-ioctrl-framework.md (per-session prep), 30-opcode-reference.md (payloads, via `ops`).

Threading model:
- `connect()`, `request()`, `send()` and `close()` block; call them from worker threads, never an
  event loop. `request()`/`send()` may be called from any thread concurrently; the actual
  `avSendIOCtrl` calls are serialised (a second concurrent send on one channel fails with -20021).
- A dedicated reader thread owns `avRecvIOCtrl`. It completes the pending request whose reply opcode
  matches, and hands camera pushes to `on_push`.
- A keepalive thread GETs 0x733 every `keepalive_s`; `keepalive_misses` missed replies in a row mark
  the session lost. Its replies are also delivered to `on_push` as `Push(0x734, Params)`.
- `on_push` / `on_lost` run on those internal threads, possibly concurrently: keep them short and
  thread-safe (e.g. `loop.call_soon_threadsafe`). `on_lost` fires at most once.

A Session is single-use: after it is lost or closed, `close()` it and build a new one to reconnect.
Session IDs and AV indexes are reused by the SDK, so never cache them across sessions.
"""
from __future__ import annotations

import datetime as dt
import logging
import queue
import threading
import time
from typing import Any, Callable, NamedTuple

from . import ops
from .crypto import KeyExchange
from .errors import (
    AV_ER_NOT_SUPPORT,
    NO_DATA,
    SEND_BUSY,
    NotLanDirect,
    ProtocolError,
    RequestTimeout,
    SessionGone,
    VtechError,
    check,
    error_for,
)

_LOG = logging.getLogger(__name__)


class Push(NamedTuple):
    """A decoded camera push (opcode + value from `ops.PUSH_DECODERS`), or a keepalive params read."""
    code: int
    value: Any


PARAMS_UPDATE = ops.GET_PARAMS.reply     # Push code used for keepalive 0x734 reads
SEND_RETRIES, SEND_RETRY_S = 20, 0.05     # a busy channel (SEND_BUSY) is retried for up to ~1 s


class Session:
    def __init__(
        self,
        sdk: Any,
        uid: str,
        authkey: str,
        password: str,
        account: str = "admin",
        *,
        name: str = "camera",
        on_push: Callable[[Push], None] | None = None,
        on_lost: Callable[[VtechError], None] | None = None,
        connect_timeout_s: int = 15,
        av_timeout_s: int = 30,
        request_timeout_s: float = 5.0,
        recv_timeout_ms: int = 1000,
        keepalive_s: float = 60.0,
        keepalive_misses: int = 3,
    ):
        self._sdk = sdk
        self._uid, self._authkey, self._password, self._account = uid, authkey, password, account
        self.name = name
        self.on_push, self.on_lost = on_push, on_lost
        self.connect_timeout_s, self.av_timeout_s = connect_timeout_s, av_timeout_s
        self.request_timeout_s, self.recv_timeout_ms = request_timeout_s, recv_timeout_ms
        self.keepalive_s, self.keepalive_misses = keepalive_s, keepalive_misses

        self.sid: int | None = None
        self.av: int | None = None
        self.devinfo: ops.DevInfo | None = None
        self.master: bytes | None = None

        self._lock = threading.Lock()                       # guards the fields below
        self._waiters: dict[int, queue.Queue] = {}          # reply opcode -> queue of payloads
        self._reply_locks: dict[int, threading.Lock] = {}   # one outstanding request per reply opcode
        self._lost: VtechError | None = None
        self._used = self._closed = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._rmw_lock = threading.Lock()                   # serialises read-modify-write SETs
        self._send_lock = threading.Lock()                  # serialises avSendIOCtrl (90-ha-runtime §3)
        self._master_lock = threading.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────────────
    def connect(self) -> ops.DevInfo:
        """Connect, start the AV channel, run the per-session prep. Returns the device info.

        Raises Unreachable (NotLanDirect for a P2P/relay session, which is refused), AuthFailed, Unlicensed, CapacityExceeded, RequestTimeout or VtechError.
        """
        with self._lock:
            if self._used:
                raise RuntimeError("Session objects are single-use; build a new one to reconnect")
            self._used = True
        sdk = self._sdk
        sid = check(sdk.get_session_id(), "IOTC_Get_SessionID")
        try:
            ses = check(sdk.connect(self._uid, sid, self._authkey, self.connect_timeout_s),
                        "IOTC_Connect_ByUIDEx")
            mode = self._mode_of(ses)
            if mode not in (None, "lan"):
                raise NotLanDirect(f"SDK connected via {mode}, not LAN-direct; refused (LAN-only)",
                                   call="IOTC_Session_Check")
            av = check(sdk.client_start(ses, self._account, self._password, self.av_timeout_s),
                       "avClientStartEx")
        except BaseException:
            sdk.session_close(sid)       # releases the ID from IOTC_Get_SessionID (unconfirmed)
            raise
        self.sid, self.av = ses, av
        self._start_thread("reader", self._read_loop)
        try:
            self._send_data_delay()
            self.devinfo = self.request(ops.DEVINFO)      # required before the VTech GETs reply
        except BaseException:
            self.close()
            raise
        if self.keepalive_s > 0:
            self._start_thread("keepalive", self._keepalive_loop)
        _LOG.debug("%s: connected (sid=%s av=%s) %s", self.name, ses, av, self.devinfo)
        return self.devinfo

    def _send_data_delay(self) -> None:
        """0x0ff, the first IOCtrl on a new AV channel. The SDK refuses it from application code with
        AV_ER_NOT_SUPPORT (-20033) on SDK 4.3.8.1; the reference client never checked
        this rc, and every later IOCtrl works regardless. Kept (and tolerated) in case another SDK
        build accepts it."""
        try:
            self.send(ops.SND_DATA_DELAY)
        except VtechError as e:
            if e.code != AV_ER_NOT_SUPPORT:
                raise
            _LOG.debug("%s: SDK refused 0x0ff (AV_ER_NOT_SUPPORT); continuing", self.name)

    def close(self) -> None:
        """Stop the threads, then avClientStop + IOTC_Session_Close. Idempotent; safe from callbacks."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            waiters = list(self._waiters.values())
        self._stop.set()
        for q in waiters:
            q.put(SessionGone("session closed"))
        me = threading.current_thread()
        for t in self._threads:
            if t is not me:
                t.join(timeout=self.recv_timeout_ms / 1000 + self.request_timeout_s + 1)
        if self.av is not None:
            self._sdk.client_stop(self.av)
        if self.sid is not None:
            self._sdk.session_close(self.sid)
        self.av = self.sid = None
        _LOG.debug("%s: closed", self.name)

    def connection_mode(self) -> str | None:
        """How the SDK reached the camera: "lan", "p2p" or "relay" (None if unknown). Without the
        companion's LAN-only policy the SDK falls back to ThroughTek's P2P/relay servers when the LAN
        path fails; connect() refuses any non-LAN session as a backstop."""
        return self._mode_of(self.sid)

    def _mode_of(self, sid: int | None) -> str | None:
        if sid is None or not hasattr(self._sdk, "session_mode"):
            return None
        return {0: "p2p", 1: "relay", 2: "lan"}.get(self._sdk.session_mode(sid))

    @property
    def connected(self) -> bool:
        return self.av is not None and self._lost is None and not self._closed

    # ── Messaging ────────────────────────────────────────────────────────────
    def send(self, op: ops.Op, *args: Any) -> None:
        """Encode and send an op without waiting for a reply."""
        self._send_raw(op.code, op.encode(*args), op.name)

    def request(self, op: ops.Op, *args: Any, timeout: float | None = None) -> Any:
        """Send and wait for the op's reply. Checks the ack (ProtocolError on failure) and returns the
        decoded value, or the raw reply bytes if the op has no decoder."""
        if op.reply is None:
            raise ValueError(f"{op.name} has no reply; use send()")
        (raw,) = self._transact(op, args, timeout, lambda _r: True)
        if op.ok is not None and not op.ok(raw):
            raise ProtocolError(f"{op.name}: camera reported failure (ack {raw.hex()})", call=op.name)
        return op.dec(raw) if op.dec else raw

    def _transact(self, op: ops.Op, args: tuple, timeout: float | None,
                  done: Callable[[bytes], bool]) -> list[bytes]:
        """Send `op` and collect replies until `done(reply)` is true."""
        reply = op.reply
        with self._lock:
            lock = self._reply_locks.setdefault(reply, threading.Lock())
        deadline = time.monotonic() + (timeout or self.request_timeout_s)
        with lock:
            q: queue.Queue = queue.Queue()
            with self._lock:
                if self._lost is not None or self._closed:
                    raise SessionGone(f"{op.name}: session is not connected", call=op.name)
                self._waiters[reply] = q
            try:
                self._send_raw(op.code, op.encode(*args), op.name)
                out: list[bytes] = []
                while True:
                    try:
                        item = q.get(timeout=max(0.0, deadline - time.monotonic()))
                    except queue.Empty:
                        raise RequestTimeout(f"{op.name}: no 0x{reply:x} reply", call=op.name) from None
                    if isinstance(item, BaseException):
                        raise item
                    out.append(item)
                    if done(item):
                        return out
            finally:
                with self._lock:
                    if self._waiters.get(reply) is q:
                        del self._waiters[reply]

    def _send_raw(self, code: int, payload: bytes, name: str) -> None:
        av = self.av
        if av is None or self._lost is not None or self._closed:
            raise SessionGone(f"{name}: session is not connected", call=name)
        with self._send_lock:            # one avSendIOCtrl per channel at a time (-20021 otherwise)
            for _ in range(SEND_RETRIES):
                rc = self._sdk.send_ioctrl(av, code, payload)
                if rc not in SEND_BUSY:  # busy: the SDK is still in another send or a buffer clean
                    break
                time.sleep(SEND_RETRY_S)
        if rc < 0:
            err = error_for(rc, f"avSendIOCtrl({name})")
            if isinstance(err, SessionGone):
                self._lose(err)
            raise err

    # ── Threads ──────────────────────────────────────────────────────────────
    def _start_thread(self, role: str, target: Callable[[], None]) -> None:
        t = threading.Thread(target=target, name=f"vtech-{self.name}-{role}", daemon=True)
        self._threads.append(t)
        t.start()

    def _read_loop(self) -> None:
        av = self.av
        while not self._stop.is_set():
            try:
                rc, code, payload = self._sdk.recv_ioctrl(av, self.recv_timeout_ms)
            except Exception as e:      # noqa: BLE001 — never let the reader die silently
                self._lose(SessionGone(f"avRecvIOCtrl raised {e!r}", call="avRecvIOCtrl"))
                return
            if rc >= 0:
                if code:
                    self._dispatch(code, payload)
            elif rc in NO_DATA:
                continue
            elif not self._stop.is_set():
                self._lose(error_for(rc, "avRecvIOCtrl"))
                return

    def _dispatch(self, code: int, payload: bytes) -> None:
        with self._lock:
            q = self._waiters.get(code)
        if q is not None:
            q.put(payload)
            return
        dec = ops.PUSH_DECODERS.get(code)
        if dec is None:
            _LOG.debug("%s: unhandled IOCtrl 0x%x len=%d %s", self.name, code, len(payload), payload[:32].hex())
            return
        try:
            value = dec(payload)
        except ProtocolError as e:
            _LOG.warning("%s: bad push 0x%x: %s", self.name, code, e)
            return
        self._emit(Push(code, value))

    def _keepalive_loop(self) -> None:
        misses = 0
        while not self._stop.wait(self.keepalive_s):
            try:
                params = self.request(ops.GET_PARAMS)
            except RequestTimeout:
                misses += 1
                _LOG.debug("%s: keepalive miss %d/%d", self.name, misses, self.keepalive_misses)
                if misses >= self.keepalive_misses:
                    self._lose(SessionGone(f"no reply to {misses} keepalives", call="keepalive"))
                    return
                continue
            except SessionGone:
                return
            except VtechError as e:
                _LOG.warning("%s: keepalive failed: %s", self.name, e)
                continue
            misses = 0
            self._emit(Push(PARAMS_UPDATE, params))

    def _emit(self, push: Push) -> None:
        if self.on_push is None:
            return
        try:
            self.on_push(push)
        except Exception:               # noqa: BLE001 — a callback bug must not kill the reader
            _LOG.exception("%s: on_push callback failed", self.name)

    def _lose(self, err: VtechError) -> None:
        with self._lock:
            if self._lost is not None or self._closed:
                return
            self._lost = err
            waiters = list(self._waiters.values())
        self._stop.set()
        for q in waiters:
            q.put(err)
        _LOG.info("%s: session lost: %s", self.name, err)
        if self.on_lost is not None:
            try:
                self.on_lost(err)
            except Exception:           # noqa: BLE001
                _LOG.exception("%s: on_lost callback failed", self.name)

    # ── Live-view key exchange ───────────────────────────────────────────────
    def live_view_master(self) -> bytes:
        """Run the 0x820/0x821 exchange once per session (round 2 only) and return the master key."""
        with self._master_lock:
            if self.master is None:
                kx = KeyExchange.new()
                self.master = kx.master(self.request(ops.LIVE_VIEW_KEY, kx.hello()))
            return self.master

    # ── Typed helpers: reads ─────────────────────────────────────────────────
    def get_params(self) -> ops.Params:
        return self.request(ops.GET_PARAMS)

    def get_night_light(self) -> ops.NightLight | int:
        return _enum(ops.NightLight, self.request(ops.GET_NIGHT_LIGHT))

    def get_status_led(self) -> bool:
        """True = the status LED is on."""
        return not self.request(ops.GET_STATUS_LED)

    def get_night_vision(self) -> ops.NightVisionMode | int:
        return _enum(ops.NightVisionMode, self.request(ops.GET_NIGHT_VISION))

    def get_quality(self) -> ops.Quality | int:
        return _enum(ops.Quality, self.request(ops.GET_STREAM_CTRL))

    def get_temp_alert(self) -> ops.TempAlert:
        return self.request(ops.GET_TEMP_ALERT)

    def get_motion(self) -> ops.MotionSetting:
        return self.request(ops.GET_MOTION)

    def get_sound_detect(self) -> bool:
        return self.request(ops.GET_SOUND)

    def get_notify_interval(self) -> ops.NotifyInterval | int:
        return _enum(ops.NotifyInterval, self.request(ops.GET_NOTIFY_INTERVAL))

    def get_parent_active(self) -> bool:
        return self.request(ops.GET_PARENT_ACTIVE)

    def get_timezone(self) -> ops.Timezone:
        return self.request(ops.GET_TIMEZONE)

    def get_dst(self) -> bool:
        return self.request(ops.GET_DST)

    def get_audio_out_format(self) -> ops.AudioFormat:
        return self.request(ops.GET_AUDIO_OUT_FORMAT)

    def list_events(self, start: dt.datetime, end: dt.datetime, timeout: float = 15.0) -> list[ops.StoredEvent]:
        """The stored (motion-only) event list between two times, all pages, newest first (camera order)."""
        pages = self._transact(ops.LIST_EVENTS, (start, end), timeout, lambda r: ops.dec_event_page(r).end)
        return [rec for raw in pages for rec in ops.dec_event_page(raw).records]

    # ── Typed helpers: writes (each raises ProtocolError if the camera acks a failure) ──
    def set_night_light(self, preset: ops.NightLight) -> None:
        self.request(ops.SET_NIGHT_LIGHT, ops.NightLight(preset))

    def set_status_led(self, on: bool) -> None:
        self.request(ops.SET_STATUS_LED, not on)

    def set_night_vision(self, mode: ops.NightVisionMode) -> None:
        """Note: applying a mode briefly drops video (~6–8 s) while the IR cut reconfigures."""
        self.request(ops.SET_NIGHT_VISION, ops.NightVisionMode(mode))

    def set_quality(self, quality: ops.Quality) -> ops.Quality | int:
        """Request a resolution, then return what the camera actually applied (it keeps 720p while
        the parent handset is active)."""
        self.request(ops.SET_STREAM_CTRL, ops.Quality(quality))
        return self.get_quality()

    def set_sound_detect(self, on: bool) -> None:
        self.request(ops.SET_SOUND, on)

    def set_motion_sensitivity(self, sensitivity: ops.MotionSensitivity) -> None:
        """Keeps the camera's current video-record flag."""
        with self._rmw_lock:
            cur = self.get_motion()
            self.request(ops.SET_MOTION, ops.MotionSensitivity(sensitivity), cur.record)

    def set_temp_alert(self, enabled: bool | None = None, max_c: float | None = None,
                       min_c: float | None = None) -> None:
        """Change any of enable / max / min, keeping the others."""
        with self._rmw_lock:
            cur = self.get_temp_alert()
            self.request(ops.SET_TEMP_ALERT, cur.enabled if enabled is None else enabled,
                         cur.max_c if max_c is None else max_c, cur.min_c if min_c is None else min_c)

    def set_notify_interval(self, index: ops.NotifyInterval) -> ops.NotifyInterval | int:
        """The SET's ack is undocumented, so it is sent fire-and-forget and confirmed by a re-read."""
        self.send(ops.SET_NOTIFY_INTERVAL, ops.NotifyInterval(index))
        return self.get_notify_interval()

    def update_params(self, *, lullaby_timer_s: int | None = None, volume: int | None = None,
                      motion_sensitivity: int | None = None) -> None:
        """0x731 read-modify-write. Sound sensitivity is not writable (the camera ignores it), and the
        opaque @28 field is echoed back unchanged."""
        with self._rmw_lock:
            cur = self.get_params()
            self.request(
                ops.SET_PARAMS,
                cur.lullaby_timer_s if lullaby_timer_s is None else lullaby_timer_s,
                cur.volume if volume is None else volume,
                cur.sound_sensitivity,
                cur.motion_sensitivity if motion_sensitivity is None else motion_sensitivity,
                cur.p28,
            )

    def play_lullaby(self, track: ops.LullabyTrack) -> None:
        """Audible in the room: only with the owner present."""
        self.request(ops.LULLABY, ops.LullabyTrack(track))

    def stop_lullaby(self) -> None:
        self.request(ops.LULLABY, ops.LullabyTrack.STOP)


def _enum(cls: type, value: int):
    """The enum member for `value`, or the raw int if the camera sent something undocumented."""
    try:
        return cls(value)
    except ValueError:
        return value
