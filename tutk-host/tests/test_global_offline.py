"""Offline checks of Global's platform layer, lists, wrappers and licence, called through ctypes on the
patched library."""
import base64
import ctypes as C
import errno
import hashlib
import hmac
import socket
import struct
import sys
import threading

import pytest
from conftest import ROOT

sys.path.insert(0, str(ROOT / "custom_components/vtech_baby"))
from vtech_lan.sdk import DEFAULT_LICENSE_KEY  # noqa: E402

TUTK_ER_INVALID_ARG = -1002
TUTK_ER_INVALID_LICENSE_KEY = -1004
TUTK_ER_NO_LICENSE_KEY = -1005
TOS_ER_NOT_FOUND = 0x1100101
TOS_ER_INVALID_ARG = 0x1100115
TOS_ER_RESOLVE_FAILED = 0x110017A
TJSON_ER_INVALID_ARG = -0x9C42
TJSON_ER_WRONG_TYPE = -0xA7F9
TJSON_ER_NO_MEMBER = -0xA7FB

LICENCE_PEM = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDQXw9rYBtx25rSHy3kNotbSoys
Cb+DqSieB7PhXgJENqOqa2Z+nlcRbgQ+lnptlVYelWPR5phmkW2OSWwoQByvXVkh
Nuiz5CAMNV+6WCYF84DQNstarDLwt7L6YhS8fTHt5ODv1uwArchCJp67+cS+gRK4
95wiu9ejOoqiDmQGYwIDAQAB
-----END PUBLIC KEY-----
"""


def fn(lib, name, restype, *argtypes):
    f = getattr(lib, name)
    f.restype, f.argtypes = restype, list(argtypes)
    return f


def decode_licence(key):
    """The kit's licence format, decoded independently of the library: returns the JSON text."""
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    nums = load_pem_public_key(LICENCE_PEM).public_numbers()
    raw = base64.b64decode(key)
    k = (nums.n.bit_length() + 7) // 8
    assert struct.unpack("<I", raw[:4])[0] == 1
    m = pow(int.from_bytes(raw[4:4 + k], "big"), nums.e, nums.n).to_bytes(k, "big")
    content_key = m[:32]
    body = bytes(b ^ content_key[i % 32] for i, b in enumerate(raw[4 + k:]))
    json_text = body.split(b"\0")[0]
    assert b"\x11" + hashlib.sha256(json_text).digest()[1:] == content_key
    return json_text.decode()


# ---- licence and master names ----

def test_licence(g):
    import json
    set_key = fn(g, "TUTK_SDK_Set_License_Key", C.c_int, C.c_char_p)
    mask = fn(g, "GetLicenseKeyMask", C.c_int, C.c_char_p, C.c_int)
    master = fn(g, "GetMasterDomainName", C.c_int, C.c_int, C.c_uint, C.c_char_p, C.POINTER(C.c_void_p))
    free = fn(g, "tutk_platform_free", None, C.c_void_p)
    realm = json.loads(decode_licence(DEFAULT_LICENSE_KEY))["realm"]
    assert len(realm) == 20

    assert set_key(None) == TUTK_ER_INVALID_ARG
    bad = DEFAULT_LICENSE_KEY[:-8] + ("A" if DEFAULT_LICENSE_KEY[-8] != "A" else "B") + DEFAULT_LICENSE_KEY[-7:]
    assert set_key(bad.encode()) == TUTK_ER_INVALID_LICENSE_KEY
    assert set_key(b"not base64!") == TUTK_ER_INVALID_LICENSE_KEY
    buf = C.create_string_buffer(16)
    assert mask(buf, 16) == TUTK_ER_NO_LICENSE_KEY
    out = C.c_void_p()
    assert master(0, 0, None, C.byref(out)) == TUTK_ER_NO_LICENSE_KEY

    assert set_key(DEFAULT_LICENSE_KEY.encode()) == 0
    assert mask(buf, 15) == TUTK_ER_INVALID_ARG
    assert mask(buf, 16) == 0 and buf.raw == bytes(16)

    assert master(0, 2, None, C.byref(out)) == -1
    assert master(0, 0, None, C.byref(out)) == 0
    name = C.string_at(out.value).decode()
    free(out)
    assert realm in name and name.endswith(".invalid")
    assert master(0, 0, b"", C.byref(out)) == 0
    assert C.string_at(out.value).decode() == name     # stable between calls
    free(out)


# ---- platform ----

