"""A fake camera for our libAVAPIs.so, over the stand-in IOTC in fake_iotc.c. Run by test_av_peer.py in
a process of its own (the stand-in shares IOTC's soname):

    av_peer.py LIB_DIR SCENARIO

The packet layouts follow the stock library's DTLS plaintext (kit: av/20-session.md, av/40-ioctrl.md,
av/50-media-recv.md, av/90-fifos.md). Exits non-zero, with the reason, when a scenario fails.
"""
import ctypes as C
import hashlib
import os
import pathlib
import struct
import sys
import threading
import time

ACCOUNT, PASSWORD = b"admin", b"secret-pw"
CAMERA_VERSION = 0x0b
W = 0x101                                   # account/password field width when the peer version >= 9
OPCODE_SUPPORT = bytes.fromhex("04000000fb071f00000000000000000000000300")

AV_ER_INVALID_ARG = -20000
AV_ER_BUFPARA_MAXSIZE_INSUFF = -20001
AV_ER_EXCEED_MAX_SIZE = -20006
AV_ER_WRONG_VIEWACCorPWD = -20009
AV_ER_TIMEOUT = -20011
AV_ER_DATA_NOREADY = -20012
AV_ER_LOSED_THIS_FRAME = -20014
AV_ER_SESSION_CLOSE_BY_REMOTE = -20015
AV_ER_REMOTE_TIMEOUT_DISCONNECT = -20016
AV_ER_NOT_SUPPORT = -20033


class AvIn(C.Structure):
    _fields_ = [("size", C.c_uint32), ("sid", C.c_uint32),
                ("ch", C.c_uint8), ("pad", C.c_uint8 * 3), ("timeout", C.c_uint32),
                ("account", C.c_char_p), ("password", C.c_char_p),
                ("resend", C.c_uint32), ("security_mode", C.c_uint32),
                ("auth_type", C.c_uint32), ("sync_recv_data", C.c_uint32),
                ("dtls_cipher_suites", C.c_char_p), ("r38", C.c_uint32), ("r3c", C.c_uint32)]


class AvOut(C.Structure):
    _fields_ = [(n, C.c_uint32) for n in ("size", "server_type", "resend", "two_way_streaming",
                                          "sync_recv_data", "security_mode", "r6", "r7")]


#: avServStart3's auth callback: account, password; returns 1 to accept.
AUTH_FN = C.CFUNCTYPE(C.c_int, C.c_char_p, C.c_char_p)


def check(cond, what):
    if not cond:
        raise AssertionError(what)


