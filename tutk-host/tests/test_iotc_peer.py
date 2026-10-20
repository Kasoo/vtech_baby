"""Our libIOTCAPIs.so against a fake camera on a local UDP socket: the LAN-direct connect (success,
wrong authKey, timeout), data both ways, keepalive and the close burst (kit: iotc/16-handshake.md,
iotc/17-inbound.md, iotc/30-data.md)."""
import ctypes as C
import hashlib
import hmac
import socket
import struct
import subprocess
import threading
import time

import pytest
from conftest import HERE, TUTK

UID = "ABCDEFGHJKLMNPRSTU11"
AUTHKEY = b"Ab12Cd34"
ER_TIMEOUT = -13
ER_WRONG_AUTH_KEY = -68
CAMERA_VERSION = 0x1b


@pytest.fixture(scope="module")
def wire(tmp_path_factory):
    so = tmp_path_factory.mktemp("wire") / "libwire.so"
    subprocess.run(["cc", "-O1", "-shared", "-fPIC", "-I", str(TUTK / "iotc"), "-I", str(TUTK / "include"),
                    "-I", str(TUTK / "common"), str(HERE / "wire_lib.c"), str(TUTK / "iotc/wire.c"),
                    "-o", str(so), "-lpthread"], check=True)
    return C.CDLL(str(so))


def has_lan_ipv4():
    out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True).stdout
    return any(line.split()[1] != "lo" for line in out.splitlines())


class FakeCamera:
    """Answers the LAN search on its own port and records every datagram it receives, unscrambled."""

    def __init__(self, wire, result=1):
        self.wire, self.result = wire, result
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.received, self.client = [], None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def scramble(self, pkt, prefix=None):
        out = C.create_string_buffer(len(pkt))
        self.wire.wire_scramble(pkt, out, len(pkt), len(pkt) if prefix is None else prefix)
        return out.raw

    def unscramble(self, data):
        out = C.create_string_buffer(len(data))
        self.wire.wire_unscramble(data, out, len(data), 16)
        prefix = 0x40 if out.raw[3] & 1 else len(data)
        self.wire.wire_unscramble(data, out, len(data), prefix)
        return out.raw

    def run(self):
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            if data[:2] == b"\x51\xcc":
                self.received.append(("record", data))
                continue
            pkt = self.unscramble(data)
            opcode = struct.unpack_from("<H", pkt, 8)[0]
            self.received.append((opcode, pkt))
            if opcode == 0x601 and self.client is None:
                self.client = addr
                self.send(self.reply(pkt[0x38:0x40]))

    def reply(self, token):
        pkt = bytearray(0xc8)
        struct.pack_into("<HBBHHHH", pkt, 0, 0x0204, CAMERA_VERSION, 0x02, 0xb8, 0, 0x0602, 0x0012)
        pkt[0xbc:0xc4] = token
        pkt[0xc4] = self.result
        return bytes(pkt)

    def send(self, pkt, prefix=None):
        self.sock.sendto(self.scramble(pkt, prefix), self.client)

    def of(self, kind):
        return [p for k, p in list(self.received) if k == kind]

    def close(self):
        self.stop.set()
        self.thread.join()
        self.sock.close()


def connect_input(authkey=AUTHKEY, timeout_s=3):
    buf = (C.c_ubyte * 160)()
    struct.pack_into("<II", buf, 0, 0xa0, 0)
    buf[8:16] = authkey
    struct.pack_into("<I", buf, 0x94, timeout_s)
    return buf


@pytest.fixture(scope="module")
def up(iotc):
    if not has_lan_ipv4():
        pytest.skip("no non-loopback IPv4 interface to broadcast on")
    assert iotc.IOTC_Initialize2(0) in (0, -3)
    iotc.IOTC_Connect_ByUIDEx.argtypes = [C.c_char_p, C.c_int, C.c_void_p]
    iotc.IOTC_Session_Write.argtypes = [C.c_int, C.c_char_p, C.c_uint, C.c_uint8]
    iotc.IOTC_Session_Read_Data_And_Datatype.argtypes = [
        C.c_uint, C.c_void_p, C.c_uint, C.c_uint, C.c_void_p, C.c_void_p, C.c_uint8, C.c_void_p]
    return iotc


@pytest.fixture
def camera(wire, up):
    cams = []

    def make(result=1):
        cam = FakeCamera(wire, result)
        assert up.IOTC_Set_LanSearchPort(cam.port) == 0
        cams.append(cam)
        return cam
    yield make
    for cam in cams:
        cam.close()


