"""LinkSession, LinkMediaSource and LinkUplink over the fake `_vtech_link` (`fake_link.py`), the error
mapping, a worker loading the module, and loading the real module when it is built."""
import datetime as dt
import os
import struct
import subprocess
import sys
import threading
import time

import pytest

from vtech_lan import ops
from vtech_lan.errors import (
    AuthFailed,
    CapacityExceeded,
    ProtocolError,
    RequestTimeout,
    SessionGone,
    Unreachable,
    VtechError,
    from_link,
)
from vtech_lan.session import PARAMS_UPDATE, LinkSession, Push

from fake_camera import clip, enable_live_view, i32
from fake_link import FakeLinkModule, LinkError, thread_client

FRAMES = [bytes([i]) * 160 for i in range(5)]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def mod():
    m = FakeLinkModule()
    enable_live_view(m.camera)
    return m


def session(mod, **kw):
    kw.setdefault("recv_timeout_ms", 20)
    kw.setdefault("request_timeout_s", 1)
    kw.setdefault("keepalive_s", 0)
    return LinkSession(mod, "UID", "k3yAuth8", "pw", **kw)


@pytest.fixture
def conn(mod):
    pushes, lost = [], []
    s = session(mod, on_push=pushes.append, on_lost=lost.append)
    s.connect()
    yield mod, s, pushes, lost
    s.close()


# ── Errors ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("kind", "detail", "code", "cls"), [
    ("unreachable", "", -13, Unreachable),
    ("iotc_rejected", "auth_key", -68, AuthFailed),
    ("iotc_rejected", "max_sessions", -48, CapacityExceeded),
    ("iotc_rejected", "port", -67, VtechError),
    ("av_auth_failed", "dtls_alert:51", -20009, AuthFailed),
    ("av_auth_failed", "dtls_silent", -20009, AuthFailed),
    ("closed", "remote_close", -22, SessionGone),
    ("closed", "remote_timeout", -23, SessionGone),
    ("closed", "local", None, SessionGone),
    ("timeout", "", -20011, RequestTimeout),
    ("protocol", "talk-back channel not open", None, ProtocolError),
    ("capacity", "", None, CapacityExceeded),
    ("invalid_argument", "authkey must be 8 characters", None, VtechError),
    ("io", "addrinuse", None, VtechError),
])
def test_from_link(kind, detail, code, cls):
    err = from_link(LinkError(kind, detail, code), "connect")
    assert type(err) is cls
    assert (err.call, err.code) == ("connect", code)
    assert kind in str(err) and detail in str(err)


# ── Session ──────────────────────────────────────────────────────────────────
def test_connect_runs_the_prep_and_answers_requests(conn):
    mod, s, _, _ = conn
    link = mod.opened[0]
    assert s.connected and s.backend == "link" and s.connection_mode() == "lan"
    assert s.devinfo.model == "RM5754"
    assert link.sent_codes() == [0x330]                     # DEVINFO first
    assert mod.connects[0]["account"] == "admin"
    assert s.get_params().volume == 5
    s.set_night_light(ops.NightLight(1))
    assert s.get_night_light() == ops.NightLight(1)
    assert s.info()["dtls_suite"] == 0xCCAC


def test_connect_failures_map_to_the_typed_exceptions(mod):
    mod.connect_error = LinkError("iotc_rejected", "auth_key", -68)
    with pytest.raises(AuthFailed) as e:
        session(mod).connect()
    assert e.value.code == -68
    mod.connect_error = LinkError("unreachable", "", -13)
    with pytest.raises(Unreachable):
        session(mod).connect()


def test_single_use(conn):
    _, s, _, _ = conn
    with pytest.raises(RuntimeError):
        s.connect()


def test_pushes_are_decoded(conn):
    mod, s, pushes, _ = conn
    s.set_night_light(ops.NightLight(2))                    # the camera follows the ack with a 0x7ac push
    assert wait_for(lambda: any(p.code == 0x7AC for p in pushes))


def test_keepalive_reads_are_pushed(mod):
    pushes = []
    s = session(mod, on_push=pushes.append, keepalive_s=0.05)
    s.connect()
    try:
        assert wait_for(lambda: any(p.code == PARAMS_UPDATE for p in pushes))
        assert isinstance(next(p for p in pushes if p.code == PARAMS_UPDATE), Push)
    finally:
        s.close()