class Camera:
    """Reads what AV sends and answers like the camera: the login reply, ACKs for our control frames,
    and the 0x25 answer to a reset. Everything AV sends is kept in `sent`."""

    def __init__(self, fake, login_state=0, ack=True, answer_reset=True):
        self.fake, self.login_state, self.ack, self.answer_reset = fake, login_state, ack, answer_reset
        self.sent, self.ctrl_frames, self.counter, self.frame_out = [], [], 0, 0
        self.acked = 0xffff                          # highest of our control frames acknowledged
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join()

    def push(self, pkt, datatype=0):
        check(self.fake.fake_push(pkt, len(pkt), datatype) == len(pkt), "fake_push failed")

    def header(self, cls, flags=0):
        self.counter += 1
        return struct.pack("<BBHHH", cls, flags, CAMERA_VERSION, self.counter, 0)

    def run(self):
        buf = C.create_string_buffer(0x600)
        while not self.stop.is_set():
            n = self.fake.fake_take(buf, len(buf))
            if n <= 0:
                time.sleep(0.002)
                continue
            pkt = buf.raw[:n]
            self.sent.append((time.monotonic(), pkt))
            if pkt[0] == 0 and pkt[1] in (0x00, 0x20) and self.login_state is not None:
                self.push(self.login_reply(struct.unpack_from("<I", pkt, 0x14)[0]))
            elif pkt[0] == 0x0c and pkt[0x10] == 0:
                self.on_ctrl(pkt[0x10:])

    def login_reply(self, nonce):
        reply = bytearray(60)
        reply[1] = 0x21
        struct.pack_into("<H", reply, 2, CAMERA_VERSION)
        struct.pack_into("<I", reply, 0x14, nonce)
        reply[0x18] = self.login_state
        return bytes(reply)

    def on_ctrl(self, f):
        frame, = struct.unpack_from("<H", f, 2)
        ctype, = struct.unpack_from("<I", f, 0x14)
        self.ctrl_frames.append((f[1], frame, ctype, f[0x18:0x18 + struct.unpack_from("<I", f, 8)[0] - 4]))
        if not self.ack:
            return
        if self.acked == 0xffff or (frame - self.acked) & 0xffff == 1:
            self.acked = frame
        self.push(self.ack_packet())
        if f[1] == 0x10 and ctype == 0x24 and self.answer_reset:
            seq, = struct.unpack_from("<H", f, 0x18)
            self.send_ctrl(0x10, 0x25, struct.pack("<HH", seq, 0))

    def ack_packet(self):
        return self.header(0x09) + struct.pack("<HHHHHHHH", 0, 0, self.acked, 0, 0, 0, 0, 0)

    def ctrl_packet(self, cls, ctype, payload, frame):
        f = struct.pack("<BBHIIIII", 0, cls, frame, 1, 4 + len(payload), frame, 0, ctype) + payload
        return self.header(0x0c) + bytes(8) + f

    def send_ctrl(self, cls, ctype, payload=b""):
        """A control frame from the camera; frame numbers count from 0 across both classes."""
        frame, self.frame_out = self.frame_out, self.frame_out + 1
        self.push(self.ctrl_packet(cls, ctype, payload, frame))
        return frame

    def media(self, kind, pn, frame_no, stream_count, payload, info, index, count):
        """One media fragment: the error-correction group header, then the fragment."""
        last = index == count - 1
        body = payload + (info if last else b"")
        w6 = len(info) if last else index
        frag = struct.pack("<BBHHHHHII", kind, 1 if last else 0, pn, count, w6, len(body), 0, frame_no,
                           stream_count) + body
        return self.header(0x0c, 0x05) + struct.pack("<HBBI", 0, index, 20, 1) + frag

    def send_frame(self, kind, pn, frame_no, data, info, stream_count=0, order=None, skip=(), frag=1024):
        """Send `data` as one frame in `frag`-byte fragments; returns the next packet number."""
        chunks = [data[i:i + frag] for i in range(0, len(data), frag)] or [b""]
        for i in order or range(len(chunks)):
            if i not in skip:
                self.push(self.media(kind, (pn + i) & 0xffff, frame_no, stream_count, chunks[i], info, i,
                                     len(chunks)))
        return (pn + len(chunks)) & 0xffff

    def wait(self, pred, timeout=2.0, what="condition"):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return
            time.sleep(0.005)
        raise AssertionError(f"timed out waiting for {what}")

    def of_class(self, cls):
        return [p for _, p in self.sent if p[0] == cls]


class Peer:
    def __init__(self, out):
        mode = os.RTLD_NOW | os.RTLD_LOCAL
        self.libs = {n: C.CDLL(str(pathlib.Path(out) / n), mode=mode)
                     for n in ("libTUTKGlobalAPIs.so", "libIOTCAPIs.so", "libAVAPIs.so")}
        self.fake = self.libs["libIOTCAPIs.so"]
        self.av = self.libs["libAVAPIs.so"]
        self.fake.fake_push.argtypes = [C.c_char_p, C.c_int, C.c_uint]
        self.fake.fake_take.argtypes = [C.c_char_p, C.c_int]
        self.fake.fake_psk_identity.restype = C.c_char_p
        self.av.avSendIOCtrl.argtypes = [C.c_int, C.c_uint, C.c_char_p, C.c_int]
        self.av.avRecvIOCtrl.argtypes = [C.c_int, C.POINTER(C.c_uint), C.c_char_p, C.c_int, C.c_uint]
        self.av.avRecvFrameData2.argtypes = [C.c_int, C.c_char_p, C.c_int, C.POINTER(C.c_int),
                                             C.POINTER(C.c_int), C.c_char_p, C.c_int,
                                             C.POINTER(C.c_int), C.POINTER(C.c_uint)]
        self.av.avSendAudioData.argtypes = [C.c_int, C.c_char_p, C.c_int, C.c_char_p, C.c_int]
        self.av.avServStart3.argtypes = [C.c_int, AUTH_FN, C.c_uint, C.c_uint, C.c_ubyte,
                                         C.POINTER(C.c_int)]
        self.av.avRecvAudioData.argtypes = [C.c_int, C.c_char_p, C.c_int, C.c_char_p, C.c_int,
                                            C.POINTER(C.c_uint)]
        self.fake.fake_set_version(0x1b)
        check(self.av.avInitialize(4) >= 0, "avInitialize")

    def start(self, security_mode=1, auth_type=0, resend=1):
        ai = AvIn(size=C.sizeof(AvIn), sid=0, ch=0, timeout=5, account=ACCOUNT, password=PASSWORD,
                  resend=resend, security_mode=security_mode, auth_type=auth_type)
        ao = AvOut(size=C.sizeof(AvOut))
        return self.av.avClientStartEx(C.byref(ai), C.byref(ao)), ao

    def recv_ioctrl(self, timeout_ms, size=1024):
        t, buf = C.c_uint(), C.create_string_buffer(size)
        rc = self.av.avRecvIOCtrl(0, C.byref(t), buf, size, timeout_ms)
        return rc, t.value, buf.raw[:max(rc, 0)]

    def recv_video(self, size=1 << 16):
        buf, info = C.create_string_buffer(size), C.create_string_buffer(64)
        actual, expected, info_len, number = C.c_int(), C.c_int(), C.c_int(), C.c_uint()
        rc = self.av.avRecvFrameData2(0, buf, size, C.byref(actual), C.byref(expected), info, 64,
                                      C.byref(info_len), C.byref(number))
        return rc, buf.raw[:max(rc, 0)], info.raw[:info_len.value], number.value, expected.value

    def recv_audio(self, size=4096):
        buf, info, number = C.create_string_buffer(size), C.create_string_buffer(64), C.c_uint()
        rc = self.av.avRecvAudioData(0, buf, size, info, 16, C.byref(number))
        return rc, buf.raw[:max(rc, 0)], info.raw[:16], number.value


