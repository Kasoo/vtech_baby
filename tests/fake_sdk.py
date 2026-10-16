"""A fake SDK with the same surface `Session` uses, backed by a tiny camera model.

`FakeSdk.camera` answers requests like the RM5754HD (per docs/vtech-lan-kit/docs/30). Tests can inject
pushes, drop replies, fail connects, or kill the session.
"""
import functools
import queue
import shutil
import struct
import subprocess
import threading
import time

from vtech_lan import ops
from vtech_lan.crypto import encrypt_frame
from vtech_lan.errors import AV_ER_DATA_NOREADY, AV_ER_TIMEOUT


def i32(*w):
    return struct.pack(f"<{len(w)}i", *w)


@functools.cache
def clip() -> list[bytes]:
    """3 s of 320x240 H.264 like the camera's (no B-frames, Annex-B, an IDR every 10 frames)."""
    if not shutil.which("ffmpeg"):
        return []
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10", "-t", "3",
         "-c:v", "libx264", "-bf", "0", "-g", "10", "-bsf:v", "h264_metadata=aud=insert", "-f", "h264", "-"],
        capture_output=True, check=True).stdout
    return [b"\x00\x00\x00\x01\x09" + a for a in raw.split(b"\x00\x00\x00\x01\x09") if a]


def enable_live_view(camera: "FakeCamera") -> None:
    """Answer the 0x820 key exchange and remember the master, so frames can be encrypted."""
    from test_crypto import fake_camera_reply

    def reply(hello):
        r, camera.master = fake_camera_reply(hello)
        return r
    camera.key_reply = reply


class FakeCamera:
    def __init__(self):
        self.night_light = 0
        self.led_off = 0
        self.nv_mode = 0
        self.quality = 2
        self.params = [21, -1, 70, 1800, 2, 5, 50, 1234, 0]
        self.temp_alert = [0, 250, 160]
        self.motion = [50, 1]
        self.sound = 1
        self.notify = 1
        self.parent = 1
        self.model = b"RM5754"
        self.silent: set[int] = set()        # request opcodes to ignore (no reply)
        self.fail_acks: set[int] = set()     # request opcodes to ack as failed
        self.event_pages: list[bytes] = []
        self.key_reply = None                # callable(hello) -> 0x821 payload
        # Live view
        self.master: bytes | None = None
        self.streaming = self.audio_on = False
        self.cleaned = False                 # buffers cleaned since the last STOP
        self.vidx = self.aidx = 0            # next clip frame / audio frames delivered this run
        self.fps = 100                       # 10x real time keeps the tests quick
        self.next_video = 0.0
        # Talk-back
        self.uplink_codec = 0x8A             # 0x32b codec id
        self.speaker_ch: int | None = None   # the channel announced by SPEAKERSTART

    def handle(self, code, p):
        """Return a list of (opcode, payload) replies for request `code`."""
        if code in self.silent:
            return []
        fail = code in self.fail_acks
        w = lambda i: struct.unpack_from("<i", p, 4 * i)[0]          # noqa: E731
        if code == 0x330:
            return [(0x331, self.model.ljust(16, b"\0") + b"vtech".ljust(16, b"\0") + bytes([9, 5, 2, 4]) + bytes(20))]
        if code == 0x733:
            return [(0x734, i32(*self.params))]
        if code == 0x731:
            if not fail:
                self.params[3], self.params[5], self.params[6], self.params[7] = w(1), w(2), w(4), w(5)
            return [(0x732, i32(0 if fail else 1))]
        if code == 0x7A0:
            return [(0x7A1, bytes([self.night_light]))]
        if code == 0x7A2:
            if not fail:
                self.night_light = p[0]
            return [(0x7A3, bytes([0 if fail else 1])), (0x7AC, i32(0, p[0]) + bytes(16))]
        if code == 0x709:
            return [(0x710, i32(0, self.led_off))]
        if code == 0x711:
            self.led_off = w(1)
            return [(0x712, i32(0))]
        if code == 0x713:
            return [(0x714, i32(0, self.nv_mode))]
        if code == 0x715:
            self.nv_mode = w(1)
            return [(0x716, i32(1))]
        if code == 0x322:
            return [(0x323, i32(0) + bytes([self.quality, 0, 0, 0]))]
        if code == 0x320:
            self.quality = p[4]
            return [(0x321, i32(0))]
        if code == 0x74B:
            return [(0x74C, i32(*self.temp_alert))]
        if code == 0x74D:
            self.temp_alert = list(struct.unpack("<3i", p))
            return [(0x74E, i32(1))]
        if code == 0x7A4:
            return [(0x7A5, i32(*self.motion, 0))]
        if code == 0x7A6:
            self.motion = [w(0), w(1)]
            return [(0x7A7, bytes([1]))]
        if code == 0x7B0:
            return [(0x7B1, i32(self.sound, 0, 0, 0))]
        if code == 0x7B2:
            self.sound = w(0)
            return [(0x7B3, i32(1, 0, 0, 0))]
        if code == 0x792:
            return [(0x793, i32(0, self.notify))]
        if code == 0x790:
            self.notify = w(1)
            return []
        if code == 0x75A:
            return [(0x75B, i32(self.parent))]
        if code == 0x788:
            return [(0x789, pg) for pg in self.event_pages]
        if code == 0x735:                    # lullaby: ack, then the track push
            if fail:
                return [(0x736, i32(0, w(1)))]
            self.params[1] = w(1)
            return [(0x736, i32(1, w(1))), (0x7AC, i32(1, w(1)) + bytes(16))]
        if code == 0x32A:
            return [(0x32B, i32(0, self.uplink_codec) + bytes(4))]
        if code == 0x820:
            return [(0x821, self.key_reply(p))] if self.key_reply else []
        if code == 0x1FF:                    # START: after a buffer clean the first frame is a keyframe
            self.streaming = True
            if self.cleaned:
                self.vidx = 0
            self.aidx, self.next_video = 0, 0.0
        elif code == 0x2FF:
            self.streaming, self.cleaned = False, False
        elif code == 0x300:
            self.audio_on = True
        elif code == 0x301:
            self.audio_on = False
        elif code == 0x350:
            self.speaker_ch = w(0)
        elif code == 0x351:
            self.speaker_ch = None
        return []