def test_connect_data_close(up, camera):
    cam = camera()
    sid = up.IOTC_Get_SessionID()
    assert up.IOTC_Connect_ByUIDEx(UID.lower().encode(), sid, connect_input()) == sid

    # The search: the signed record and the probe, carrying UID, token and authKey.
    record = cam.of("record")[0]
    probe = cam.of(0x601)[0]
    token = probe[0x38:0x40]
    assert probe[:4] == bytes([4, 2, 0x1d, 2]) and struct.unpack_from("<H", probe, 4)[0] == 0x48
    assert probe[0x10:0x24] == UID.encode() and probe[0x4a:0x52] == AUTHKEY
    assert struct.unpack_from("<I", probe, 0x34)[0] == 0x04030864          # 4.3.8.100, this build
    assert record[:8] == bytes([0x51, 0xcc, 0, 0, 2, 0x10, 0x28, 0]) and record[0x10:0x18] == token
    assert record[0x18:0x20] == bytes([0x64, 8, 3, 4, 0x1d, 0, 0, 0])    # 4.3.8.100, then the version byte
    assert record[0x20:] == hmac.new(UID.encode() + AUTHKEY, record[:0x20], hashlib.sha1).digest()

    # The answer to the connect response: the probe unicast, then the 0x0402 ack.
    deadline = time.time() + 1
    while not cam.of(0x402) and time.time() < deadline:
        time.sleep(0.01)
    assert len(cam.of(0x601)) >= 2 and cam.of(0x402)[0][0x24:0x2c] == token

    info = (C.c_uint8 * 256)()
    assert up.IOTC_Session_Check(sid, info) == 0 and info[0] == 2
    assert up.IOTC_Get_Remote_ProtocolVersion(sid) == CAMERA_VERSION

    # Out: a 0x0407 with the sub-header, per-channel sequence from 0.
    assert up.IOTC_Session_Write(sid, b"hello", 5, 0) == 5
    assert up.IOTC_Session_Write(sid, b"again", 5, 0) == 5
    time.sleep(0.1)
    first, second = cam.of(0x407)[:2]
    assert first[:4] == bytes([4, 2, 0x1d, 0x0a]) and struct.unpack_from("<H", first, 4)[0] == 12 + 5
    assert struct.unpack_from("<HHHH", first, 6) == (0, 0x407, 0x21, struct.unpack_from("<H", token)[0])
    assert first[0xe:0x10] == b"\x00\x00" and first[0x10:0x1c] == b"\x0c\0\0\0" + token
    assert first[0x1c:] == b"hello" and struct.unpack_from("<H", second, 6)[0] == 1

    # In: a 0x0408 from the camera reaches the channel queue with its datatype.
    pkt = bytearray(16 + 12 + 4)
    struct.pack_into("<HBBHHHHHBB", pkt, 0, 0x0204, CAMERA_VERSION, 0x0b, 12 + 4, 7, 0x408, 0x12,
                     struct.unpack_from("<H", token)[0], 0, 1)
    pkt[16:28] = b"\x0c\0\0\0" + token
    pkt[28:] = b"ping"
    cam.send(bytes(pkt), prefix=0x40)
    buf, flags = (C.c_char * 64)(), C.c_uint()
    assert up.IOTC_Session_Read_Data_And_Datatype(sid, buf, 64, 1000, None, None, 0, C.byref(flags)) == 4
    assert buf.raw[:4] == b"ping" and flags.value == 1

    up.IOTC_Session_Close(sid)
    time.sleep(0.1)
    closes = cam.of(0x417)
    assert len(closes) == 3 and closes[0][4:6] == b"\x08\x00" and closes[0][16:24] == token


def test_wrong_authkey(up, camera):
    camera(result=5)
    sid = up.IOTC_Get_SessionID()
    assert up.IOTC_Connect_ByUIDEx(UID.encode(), sid, connect_input()) == ER_WRONG_AUTH_KEY
    up.IOTC_Session_Close(sid)


def test_no_answer_times_out(up, camera):
    cam = camera()
    cam.client = ("never", 0)                       # it records the search but does not answer
    sid = up.IOTC_Get_SessionID()
    start = time.time()
    assert up.IOTC_Connect_ByUIDEx(UID.encode(), sid, connect_input(timeout_s=1)) == ER_TIMEOUT
    assert 0.9 < time.time() - start < 2
    assert len(cam.of(0x601)) >= 5
    up.IOTC_Session_Close(sid)


def test_bad_input(up):
    sid = up.IOTC_Get_SessionID()
    assert up.IOTC_Connect_ByUIDEx(UID.encode(), sid, connect_input(authkey=b"bad-key!")) == -46
    assert up.IOTC_Connect_ByUIDEx(UID.encode(), sid, None) == -46
    up.IOTC_Session_Close(sid)
