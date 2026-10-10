"""The companion's LAN-only egress policy (vtech-lan-kit/tooling/companion/net.c), exercised directly on
the vendored libtk.so, plus a check that the patched SDK actually routes its network calls through it."""
import ctypes as C
import os
import socket
import subprocess

import pytest

from custom_components.vtech_baby import native

SDK_DIR = os.environ.get("VTECH_SDK_DIR",
                         os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vtech-lan-kit", "sdk"))
LIBTK = native.companion_path("x86_64")
HOST_SDK = os.path.join(SDK_DIR, "x86_64")
ENETUNREACH = 101

pytestmark = pytest.mark.skipif(os.uname().machine != "x86_64" or not os.path.isfile(LIBTK),
                                reason="needs an x86_64 host and native/x86_64/libtk.so")


@pytest.fixture(scope="module")
def tk():
    lib = C.CDLL(LIBTK, use_errno=True)
    lib.Sendto.restype = C.c_ssize_t
    return lib


def sa4(ip, port=9):
    raw = socket.AF_INET.to_bytes(2, "little") + port.to_bytes(2, "big") + socket.inet_aton(ip) + bytes(8)
    return C.create_string_buffer(raw, len(raw)), len(raw)


def sa6(ip, port=9):
    raw = (socket.AF_INET6.to_bytes(2, "little") + port.to_bytes(2, "big") + bytes(4)
           + socket.inet_pton(socket.AF_INET6, ip) + bytes(4))
    return C.create_string_buffer(raw, len(raw)), len(raw)


LIBC = C.CDLL(None, use_errno=True)


class raw_socket:
    """A socket fd from libc directly: the HA test plugin blocks Python's socket.socket."""

    def __init__(self, family, kind):
        self.fd = LIBC.socket(family, kind, 0)
        assert self.fd >= 0
        one = C.c_int(1)
        LIBC.setsockopt(self.fd, socket.SOL_SOCKET, socket.SO_BROADCAST, C.byref(one), C.sizeof(one))

    def __enter__(self):
        return self.fd

    def __exit__(self, *exc):
        LIBC.close(self.fd)


def sendto(tk, family, sa):
    with raw_socket(family, socket.SOCK_DGRAM) as fd:
        C.set_errno(0)
        rc = tk.Sendto(fd, b"x", 1, 0, *sa)
        return rc, C.get_errno()


@pytest.mark.parametrize("ip", ["127.0.0.1", "255.255.255.255"])
def test_on_link_ipv4_passes(tk, ip):
    assert sendto(tk, socket.AF_INET, sa4(ip)) == (1, 0)


def test_ipv4_mapped_on_link_passes(tk):
    assert sendto(tk, socket.AF_INET6, sa6("::ffff:127.0.0.1")) == (1, 0)


@pytest.mark.parametrize("family,sa", [
    (socket.AF_INET, sa4("8.8.8.8")),
    (socket.AF_INET, sa4("51.158.202.75")),                # a ThroughTek STUN/relay server
    (socket.AF_INET6, sa6("64:ff9b::339e:ca4b")),          # the same through NAT64
    (socket.AF_INET6, sa6("2001:4860:4860::8888")),
    (socket.AF_INET6, sa6("::ffff:8.8.8.8")),
    (socket.AF_INET6, sa6("::1")),                          # no IPv6 at all: the camera is IPv4-only
])
def test_off_link_send_refused(tk, family, sa):
    assert sendto(tk, family, sa) == (-1, ENETUNREACH)


def test_off_link_connect_refused(tk):
    with raw_socket(socket.AF_INET, socket.SOCK_STREAM) as fd:
        C.set_errno(0)
        assert tk.Connect(fd, *sa4("34.193.155.98", 443)) == -1   # a ThroughTek master
        assert C.get_errno() == ENETUNREACH


def test_names_never_resolved_numeric_literals_are(tk):
    res = C.c_void_p()
    assert tk.Getaddrinfo(b"us-c-master-1.iotcplatform.com", None, None, C.byref(res)) == socket.EAI_NONAME
    assert tk.Getaddrinfo(b"127.0.0.1", None, None, C.byref(res)) == 0
    tk.Freeaddrinfo(res)


def test_refusals_are_counted(tk):
    before = (C.c_ulong(), C.c_ulong())
    tk.tk_lan_only_stats(C.byref(before[0]), C.byref(before[1]))
    sendto(tk, socket.AF_INET, sa4("8.8.8.8"))
    tk.Getaddrinfo(b"example.com", None, None, C.byref(C.c_void_p()))
    after = (C.c_ulong(), C.c_ulong())
    tk.tk_lan_only_stats(C.byref(after[0]), C.byref(after[1]))
    assert (after[0].value - before[0].value, after[1].value - before[1].value) == (1, 1)


@pytest.mark.skipif(not os.path.isdir(HOST_SDK), reason="needs the development SDK in vtech-lan-kit/sdk/")
def test_patched_sdk_routes_network_calls_through_the_companion(tmp_path):
    libdir = native.prepare(HOST_SDK, str(tmp_path)).libdir
    imports = set()
    for lib in ("libTUTKGlobalAPIs.so", "libIOTCAPIs.so", "libAVAPIs.so", "libNebulaAPIs.so"):
        out = subprocess.run(["nm", "-D", "--undefined-only", os.path.join(libdir, lib)],
                             capture_output=True, text=True, check=True).stdout
        imports |= {line.split()[-1].split("@")[0] for line in out.splitlines() if line.strip()}
    assert {"Connect", "Sendto", "Getaddrinfo"} <= imports
    # Nothing else that can put a packet on the wire or resolve a name may bind to the host directly.
    assert not imports & {"connect", "sendto", "sendmsg", "sendmmsg", "getaddrinfo", "gethostbyname",
                          "gethostbyname2", "gethostbyname_r"}
