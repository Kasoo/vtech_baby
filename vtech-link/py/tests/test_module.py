"""End-to-end tests of the `_vtech_link` module against the simulated camera (`sim-camera`).

Run with `py/tests/run.sh`, which builds the module and the simulator first. `VTECH_LINK_MODULE` and
`VTECH_SIM` override where they are found.
"""

import importlib.machinery
import importlib.util
import os
import struct
import subprocess
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "..", "..", "target", "debug")
MODULE = os.environ.get("VTECH_LINK_MODULE", os.path.join(TARGET, "lib_vtech_link.so"))
SIM = os.environ.get("VTECH_SIM", os.path.join(TARGET, "sim-camera"))

UID = "SIMCAM0123456789ABCD"
AUTHKEY = "k3yAuth8"
PASSWORD = "simpass"
OP_DEVINFO, OP_DEVINFO_REPLY = 0x330, 0x331
OP_START, OP_STOP = 0x1FF, 0x2FF
OP_SPEAKER_START, OP_SPEAKER_STOP = 0x350, 0x351


def load():
    loader = importlib.machinery.ExtensionFileLoader("_vtech_link", MODULE)
    spec = importlib.util.spec_from_file_location("_vtech_link", MODULE, loader=loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


vl = load()


class Sim:
    """A `sim-camera` process."""

    def __init__(self, *args):
        self.proc = subprocess.Popen(
            [SIM, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        self.port = int(self.proc.stdout.readline())

    def command(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def stats(self):
        self.command("stats")
        return {k: int(v) for k, v in (w.split("=") for w in self.proc.stdout.readline().split())}

    def wait_for(self, key, value, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.stats()[key] == value:
                return True
            time.sleep(0.02)
        return False

    def stop(self):
        self.proc.stdin.close()
        self.proc.wait(5)
        self.proc.stdout.close()

    def connect(self, **kw):
        kw.setdefault("connect_timeout", 3.0)
        kw.setdefault("av_timeout", 5.0)
        return vl.connect(
            kw.pop("uid", UID),
            kw.pop("authkey", AUTHKEY),
            kw.pop("password", PASSWORD),
            search_port=self.port,
            peer_hint="127.0.0.1",
            interface="lo",
            **kw,
        )


class SimTest(unittest.TestCase):
    args = ()

    def setUp(self):
        self.sim = Sim(*self.args)
        self.addCleanup(self.sim.stop)

    def link(self, **kw):
        link = self.sim.connect(**kw)
        self.addCleanup(link.close)
        return link

    def assertLinkError(self, ctx, kind, detail="", code=None):
        e = ctx.exception
        self.assertIsInstance(e, vl.LinkError)
        self.assertEqual((e.kind, e.detail, e.code), (kind, detail, code))


class ModuleTest(unittest.TestCase):
    def test_attributes(self):
        self.assertRegex(vl.__version__, r"^\d+\.\d+\.\d+")
        self.assertEqual(vl.SDK_VERSION, 0x040308C8)
        self.assertTrue(issubclass(vl.LinkError, Exception))
        self.assertEqual(vl.lan_only_stats()["refused_lookups"], 0)

    def test_invalid_arguments(self):
        cases = [
            dict(authkey="short"),
            dict(peer_hint="camera.local"),
            dict(connect_timeout=-1.0),
            dict(connect_timeout=float("nan")),
        ]
        for kw in cases:
            with self.subTest(kw=kw), self.assertRaises(vl.LinkError) as ctx:
                vl.connect(UID, kw.pop("authkey", AUTHKEY), PASSWORD, **kw)
            self.assertEqual(ctx.exception.kind, "invalid_argument")

    def test_panic_becomes_exception(self):
        with self.assertRaises(BaseException) as ctx:
            vl._panic()
        self.assertEqual(type(ctx.exception).__name__, "PanicException")


class ConnectTest(SimTest):
    def test_requests_info_close(self):
        link = self.link()
        info = link.info()
        self.assertEqual(info["peer"], f"127.0.0.1:{self.sim.port}")
        self.assertEqual(info["mode"], "lan")
        self.assertEqual(info["dtls_suite"], 0xCCAC)
        self.assertIsNone(info["close_reason"])
        self.assertLessEqual(info["up_since"], time.monotonic())
        self.assertIn(info["local_port"], [i["local_port"] for i in vl.links()])

        devinfo = link.request(OP_DEVINFO, bytes(4), OP_DEVINFO_REPLY)
        self.assertEqual(devinfo[:6], b"RM5754")
        self.assertEqual(link.request(0x5010, b"hello", 0x5011, timeout=2.0), b"hello")
        link.send(0x5020, b"x")

        stats = link.stats()
        self.assertGreater(stats["datagrams_in"], 0)
        self.assertEqual(stats["control"]["frames_sent"], 3)
        self.assertEqual(len(stats["control"]["ack_latency"]), len(stats["ack_latency_buckets_ms"]) + 1)

        self.assertIsNone(link.closed)
        link.close()
        link.close()
        self.assertEqual(link.closed.kind, "closed")
        self.assertEqual(link.closed.detail, "local")
        self.assertEqual(link.info()["close_reason"], "local")
        with self.assertRaises(vl.LinkError) as ctx:
            link.send(0x5020)
        self.assertLinkError(ctx, "closed", "local")

    def test_exchange_pages(self):
        link = self.link()
        ex = link.exchange(0x788, bytes(8), 0x789, timeout=5.0)
        for i in range(4):
            self.assertEqual(ex.next(1.0), bytes([i]) * (100 + i))
        self.assertIsNone(ex.next(0.2))
        ex.close()
        # With the exchange closed, a late page is a push.
        self.sim.command("push 789 6c617465")
        self.assertEqual(link.next_push(1.0), (0x789, b"late"))

    def test_pushes_and_remote_close(self):
        link = self.link()
        self.assertIsNone(link.next_push(0.1))
        for i in range(3):
            self.sim.command(f"push 1fff {i:02x}")
        self.assertEqual([link.next_push(1.0) for _ in range(3)], [(0x1FFF, bytes([i])) for i in range(3)])
        self.sim.command("close")
        with self.assertRaises(vl.LinkError) as ctx:
            link.next_push(3.0)
        self.assertLinkError(ctx, "closed", "remote_close", -22)
        self.assertEqual(link.closed.code, -22)

    def test_waits_release_the_gil(self):
        link = self.link()
        ticks = 0
        stop = threading.Event()

        def count():
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                time.sleep(0.001)

        t = threading.Thread(target=count)
        t.start()
        link.next_push(0.3)
        stop.set()
        t.join()
        self.assertGreater(ticks, 50)

    def test_logs(self):
        vl.set_log_level(10)
        try:
            vl.next_logs(0)
            link = self.link()
            link.close()
            records = []
            deadline = time.monotonic() + 2
            while not any("link closed" in r[2] for r in records) and time.monotonic() < deadline:
                records += vl.next_logs(0.5)
        finally:
            vl.set_log_level(20)
        messages = [m for _, _, m in records]
        self.assertTrue(any(m.startswith("link up") for m in messages), messages)
        level, target, _ = next(r for r in records if r[2].startswith("link up"))
        self.assertEqual(level, 20)
        self.assertTrue(target.startswith("vtech_link_core"))
        self.assertEqual(vl.next_logs(0.05), [])


class MediaTest(SimTest):
    def test_frames(self):
        link = self.link()
        link.request(OP_DEVINFO, bytes(4), OP_DEVINFO_REPLY)  # the camera streams only after DEVINFO
        rx = link.media()
        rx.start()
        rx.reset()
        link.send(OP_START, bytes(8))
        video, audio = [], []
        deadline = time.monotonic() + 5
        while len(video) < 20 and time.monotonic() < deadline:
            f = rx.next(0.5)
            if f is not None:
                (video if f.kind == "video" else audio).append(f)
        link.send(OP_STOP, bytes(8))
        rx.close()
        self.assertEqual(len(video), 20)
        self.assertTrue(audio)
        self.assertTrue(video[0].key)
        self.assertEqual([f.number for f in video], sorted(f.number for f in video))
        now = time.monotonic()
        for f in video + audio:
            self.assertIsInstance(f.data, bytes)
            self.assertEqual(f.lost_before, 0)
            self.assertTrue(now - 6 < f.arrived <= now, (f.arrived, now))
        tag, _gen, index, length = struct.unpack_from("<4sIII", video[0].data)
        self.assertEqual(index % 15, 0)
        self.assertEqual(length, len(video[0].data))
        self.assertRegex(repr(video[0]), r"^Frame\(video #\d+ key, \d+ bytes, lost_before=0\)$")
        stats = rx.stats()
        self.assertGreaterEqual(stats["video"]["frames"], 20)
        self.assertEqual(stats["decrypt_failed_video"] + stats["decrypt_failed_audio"], 0)


class TalkBackTest(SimTest):
    def test_send_and_drain(self):
        link = self.link()
        tb = link.talkback()
        self.assertGreaterEqual(tb.channel, 1)
        link.send(OP_SPEAKER_START, struct.pack("<I", tb.channel))
        tb.accept(3.0)
        t0 = time.monotonic()
        for i in range(25):
            tb.send(bytes([i]) * 160, timeout=1.0)
        tb.close(drain=True, timeout=3.0)
        took = time.monotonic() - t0
        self.assertTrue(self.sim.wait_for("talk_frames", 25))
        self.assertGreaterEqual(took, 0.4)
        self.assertEqual(self.sim.stats()["talk_undecryptable"], 0)
        self.assertEqual(tb.stats()["frames_sent"], 25)
        link.send(OP_SPEAKER_STOP, bytes(4))


class RefusedAuthKeyTest(SimTest):
    args = ("--refuse-authkey",)

    def test_auth_key(self):
        with self.assertRaises(vl.LinkError) as ctx:
            self.sim.connect()
        self.assertLinkError(ctx, "iotc_rejected", "auth_key", -68)


class WrongPasswordTest(SimTest):
    args = ("--wrong-password-alert",)

    def test_alert(self):
        with self.assertRaises(vl.LinkError) as ctx:
            self.sim.connect(password="wrong")
        self.assertLinkError(ctx, "av_auth_failed", "dtls_alert:51", -20009)


class UnreachableTest(SimTest):
    def test_no_reply(self):
        self.sim.command("kill")
        t0 = time.monotonic()
        with self.assertRaises(vl.LinkError) as ctx:
            self.sim.connect(connect_timeout=1.0)
        self.assertLinkError(ctx, "unreachable", "", -13)
        self.assertLess(time.monotonic() - t0, 3.0)


if __name__ == "__main__":
    unittest.main()