def test_silent_camera_times_out(conn):
    mod, s, _, _ = conn
    mod.camera.silent.add(0x733)
    with pytest.raises(RequestTimeout):
        s.get_params()
    assert s.connected                                      # a timeout doesn't lose the session


def test_failed_ack_raises(conn):
    mod, s, _, _ = conn
    mod.camera.fail_acks.add(0x7A2)
    with pytest.raises(ProtocolError):
        s.set_night_light(ops.NightLight(1))


def test_event_list_collects_every_page(conn):
    from test_ops import event_page
    mod, s, _, _ = conn
    t = [dt.datetime(2026, 9, 22, h, tzinfo=ops.UTC) for h in range(3)]
    mod.camera.event_pages = [event_page(3, 0, 0, t[:2]), event_page(3, 1, 1, t[2:])]
    assert [r.time for r in s.list_events(t[0], t[2])] == t
    # The exchange is closed: a late page is a push, and the next list works.
    assert [r.time for r in s.list_events(t[0], t[2])] == t


def test_link_lost_from_the_camera(conn):
    mod, s, _, lost = conn
    mod.opened[0].kill()
    assert wait_for(lambda: lost)
    assert isinstance(lost[0], SessionGone) and lost[0].code == -22
    assert not s.connected
    with pytest.raises(SessionGone):
        s.get_params()


def test_close_closes_the_link(conn):
    mod, s, _, lost = conn
    s.close()
    s.close()
    assert mod.opened[0].closed.detail == "local"
    assert not lost


# ── Request dispatch, errors and loss ────────────────────────────────────────
def test_devinfo_timeout_tears_down(mod):
    mod.camera.silent.add(0x330)
    with pytest.raises(RequestTimeout):
        session(mod).connect()
    assert mod.opened[0].closed.detail == "local"


def test_push_interleaved_before_reply(conn):
    mod, s, pushes, _ = conn
    mod.opened[0].push(0x738, i32(23, 73, 0))               # arrives before the reply
    assert s.get_params().temp_c == 21
    assert wait_for(lambda: pushes)
    assert pushes[0] == Push(0x738, ops.TempNotify(23, 73, 0))


def test_set_emits_ack_and_push(conn):
    mod, s, pushes, _ = conn
    s.set_night_light(ops.NightLight.ORANGE)
    assert mod.camera.night_light == 2
    assert wait_for(lambda: any(p.code == 0x7AC for p in pushes))
    assert s.get_night_light() is ops.NightLight.ORANGE


def test_timeout_leaves_session_usable(conn):
    mod, s, _, _ = conn
    mod.camera.silent.add(0x7B0)
    with pytest.raises(RequestTimeout):
        s.get_sound_detect()
    assert s.connected
    assert s.get_params().volume == 5


def test_unknown_opcode_ignored(conn):
    mod, s, pushes, _ = conn
    mod.opened[0].push(0x1234, b"\x01\x02")
    mod.opened[0].push(0x738, b"\x01")                      # malformed push: dropped, not fatal
    assert s.get_params()
    assert pushes == []


def test_reply_opcode_exceptions(conn):
    mod, s, _, _ = conn
    link = mod.opened[0]
    mod.camera.led_off = 1
    assert s.get_status_led() is False                      # 0x709 answered by 0x710
    s.set_status_led(True)
    assert link.sent[-1] == (0x711, i32(0, 0))


def test_update_params_rmw_echoes_opaque(conn):
    mod, s, _, _ = conn
    s.update_params(volume=3)
    code, payload = mod.opened[0].sent[-1]
    assert code == 0x731
    assert struct.unpack("<6i", payload) == (0, 1800, 3, 2, 50, 1234)   # sound sens + @28 as read


def test_motion_keeps_record_flag(conn):
    mod, s, _, _ = conn
    s.set_motion_sensitivity(ops.MotionSensitivity.HIGH)
    assert mod.camera.motion == [75, 1]


def test_temp_alert_partial_update(conn):
    mod, s, _, _ = conn
    s.set_temp_alert(enabled=True)
    assert mod.camera.temp_alert == [1, 250, 160]
    s.set_temp_alert(max_c=26.5)
    assert s.get_temp_alert() == ops.TempAlert(True, 26.5, 16.0)


