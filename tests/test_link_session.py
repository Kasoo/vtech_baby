"""LinkSession, LinkMediaSource and LinkUplink over the fake `_vtech_link` (`fake_link.py`), the error
mapping, the worker running both backends side by side, and loading the real module when it is built."""
import os
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
    SdkMismatch,
    SessionGone,
    Unreachable,
    VtechError,
    from_link,
)
from vtech_lan.session import PARAMS_UPDATE, LinkSession, Push

from fake_link import FakeLinkModule, LinkError
from fake_sdk import FakeSdk, clip, enable_live_view, thread_client

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
    assert link.sent_codes() == [0x330]                     # DEVINFO first; no 0x0ff on the link
    assert mod.connects[0]["account"] == "admin"
    assert s.get_params().volume == 5
    s.set_night_light(ops.NightLight(1))
    assert s.get_night_light() == ops.NightLight(1)
    assert s.info()["dtls_suite"] == 0xCCAC


def test_connect_failures_map_to_the_sdk_exceptions(mod):
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
    import datetime as dt

    from test_ops import event_page                         # the same pages the SDK tests use
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
    return TalkBack(lambda: s, name="link", open_media=lambda _url: iter(media or FRAMES), frame_s=0.001, **kw)


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


def _drop(handle, code):
    def wrapped(c, p):
        return [] if c == code else handle(c, p)
    return wrapped


# ── The worker, both backends at once ────────────────────────────────────────
def test_worker_runs_sdk_and_link_sessions_side_by_side(mod, socket_enabled):
    sdk = FakeSdk()
    loaded = []

    def loader(path):
        loaded.append(path)
        return mod
    client = thread_client(sdk, link_loader=loader)
    try:
        client.ensure("/libdir", "fp", link_path="/x/_vtech_link.abi3.so")
        info = client.sdk_info()
        assert info["link"]["version"] == "0.1.0+fake" and info["link_path"] == "/x/_vtech_link.abi3.so"
        assert loaded == ["/x/_vtech_link.abi3.so"]
        a = client.session("SDKCAM", "k3yAuth8", "pw", name="a", keepalive_s=0)
        b = client.session("LINKCAM", "k3yAuth8", "pw", name="b", keepalive_s=0, backend="link")
        assert a.connect().model == "RM5754" and b.connect().model == "RM5754"
        assert b.connection_mode() == "lan"
        assert mod.opened[0].uid == "LINKCAM" and 0x330 in sdk.sent_codes()
        assert b.get_params().volume == 5
        a.close()
        b.close()
    finally:
        client.stop()


def test_link_only_worker(mod, socket_enabled):
    client = thread_client(FakeSdk(), link_loader=lambda _p: mod)
    try:
        client.ensure(None, "fp", link_path="/x/_vtech_link.abi3.so")
        assert client.sdk_info()["libdir"] is None
        s = client.session("LINKCAM", "k3yAuth8", "pw", keepalive_s=0, backend="link")
        s.connect()
        s.close()
        from custom_components.vtech_baby.vtech_lan.errors import SessionGone as WorkerSessionGone
        sdk_session = client.session("SDKCAM", "k3yAuth8", "pw", keepalive_s=0)
        with pytest.raises(WorkerSessionGone):
            sdk_session.connect()                           # no SDK in this worker
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
from vtech_lan.errors import SdkMismatch, from_link
m = link.load(sys.argv[2])
assert link.load(sys.argv[2]) is m
try:
    link.load(sys.argv[2] + ".other")
except SdkMismatch:
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