class FakeSdk:
    def __init__(self, camera: FakeCamera | None = None):
        self.camera = camera or FakeCamera()
        self.connect_rc = 0
        self.client_start_rc = 0
        self.sent: list[tuple[int, bytes]] = []
        self.calls: list[str] = []
        self.inbox: queue.Queue = queue.Queue()
        self.dead_rc: int | None = None      # when set, recv returns this code
        self.send_rc = 0
        self.mode = 2                        # IOTC_Session_Check Mode: 2 = LAN
        self.send_rc_for: dict[int, int] = {}   # per-opcode send rc override
        self.serv_start_rc = 4               # avServStart3: the AV index, or an error
        self.send_audio_rcs: list[int] = []  # avSendAudioData rcs to return next (then 0)
        self.uplink: list[tuple[bytes, bytes]] = []     # (frame, frameinfo) sent
        self._lock = threading.Lock()

    # Session-facing surface
    def get_session_id(self):
        self.calls.append("get_session_id")
        return 7

    def connect(self, uid, sid, authkey, timeout_s):
        self.calls.append("connect")
        return sid if self.connect_rc >= 0 else self.connect_rc

    def client_start(self, sid, account, password, timeout_s):
        self.calls.append("client_start")
        return 3 if self.client_start_rc >= 0 else self.client_start_rc

    def client_stop(self, av):
        self.calls.append(f"client_stop:{av}")

    def session_close(self, sid):
        self.calls.append(f"session_close:{sid}")

    def session_mode(self, sid):
        return self.mode

    def send_ioctrl(self, av, code, payload):
        with self._lock:
            self.sent.append((code, bytes(payload)))
        if self.send_rc < 0:
            return self.send_rc
        if code in self.send_rc_for:
            return self.send_rc_for[code]
        for reply in self.camera.handle(code, payload):
            self.inbox.put(reply)
        return 0

    def recv_ioctrl(self, av, timeout_ms):
        if self.dead_rc is not None:
            return self.dead_rc, 0, b""
        try:
            code, payload = self.inbox.get(timeout=timeout_ms / 1000)
        except queue.Empty:
            if self.dead_rc is not None:
                return self.dead_rc, 0, b""
            return AV_ER_TIMEOUT, 0, b""
        return len(payload), code, payload

    # Media surface (LiveStream)
    def clean_media_buffers(self, av):
        self.calls.append("clean_media_buffers")
        self.camera.cleaned = True

    def frame_reader(self, av):
        return FakeFrameReader(self)

    # Talk-back surface (TalkBack)
    def free_channel(self, sid):
        self.calls.append("free_channel")
        return 1

    def serv_start(self, sid, ch, timeout_s):
        self.calls.append(f"serv_start:{ch}")
        return self.serv_start_rc

    def send_audio(self, idx, frame, info):
        with self._lock:
            self.uplink.append((bytes(frame), bytes(info)))
            return self.send_audio_rcs.pop(0) if self.send_audio_rcs else 0

    def serv_reset(self, idx):
        self.calls.append(f"serv_reset:{idx}")
        return 0

    def serv_stop(self, idx):
        self.calls.append(f"serv_stop:{idx}")

    def channel_off(self, sid, ch):
        self.calls.append(f"channel_off:{ch}")

    def serv_exit(self, sid, ch):
        self.calls.append(f"serv_exit:{ch}")

    # Test helpers
    def push(self, code, payload):
        self.inbox.put((code, payload))

    def sent_codes(self):
        with self._lock:
            return [c for c, _ in self.sent]


