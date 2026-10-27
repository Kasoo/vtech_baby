"""CameraManager: owns one camera session, keeps its state cache, reconnects, and feeds entities.

Runtime rules: docs/vtech-lan-kit/docs/90-ha-runtime.md.
- The native module runs in a worker process (`vtech_lan/worker.py`); the manager drives it through
  proxies with the session's blocking methods. If the worker dies, its sessions are lost and reconnect like any other.
- Every session call runs in the executor; pushes arrive on the client's reader thread and are
  marshalled to the event loop with `call_soon_threadsafe`.
- Pushes drive state (`local_push`); a full re-read happens on every (re)connect.
- After a lost session or a failed connect, reconnect with backoff 10 s → 5 min.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import native
from .const import (
    CONF_ACCOUNT,
    CONF_AUTHKEY,
    CONF_AV_PASSWORD,
    CONF_MAC,
    CONF_UID,
    DOMAIN,
    ISSUE_WORKER_CRASHED,
    KEEPALIVE_S,
    NOTIFY_INTERVAL_S,
    QUALITY_REREAD_S,
    RECONNECT_MAX_S,
    RECONNECT_MIN_S,
    SELF_TRIGGER_S,
    SIGNAL_EVENT,
    STALE_CREDS_FAILURES,
)
from .vtech_lan import ops
from .vtech_lan.errors import (
    AuthFailed,
    MediaError,
    ProtocolError,
    RequestTimeout,
    SessionGone,
    Unreachable,
    VtechError,
)
from .vtech_lan.session import PARAMS_UPDATE, Push
from .vtech_lan.worker_client import SessionProxy, WorkerClient

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOG = logging.getLogger(__name__)


# ── The worker (shared by setup and the config flow) ─────────────────────────
class SetupError(Exception):
    """The native module can't be found or loaded; the message is user-presentable."""


DATA_CLIENT = f"{DOMAIN}_worker"


def get_client(hass: HomeAssistant) -> WorkerClient:
    """The process-wide worker client, stopped with Home Assistant. The worker itself starts on the
    first `ensure()`."""
    client: WorkerClient | None = hass.data.get(DATA_CLIENT)
    if client is None:
        def on_crash(reason: str, count: int) -> None:          # reader thread
            hass.loop.call_soon_threadsafe(_crash_issue, hass, reason, count)

        client = hass.data[DATA_CLIENT] = make_client(on_crash)

        async def _stop(_event: Event) -> None:
            await hass.async_add_executor_job(client.stop)
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)
    return client


def make_client(on_crash: Callable[[str, int], None]) -> WorkerClient:
    """Separate so tests can run the worker on a thread with a fake module."""
    return WorkerClient(on_crash=on_crash)


@callback
def _crash_issue(hass: HomeAssistant, reason: str, count: int) -> None:
    ir.async_create_issue(hass, DOMAIN, ISSUE_WORKER_CRASHED, is_fixable=False, is_persistent=False,
                          severity=ir.IssueSeverity.WARNING, translation_key=ISSUE_WORKER_CRASHED,
                          translation_placeholders={"reason": reason, "count": str(count)})


def _prepare_and_load(client: WorkerClient) -> WorkerClient:
    try:
        prepared = native.prepare()
        client.ensure(prepared.link_path, prepared.fingerprint)
        return client
    except (VtechError, OSError) as err:
        raise SetupError(str(err)) from err


async def async_load(hass: HomeAssistant) -> WorkerClient:
    """Make sure the worker runs with this host's module. Serialised, so one setup starts it."""
    lock = hass.data.setdefault(f"{DOMAIN}_load_lock", asyncio.Lock())
    async with lock:
        return await hass.async_add_executor_job(_prepare_and_load, get_client(hass))


def _probe(client: WorkerClient, uid: str, authkey: str, password: str, account: str) -> ops.DevInfo:
    s = client.session(uid, authkey, password, account, name=f"probe {uid[-4:]}", keepalive_s=0)
    try:
        return s.connect()
    finally:
        s.close()


async def async_probe(hass: HomeAssistant, client: WorkerClient, creds: dict[str, str]) -> ops.DevInfo:
    """Connect once and disconnect: the config flow's credential test. Raises VtechError subclasses."""
    return await hass.async_add_executor_job(
        _probe, client, creds[CONF_UID], creds[CONF_AUTHKEY], creds[CONF_AV_PASSWORD], creds[CONF_ACCOUNT])


