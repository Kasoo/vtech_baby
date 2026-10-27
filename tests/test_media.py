"""LiveStream over the fake link: on-demand start/stop, keyframe joins, grace, loss, snapshots."""
import io
import threading
import time

import pytest

av = pytest.importorskip("av")

from vtech_lan import media, ops  # noqa: E402
from vtech_lan.errors import SessionGone  # noqa: E402
from vtech_lan.media import LiveStream, has_nal, is_keyframe  # noqa: E402
from vtech_lan.session import LinkSession  # noqa: E402

import fake_camera  # noqa: E402
from fake_camera import clip, enable_live_view  # noqa: E402
from fake_link import FakeLinkModule  # noqa: E402

pytestmark = pytest.mark.skipif(not clip(), reason="needs ffmpeg for the test clip")


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class Sink:
    def __init__(self):
        self.chunks, self.ended = [], threading.Event()

    def __call__(self, data):
        if data is None:
            self.ended.set()
        else:
            self.chunks.append(data)

    @property
    def data(self):
        return b"".join(self.chunks)


@pytest.fixture
def cam():
    mod = FakeLinkModule()
    enable_live_view(mod.camera)
    s = LinkSession(mod, "UID", "k3yAuth8", "pw", recv_timeout_ms=20, request_timeout_s=1, keepalive_s=0)
    s.connect()
    holder = {"s": s}
    ls = LiveStream(lambda: holder["s"], name="test", grace_s=0.3)
    yield mod.opened[0], s, ls, holder
    ls.close()
    s.close()


def streams_of(ts: bytes):
    with av.open(io.BytesIO(ts), format="mpegts") as c:
        return sorted((st.type, st.codec_context.name) for st in c.streams)


def starts(link):
    return link.sent_codes().count(ops.START.code)


def test_subscribe_starts_and_delivers_ts(cam):
    link, _, ls, _ = cam
    sink = Sink()
    sub = ls.subscribe(sink)
    assert wait_for(lambda: len(sink.data) > 50_000)
    codes = link.sent_codes()
    assert codes.index(ops.AUDIO_START.code) < codes.index(ops.VIDEO_FULLRATE.code) < codes.index(ops.START.code)
    assert link.resets == 1
    assert sink.data[0] == 0x47 and len(sink.data) % 188 == 0
    assert (sink.data[1] & 0x1F) << 8 | sink.data[2] == 0             # starts at a PAT
    assert streams_of(sink.data) == [("audio", "aac"), ("video", "h264")]
    sub.close()


def test_receive_counters_refresh_and_log_losses(cam, monkeypatch, caplog):
    link, _, ls, _ = cam
    monkeypatch.setattr(media, "RECV_STATS_S", 0.05)
    zero = dict.fromkeys(("packets_requested", "video_given_up", "video_late", "video_overflow",
                          "audio_given_up", "audio_late", "audio_overflow", "packets_recovered"), 0)
    link.recv = dict(zero)
    caplog.set_level("DEBUG", logger=media.__name__)
    sink = Sink()
    sub = ls.subscribe(sink)
    assert wait_for(lambda: (ls.stats.get("receive") or {}).items() >= zero.items())
    link.recv = {**zero, "packets_requested": 4, "video_late": 1}
    assert wait_for(lambda: ls.stats["receive"]["packets_requested"] == 4)
    assert "frames lost" not in caplog.text
    link.recv = {**zero, "packets_requested": 9, "video_given_up": 2}
    assert wait_for(lambda: "frames lost: {'video_given_up': 2}" in caplog.text)
    sub.close()
    assert wait_for(lambda: not ls.running)
    assert ls.stats["receive"]["video_given_up"] == 2


def test_losses_before_a_start_are_not_logged_again(cam, monkeypatch, caplog):
    link, _, ls, _ = cam
    monkeypatch.setattr(media, "RECV_STATS_S", 0.05)
    zero = dict.fromkeys(("packets_requested", "video_given_up", "video_late", "video_overflow",
                          "audio_given_up", "audio_late", "audio_overflow", "packets_recovered"), 0)
    link.recv = {**zero, "packets_requested": 9, "video_given_up": 2}
    caplog.set_level("DEBUG", logger=media.__name__)
    sink = Sink()
    sub = ls.subscribe(sink)
    assert wait_for(lambda: sink.chunks)
    link.recv = {**zero, "packets_requested": 9, "video_given_up": 3}
    assert wait_for(lambda: "frames lost: {'video_given_up': 1}" in caplog.text)
    assert "'video_given_up': 2}" not in caplog.text
    sub.close()


def test_audio_start_left_to_a_talk_back(cam):
    link, s, _, holder = cam
    ls = LiveStream(lambda: holder["s"], name="held", grace_s=0.3, audio_held=lambda: True)
    sink = Sink()
    sub = ls.subscribe(sink)
    assert wait_for(lambda: len(sink.data) > 20_000)                 # video flows without camera audio
    assert ops.AUDIO_START.code not in link.sent_codes() and ops.START.code in link.sent_codes()
    sub.close()
    ls.close()


def test_stops_after_grace_and_restarts(cam):
    link, _, ls, _ = cam
    sink = Sink()
    sub = ls.subscribe(sink)
    assert wait_for(lambda: sink.chunks)
    sub.close()
    assert wait_for(lambda: ops.STOP.code in link.sent_codes())
    assert ops.AUDIO_STOP.code in link.sent_codes()
    assert wait_for(lambda: ls._thread is None)
    assert not link.camera.streaming
    sink2 = Sink()
    sub2 = ls.subscribe(sink2)
    assert wait_for(lambda: sink2.chunks) and starts(link) == 2
    assert (sink2.data[1] & 0x1F) << 8 | sink2.data[2] == 0           # the restart joins at a keyframe too
    sub2.close()


