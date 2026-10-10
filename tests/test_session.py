"""Session: connect/prep sequence, request dispatch with interleaved pushes, errors, loss, keepalive."""
import datetime as dt
import struct
import threading
import time

import pytest

from vtech_lan import ops
from vtech_lan.errors import (
    AV_ER_NOT_SUPPORT,
    AV_ER_SESSION_CLOSE_BY_REMOTE,
    IOTC_ER_DEVICE_OFFLINE,
    IOTC_ER_DEVICE_REJECT_BY_WRONG_AUTH_KEY,
    AV_ER_WRONG_VIEWACCorPWD,
    AuthFailed,
    NotLanDirect,
    ProtocolError,
    RequestTimeout,
    SessionGone,
    Unreachable,
)
from vtech_lan.session import PARAMS_UPDATE, Push, Session

from fake_sdk import FakeSdk, i32
from test_crypto import fake_camera_reply
from test_ops import event_page


def make(sdk=None, **kw):
    sdk = sdk or FakeSdk()
    pushes, lost = [], []
    kw.setdefault("keepalive_s", 0)
    s = Session(sdk, "UID", "authkey", "pw", on_push=pushes.append, on_lost=lost.append,
                recv_timeout_ms=20, request_timeout_s=0.5, **kw)
    return s, sdk, pushes, lost


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def conn():
    s, sdk, pushes, lost = make()
    s.connect()
    yield s, sdk, pushes, lost
    s.close()


def test_connect_prep_order_and_devinfo(conn):
    s, sdk, _, _ = conn
    assert sdk.sent_codes()[:2] == [0x0FF, 0x330]          # SND_DATA_DELAY first, then DEVINFO
    assert s.devinfo == ops.DevInfo("RM5754", "vtech", "4.2.5.9")
    assert s.connected and (s.sid, s.av) == (7, 3)


def test_sdk_refusing_data_delay_is_tolerated():
    sdk = FakeSdk()
    sdk.send_rc_for[0x0FF] = AV_ER_NOT_SUPPORT        # what SDK 4.3.8.1 actually does
    s, _, _, _ = make(sdk)
    assert s.connect().model == "RM5754"
    s.close()


def test_other_data_delay_error_fails_connect():
    sdk = FakeSdk()
    sdk.send_rc_for[0x0FF] = -20003
    s, _, _, _ = make(sdk)
    with pytest.raises(Exception):
        s.connect()
    assert sdk.calls[-2:] == ["client_stop:3", "session_close:7"]


def test_close_order_and_idempotent():
    s, sdk, _, _ = make()
    s.connect()
    s.close()
    s.close()
    assert sdk.calls[-2:] == ["client_stop:3", "session_close:7"]
    assert not s.connected
    with pytest.raises(SessionGone):
        s.get_params()


def test_single_use():
    s, _, _, _ = make()
    s.connect()
    s.close()
    with pytest.raises(RuntimeError):
        s.connect()


@pytest.mark.parametrize("rc,exc", [(IOTC_ER_DEVICE_REJECT_BY_WRONG_AUTH_KEY, AuthFailed),
                                    (IOTC_ER_DEVICE_OFFLINE, Unreachable)])
def test_connect_failure_releases_sid(rc, exc):
    sdk = FakeSdk()
    sdk.connect_rc = rc
    s, _, _, _ = make(sdk)
    with pytest.raises(exc) as ei:
        s.connect()
    assert ei.value.code == rc
    assert "session_close:7" in sdk.calls and "client_start" not in sdk.calls


@pytest.mark.parametrize("mode", [0, 1])               # 0 = P2P, 1 = ThroughTek relay
def test_non_lan_session_refused_before_av(mode):
    sdk = FakeSdk()
    sdk.mode = mode
    s, _, _, _ = make(sdk)
    with pytest.raises(NotLanDirect):
        s.connect()
    assert "session_close:7" in sdk.calls
    assert not any(c.startswith("client_start") for c in sdk.calls)