def login_fields(pkt):
    return {"sub": pkt[1], "version": struct.unpack_from("<H", pkt, 2)[0],
            "body_len": struct.unpack_from("<H", pkt, 0x10)[0], "variant": pkt[0x12],
            "nonce": struct.unpack_from("<I", pkt, 0x14)[0],
            "account": pkt[0x18:0x18 + W].rstrip(b"\0"), "password": pkt[0x18 + W:0x18 + 2 * W],
            "variant2": pkt[0x18 + 2 * W], "opcodes": pkt[0x1c + 2 * W:0x30 + 2 * W],
            "cred_mode": pkt[0x30 + 2 * W], "one": pkt[0x34 + 2 * W]}


# ---- scenarios ----

def s_login(p):
    """DTLS-PSK identity and key, both login packets byte for byte, then the AvOut values."""
    cam = Camera(p.fake)
    rc, ao = p.start()
    check(rc == 0, f"avClientStartEx -> {rc}")
    check(p.fake.fake_psk_identity() == b"AUTHPWD_admin", p.fake.fake_psk_identity())
    key = C.create_string_buffer(64)
    check(p.fake.fake_psk_key(key) == 32 and key.raw[:32] == hashlib.sha256(PASSWORD).digest(), "PSK key")
    logins = [pkt for _, pkt in cam.sent if pkt[0] == 0 and pkt[1] in (0, 0x20)]
    check(len(logins) >= 2, f"{len(logins)} login packets")
    a, b = login_fields(logins[0]), login_fields(logins[1])
    check(len(logins[0]) == 2 * W + 0x38 and len(logins[1]) == 2 * W + 0x3a, "login lengths")
    check((a["sub"], b["sub"]) == (0, 0x20), "sub-types")
    check(a["version"] == 0x0c and a["body_len"] == 2 * W + 0x20 and b["body_len"] == 2 * W + 0x22,
          "version and body length")
    check(b["nonce"] == (a["nonce"] + 1) & 0xffffffff and a["nonce"] not in (0, 0xffffffff), "nonces")
    check(a["account"] == ACCOUNT and a["password"].rstrip(b"\0") == PASSWORD, "credentials")
    check(a["variant"] == a["variant2"] == 1 and a["opcodes"] == OPCODE_SUPPORT, "variant and opcodes")
    check(a["cred_mode"] == 0 and a["one"] == 1, "credential mode")
    check(ao.resend == 1, f"AvOut resend {ao.resend}")
    check(p.fake.fake_channel_on() == 1, "channel 0 on")
    p.av.avClientStop(0)
    check(p.fake.fake_channel_on() == 0, "channel off after stop")
    cam.close()


def s_login_sha256(p):
    """Credential mode 1 sends SHA-256(password) in the password field."""
    cam = Camera(p.fake)
    rc, _ = p.start(auth_type=1)
    check(rc == 0, f"avClientStartEx -> {rc}")
    first = next(pkt for _, pkt in cam.sent if pkt[0] == 0 and pkt[1] == 0)
    f = login_fields(first)
    check(f["password"][:32] == hashlib.sha256(PASSWORD).digest() and f["cred_mode"] == 1, "hashed password")
    p.av.avClientStop(0)
    cam.close()