def test_memory_and_snprintf(g):
    malloc = fn(g, "tutk_platform_malloc", C.c_void_p, C.c_int)
    calloc = fn(g, "tutk_platform_calloc", C.c_void_p, C.c_int, C.c_int)
    free = fn(g, "tutk_platform_free", None, C.c_void_p)
    p = calloc(4, 8)
    assert C.string_at(p, 32) == bytes(32)
    free(p)
    free(malloc(100))
    snprintf = g.tutk_platform_snprintf
    snprintf.restype = C.c_int
    buf = C.create_string_buffer(8)
    assert snprintf(buf, C.c_uint(8), b"%s-%d", b"abcdef", C.c_int(42)) == 9
    assert buf.value == b"abcdef-"


def test_recursive_mutex(g):
    libc = C.CDLL(None)
    init = fn(g, "tutk_platform_mutex_init", C.c_int, C.c_void_p)
    destroy = fn(g, "tutk_platform_mutex_destroy", C.c_int, C.c_void_p)
    lock = fn(libc, "pthread_mutex_lock", C.c_int, C.c_void_p)
    trylock = fn(libc, "pthread_mutex_trylock", C.c_int, C.c_void_p)
    unlock = fn(libc, "pthread_mutex_unlock", C.c_int, C.c_void_p)
    m = C.create_string_buffer(40)
    assert init(m) == 0
    assert lock(m) == 0
    assert trylock(m) == 0                          # recursive: the owner may lock again
    other = []
    t = threading.Thread(target=lambda: other.append(trylock(m)))
    t.start()
    t.join()
    assert other == [errno.EBUSY]
    assert unlock(m) == 0 and unlock(m) == 0
    assert destroy(m) == 0


def test_create_task_and_join(g):
    START = C.CFUNCTYPE(C.c_void_p, C.c_void_p)
    create = fn(g, "tutk_platform_CreateTask", C.c_uint64, C.POINTER(C.c_int), START, C.c_void_p,
                C.c_char, C.c_int)
    wait = fn(g, "tutk_platform_WaitForTaskExit", C.c_int, C.c_uint64)
    seen = []
    start = START(lambda arg: seen.append(arg) or None)
    err = C.c_int(99)
    t = create(C.byref(err), start, 1234, b"\0", 64 * 1024)
    assert err.value == 0 and t not in (0, 2**64 - 1)
    assert wait(t) == 0
    assert seen == [1234]
    assert wait(0) == 0


def test_random(g):
    fn(g, "tutk_platform_srand", None)()
    rand = fn(g, "tutk_platform_rand", C.c_long)
    rng = fn(g, "tutk_platform_rand_range", C.c_int, C.c_int, C.c_int)
    assert all(0 <= rand() < 2**31 for _ in range(100))
    values = {rng(5, 3) for _ in range(300)}
    assert values == {3, 4, 5}
    sslrand = fn(g, "TUTKSSL_rand", C.c_int, C.c_char_p, C.c_int)
    buf = C.create_string_buffer(32)
    assert sslrand(buf, 32) == 0 and buf.raw != bytes(32)


# ---- addresses ----

class SockaddrIn6(C.Structure):
    _fields_ = [("family", C.c_uint16), ("port", C.c_uint16), ("flowinfo", C.c_uint32),
                ("addr", C.c_uint8 * 16), ("scope_id", C.c_uint32)]


def test_host_get_sockaddr(g):
    get = fn(g, "tutk_Host_Get_Sockaddr", C.c_int, C.c_char_p, C.c_int, C.c_uint16, C.c_uint,
             C.POINTER(SockaddrIn6))
    port = socket.htons(32761)
    sa = SockaddrIn6()
    assert get(None, 1, port, 0, sa) == 0
    assert sa.family == socket.AF_INET6 and bytes(sa.addr) == bytes(16) and sa.port == port

    assert get(b"192.0.2.10", 1, port, 0, sa) == 0
    assert sa.family == socket.AF_INET6 and sa.port == port
    assert socket.inet_ntop(socket.AF_INET6, bytes(sa.addr)) == "::ffff:192.0.2.10"

    assert get(b"2001:db8::1", 0, port, 0, sa) == 0
    assert socket.inet_ntop(socket.AF_INET6, bytes(sa.addr)) == "2001:db8::1" and sa.port == port

    raw = (C.c_uint8 * 28)()
    assert get(b"192.0.2.10", 1, port, 2, C.cast(raw, C.POINTER(SockaddrIn6))) == 0
    family, p = struct.unpack("<HH", bytes(raw[:4]))
    assert family == socket.AF_INET and p == port and bytes(raw[4:8]) == bytes([192, 168, 1, 220])

    assert get(b"example.com", 1, port, 0, sa) == -TOS_ER_RESOLVE_FAILED       # names never resolve
    assert get(b"192.0.2.10", 5, port, 0, sa) == TUTK_ER_INVALID_ARG