def test_resubscribe_within_grace_keeps_stream(cam):
    link, _, ls, _ = cam
    a, b = Sink(), Sink()
    sa = ls.subscribe(a)
    assert wait_for(lambda: a.chunks)
    sa.close()
    time.sleep(0.1)                                                      # < grace
    sb = ls.subscribe(b)
    assert wait_for(lambda: b.chunks)
    assert starts(link) == 1 and ops.STOP.code not in link.sent_codes()
    sb.close()


def test_late_subscriber_joins_at_keyframe_and_decodes(cam):
    _, _, ls, _ = cam
    a, b = Sink(), Sink()
    sa = ls.subscribe(a)
    assert wait_for(lambda: len(a.data) > 20_000)
    sb = ls.subscribe(b)
    assert wait_for(lambda: len(b.data) > 50_000)
    assert (b.data[1] & 0x1F) << 8 | b.data[2] == 0
    with av.open(io.BytesIO(b.data), format="mpegts") as c:
        frames = list(c.decode(video=0))
    assert frames and frames[0].key_frame
    sa.close()
    sb.close()


def test_on_running_reports_start_and_stop():
    mod = FakeLinkModule()
    enable_live_view(mod.camera)
    s = LinkSession(mod, "UID", "k3yAuth8", "pw", recv_timeout_ms=20, request_timeout_s=1, keepalive_s=0)
    s.connect()
    seen = []
    ls = LiveStream(lambda: s, name="t", grace_s=0.2, on_running=seen.append)
    sub = ls.subscribe(Sink())
    assert wait_for(lambda: seen == [True]) and ls.running
    sub.close()
    assert wait_for(lambda: seen == [True, False]) and not ls.running
    ls.close()
    s.close()


def test_session_loss_ends_subscribers(cam):
    link, _, ls, _ = cam
    sink = Sink()
    ls.subscribe(sink)
    assert wait_for(lambda: sink.chunks)
    link.kill()
    assert sink.ended.wait(3)
    assert wait_for(lambda: ls._thread is None)
    assert "SessionGone" in ls.stats["last_error"]


def test_no_session_refuses(cam):
    _, _, ls, holder = cam
    holder["s"] = None
    with pytest.raises(SessionGone):
        ls.subscribe(Sink())


def test_close_ends_subscribers(cam):
    _, _, ls, _ = cam
    sink = Sink()
    ls.subscribe(sink)
    assert wait_for(lambda: sink.chunks)
    ls.close()
    assert sink.ended.is_set() and ls._thread is None
    with pytest.raises(SessionGone):
        ls.subscribe(Sink())


def test_snapshot_starts_briefly_and_caches(cam):
    link, _, ls, _ = cam
    jpeg = ls.snapshot()
    assert jpeg[:2] == b"\xff\xd8"
    assert starts(link) == 1
    assert wait_for(lambda: ls._thread is None)                       # stopped again after the grace
    assert ls.snapshot() is jpeg                                         # recent: no restart
    assert starts(link) == 1
    small = ls.snapshot(width=160)
    with av.open(io.BytesIO(small)) as c:
        assert (c.streams.video[0].width, c.streams.video[0].height) == (160, 120)


def test_snapshot_while_streaming_uses_live_keyframe(cam):
    link, _, ls, _ = cam
    sink = Sink()
    sub = ls.subscribe(sink)
    assert wait_for(lambda: ls._keyframe is not None)
    assert ls.snapshot()[:2] == b"\xff\xd8"
    assert starts(link) == 1
    sub.close()


def test_no_keyframe_gives_up(cam, monkeypatch):
    link, _, ls, _ = cam
    ls._first_keyframe_s = 0.3
    link.camera.master = None                     # frames never arrive
    link.camera.key_reply = lambda hello: __import__("test_crypto").fake_camera_reply(hello)[0]
    sink = Sink()
    ls.subscribe(sink)
    assert sink.ended.wait(3)
    assert "no keyframe" in ls.stats["last_error"]


def test_is_keyframe():
    frames = clip()
    assert is_keyframe(frames[0]) and not is_keyframe(frames[1])
    assert sum(map(is_keyframe, frames)) == 3


def test_has_nal():
    assert has_nal(clip()[1])
    assert not any(map(has_nal, [b"", b"\x00\x00\x00\x01", b"\x00\x00\x01\x00\x00\x00\x01\x00"]))


def test_empty_video_frames_are_dropped(cam, monkeypatch):
    # PyAV takes an empty packet as a flush, and muxes a bare start code as a PES that go2rtc's
    # MPEG-TS reader turns into an empty packet (a panic in its H.264 repair).
    link, _, ls, _ = cam
    frames = clip()
    monkeypatch.setattr(fake_camera, "clip", lambda: [frames[0], b"", b"\x00\x00\x00\x01", *frames[1:]])
    sink = Sink()
    sub = ls.subscribe(sink)
    assert wait_for(lambda: ls.stats.get("bad", 0) >= 2 and len(sink.data) > 50_000)
    sub.close()
    assert streams_of(sink.data) == [("audio", "aac"), ("video", "h264")]


def test_grace_default():
    assert media.GRACE_S >= 2