def s_login_rejected(p):
    cam = Camera(p.fake, login_state=3)
    rc, _ = p.start()
    check(rc == AV_ER_WRONG_VIEWACCorPWD, f"avClientStartEx -> {rc}")
    cam.close()


def s_login_closed(p):
    """No reply, then the session closes under the login."""
    cam = Camera(p.fake, login_state=None)
    threading.Timer(0.3, lambda: p.fake.fake_set_status(-22)).start()
    rc, _ = p.start()
    check(rc == AV_ER_SESSION_CLOSE_BY_REMOTE, f"avClientStartEx -> {rc}")
    cam.close()


def s_ioctrl_send(p):
    """avSendIOCtrl: the 0x0c envelope, frame numbers from 0, returns once acknowledged; argument checks."""
    cam = Camera(p.fake)
    check(p.start()[0] == 0, "start")
    check(p.av.avSendIOCtrl(0, 0x7a2, b"\x01\x02\x03\x04", 4) == 0, "send 1")
    check(p.av.avSendIOCtrl(0, 0x733, b"", 0) == 0, "send 2")
    user = [f for f in cam.ctrl_frames if f[0] == 0x70]
    check([(f[1], f[2], f[3]) for f in user[:2]] == [(0, 0x7a2, b"\x01\x02\x03\x04"), (1, 0x733, b"")],
          f"frames {user}")
    pkt = next(pk for pk in cam.of_class(0x0c) if pk[0x10] == 0 and pk[0x11] == 0x70)
    check(pkt[1] == 0 and struct.unpack_from("<H", pkt, 2)[0] == 0x0c and pkt[8:0x10] == bytes(8),
          "packet header")
    check(len(pkt) == 0x10 + 0x18 + 4, f"length {len(pkt)}")
    check(p.av.avSendIOCtrl(0, 0xff, b"", 0) == AV_ER_NOT_SUPPORT, "type below 0x100")
    check(p.av.avSendIOCtrl(0, 0x800, bytes(0x3fd), 0x3fd) == AV_ER_EXCEED_MAX_SIZE, "too long")
    check(p.av.avSendIOCtrl(0, 0x800, bytes(0x3fc), 0x3fc) == 0, "longest")
    p.av.avClientStop(0)
    cam.close()


def s_ioctrl_retransmit(p):
    """An unacknowledged control frame is resent about every 60 ms until the camera acknowledges it."""
    cam = Camera(p.fake)
    check(p.start()[0] == 0, "start")
    cam.ack = False
    done = []
    t = threading.Thread(target=lambda: done.append(p.av.avSendIOCtrl(0, 0x7a2, b"\x01", 1)))
    t.start()
    cam.wait(lambda: len([f for f in cam.ctrl_frames if f[0] == 0x70]) >= 4, what="resends")
    times = [ts for ts, pk in cam.sent if pk[0] == 0x0c and pk[0x11] == 0x70]
    gaps = [b - a for a, b in zip(times, times[1:])]
    check(all(0.04 <= g <= 0.2 for g in gaps[:3]), f"resend gaps {gaps}")
    check(not done, "returned before the ACK")
    cam.ack = True
    cam.acked = 0
    cam.push(cam.ack_packet())
    t.join(2)
    check(done == [0], f"avSendIOCtrl -> {done}")
    p.av.avClientStop(0)
    cam.close()


def s_ioctrl_recv(p):
    """Camera frames are delivered in order, internal ones are not, and our ACK carries the highest frame."""
    cam = Camera(p.fake)
    check(p.start()[0] == 0, "start")
    check(p.recv_ioctrl(0)[0] == AV_ER_DATA_NOREADY, "empty, no wait")
    t = time.monotonic()
    check(p.recv_ioctrl(100)[0] == AV_ER_TIMEOUT and time.monotonic() - t >= 0.09, "empty, 100 ms")
    cam.send_ctrl(0x10, 0x40, bytes(8))             # internal: not delivered
    cam.send_ctrl(0x70, 0x331, b"RM5754")
    later = cam.ctrl_packet(0x70, 0x735, b"\x05", 3)
    cam.push(later)                                 # frame 3 before 2: dropped until resent
    cam.send_ctrl(0x70, 0x734, b"\x15\0\0\0")
    check(p.recv_ioctrl(500) == (6, 0x331, b"RM5754"), "first")
    check(p.recv_ioctrl(500) == (4, 0x734, b"\x15\0\0\0"), "second")
    check(p.recv_ioctrl(50)[0] == AV_ER_TIMEOUT, "out-of-order frame held back")
    cam.frame_out = 4
    cam.push(later)
    check(p.recv_ioctrl(500) == (1, 0x735, b"\x05"), "resent frame")
    cam.wait(lambda: any(struct.unpack_from("<H", pk, 0x0c)[0] == 3 for pk in cam.of_class(0x09)),
             what="ACK of frame 3")
    cam.send_ctrl(0x70, 0x736, b"abcd")
    rc, ctype, data = p.recv_ioctrl(500, size=2)
    check((rc, ctype, data) == (2, 0x736, b"ab"), f"truncated {rc} {data}")
    p.av.avClientStop(0)
    cam.close()