def mac_present(mac: str, arp_path: str = "/proc/net/arp") -> bool:
    """True if `mac` has a complete IPv4 neighbour entry on this host (HA runs with Network=host).

    The cheap disambiguator for the -13 trap: the camera is on the LAN, yet IOTC connects time out.
    """
    if not mac:
        return False
    mac = mac.lower()
    try:
        with open(arp_path) as f:
            next(f, None)
            for line in f:
                cols = line.split()
                if len(cols) >= 4 and cols[3].lower() == mac and int(cols[2], 16) & 0x2:
                    return True
    except (OSError, ValueError):
        pass
    return False


# ── State ────────────────────────────────────────────────────────────────────
@dataclass
class CameraState:
    """The manager's state cache. None = not read yet (or the read failed)."""
    devinfo: ops.DevInfo | None = None
    params: ops.Params | None = None
    temp_state: int | None = None          # ops.TempState
    night_light: int | None = None
    status_led: bool | None = None
    night_vision: int | None = None        # mode setting
    night_active: bool | None = None       # IR currently on (push 0x785 only)
    quality: int | None = None
    temp_alert: ops.TempAlert | None = None
    motion: ops.MotionSetting | None = None
    sound_detect: bool | None = None
    notify_interval: int | None = None
    parent_active: bool | None = None
    connection_mode: str | None = None     # lan / p2p / relay (IOTC_Session_Check)


def derive_temp_state(temp_c: int, alert: ops.TempAlert) -> int:
    """Initial temperature state before the first 0x738 push. The camera's own comparison
    (strict or not, °C or °F) is unverified; 0x738 pushes overwrite this."""
    if temp_c > alert.max_c:
        return ops.TempState.TOO_HOT
    if temp_c < alert.min_c:
        return ops.TempState.TOO_COLD
    return ops.TempState.IN_RANGE


_READS: tuple[tuple[str, str], ...] = (
    ("params", "get_params"), ("night_light", "get_night_light"), ("status_led", "get_status_led"),
    ("night_vision", "get_night_vision"), ("quality", "get_quality"), ("temp_alert", "get_temp_alert"),
    ("motion", "get_motion"), ("sound_detect", "get_sound_detect"),
    ("notify_interval", "get_notify_interval"), ("parent_active", "get_parent_active"),
)


def read_state(session: SessionProxy, previous: CameraState | None = None) -> CameraState:
    """All GETs. A failed read keeps the previous value (None on a first read); a lost session raises.

    After two timeouts in a row the rest is skipped: the camera has stopped answering for now (seen live
    for ~35 s, cause unknown; AGENTS.md, "Known issues"), and the keepalive decides whether the session is lost.
    """
    st = dataclasses.replace(previous) if previous is not None else CameraState()
    st.devinfo, st.connection_mode = session.devinfo, session.connection_mode()
    timeouts = 0
    for field, getter in _READS:
        try:
            setattr(st, field, getattr(session, getter)())
            timeouts = 0
        except RequestTimeout as err:
            timeouts += 1
            _LOG.warning("%s: %s failed: %s", session.name, getter, err)
            if timeouts >= 2:
                _LOG.warning("%s: camera not answering; skipping the rest of this re-read", session.name)
                break
        except ProtocolError as err:
            _LOG.warning("%s: %s failed: %s", session.name, getter, err)
    if st.params is not None and st.temp_alert is not None:
        st.temp_state = derive_temp_state(st.params.temp_c, st.temp_alert)
    return st


def _connect_and_read(session: SessionProxy) -> CameraState:
    session.connect()
    return read_state(session)