def test_av_auth_failure():
    sdk = FakeSdk()
    sdk.client_start_rc = AV_ER_WRONG_VIEWACCorPWD
    s, _, _, _ = make(sdk)
    with pytest.raises(AuthFailed):
        s.connect()
    assert "session_close:7" in sdk.calls


def test_devinfo_timeout_tears_down():
    sdk = FakeSdk()
    sdk.camera.silent.add(0x330)
    s, _, _, _ = make(sdk)
    with pytest.raises(RequestTimeout):
        s.connect()
    assert sdk.calls[-2:] == ["client_stop:3", "session_close:7"]


def test_push_interleaved_before_reply(conn):
    s, sdk, pushes, _ = conn
    sdk.push(0x738, i32(23, 73, 0))                       # arrives before the reply
    assert s.get_params().temp_c == 21
    assert wait_for(lambda: pushes)
    assert pushes[0] == Push(0x738, ops.TempNotify(23, 73, 0))


def test_set_emits_ack_and_push(conn):
    s, sdk, pushes, _ = conn
    s.set_night_light(ops.NightLight.ORANGE)
    assert sdk.camera.night_light == 2
    assert wait_for(lambda: any(p.code == 0x7AC for p in pushes))
    assert s.get_night_light() is ops.NightLight.ORANGE


def test_ack_failure_raises(conn):
    s, sdk, _, _ = conn
    sdk.camera.fail_acks.add(0x7A2)
    with pytest.raises(ProtocolError):
        s.set_night_light(ops.NightLight.RED)


def test_timeout_leaves_session_usable(conn):
    s, sdk, _, _ = conn
    sdk.camera.silent.add(0x7B0)
    with pytest.raises(RequestTimeout):
        s.get_sound_detect()
    assert s.connected
    assert s.get_params().volume == 5


def test_unknown_opcode_ignored(conn):
    s, sdk, pushes, _ = conn
    sdk.push(0x1234, b"\x01\x02")
    sdk.push(0x738, b"\x01")                                # malformed push: dropped, not fatal
    assert s.get_params()
    assert pushes == []


def test_reply_opcode_exceptions(conn):
    s, sdk, _, _ = conn
    sdk.camera.led_off = 1
    assert s.get_status_led() is False                     # 0x709 answered by 0x710
    s.set_status_led(True)
    assert sdk.sent[-1] == (0x711, i32(0, 0))


def test_update_params_rmw_echoes_opaque(conn):
    s, sdk, _, _ = conn
    s.update_params(volume=3)
    code, payload = sdk.sent[-1]
    assert code == 0x731
    assert struct.unpack("<6i", payload) == (0, 1800, 3, 2, 50, 1234)   # sound sens + @28 as read


def test_motion_keeps_record_flag(conn):
    s, sdk, _, _ = conn
    s.set_motion_sensitivity(ops.MotionSensitivity.HIGH)
    assert sdk.camera.motion == [75, 1]


def test_temp_alert_partial_update(conn):
    s, sdk, _, _ = conn
    s.set_temp_alert(enabled=True)
    assert sdk.camera.temp_alert == [1, 250, 160]
    s.set_temp_alert(max_c=26.5)
    assert s.get_temp_alert() == ops.TempAlert(True, 26.5, 16.0)


def test_notify_interval_fire_and_forget_then_reread(conn):
    s, _, _, _ = conn
    assert s.set_notify_interval(ops.NotifyInterval.MIN_10) is ops.NotifyInterval.MIN_10


def test_quality_reread(conn):
    s, _, _, _ = conn
    assert s.set_quality(ops.Quality.HD_720P) is ops.Quality.HD_720P


def test_unknown_enum_value_passes_through(conn):
    s, sdk, _, _ = conn
    sdk.camera.night_light = 200                            # the register stores invalid writes raw
    assert s.get_night_light() == 200


def test_list_events_multi_page(conn):
    s, sdk, _, _ = conn
    t = [dt.datetime(2026, 9, 22, h, tzinfo=ops.UTC) for h in range(3)]
    sdk.camera.event_pages = [event_page(3, 0, 0, t[:2]), event_page(3, 1, 1, t[2:])]
    recs = s.list_events(t[0], t[2])
    assert [r.time for r in recs] == t