def s_media(p):
    """Fragments reassemble in any order with their frame info. Frames are delivered in order: an
    incomplete frame holds back later ones until its resend (new packet number, same frame and index)
    completes it, or for 500 ms, after which it is reported lost. Audio has its own queue."""
    cam = Camera(p.fake)
    check(p.start()[0] == 0, "start")
    check(p.av.avClientCleanVideoBuf(0) == 0, "stream start reset")
    check(p.recv_video()[0] == AV_ER_DATA_NOREADY, "nothing yet")
    info = bytes(range(16))
    data = os.urandom(2500)
    pn = cam.send_frame(5, 0x4000, 0, data, info, order=[2, 0, 1])
    deadline = time.monotonic() + 1
    while (r := p.recv_video())[0] == AV_ER_DATA_NOREADY and time.monotonic() < deadline:
        time.sleep(0.01)
    check(r[0] == 2500 and r[1] == data and r[2] == info and r[3] == 0, f"video frame {r[0]} {r[3]}")
    short = os.urandom(2500)                                           # kind 8, 1021-byte fragments
    pn = cam.send_frame(8, pn, 1, short, info, frag=1021, order=[1, 2, 0])
    time.sleep(0.05)
    r = p.recv_video()
    check(r[0] == 2500 and r[1] == short and r[3] == 1, f"short-fragment frame {r[0]} {r[3]}")
    p1 = os.urandom(3000)
    pn = cam.send_frame(7, pn, 11, p1, info, skip={1})                # fragment 1 missing
    pn = cam.send_frame(7, pn, 12, b"x" * 100, info)
    time.sleep(0.1)
    check(p.recv_video()[0] == AV_ER_DATA_NOREADY, "frame 2 held behind incomplete frame 1")
    cam.push(cam.media(7, pn, 11, 0, p1[1024:2048], info, 1, 3))        # the resend, new packet number
    pn += 1
    time.sleep(0.05)
    r = p.recv_video()
    check(r[0] == 3000 and r[1] == p1 and r[3] == 11, f"frame 11 completed by the resend {r[0]} {r[3]}")
    r = p.recv_video()
    check(r[0] == 100 and r[3] == 12, f"frame 12 {r[0]} {r[3]}")
    pn = cam.send_frame(7, pn, 3, os.urandom(3000), info, skip={2})   # never resent
    pn = cam.send_frame(7, pn, 4, b"y" * 50, info)
    audio = os.urandom(320)
    pn = cam.send_frame(3, pn, 5, audio, info)
    time.sleep(0.2)
    check(p.recv_video()[0] == AV_ER_DATA_NOREADY, "frame 4 held")
    time.sleep(0.45)
    check(p.recv_video()[0] == AV_ER_LOSED_THIS_FRAME, "frame 3 lost after the wait")
    r = p.recv_video()
    check(r[0] == 50 and r[3] == 4, f"frame 4 {r[0]} {r[3]}")
    r = p.recv_audio()
    check(r[0] == 320 and r[1] == audio and r[3] == 5, f"audio {r[0]}")
    check(p.recv_audio()[0] == AV_ER_DATA_NOREADY, "audio drained")
    big = os.urandom(5000)
    cam.send_frame(5, pn, 6, big, info)
    time.sleep(0.1)
    r = p.recv_video(size=4096)
    check(r[0] == AV_ER_BUFPARA_MAXSIZE_INSUFF and r[4] == 5000, f"small buffer {r[0]} {r[4]}")
    p.av.avClientStop(0)
    cam.close()


