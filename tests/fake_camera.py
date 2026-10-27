"""A tiny camera model that answers requests like the RM5754HD (per docs/vtech-lan-kit/docs/30), and
the test clip it streams. `fake_link.py` puts it behind the `_vtech_link` surface."""
import functools
import shutil
import struct
import subprocess


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
