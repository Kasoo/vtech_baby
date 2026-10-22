"""A pure-Python stand-in for `_vtech_link` (vtech-link's Python module), backed by `FakeCamera`.

The same surface `LinkSession`, `LinkMediaSource` and `LinkUplink` use (docs/vtech-link-design/
75-python-api.md): `connect` returns a `FakeLink` whose requests the camera model answers. Replies the
caller waits for go to its exchange; everything else is a push. Media comes out decrypted, as the real
module delivers it, and the live-view key exchange really runs against the camera model. Tests can make
connects fail, kill a link from the camera's side, and read what the uplink sent.
"""
import queue
import threading
import time
from dataclasses import dataclass

from vtech_lan.crypto import KeyExchange, decrypt_frame
from vtech_lan.media import is_keyframe

from fake_sdk import FakeCamera, FakeFrameReader


class LinkError(Exception):
    def __init__(self, kind, detail="", code=None):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail, self.code = kind, detail, code


CLOSED_LOCAL = ("closed", "local", None)


@dataclass(frozen=True)
class Frame:
    kind: str
    key: bool
    number: int
    seq: int
    arrived: float
    lost_before: int
    data: bytes


class FakeExchange:
    def __init__(self, link, reply):
        self._link, self.reply = link, reply
        self.pages: queue.Queue = queue.Queue()
        self.closed = False

    def next(self, timeout):
        self._link._check()
        try:
            return self.pages.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self.closed = True
        self._link._end_exchange(self)


class FakeMediaReceiver:
    def __init__(self, link):
        self._link = link
        self._reader = FakeFrameReader(link)
        self.started = self.closed = False
        self.lost_next = 0                      # tests: lost_before for the next frame
        self._numbers = {"video": 0, "audio": 0}

    def start(self, timeout=5.0):
        self._link._check()
        self._link.key_exchange()
        self.started = True

    def reset(self, timeout=2.0):
        self._link._check()
        self._link.camera.cleaned = True
        self._link.resets += 1

    def next(self, timeout):
        link, cam = self._link, self._link.camera
        deadline = time.monotonic() + timeout
        while True:
            link._check()
            for kind, read in (("audio", self._reader.audio), ("video", self._reader.video)):
                rc, frame = read()
                if rc > 0:
                    data = decrypt_frame(frame, cam.master)
                    self._numbers[kind] += 1
                    lost, self.lost_next = self.lost_next, 0
                    return Frame(kind, kind == "video" and is_keyframe(data), sum(self._numbers.values()),
                                 self._numbers[kind], time.monotonic(), lost, data)
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.002)

    def stats(self):
        streams = {"frames": 0, "bytes": 0, "given_up": 0, "late": 0}
        return {"video": dict(streams), "audio": dict(streams), "packets_requested": 0,
                "packets_recovered": 0, "packets_duplicate": 0, "packets_stale": 0, "fragments_dropped": 0,
                "gaps_skipped": 0, "fragments_behind": 0, "packets_dropped_test": 0, "video_overflow": 0,
                "audio_overflow": 0, "decrypt_failed_video": 0, "decrypt_failed_audio": 0}

    def close(self):
        self.closed = True