def s_media_ack(p):
    """ACK +8 is the highest contiguous media packet, +0xa the highest seen. A gap is named in the NAK list
    (+0x0e count, entries from +0x16 as offsets from +8); a resend fills it; a packet that never comes is
    skipped after about 300 ms, checked as later packets arrive."""
    cam = Camera(p.fake)
    check(p.start()[0] == 0, "start")
    check(p.av.avClientCleanVideoBuf(0) == 0, "stream start reset")

    def acks():
        return [pk for pk in cam.of_class(0x09)]

    def last_ack():
        a = acks()
        return struct.unpack_from("<HH", a[-1], 8) if a else None

    cam.push(cam.media(3, 0x4000, 0, 0, b"a", b"", 0, 1))
    cam.push(cam.media(3, 0x4002, 1, 0, b"b", b"", 0, 1))
    cam.wait(lambda: last_ack() == (0x4000, 0x4002), what="ACK with a gap")
    cam.wait(lambda: any(len(pk) == 0x18 and struct.unpack_from("<HHH", pk, 0x0e)[0] == 1
                         and struct.unpack_from("<H", pk, 0x16)[0] == 1 and pk[8:10] == b"\x00\x40"
                         for pk in acks()), what="NAK of 0x4001")
    cam.push(cam.media(3, 0x4001, 2, 0, b"c", b"", 0, 1))      # the resend
    cam.wait(lambda: last_ack() == (0x4002, 0x4002), what="gap filled")
    check(len(acks()[-1]) == 0x18 and struct.unpack_from("<H", acks()[-1], 0x0e)[0] == 0, "no NAK after")

    cam.push(cam.media(3, 0x4004, 3, 0, b"d", b"", 0, 1))      # 0x4003 never comes
    time.sleep(0.15)
    check(last_ack() == (0x4002, 0x4004), f"gap still open at 150 ms: {last_ack()}")
    time.sleep(0.2)
    cam.push(cam.media(3, 0x4005, 4, 0, b"e", b"", 0, 1))
    cam.wait(lambda: last_ack() == (0x4005, 0x4005), what="gap skipped")
    p.av.avClientStop(0)
    cam.close()


def s_clean_video(p):
    """avClientCleanVideoBuf drops queued video and resets the numbering with 0x24 {seq, base}, waiting for
    0x25. The first base is 0x3fff; later ones are the cumulative packet number + 0x4000."""
    cam = Camera(p.fake)
    check(p.start()[0] == 0, "start")
    check(p.av.avClientCleanVideoBuf(0) == 0, "first clean")
    cam.send_frame(5, 0x4000, 0, b"v" * 10, bytes(16))
    time.sleep(0.1)
    check(p.av.avClientCleanVideoBuf(0) == 0, "second clean")
    resets = [struct.unpack_from("<HH", f[3]) for f in cam.ctrl_frames if f[0] == 0x10 and f[2] == 0x24]
    check(resets == [(1, 0x3fff), (2, 0x8000)], f"resets {resets}")
    check(p.recv_video()[0] == AV_ER_DATA_NOREADY, "queue cleared")
    cam.answer_reset = False
    t = time.monotonic()
    check(p.av.avClientCleanVideoBuf(0) == 0, "clean without an answer")
    check(0.9 <= time.monotonic() - t <= 1.5, "waits about 1 s for 0x25")
    p.av.avClientStop(0)
    cam.close()


def s_keepalive(p):
    """With no traffic: an ACK about every 110 ms and a report about every 200 ms; a report is echoed in an
    RTT packet."""
    cam = Camera(p.fake)
    check(p.start()[0] == 0, "start")
    t0 = time.monotonic()
    time.sleep(1.0)
    acks = [ts for ts, pk in cam.sent if pk[0] == 0x09 and ts >= t0]
    reports = [pk for ts, pk in cam.sent if pk[0] == 0x0a and ts >= t0]
    check(7 <= len(acks) <= 11, f"{len(acks)} ACKs in 1 s")
    check(4 <= len(reports) <= 6, f"{len(reports)} reports in 1 s")
    check(all(len(r) == 16 and r[1] == 0x08 for r in reports), "report shape")
    check(all(len(pk) == 24 for pk in cam.of_class(0x09)), "ACK length")
    cam.push(cam.header(0x0a, 0x08) + struct.pack("<HH", 0x1234, 100) + bytes(4))
    cam.wait(lambda: any(struct.unpack_from("<H", pk, 8)[0] == 0x1234 and len(pk) == 20
                         for pk in cam.of_class(0x0b)), what="RTT echo")
    p.av.avClientStop(0)
    cam.close()


