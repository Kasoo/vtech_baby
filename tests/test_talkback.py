"""Talk-back over the fake link (call order, stop sequence, failures), and media decoding with PyAV."""
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

from vtech_lan.errors import MediaError, ProtocolError, SessionGone
from vtech_lan.session import LinkSession
from vtech_lan.talkback import TalkBack

from fake_camera import enable_live_view
from fake_link import FakeLinkModule

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
    mod = FakeLinkModule()
    enable_live_view(mod.camera)
    s = LinkSession(mod, "UID", "k3yAuth8", "pw", recv_timeout_ms=20, request_timeout_s=1, keepalive_s=0)
    s.connect()
    yield mod, s
    s.close()


def talk(s, media=None, **kw):
    return TalkBack(lambda: s, name="test", open_media=lambda _url: iter(media or FRAMES), **kw)


def talk_codes(mod):
    """The IOCtrl opcodes sent after connecting (DEVINFO is setup)."""
    return [c for c in mod.opened[0].sent_codes() if c != 0x330]


def test_resumes_camera_audio_when_live_view_wants_it(cam):
    mod, s = cam
    ended = Ended()
    talk(s, audio_wanted=lambda: True).start("http://media/x", ended)
    ended.wait()
    assert talk_codes(mod)[-2:] == [0x351, 0x300]


def test_bad_media_never_touches_the_camera(cam):
    mod, s = cam

    def bad(_url):
        raise MediaError("can't open the media")
        yield                                                       # noqa: unreachable - a generator

    with pytest.raises(MediaError):
        TalkBack(lambda: s, open_media=bad).start("http://media/x")
    with pytest.raises(MediaError, match="no audio"):
        TalkBack(lambda: s, open_media=lambda _u: iter([])).start("http://media/x")
    assert talk_codes(mod) == [] and mod.opened[0].uplink == []


def test_not_connected(cam):
    with pytest.raises(SessionGone):
        TalkBack(lambda: None, open_media=lambda _u: iter(FRAMES)).start("http://media/x")


def test_unsupported_uplink_format_is_refused(cam):
    mod, s = cam
    mod.camera.uplink_codec = 0x88
    ended = Ended()
    with pytest.raises(ProtocolError, match="uplink format"):
        talk(s).start("http://media/x", ended)
    assert talk_codes(mod) == [0x32A] and not ended.event.is_set()


def test_stop_ends_an_endless_stream(cam):
    mod, s = cam
    closed = threading.Event()

    def media(_url):
        try:
            yield from endless()
        finally:
            closed.set()

    ended = Ended()
    t = TalkBack(lambda: s, open_media=media)
    t.start("http://media/x", ended)
    time.sleep(0.05)
    assert t.running
    t.stop()
    assert ended.wait() is None and closed.is_set() and not t.running
    assert mod.opened[0].talks[0].closed_with is not None


def test_start_replaces_the_one_playing(cam):
    mod, s = cam
    t = TalkBack(lambda: s, open_media=lambda _u: endless())
    first, second = Ended(), Ended()
    t.start("http://media/1", first)
    t.start("http://media/2", second)
    assert first.wait() is None and not second.event.is_set() and t.running
    t.stop()
    assert second.wait() is None
    assert talk_codes(mod).count(0x350) == 2 and talk_codes(mod).count(0x351) == 2


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
    mod, s = cam
    t = TalkBack(lambda: s, open_media=lambda _u: endless())
    assert t.hold_audio_start() is False                           # nothing playing: the live view sends it
    ended = Ended()
    t.start("http://media/x", ended)
    assert t.hold_audio_start() is True                            # held: the live view must not send it
    assert 0x300 not in talk_codes(mod)
    t.stop()
    ended.wait()
    assert talk_codes(mod)[-2:] == [0x351, 0x300]                  # sent at the end, though audio_wanted is False
    assert t.hold_audio_start() is False