def test_notify_interval_fire_and_forget_then_reread(conn):
    _, s, _, _ = conn
    assert s.set_notify_interval(ops.NotifyInterval.MIN_10) is ops.NotifyInterval.MIN_10


def test_quality_reread(conn):
    _, s, _, _ = conn
    assert s.set_quality(ops.Quality.HD_720P) is ops.Quality.HD_720P


def test_unknown_enum_value_passes_through(conn):
    mod, s, _, _ = conn
    mod.camera.night_light = 200                            # the register stores invalid writes raw
    assert s.get_night_light() == 200


def test_concurrent_requests(conn):
    _, s, _, _ = conn
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


def test_remote_close_marks_lost_once_and_fails_pending(conn):
    mod, s, _, lost = conn
    mod.camera.silent.add(0x733)
    box = {}

    def pending():
        try:
            s.get_params()
        except Exception as e:                              # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=pending)
    t.start()
    time.sleep(0.05)
    mod.opened[0].kill()
    t.join(2)
    assert isinstance(box.get("e"), SessionGone)
    assert wait_for(lambda: lost) and len(lost) == 1 and isinstance(lost[0], SessionGone)
    assert not s.connected
    with pytest.raises(SessionGone):
        s.get_params()
    s.close()
    assert len(lost) == 1


def test_close_from_on_lost_callback_does_not_deadlock(mod):
    holder = {}
    s = session(mod, on_lost=lambda _e: holder["s"].close())
    holder["s"] = s
    s.connect()
    mod.opened[0].kill()
    assert wait_for(lambda: mod.opened[0].close_calls)


def test_keepalive_misses_mark_lost(mod):
    lost = []
    s = session(mod, on_lost=lost.append, keepalive_s=0.02, keepalive_misses=2, request_timeout_s=0.2)
    s.connect()
    mod.camera.silent.add(0x733)
    assert wait_for(lambda: lost, timeout=5)
    assert isinstance(lost[0], SessionGone) and "keepalive" in str(lost[0])
    s.close()


# ── Live view ────────────────────────────────────────────────────────────────
@pytest.mark.skipif(not clip(), reason="needs ffmpeg for the test clip")
def test_live_view_over_the_link(conn):
    pytest.importorskip("av")
    from test_media import Sink, streams_of
    from vtech_lan.media import LiveStream
    mod, s, _, _ = conn
    link = mod.opened[0]
    ls = LiveStream(lambda: s, name="link", grace_s=0.3)
    sink = Sink()
    sub = ls.subscribe(sink)
    try:
        assert wait_for(lambda: len(sink.data) > 50_000)
        codes = link.sent_codes()
        assert codes.index(ops.AUDIO_START.code) < codes.index(ops.VIDEO_FULLRATE.code) < codes.index(ops.START.code)
        assert link.resets == 1                             # the media reset, before START
        assert streams_of(sink.data) == [("audio", "aac"), ("video", "h264")]
    finally:
        sub.close()
        assert wait_for(lambda: not ls.running)
        ls.close()
    assert ops.STOP.code in link.sent_codes()
    assert ls.stats["receive"]["video_given_up"] == 0
    assert ls.stats["bad"] == 0


@pytest.mark.skipif(not clip(), reason="needs ffmpeg for the test clip")
def test_lost_frames_count_as_bad(conn):
    pytest.importorskip("av")
    from test_media import Sink
    from vtech_lan.media import LiveStream, LinkMediaSource
    mod, s, _, _ = conn
    sources = []
    real = s.media_source

    def keep():
        src = real()
        sources.append(src)
        return src
    s.media_source = keep
    ls = LiveStream(lambda: s, name="link", grace_s=0.3)
    sink = Sink()
    sub = ls.subscribe(sink)
    try:
        assert wait_for(lambda: sources and sources[0]._rx is not None and len(sink.data) > 10_000)
        assert isinstance(sources[0], LinkMediaSource)
        sources[0]._rx.lost_next = 3
        assert wait_for(lambda: ls.stats["bad"] == 3)
    finally:
        sub.close()
        ls.close()


# ── Talk-back ────────────────────────────────────────────────────────────────
class Ended:
    def __init__(self):
        self.event, self.err = threading.Event(), "not ended"

    def __call__(self, err):
        self.err = err
        self.event.set()

    def wait(self, timeout=5.0):
        assert self.event.wait(timeout), "talk-back did not end"
        return self.err