def s_session_closed(p):
    """The close callback and a closed session status both end the channel with -20015 / -20016."""
    cam = Camera(p.fake)
    check(p.start()[0] == 0, "start")
    check(p.fake.fake_close(-22) == 0, "close callback registered")
    check(p.fake.fake_close_magic() == -0x27955e4, f"close callback magic {p.fake.fake_close_magic():#x}")
    check(p.recv_ioctrl(100)[0] == AV_ER_SESSION_CLOSE_BY_REMOTE, "after close")
    check(p.av.avSendIOCtrl(0, 0x733, b"", 0) == AV_ER_SESSION_CLOSE_BY_REMOTE, "send after close")
    p.av.avClientStop(0)
    cam.close()
    cam = Camera(p.fake)
    rc, _ = p.start()
    check(rc == 0, f"restart -> {rc}")
    p.fake.fake_close(-23)
    rc = p.recv_video()[0]
    check(rc == AV_ER_REMOTE_TIMEOUT_DISCONNECT, f"after timeout close: {rc}")
    p.av.avClientStop(0)
    cam.close()


def camera_login(nonce, variant=1, account=b"", password=b""):
    """The camera's login on the talk-back channel, as captured: sub-type 0x00 then 0x20 (nonce + 1),
    both 2W + 0x38 bytes with an empty account and password."""
    out = []
    for sub, n in ((0x00, nonce), (0x20, nonce + 1)):
        pkt = bytearray(2 * W + 0x38)
        pkt[1] = sub
        struct.pack_into("<HH", pkt, 2, CAMERA_VERSION, 0)
        struct.pack_into("<H", pkt, 0x10, 2 * W + 0x20)
        pkt[0x12] = variant if sub == 0 else 0
        struct.pack_into("<I", pkt, 0x14, n)
        pkt[0x18:0x18 + len(account)] = account
        pkt[0x18 + W:0x18 + W + len(password)] = password
        pkt[0x18 + 2 * W] = variant
        pkt[0x1c + 2 * W:0x30 + 2 * W] = OPCODE_SUPPORT
        pkt[0x34 + 2 * W] = 1
        out.append(bytes(pkt))
    return out


def serv_start(p, cam, auth=lambda a, b: 1, timeout_s=2, login=True, stale_ack=False):
    """avServStart3 on channel 1 while the camera sends a ClientHello and then (if `login`) logs in.
    `stale_ack` first sends an ACK left over from the channel's previous talk-back (control frame 0)."""
    seen, result, resend = [], [], C.c_int()

    def check_auth(account, password):
        seen.append((account, password))
        return auth(account, password)

    cb = AUTH_FN(check_auth)
    t = threading.Thread(target=lambda: result.append(p.av.avServStart3(0, cb, timeout_s, 0, 1,
                                                                        C.byref(resend))))
    t.start()
    if stale_ack:
        cam.push(cam.header(0x09) + struct.pack("<HHHHHHHH", 0x94, 0x95, 0, 0, 0, 0x1e, 0, 0))
    cam.push(b"\x16\xfe\xff" + bytes(10), datatype=1)                # a DTLS ClientHello
    cam.wait(lambda: any(pk == b"recv dtls data!" for _, pk in cam.sent), what="DTLS refusal")
    if login:
        for pkt in camera_login(0x3a381dcd):
            cam.push(pkt)
    t.join(timeout_s + 2)
    return result[0], resend.value, seen, cb