def test_live_view_master(conn):
    s, sdk, _, _ = conn
    cam = {}

    def reply(hello):
        r, cam["master"] = fake_camera_reply(hello)
        return r
    sdk.camera.key_reply = reply
    m = s.live_view_master()
    assert m == cam["master"]
    assert s.live_view_master() is m                        # once per session
    assert sdk.sent_codes().count(0x820) == 1


def test_concurrent_requests(conn):
    s, _, _, _ = conn
    results, errors = [], []

    def worker(fn):
        try:
            for _ in range(20):
                results.append(fn())
        except Exception as e:                              # noqa: BLE001
            errors.append(e)
    fns = [s.get_params, s.get_night_light, s.get_motion, s.get_sound_detect, s.get_params]
    threads = [threading.Thread(target=worker, args=(f,)) for f in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(results) == 100


def test_remote_close_marks_lost_once_and_fails_pending():
    s, sdk, _, lost = make()
    s.connect()
    sdk.camera.silent.add(0x733)
    box = {}

    def pending():
        try:
            s.get_params()
        except Exception as e:                              # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=pending)
    t.start()
    time.sleep(0.05)
    sdk.dead_rc = AV_ER_SESSION_CLOSE_BY_REMOTE
    t.join(2)
    assert isinstance(box.get("e"), SessionGone)
    assert wait_for(lambda: lost) and len(lost) == 1 and isinstance(lost[0], SessionGone)
    assert not s.connected
    with pytest.raises(SessionGone):
        s.get_params()
    s.close()
    assert sdk.calls[-2:] == ["client_stop:3", "session_close:7"]


def test_close_from_on_lost_callback_does_not_deadlock():
    sdk = FakeSdk()
    holder = {}
    s = Session(sdk, "UID", "k", "pw", on_lost=lambda e: holder["s"].close(),
                recv_timeout_ms=20, request_timeout_s=0.5, keepalive_s=0)
    holder["s"] = s
    s.connect()
    sdk.dead_rc = AV_ER_SESSION_CLOSE_BY_REMOTE
    assert wait_for(lambda: "session_close:7" in sdk.calls)


def test_keepalive_emits_params():
    s, sdk, pushes, lost = make(keepalive_s=0.05)
    s.connect()
    assert wait_for(lambda: any(p.code == PARAMS_UPDATE for p in pushes))
    assert isinstance(next(p for p in pushes if p.code == PARAMS_UPDATE).value, ops.Params)
    assert not lost
    s.close()


def test_keepalive_misses_mark_lost():
    s, sdk, _, lost = make(keepalive_s=0.02, keepalive_misses=2)
    s.connect()
    sdk.camera.silent.add(0x733)
    assert wait_for(lambda: lost, timeout=5)
    assert isinstance(lost[0], SessionGone) and "keepalive" in str(lost[0])
    s.close()


def test_send_error_marks_lost(conn):
    s, sdk, _, lost = conn
    sdk.send_rc = AV_ER_SESSION_CLOSE_BY_REMOTE
    with pytest.raises(SessionGone):
        s.get_params()
    assert lost


def test_sends_are_serialised():
    """Two threads requesting at once never overlap inside avSendIOCtrl (-20021 otherwise)."""
    sdk = FakeSdk()
    inside, overlaps = [0], []
    real = sdk.send_ioctrl

    def slow_send(av, code, payload):
        inside[0] += 1
        if inside[0] > 1:
            overlaps.append(code)
        time.sleep(0.02)
        try:
            return real(av, code, payload)
        finally:
            inside[0] -= 1

    sdk.send_ioctrl = slow_send
    s = make(sdk)[0]
    s.connect()
    ts = [threading.Thread(target=s.get_params) for _ in range(2)] + \
         [threading.Thread(target=s.get_motion) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    s.close()
    assert overlaps == []