class FakeTalkBack:
    def __init__(self, link, channel):
        self._link, self.channel = link, channel
        self.closed_with: tuple | None = None

    def accept(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while self._link.camera.speaker_ch != self.channel:
            self._link._check()
            if time.monotonic() >= deadline:
                raise LinkError("timeout", code=-20011)
            time.sleep(0.002)

    def send(self, alaw, timeout=None):
        self._link._check()
        if self._link.talk_send_error is not None:
            raise self._link.talk_send_error
        self._link.uplink.append(bytes(alaw))

    def close(self, drain=True, timeout=2.0):
        self.closed_with = (drain, timeout)

    def stats(self):
        return {"frames_sent": len(self._link.uplink)}


class FakeLink:
    def __init__(self, module, uid, peer_hint):
        self.module, self.camera = module, module.camera
        self.uid, self.peer_hint = uid, peer_hint
        self.sent: list[tuple[int, bytes]] = []
        self.pushes: queue.Queue = queue.Queue()
        self.uplink: list[bytes] = []
        self.talks: list[FakeTalkBack] = []
        self.resets = 0
        self.dead_rc = None                     # FakeFrameReader reads this
        self.talk_send_error = None
        self._error = None
        self._exchanges: dict[int, FakeExchange] = {}
        self._lock = threading.Lock()

    # The module's Link surface
    def send(self, type, payload=b"", timeout=5.0):        # noqa: A002 - the module's argument name
        self._check()
        with self._lock:
            self.sent.append((type, bytes(payload)))
        for code, body in self.camera.handle(type, bytes(payload)):
            self._deliver(code, body)

    def request(self, type, payload, reply, timeout=5.0):  # noqa: A002
        ex = self.exchange(type, payload, reply, timeout)
        try:
            page = ex.next(timeout)
        finally:
            ex.close()
        if page is None:
            raise LinkError("timeout", code=-20011)
        return page

    def exchange(self, type, payload, reply, timeout=15.0):  # noqa: A002
        self._check()
        ex = FakeExchange(self, reply)
        with self._lock:
            self._exchanges[reply] = ex
        self.send(type, payload)
        return ex

    def next_push(self, timeout):
        self._check()
        try:
            return self.pushes.get(timeout=timeout)
        except queue.Empty:
            self._check()
            return None

    def media(self):
        return FakeMediaReceiver(self)

    def talkback(self, timeout=10.0):
        self._check()
        self.key_exchange()
        tb = FakeTalkBack(self, len(self.talks) + 1)
        self.talks.append(tb)
        return tb

    def info(self):
        return {"peer": "127.0.0.1:41782", "mode": "lan", "dtls_suite": 0xCCAC,
                "close_reason": self._error.detail if self._error else None}

    def stats(self):
        return {"datagrams_in": len(self.sent), "datagrams_out": len(self.sent)}

    def close(self):
        if self._error is None:
            self._error = LinkError(*CLOSED_LOCAL)
        self.pushes.put(None)

    @property
    def closed(self):
        return self._error

    # Helpers for the fake and for tests
    def key_exchange(self):
        """The 0x820/0x821 exchange against the camera model: sets its master, as the real one does."""
        if self.camera.master is not None:
            return
        if not self.camera.handle(0x820, KeyExchange.new().hello()):
            raise LinkError("timeout", code=-20011)

    def kill(self, kind="closed", detail="remote_close", code=-22):
        """The link ends from the camera's side (or the network)."""
        self._error = LinkError(kind, detail, code)
        self.pushes.put(None)

    def push(self, code, payload):
        self.pushes.put((code, payload))

    def sent_codes(self):
        with self._lock:
            return [c for c, _ in self.sent]

    def _deliver(self, code, body):
        with self._lock:
            ex = self._exchanges.get(code)
        if ex is not None and not ex.closed:
            ex.pages.put(body)
        else:
            self.pushes.put((code, body))

    def _end_exchange(self, ex):
        with self._lock:
            if self._exchanges.get(ex.reply) is ex:
                del self._exchanges[ex.reply]

    def _check(self):
        if self._error is not None:
            raise self._error


class FakeLinkModule:
    """Stands in for the loaded `_vtech_link` module."""

    LinkError = LinkError
    __version__ = "0.1.0+fake"
    SDK_VERSION = 0x040308C8

    def __init__(self, camera=None):
        self.camera = camera or FakeCamera()
        self.connect_error: LinkError | None = None
        self.opened: list[FakeLink] = []
        self.connects: list[dict] = []
        self.log_level = 20

    def connect(self, uid, authkey, password, account="admin", *, connect_timeout=15.0, av_timeout=30.0,
                search_port=32762, peer_hint=None, interface=None):
        self.connects.append({"uid": uid, "authkey": authkey, "account": account, "peer_hint": peer_hint,
                              "connect_timeout": connect_timeout, "av_timeout": av_timeout})
        if len(authkey) != 8:
            raise LinkError("invalid_argument", "authkey must be 8 characters")
        if self.connect_error is not None:
            raise self.connect_error
        link = FakeLink(self, uid, peer_hint)
        self.opened.append(link)
        return link

    def lan_only_stats(self):
        return {"refused_sends": 0, "refused_lookups": 0}

    def next_logs(self, timeout):
        time.sleep(min(timeout, 0.05))
        return []

    def logs_dropped(self):
        return 0

    def set_log_level(self, level):
        self.log_level = level

    def links(self):
        return [link.info() for link in self.opened if link.closed is None]