def apply_push(st: CameraState, push: Push) -> bool:
    """Fold a push into the state. Returns False if it carries no state (e.g. an event report)."""
    c, v = push.code, push.value
    if c == PARAMS_UPDATE:
        st.params = v
        if st.motion is not None and st.motion.sensitivity != v.motion_sensitivity:
            st.motion = dataclasses.replace(st.motion, sensitivity=v.motion_sensitivity)
    elif c == ops.TEMP_NOTIFY:
        st.temp_state = v.state
        if st.params is not None:
            st.params = dataclasses.replace(st.params, temp_c=v.temp_c, temp_f=v.temp_f)
    elif c == ops.DAYNIGHT_NOTIFY:
        st.night_active = v
    elif c == ops.PARENT_NOTIFY:
        st.parent_active = v
    elif c == ops.PARAM_NOTIFY:
        sub, val = v.subtype, v.value
        if sub == ops.ParamSubtype.NIGHT_LIGHT:
            st.night_light = val
        elif sub == ops.ParamSubtype.STATUS_LED:
            st.status_led = not val                    # the wire flag means "LED disabled"
        elif sub == ops.ParamSubtype.TEMP_DETECT_ENABLE and st.temp_alert is not None:
            st.temp_alert = dataclasses.replace(st.temp_alert, enabled=bool(val))
        elif st.params is not None and sub == ops.ParamSubtype.LULLABY_TRACK:
            st.params = dataclasses.replace(st.params, lullaby_track=val)
        elif st.params is not None and sub == ops.ParamSubtype.SPEAKER_VOLUME:
            st.params = dataclasses.replace(st.params, volume=val)
        elif st.params is not None and sub == ops.ParamSubtype.LULLABY_TIMER:
            st.params = dataclasses.replace(st.params, lullaby_timer_s=val)
        else:
            return False
    else:
        return False
    return True


