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

from camera_crypto import KeyExchange, decrypt_frame, encrypt_frame
from vtech_lan.media import is_keyframe

import fake_camera
from fake_camera import FakeCamera


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
            page = self.pages.get(timeout=timeout)
        except queue.Empty:
            return None
        if page is None:                        # woken because the link ended
            self._link._check()
        return page

    def close(self):
        self.closed = True
        self._link._end_exchange(self)


class FakeMediaReceiver:
    def __init__(self, link):
        self._link = link
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
            for kind, read in (("audio", self._audio), ("video", self._video)):
                frame = read()
                if frame is not None:
                    data = decrypt_frame(frame, cam.master)
                    self._numbers[kind] += 1
                    lost, self.lost_next = self.lost_next, 0
                    return Frame(kind, kind == "video" and is_keyframe(data), sum(self._numbers.values()),
                                 self._numbers[kind], time.monotonic(), lost, data)
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.002)

    def _video(self):
        """The next encrypted clip frame while the camera streams, at `camera.fps`."""
        cam = self._link.camera
        now = time.monotonic()
        if not cam.streaming or cam.master is None or now < cam.next_video:
            return None
        cam.next_video = now + 1 / cam.fps
        frames = fake_camera.clip()
        au = frames[cam.vidx % len(frames)]
        cam.vidx += 1
        return encrypt_frame(au, cam.master)

    def _audio(self):
        """An encrypted A-law frame, 2.5 per video frame."""
        cam = self._link.camera
        if not (cam.streaming and cam.audio_on and cam.master) or cam.aidx >= cam.vidx * 2.5:
            return None
        cam.aidx += 1
        return encrypt_frame(b"\xD5" * 320, cam.master)

    def stats(self):
        """The module's receive counters, with the flat `link.recv` names (as LiveStream reports them)
        applied on top."""
        streams = {"frames": 0, "bytes": 0, "given_up": 0, "late": 0}
        out = {"video": dict(streams), "audio": dict(streams), "packets_requested": 0,
               "packets_recovered": 0, "packets_duplicate": 0, "packets_stale": 0, "fragments_dropped": 0,
               "gaps_skipped": 0, "fragments_behind": 0, "packets_dropped_test": 0, "video_overflow": 0,
               "audio_overflow": 0, "decrypt_failed_video": 0, "decrypt_failed_audio": 0}
        for name, value in self._link.recv.items():
            kind, _, field = name.partition("_")
            if kind in ("video", "audio") and field in streams:
                out[kind][field] = value
            else:
                out[name] = value
        return out

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
        time.sleep(self._link.talk_pace_s)

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
        self.recv: dict[str, int] = {}          # tests: receive counters to report
        self.talk_send_error = None
        self.talk_pace_s = 0.02                 # the module sends one 20 ms frame per 20 ms
        self._error = None
        self._exchanges: dict[int, list[FakeExchange]] = {}   # per reply opcode, oldest first
        self.close_calls = 0
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
            self._exchanges.setdefault(reply, []).append(ex)
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
        self.close_calls += 1
        if self._error is None:
            self._error = LinkError(*CLOSED_LOCAL)
        self._wake()

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
        self._wake()

    def push(self, code, payload):
        self.pushes.put((code, payload))

    def sent_codes(self):
        with self._lock:
            return [c for c, _ in self.sent]

    def _wake(self):
        """Wake the push reader and every waiting exchange so they see the error."""
        self.pushes.put(None)
        with self._lock:
            waiting = [ex for exs in self._exchanges.values() for ex in exs]
        for ex in waiting:
            ex.pages.put(None)

    def _deliver(self, code, body):
        with self._lock:
            ex = next((e for e in self._exchanges.get(code, ()) if not e.closed), None)
        if ex is not None:
            ex.pages.put(body)
        else:
            self.pushes.put((code, body))

    def _end_exchange(self, ex):
        with self._lock:
            exs = self._exchanges.get(ex.reply, [])
            if ex in exs:
                exs.remove(ex)

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


class ThreadWorker:
    """Runs the worker (`vtech_lan/worker.py`) on a thread instead of a process, over the same socket
    pair and messages, loading `link_loader(path)`. `worker` is the live Worker object."""

    pid = None

    def __init__(self, child, listen, link_loader):
        from custom_components.vtech_baby.vtech_lan.ipc import Channel
        from custom_components.vtech_baby.vtech_lan.worker import Worker
        self._sock = child.dup()
        self.worker = Worker(Channel(self._sock), listen, link_loader=link_loader)
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


def thread_client(link_mod, **kw):
    """A WorkerClient whose worker runs on a thread and loads `link_mod` as the vtech-link module."""
    from custom_components.vtech_baby.vtech_lan.worker_client import WorkerClient
    return WorkerClient(spawn=lambda child, listen: ThreadWorker(child, listen, lambda _path: link_mod), **kw)
