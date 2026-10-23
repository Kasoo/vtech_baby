"""Talk-back over the fake SDK (call order, framing, stop sequence, failures), and media decoding with PyAV."""
import contextlib
import functools
import http.server
import io
import math
import struct
import threading
import time
import wave

import pytest

from vtech_lan import ops
from vtech_lan.crypto import decrypt_frame
from vtech_lan.errors import (
    AV_ER_EXCEED_MAX_SIZE,
    AV_ER_SESSION_CLOSE_BY_REMOTE,
    AV_ER_TIMEOUT,
    MediaError,
    ProtocolError,
    SessionGone,
    VtechError,
)
from vtech_lan.session import Session
from vtech_lan.talkback import CODEC_ALAW, FLAGS, TalkBack, frame_info

from fake_sdk import FakeSdk, enable_live_view

FRAMES = [bytes([i]) * 160 for i in range(5)]


class Ended:
    def __init__(self):
        self.event, self.err = threading.Event(), "not ended"

    def __call__(self, err):
        self.err = err
        self.event.set()

    def wait(self, timeout=5.0):
        assert self.event.wait(timeout), "talk-back did not end"
        return self.err


def endless():
    while True:
        yield b"\x55" * 160


@pytest.fixture
def cam():
    sdk = FakeSdk()
    enable_live_view(sdk.camera)
    s = Session(sdk, "UID", "authkey", "pw", recv_timeout_ms=20, request_timeout_s=1, keepalive_s=0)
    s.connect()
    yield sdk, s
    s.close()


def talk(sdk, s, media=None, **kw):
    return TalkBack(lambda: s, name="test", open_media=lambda _url: iter(media or FRAMES),
                    frame_s=0.001, **kw)


def talk_codes(sdk):
    """The IOCtrl opcodes sent after connecting (DEVINFO and 0x0ff are setup)."""
    return [c for c in sdk.sent_codes() if c not in (0x0FF, 0x330)]


def test_plays_frames_in_order_then_stops(cam):
    sdk, s = cam
    ended = Ended()
    talk(sdk, s).start("http://media/x", ended)
    assert ended.wait() is None
    assert [decrypt_frame(f, sdk.camera.master) for f, _ in sdk.uplink] == FRAMES
    assert all(len(f) == 220 for f, _ in sdk.uplink)
    assert talk_codes(sdk) == [0x32A, 0x820, 0x301, 0x350, 0x351]      # no 0x300: no live view to resume
    talk_calls = [c for c in sdk.calls if c.split(":")[0] in
                  ("free_channel", "serv_start", "serv_stop", "channel_off", "serv_exit")]
    assert talk_calls == ["free_channel", "serv_start:1", "serv_stop:4", "channel_off:1", "serv_exit:1"]
    assert sdk.camera.speaker_ch is None


def test_frame_info_layout():
    info = frame_info(0x1_2345_6789)
    assert len(info) == 16
    assert struct.unpack_from("<H", info, 0)[0] == CODEC_ALAW and info[2] == FLAGS
    assert struct.unpack_from("<I", info, 12)[0] == 0x2345_6789
    assert info[3:12] == bytes(9)


def test_resumes_camera_audio_when_live_view_wants_it(cam):
    sdk, s = cam
    ended = Ended()
    talk(sdk, s, audio_wanted=lambda: True).start("http://media/x", ended)
    ended.wait()
    assert talk_codes(sdk)[-2:] == [0x351, 0x300]


def test_bad_media_never_touches_the_camera(cam):
    sdk, s = cam

    def bad(_url):
        raise MediaError("can't open the media")
        yield                                                       # noqa: unreachable - a generator

    with pytest.raises(MediaError):
        TalkBack(lambda: s, open_media=bad).start("http://media/x")
    with pytest.raises(MediaError, match="no audio"):
        TalkBack(lambda: s, open_media=lambda _u: iter([])).start("http://media/x")
    assert talk_codes(sdk) == [] and sdk.uplink == []


def test_not_connected(cam):
    sdk, _s = cam
    with pytest.raises(SessionGone):
        TalkBack(lambda: None, open_media=lambda _u: iter(FRAMES)).start("http://media/x")


def test_unsupported_uplink_format_is_refused(cam):
    sdk, s = cam
    sdk.camera.uplink_codec = 0x88
    ended = Ended()
    with pytest.raises(ProtocolError, match="uplink format"):
        talk(sdk, s).start("http://media/x", ended)
    assert talk_codes(sdk) == [0x32A] and not ended.event.is_set()


def test_camera_not_connecting_runs_the_stop_sequence(cam):
    sdk, s = cam
    sdk.serv_start_rc = AV_ER_TIMEOUT
    ended = Ended()
    with pytest.raises(VtechError, match="avServStart3"):
        talk(sdk, s).start("http://media/x", ended)
    assert not ended.event.is_set()                                 # a failed start is raised, not ended
    assert "channel_off:1" in sdk.calls and "serv_exit:1" in sdk.calls
    assert not any(c.startswith("serv_stop") for c in sdk.calls)
    assert talk_codes(sdk)[-1] == 0x351 and sdk.camera.speaker_ch is None