def test_sockaddr_to_ip_string(g):
    to_str = fn(g, "tutk_Sockaddr_To_Ip_String", C.c_int, C.c_char_p, C.c_uint, C.c_uint, C.c_char_p)
    sin = struct.pack("<HH4s8x", socket.AF_INET, 0, bytes([192, 168, 1, 220]))
    out = C.create_string_buffer(64)
    assert to_str(sin, 64, 0, out) == 0 and out.value == b"::ffff:192.0.2.10"
    assert to_str(None, 64, 0, out) == -1


def test_small_address_helpers(g):
    is6 = fn(g, "DetermineIpv6Address", C.c_int, C.c_char_p)
    assert is6(b"::1") == 1 and is6(b"10.0.0.1") == 0
    assert fn(g, "GetProcEnvVarUsingNat64", C.c_int)() == 0
    ntop = fn(g, "tutk_platform_inet_ntop", C.c_char_p, C.c_int, C.c_char_p, C.c_char_p, C.c_uint)
    buf = C.create_string_buffer(16)
    assert ntop(socket.AF_INET, bytes([10, 1, 2, 3]), buf, 16) == b"10.1.2.3"


def test_interface_info(g):
    info = (C.c_uint8 * 488)(*([0xAA] * 488))
    n = fn(g, "tutk_Net_GetInterfaceInfo", C.c_int, C.c_void_p)(info)
    raw = bytes(info)
    assert 0 <= n <= 10 and raw[0] == n
    assert raw[1:244] == bytes(243) and raw[284 + 40:] == bytes(164)
    addrs = [socket.inet_ntoa(raw[244 + 4 * i:248 + 4 * i]) for i in range(n)]
    assert "127.0.0.1" not in addrs


# ---- tlist ----

class TListNode(C.Structure):
    pass


TListNode._fields_ = [("data", C.c_void_p), ("next", C.POINTER(TListNode)), ("prev", C.POINTER(TListNode))]


def test_tlist(g):
    new = fn(g, "tlistNew", C.c_void_p)
    append = fn(g, "tlistAppend", C.c_int, C.c_void_p, C.c_void_p)
    head = fn(g, "tlistHead", C.POINTER(TListNode), C.c_void_p)
    at = fn(g, "tlistAtIndex", C.c_void_p, C.c_void_p, C.c_int)
    length = fn(g, "tlistLength", C.c_int, C.c_void_p)
    remove = fn(g, "tlistRemove", C.c_int, C.c_void_p, C.c_void_p)
    destroy = fn(g, "tlistDestroy", C.c_int, C.c_void_p)
    EACH = C.CFUNCTYPE(None, C.c_void_p, C.c_void_p)
    foreach = fn(g, "tlistForeach", C.c_int, C.c_void_p, EACH, C.c_void_p)

    lst = new()
    assert length(lst) == 0 and not head(lst)
    for v in (10, 20, 30):
        assert append(lst, v) == 0
    assert length(lst) == 3 and [at(lst, i) for i in range(3)] == [10, 20, 30]
    assert at(lst, 3) is None and at(lst, -1) is None

    walked, node = [], head(lst)
    while node:
        walked.append(node.contents.data)
        node = node.contents.next
    assert walked == [10, 20, 30]

    seen = []
    assert foreach(lst, EACH(lambda d, a: seen.append((d, a))), 7) == 0
    assert seen == [(10, 7), (20, 7), (30, 7)]

    assert remove(lst, 20) == 0 and remove(lst, 20) == -TOS_ER_NOT_FOUND
    node = head(lst)
    assert node.contents.data == 10 and node.contents.next.contents.data == 30
    assert node.contents.next.contents.prev.contents.data == 10
    assert remove(None, 10) == -TOS_ER_INVALID_ARG and length(None) == 0
    assert destroy(lst) == 0


# ---- crypto ----