# ── Manager ──────────────────────────────────────────────────────────────────
class CameraManager(DataUpdateCoordinator[CameraState]):
    """One per config entry. `data` is the CameraState; availability follows the session."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: WorkerClient) -> None:
        super().__init__(hass, _LOG, config_entry=entry, name=f"{DOMAIN} {entry.title}",
                         update_interval=None, always_update=True)
        self.client = client
        self.uid: str = entry.data[CONF_UID]
        self.session: SessionProxy | None = None
        self.failures = 0                       # consecutive failed connects
        self.last_error: str | None = None
        self._reconnect: asyncio.Task | None = None
        self._quality_reread: asyncio.Task | None = None
        self._closing = False
        self._lullaby_stopped = float("-inf")
        self.talking = False
        self._talk_stopped = float("-inf")
        # The live view and talk-back run in the worker on this camera's current session; they end when it goes.
        self.media = client.media(self.uid, on_running=lambda _r: hass.loop.call_soon_threadsafe(
            self.async_update_listeners))
        self.talk = client.talk(self.uid)

    # Connection lifecycle
    def _new_session(self) -> SessionProxy:
        d = self.config_entry.data
        s: SessionProxy
        s = self.client.session(
            d[CONF_UID], d[CONF_AUTHKEY], d[CONF_AV_PASSWORD], d[CONF_ACCOUNT], media=True,
            name=self.uid[-4:], keepalive_s=KEEPALIVE_S,
            on_push=lambda p: self.hass.loop.call_soon_threadsafe(self._on_push, s, p),
            on_lost=lambda e: self.hass.loop.call_soon_threadsafe(self._on_lost, s, e))
        return s

    async def _async_connect(self) -> None:
        session = self._new_session()
        try:
            state = await self.hass.async_add_executor_job(_connect_and_read, session)
        except BaseException:
            await self.hass.async_add_executor_job(session.close)
            raise
        if self._closing:
            await self.hass.async_add_executor_job(session.close)
            return
        self.session, self.failures, self.last_error = session, 0, None
        _LOG.info("%s: connected via %s (%s)", self.name, state.connection_mode, state.devinfo)
        # The credentials work after all (e.g. a -13 trap false alarm): withdraw any reauth prompt.
        for flow in self.config_entry.async_get_active_flows(self.hass, {SOURCE_REAUTH}):
            self.hass.config_entries.flow.async_abort(flow["flow_id"])
        self.async_set_updated_data(state)

    async def async_first_connect(self) -> None:
        """Setup-time connect: maps failures to HA's setup exceptions."""
        try:
            await self._async_connect()
        except AuthFailed as err:
            raise ConfigEntryAuthFailed(f"camera rejected the credentials: {err}") from err
        except VtechError as err:
            self.last_error = str(err)
            raise ConfigEntryNotReady(f"cannot connect to the camera: {err}") from err

    async def _async_update_data(self) -> CameraState:
        """A full re-read (homeassistant.update_entity). Never polled: update_interval is None."""
        if self.session is None:
            raise HomeAssistantError("camera not connected")
        try:
            return await self.hass.async_add_executor_job(read_state, self.session, self.data)
        except VtechError as err:
            raise HomeAssistantError(str(err)) from err

    @callback
    def _on_push(self, session: SessionProxy, push: Push) -> None:
        if session is not self.session or self.data is None:
            return                              # a stale session, or before the first full read
        if push.code == ops.EVENT_REPORT:
            self._dispatch_event(push.value)
            return
        was_playing, parent = self.lullaby_playing, self.data.parent_active
        if apply_push(self.data, push):
            if was_playing and not self.lullaby_playing:
                self._lullaby_stopped = time.monotonic()
            if push.code == ops.PARENT_NOTIFY and self.data.parent_active != parent:
                self._schedule_quality_reread()
            self.async_set_updated_data(self.data)

    @callback
    def _schedule_quality_reread(self) -> None:
        if self._quality_reread is not None:
            self._quality_reread.cancel()           # only the latest handset change matters
        self._quality_reread = self.config_entry.async_create_background_task(
            self.hass, self._async_reread_quality(), f"{DOMAIN} quality {self.uid[-4:]}")

    async def _async_reread_quality(self) -> None:
        """Follow the handset's 720p cap being applied or lifted (it isn't pushed; kit 40)."""
        for delay in QUALITY_REREAD_S:
            await asyncio.sleep(delay)
            session = self.session
            if session is None or self._closing or self.data is None:
                return
            try:
                quality = int(await self.hass.async_add_executor_job(session.get_quality))
            except VtechError as err:
                _LOG.debug("%s: resolution re-read failed: %s", self.name, err)
                continue
            if self.data is not None and self.data.quality != quality:
                self.async_patch_state(quality=quality)

    @property
    def lullaby_playing(self) -> bool:
        return self.lullaby_track is not None

    @property
    def lullaby_track(self) -> ops.LullabyTrack | None:
        """The lullaby playing (PLAY_ALL while cycling through them all), or None."""
        p = self.data.params if self.data else None
        if p is None or p.lullaby_track < 0:
            return None
        try:
            return ops.LullabyTrack(p.lullaby_track)
        except ValueError:
            return None

    async def async_play_lullaby(self, track: ops.LullabyTrack) -> None:
        """Audible in the room."""
        await self.async_command(lambda s: s.play_lullaby(track))
        self._patch_lullaby(int(track))

    async def async_stop_lullaby(self) -> None:
        await self.async_command(lambda s: s.stop_lullaby())
        self._patch_lullaby(int(ops.LullabyTrack.STOP))

    async def async_set_volume(self, volume: int) -> None:
        """Set the speaker volume on the camera's scale (ops.VOLUME_MIN..VOLUME_MAX)."""
        volume = min(max(volume, ops.VOLUME_MIN), ops.VOLUME_MAX)
        await self.async_command(lambda s: s.update_params(volume=volume))
        if self.data is not None and self.data.params is not None:
            self.async_patch_state(params=dataclasses.replace(self.data.params, volume=volume))

    @callback
    def _patch_lullaby(self, track: int) -> None:
        """Apply an acked 0x735 now; the camera's push confirms it later."""
        if self.data is None or self.data.params is None:
            return
        if self.lullaby_playing and track < 0:
            self._lullaby_stopped = time.monotonic()
        self.async_patch_state(params=dataclasses.replace(self.data.params, lullaby_track=track))

    @callback
    def _dispatch_event(self, report: ops.EventReport) -> None:
        """Sound alerts while a lullaby or talk-back plays, or within SELF_TRIGGER_S after it stops, are
        tagged self-triggered (the camera hears its own speaker)."""
        self_triggered = report.code == ops.EventCode.SOUND and (
            self.lullaby_playing or self.talking
            or time.monotonic() - max(self._lullaby_stopped, self._talk_stopped) < SELF_TRIGGER_S)
        async_dispatcher_send(self.hass, SIGNAL_EVENT.format(self.config_entry.entry_id), report, self_triggered)

    def event_timeout_s(self) -> float:
        """How long a motion/sound binary_sensor stays on after a push: the notification interval
        (which rate-limits the pushes) + 15 s, minimum 30 s."""
        idx = self.data.notify_interval if self.data else None
        return max(30.0, NOTIFY_INTERVAL_S.get(idx, 60.0) + 15.0)

    @callback
    def _on_lost(self, session: SessionProxy, err: VtechError) -> None:
        if session is not self.session:
            return
        _LOG.warning("%s: session lost: %s", self.name, err)
        self.session, self.last_error = None, str(err)
        self.hass.async_add_executor_job(session.close)
        self.last_update_success = False
        self.async_update_listeners()
        self._schedule_reconnect()

    @callback
    def _schedule_reconnect(self) -> None:
        if self._closing or (self._reconnect is not None and not self._reconnect.done()):
            return
        self._reconnect = self.config_entry.async_create_background_task(
            self.hass, self._reconnect_loop(), f"{DOMAIN} reconnect {self.uid[-4:]}")

    async def _reconnect_loop(self) -> None:
        delay = RECONNECT_MIN_S
        while not self._closing:
            await asyncio.sleep(delay)
            try:
                await self._async_connect()
                return
            except AuthFailed as err:
                self.last_error = str(err)
                _LOG.warning("%s: credentials rejected (%s); starting re-authentication", self.name, err)
                self.config_entry.async_start_reauth(self.hass)
                return
            except Unreachable as err:
                self.failures += 1
                self.last_error = str(err)
                if self.failures >= STALE_CREDS_FAILURES and await self.hass.async_add_executor_job(
                        mac_present, self.config_entry.data.get(CONF_MAC, "")):
                    # The -13 trap: on the LAN but not answering IOTC. Most likely the camera's
                    # credentials rotated (Wi-Fi change). Ask for a fresh QR; keep retrying meanwhile.
                    _LOG.warning("%s: camera is on the LAN but connects keep timing out; its "
                                 "credentials may have rotated, starting re-authentication", self.name)
                    self.config_entry.async_start_reauth(self.hass)
            except VtechError as err:
                self.failures += 1
                self.last_error = str(err)
                _LOG.debug("%s: reconnect failed: %s", self.name, err)
            delay = min(delay * 2, RECONNECT_MAX_S)

    async def async_close(self) -> None:
        """Unload: close this entry's session and live view. The worker keeps running for other entries."""
        self._closing = True
        for task in (self._reconnect, self._quality_reread):
            if task is not None:
                task.cancel()
        session, self.session = self.session, None
        # Before the session: they use its channels.
        await self.hass.async_add_executor_job(self.talk.close)
        await self.hass.async_add_executor_job(self.media.close)
        if session is not None:
            await self.hass.async_add_executor_job(session.close)

    # Commands
    async def async_command(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run a blocking session call in the executor; map failures to HomeAssistantError."""
        session = self.session
        if session is None:
            raise HomeAssistantError("camera not connected")
        try:
            return await self.hass.async_add_executor_job(partial(fn, session, *args))
        except SessionGone as err:
            raise HomeAssistantError(f"camera connection lost: {err}") from err
        except VtechError as err:
            raise HomeAssistantError(f"camera command failed: {err}") from err

    async def async_talk(self, url: str) -> None:
        """Play `url` through the camera's speaker and return when it has played. Cancelling stops it."""
        if self.session is None:
            raise HomeAssistantError("camera not connected")
        loop = self.hass.loop
        ended: asyncio.Future[VtechError | None] = loop.create_future()

        def on_end(err: VtechError | None) -> None:               # client reader thread
            loop.call_soon_threadsafe(lambda: ended.done() or ended.set_result(err))

        try:
            await self.hass.async_add_executor_job(self.talk.start, url, on_end)
        except asyncio.CancelledError:
            await self.hass.async_add_executor_job(self.talk.stop)     # it may have started regardless
            raise
        except MediaError as err:
            raise HomeAssistantError(f"can't play the media: {err}") from err
        except VtechError as err:
            raise HomeAssistantError(f"talk-back failed: {err}") from err
        self.talking = True
        self.async_update_listeners()
        try:
            err = await asyncio.shield(ended)                     # a cancel must leave `ended` pending
        finally:
            if not ended.done():
                await self.hass.async_add_executor_job(self.talk.stop)
            self.talking, self._talk_stopped = False, time.monotonic()
            self.async_update_listeners()
        if err is not None:
            raise HomeAssistantError(f"talk-back stopped: {err}")

    @callback
    def async_patch_state(self, **changes: Any) -> None:
        """Apply a confirmed write to the cache and notify entities."""
        if self.data is None:
            return
        for k, v in changes.items():
            setattr(self.data, k, v)
        self.async_set_updated_data(self.data)