class FakeFrameReader:
    """Encrypted clip frames while the camera streams, at `camera.fps`; 2.5 A-law frames per video frame."""

    def __init__(self, sdk: FakeSdk):
        self.sdk, self.cam = sdk, sdk.camera

    def video(self):
        cam = self.cam
        if self.sdk.dead_rc is not None:
            return self.sdk.dead_rc, b""
        now = time.monotonic()
        if not cam.streaming or cam.master is None or now < cam.next_video:
            return AV_ER_DATA_NOREADY, b""
        cam.next_video = now + 1 / cam.fps
        frames = clip()
        au = frames[cam.vidx % len(frames)]
        cam.vidx += 1
        return 1, encrypt_frame(au, cam.master)

    def audio(self):
        cam = self.cam
        if self.sdk.dead_rc is not None:
            return self.sdk.dead_rc, b""
        if not (cam.streaming and cam.audio_on and cam.master) or cam.aidx >= cam.vidx * 2.5:
            return AV_ER_DATA_NOREADY, b""
        cam.aidx += 1
        return 1, encrypt_frame(b"\xD5" * 320, cam.master)


class ThreadWorker:
    """Runs the SDK worker (`vtech_lan/worker.py`) on a thread instead of a process, over the same
    socket pair and messages, with `sdk` (usually a FakeSdk). `worker` is the live Worker object."""

    pid = None

    def __init__(self, child, listen, sdk):
        from custom_components.vtech_baby.vtech_lan.ipc import Channel
        from custom_components.vtech_baby.vtech_lan.worker import Worker
        self._sock = child.dup()
        self.worker = Worker(Channel(self._sock), lambda _libdir: sdk, listen)
        self._thread = threading.Thread(target=self.worker.run, name="vtech-thread-worker", daemon=True)
        self._thread.start()

    def wait(self, timeout=None):
        self._thread.join(timeout)
        return None if self._thread.is_alive() else 0

    def kill(self):
        """Like the process dying: both directions of its socket end at once."""
        import socket
        try:
            self._sock.shutdown(socket.SHUT_RDWR)       # wakes the worker's blocked recv (close alone doesn't)
        except OSError:
            pass
        self._sock.close()


def thread_client(sdk, **kw):
    """A WorkerClient whose worker runs on a thread with `sdk`."""
    from custom_components.vtech_baby.vtech_lan.worker_client import WorkerClient
    return WorkerClient(spawn=lambda child, listen: ThreadWorker(child, listen, sdk), **kw)