def s_talkback(p):
    """The server: refuse DTLS, answer the login, send the hello frame, then audio as the stock uplink
    does: packet and frame numbers from 0, the error-correction group header, and the byte-sum
    checksum."""
    cam = Camera(p.fake, login_state=None)
    idx, resend, seen, _cb = serv_start(p, cam, stale_ack=True)
    check(idx >= 0 and resend == 1, f"avServStart3 -> {idx}, resend {resend}")
    check(seen == [(b"", b"")], f"auth calls {seen}")
    check(p.fake.fake_channel_on() & 2, "channel 1 on")
    reply = next(pk for _, pk in cam.sent if pk[:2] == b"\x00\x21")
    check(len(reply) == 60 and struct.unpack_from("<HHI", reply, 0x10)[::2] == (0x24, 0x3a381dce),
          "reply length, body length, nonce of the 0x20 packet")
    check(struct.unpack_from("<I", reply, 0x18)[0] == 0 and reply[0x1d] == reply[0x1f] == reply[0x20] == 1,
          "reply state and flags")
    check(reply[0x24:0x38] == OPCODE_SUPPORT and struct.unpack_from("<H", reply, 2)[0] == 0x0c, "reply tail")
    cam.wait(lambda: any(f[:3] == (0x10, 0, 0x40) for f in cam.ctrl_frames), what="hello frame")
    hello = next(f for f in cam.ctrl_frames if f[2] == 0x40)
    check(hello[3] == b"\x00\x01\x00\x00", f"hello payload {hello[3]}")
    time.sleep(0.3)
    hellos = len([f for f in cam.ctrl_frames if f[2] == 0x40])
    check(hellos == 1, f"hello sent {hellos} times: acknowledged, so never resent")

    info = struct.pack("<HBBB7xI", 0x8a, 2, 0, 0, 1234)
    for i in range(25):
        check(p.av.avSendAudioData(idx, bytes([i]) * 220, 220, info, 16) == 0, f"audio {i}")
    check(p.av.avSendAudioData(idx, b"z" * 2100, 2100, info, 16) == 0, "long frame")
    cam.wait(lambda: len([pk for _, pk in cam.sent if pk[0] == 0x0c and pk[1] == 0x01]) == 28, what="media")
    media = [pk for _, pk in cam.sent if pk[0] == 0x0c and pk[1] == 0x01]
    for pn, pk in enumerate(media):
        group, index, k, one = struct.unpack_from("<HBBI", pk, 8)
        m = pk[0x10:]
        kind, sub, mpn, count, w6, plen, ck, frame, scount = struct.unpack_from("<BBHHHHHII", m)
        check((group, index, k, one) == (pn // 20, pn % 20, 20, 1), f"group header of {pn}")
        check(kind == 3 and mpn == pn and len(m) == 0x14 + plen, f"media header of {pn}")
        hdr = bytearray(m[:0x14])
        hdr[10:12] = b"\0\0"
        check(ck == (sum(hdr) + sum(m[0x14:])) & 0xffff, f"checksum of {pn}")
        if pn < 25:
            check((sub, count, w6, plen, frame, scount) == (1, 1, 16, 236, pn, pn + 1), f"fields of {pn}")
            check(m[0x14:0x14 + 220] == bytes([pn]) * 220 and m[-16:] == info, f"payload of {pn}")
    tail = [struct.unpack_from("<BHHH", pk, 0x11) for pk in media[25:]]
    check(tail == [(0, 25, 3, 0), (0, 26, 3, 1), (1, 27, 3, 16)], f"long frame fragments {tail}")
    check(struct.unpack_from("<H", media[27], 0x18)[0] == 2100 - 2048 + 16, "last fragment length")

    p.av.avServStop(idx)
    check(not p.fake.fake_channel_on() & 2, "channel 1 off after stop")
    p.av.avServExit(0, 1)
    check(p.av.avSendAudioData(idx, b"x", 1, info, 16) == AV_ER_INVALID_ARG, "send after stop")
    cam.close()


def s_talkback_refused(p):
    """A login the auth callback refuses ends avServStart3 with -20009; no login at all, with -20011."""
    cam = Camera(p.fake, login_state=None)
    idx, _, seen, _cb = serv_start(p, cam, auth=lambda a, b: 0)
    check(idx == AV_ER_WRONG_VIEWACCorPWD and len(seen) == 1, f"refused login -> {idx}")
    reply = next(pk for _, pk in cam.sent if pk[:2] == b"\x00\x21")
    check(struct.unpack_from("<I", reply, 0x18)[0] == 3, "reply state 3")
    check(p.fake.fake_channel_on() == 0, "channel off after the refusal")
    cam.close()
    cam = Camera(p.fake, login_state=None)
    t = time.monotonic()
    idx, *_ = serv_start(p, cam, timeout_s=1, login=False)
    check(idx == AV_ER_TIMEOUT and 0.9 <= time.monotonic() - t <= 2, f"no login -> {idx}")
    cam.close()


def s_talkback_client_refused(p):
    """avSendAudioData on a client channel is refused."""
    cam = Camera(p.fake)
    check(p.start()[0] == 0, "start")
    check(p.av.avSendAudioData(0, b"x", 1, bytes(16), 16) == AV_ER_NOT_SUPPORT, "client send")
    p.av.avClientStop(0)
    cam.close()


SCENARIOS = {n[2:]: f for n, f in globals().items() if n.startswith("s_")}

if __name__ == "__main__":
    peer = Peer(sys.argv[1])
    SCENARIOS[sys.argv[2]](peer)
    print("ok")