def talk(s, media=None, **kw):
    from vtech_lan.talkback import TalkBack
    return TalkBack(lambda: s, name="link", open_media=lambda _url: iter(media or FRAMES), **kw)


def test_talkback_plays_and_drains(conn):
    mod, s, _, _ = conn
    link = mod.opened[0]
    ended = Ended()
    talk(s).start("http://media/x", ended)
    assert ended.wait() is None
    assert link.uplink == FRAMES                            # plain A-law: the module encrypts
    assert [c for c in link.sent_codes() if c != 0x330] == [0x32A, 0x301, 0x350, 0x351]
    assert link.talks[0].closed_with[0] is True             # played out: queued frames drain
    assert mod.camera.speaker_ch is None


def test_talkback_stopped_does_not_drain(conn):
    mod, s, _, _ = conn

    def endless():
        while True:
            yield b"\x55" * 160
    t = talk(s, media=None)
    t._open_media = lambda _u: endless()
    ended = Ended()
    t.start("http://media/x", ended)
    assert wait_for(lambda: len(mod.opened[0].uplink) > 10)
    t.stop()
    assert ended.wait() is None
    assert mod.opened[0].talks[0].closed_with[0] is False


def test_talkback_send_failure_ends_it(conn):
    mod, s, _, lost = conn
    mod.opened[0].talk_send_error = LinkError("closed", "remote_close", -22)
    ended = Ended()
    talk(s).start("http://media/x", ended)
    err = ended.wait()
    assert isinstance(err, SessionGone) and err.code == -22
    assert wait_for(lambda: lost)


def test_talkback_never_accepted(conn, monkeypatch):
    from vtech_lan import talkback
    monkeypatch.setattr(talkback, "SERV_TIMEOUT_S", 0.1)
    mod, s, _, _ = conn
    mod.camera.handle = _drop(mod.camera.handle, 0x350)     # the camera never hears the announcement
    with pytest.raises(RequestTimeout):
        talk(s).start("http://media/x")
    assert mod.opened[0].sent_codes()[-1] == 0x351          # the stop sequence still ran


def _drop(handle, code):
    def wrapped(c, p):
        return [] if c == code else handle(c, p)
    return wrapped


# ── The worker ───────────────────────────────────────────────────────────────
def test_worker_loads_the_module_and_runs_sessions(mod, socket_enabled):
    client = thread_client(mod)
    try:
        info = client.ensure("/x/_vtech_link.abi3.so", "fp")
        assert info["link"]["version"] == "0.1.0+fake" and info["link_path"] == "/x/_vtech_link.abi3.so"
        s = client.session("LINKCAM", "k3yAuth8", "pw", name="b", keepalive_s=0)
        assert s.connect().model == "RM5754"
        assert s.connection_mode() == "lan"
        assert mod.opened[0].uid == "LINKCAM"
        assert s.get_params().volume == 5
        s.close()
    finally:
        client.stop()


# ── Loading the real module ──────────────────────────────────────────────────
REAL = os.path.join(ROOT, "vtech-link", "target", "debug", "lib_vtech_link.so")


@pytest.mark.skipif(not os.path.isfile(REAL), reason="vtech-link's Python module isn't built (py/tests/run.sh)")
def test_load_the_real_module():
    """In a subprocess: `link.load` starts a log thread that lives as long as the process."""
    script = """
import sys
sys.path.append(sys.argv[1])
from vtech_lan import link
from vtech_lan.errors import ModuleMismatch, from_link
m = link.load(sys.argv[2])
assert link.load(sys.argv[2]) is m
try:
    link.load(sys.argv[2] + ".other")
except ModuleMismatch:
    pass
else:
    raise SystemExit("a second path loaded")
info = link.info(m)
assert info["lan_only"] == {"refused_sends": 0, "refused_lookups": 0}, info
try:
    m.connect("UID", "short", "pw")
except m.LinkError as e:
    err = from_link(e, "connect")
    assert (type(err).__name__, err.call) == ("VtechError", "connect"), err
print("OK", m.__version__)
"""
    out = subprocess.run([sys.executable, "-I", "-c", script, os.path.join(ROOT, "custom_components", "vtech_baby"),
                          REAL], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("OK 0.")