def test_full_send_buffer_is_reset_and_playing_continues(cam):
    sdk, s = cam
    sdk.send_audio_rcs = [0, AV_ER_EXCEED_MAX_SIZE]
    ended = Ended()
    talk(sdk, s).start("http://media/x", ended)
    assert ended.wait() is None
    assert "serv_reset:4" in sdk.calls and len(sdk.uplink) == len(FRAMES)


def test_lost_session_ends_with_the_error(cam):
    sdk, s = cam
    sdk.send_audio_rcs = [0, 0, AV_ER_SESSION_CLOSE_BY_REMOTE]
    ended = Ended()
    talk(sdk, s, media=endless()).start("http://media/x", ended)
    assert isinstance(ended.wait(), SessionGone)
    assert "serv_stop:4" in sdk.calls and "serv_exit:1" in sdk.calls


def test_stop_ends_an_endless_stream(cam):
    sdk, s = cam
    closed = threading.Event()

    def media(_url):
        try:
            yield from endless()
        finally:
            closed.set()

    ended = Ended()
    t = TalkBack(lambda: s, open_media=media, frame_s=0.005)
    t.start("http://media/x", ended)
    time.sleep(0.05)
    assert t.running
    t.stop()
    assert ended.wait() is None and closed.is_set() and not t.running
    assert "serv_exit:1" in sdk.calls


def test_start_replaces_the_one_playing(cam):
    sdk, s = cam
    t = TalkBack(lambda: s, open_media=lambda _u: endless(), frame_s=0.005)
    first, second = Ended(), Ended()
    t.start("http://media/1", first)
    t.start("http://media/2", second)
    assert first.wait() is None and not second.event.is_set() and t.running
    t.stop()
    assert second.wait() is None
    assert talk_codes(sdk).count(0x350) == 2 and talk_codes(sdk).count(0x351) == 2


def test_paces_in_real_time(cam):
    sdk, s = cam
    ended = Ended()
    t0 = time.monotonic()
    TalkBack(lambda: s, open_media=lambda _u: iter(FRAMES * 4), frame_s=0.01).start("http://m/x", ended)
    ended.wait()
    assert time.monotonic() - t0 >= 0.18                            # 20 frames at 10 ms


# ── Media decoding (PyAV) ─────────────────────────────────────────────────────
def wav(seconds=1.0, rate=44100, channels=2) -> bytes:
    n = int(seconds * rate)
    tone = [int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(n)]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{n * channels}h", *(v for v in tone for _ in range(channels))))
    return buf.getvalue()


@contextlib.contextmanager
def serve(files: dict[str, bytes]):
    """A loopback HTTP server for `files`; yields its base URL."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                           # noqa: N802
            body = files.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@functools.cache
def _av():
    return pytest.importorskip("av")


def test_decodes_a_url_to_alaw_frames(socket_enabled):
    _av()
    from vtech_lan.audio import alaw_frames

    with serve({"/tone.wav": wav(1.0)}) as base:
        frames = list(alaw_frames(f"{base}/tone.wav"))
    assert all(len(f) == 160 for f in frames)
    assert 49 <= len(frames) <= 51                                  # 1 s of 20 ms frames
    assert len(set(b"".join(frames))) > 20                          # a tone, not silence


def test_bad_media_urls(socket_enabled):
    _av()
    from vtech_lan.audio import alaw_frames

    for url in ("file:///etc/passwd", "/etc/passwd", "rtsp://camera/stream"):
        with pytest.raises(MediaError, match="only http"):
            next(alaw_frames(url))
    with serve({"/text": b"not audio at all " * 100}) as base:
        with pytest.raises(MediaError):
            next(alaw_frames(f"{base}/text"))
        with pytest.raises(MediaError):
            next(alaw_frames(f"{base}/missing.wav"))


def test_closing_the_frames_closes_the_media(socket_enabled):
    _av()
    from vtech_lan.audio import alaw_frames

    with serve({"/long.wav": wav(30.0, rate=8000, channels=1)}) as base:
        frames = alaw_frames(f"{base}/long.wav")
        assert len(next(frames)) == 160
        frames.close()


def test_a_live_view_starting_mid_talk_leaves_audiostart_to_talk_back(cam):
    sdk, s = cam
    t = TalkBack(lambda: s, open_media=lambda _u: endless(), frame_s=0.005)
    assert t.hold_audio_start() is False                           # nothing playing: the live view sends it
    ended = Ended()
    t.start("http://media/x", ended)
    assert t.hold_audio_start() is True                            # held: the live view must not send it
    assert 0x300 not in talk_codes(sdk)
    t.stop()
    ended.wait()
    assert talk_codes(sdk)[-2:] == [0x351, 0x300]                  # sent at the end, though audio_wanted is False
    assert t.hold_audio_start() is False