def test_hashes(g):
    sha = fn(g, "TUTK3rdSHA256", C.c_int, C.c_char_p, C.c_int, C.c_char_p, C.c_uint)
    mac = fn(g, "TUTK3rdSHA1HMAC", C.c_int, C.c_char_p, C.c_int, C.c_char_p, C.c_int, C.c_char_p, C.c_uint)
    out = C.create_string_buffer(32)
    assert sha(b"hello", 5, out, 31) == -1
    assert sha(b"hello", 5, out, 32) == 0 and out.raw == hashlib.sha256(b"hello").digest()
    assert mac(b"key", 3, b"data", 4, out, 19) == -1
    assert mac(b"key", 3, b"data", 4, out, 20) == 0
    assert out.raw[:20] == hmac.new(b"key", b"data", "sha1").digest()


@pytest.mark.parametrize("data", [b"", b"a", b"ab", b"abc", b"abcd", bytes(range(256))])
def test_base64_decode(g, data):
    dec = fn(g, "TUTK3rdBase64Decode", C.c_int, C.c_char_p, C.c_uint, C.POINTER(C.c_void_p))
    free = fn(g, "tutk_platform_free", None, C.c_void_p)
    text = base64.b64encode(data)
    out = C.c_void_p()
    n = dec(text, len(text), C.byref(out))
    assert n == len(data) and C.string_at(out.value, n) == data
    free(out)


def test_base64_decode_rejects(g):
    dec = fn(g, "TUTK3rdBase64Decode", C.c_int, C.c_char_p, C.c_uint, C.POINTER(C.c_void_p))
    out = C.c_void_p()
    assert dec(b"abc", 3, C.byref(out)) == TUTK_ER_INVALID_ARG
    assert dec(b"ab!d", 4, C.byref(out)) == TUTK_ER_INVALID_ARG
    assert dec(b"a=bc", 4, C.byref(out)) == TUTK_ER_INVALID_ARG


def test_ecdh_keypair(g):
    create = fn(g, "TUTK3rdECDHCreateKeyPair", C.c_void_p)
    free = fn(g, "TUTK3rdECDHKeysFree", None, C.c_void_p)
    k = create()
    assert k
    free(k)
    free(None)


# ---- JSON ----

def test_json(g):
    parse = fn(g, "TUTK_Json_Obj_Create_From_String", C.c_int, C.c_char_p, C.POINTER(C.c_void_p))
    release = fn(g, "TUTK_Json_Obj_Release", C.c_int, C.c_void_p)
    sub = fn(g, "TUTK_Json_Obj_Get_Sub_Obj", C.c_int, C.c_void_p, C.c_char_p, C.POINTER(C.c_void_p))
    get_str = fn(g, "TUTK_Json_Obj_Get_Sub_Obj_String", C.c_int, C.c_void_p, C.c_char_p,
                 C.POINTER(C.c_char_p))
    get_bool = fn(g, "TUTK_Json_Obj_Get_Sub_Obj_Bool", C.c_int, C.c_void_p, C.c_char_p, C.POINTER(C.c_int))

    root = C.c_void_p()
    doc = '{"realm": "r\\u00e9alm\\n", "flag": true, "n": null, "num": -1.5e2, "o": {"a": [1, "x"]}}'
    assert parse(doc.encode(), C.byref(root)) == 0
    s, b, o = C.c_char_p(), C.c_int(), C.c_void_p()
    assert get_str(root, b"realm", C.byref(s)) == 0 and s.value.decode() == "réalm\n"
    assert get_str(root, b"missing", C.byref(s)) == TJSON_ER_NO_MEMBER
    assert get_str(root, b"n", C.byref(s)) == TJSON_ER_INVALID_ARG
    assert get_bool(root, b"flag", C.byref(b)) == 0 and b.value == 1
    assert get_bool(root, b"realm", C.byref(b)) == TJSON_ER_WRONG_TYPE
    assert sub(root, b"o", C.byref(o)) == 0 and o.value
    assert sub(o, b"zz", C.byref(o)) == TJSON_ER_NO_MEMBER and not o.value
    assert release(root) == 0

    assert parse(b"[1, 2]", C.byref(root)) == 0
    assert get_str(root, b"a", C.byref(s)) == TJSON_ER_WRONG_TYPE
    assert release(root) == 0
    assert parse(b'"just a string"', C.byref(root)) == TJSON_ER_WRONG_TYPE
    for bad in (b"{", b'{"a":}', b"[1,]", b'{"a":1} x', b""):
        assert parse(bad, C.byref(root)) != 0 and not root.value
    assert release(None) == TJSON_ER_INVALID_ARG
