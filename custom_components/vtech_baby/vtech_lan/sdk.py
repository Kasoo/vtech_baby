"""Load and initialise the patched ThroughTek/TUTK Kalay SDK — once per process, never unloaded.

Authority: vtech-lan-kit/docs/85-ha-deployment.md (loading), 90-ha-runtime.md §1 (process-wide init),
10-transport-session.md (call signatures and struct layouts).

`load()` returns the process-wide `Sdk`. It exposes small Python wrappers over the SDK calls a session
needs, so `Session` can be driven by a fake with the same methods in tests. Every call blocks; never
call them on an event loop. ctypes releases the GIL during a foreign call.
"""
from __future__ import annotations

import ctypes as C
import os
import platform
import struct
import threading

from .errors import (
    AV_ER_ALREADY_INITIALIZED,
    IOTC_ER_ALREADY_INITIALIZED,
    SdkMismatch,
    Unlicensed,
    UnsupportedHost,
    check,
)

LIBS = ("libtk.so", "libTUTKGlobalAPIs.so", "libNebulaAPIs.so", "libIOTCAPIs.so", "libAVAPIs.so")
IOCTRL_BUF = 2048
# The camera answers LAN search on 32762; the SDK's built-in default (32761) goes unanswered, so without
# this only ThroughTek's master can find the camera (vtech-lan-kit/docs/00-prerequisites.md).
LAN_SEARCH_PORT = 32762

_EM = {62: "x86_64", 183: "aarch64"}
_HOST_ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


class AvIn(C.Structure):
    """avClientStartEx input, 64 bytes (10-transport-session.md)."""
    _fields_ = [("size", C.c_uint32), ("sid", C.c_uint32),
                ("ch", C.c_uint8), ("pad", C.c_uint8 * 3), ("timeout", C.c_uint32),
                ("account", C.c_char_p), ("password", C.c_char_p),
                ("resend", C.c_uint32), ("security_mode", C.c_uint32),
                ("auth_type", C.c_uint32), ("sync_recv_data", C.c_uint32),
                ("dtls_cipher_suites", C.c_char_p), ("r38", C.c_uint32), ("r3c", C.c_uint32)]


class AvOut(C.Structure):
    _fields_ = [(n, C.c_uint32) for n in ("size", "server_type", "resend", "two_way_streaming",
                                          "sync_recv_data", "security_mode", "r6", "r7")]


#: avServStart3's auth callback: int (*)(const char *account, char *password); return 1 = accept.
AUTH_FN = C.CFUNCTYPE(C.c_int, C.c_char_p, C.c_char_p)


def host_arch() -> str:
    arch = _HOST_ARCH.get(platform.machine().lower())
    if arch is None:
        raise UnsupportedHost(f"unsupported host architecture {platform.machine()!r}: only 64-bit x86_64 "
                              "and aarch64 are supported (32-bit armv7 / i386 installs are not)")
    return arch


def preflight(libdir: str) -> str:
    """Check the host can map this library set before dlopen fails opaquely. Returns the arch."""
    host = host_arch()
    path = os.path.join(libdir, "libAVAPIs.so")
    try:
        with open(path, "rb") as f:
            hdr = f.read(64)
            if hdr[:4] != b"\x7fELF" or hdr[4] != 2:
                raise UnsupportedHost(f"{path}: not an ELF64 library")
            (machine,) = struct.unpack_from("<H", hdr, 0x12)
            (phoff,) = struct.unpack_from("<Q", hdr, 0x20)
            phentsize, phnum = struct.unpack_from("<HH", hdr, 0x36)
            f.seek(phoff)
            ph = f.read(phentsize * phnum)
    except OSError as e:
        raise UnsupportedHost(f"cannot read {path}: {e}") from e
    lib = _EM.get(machine, f"e_machine={machine}")
    if lib != host:
        raise UnsupportedHost(f"library set in {libdir} is {lib} but this host is {host}")
    align = min(struct.unpack_from("<Q", ph, i * phentsize + 0x30)[0]
                for i in range(phnum) if struct.unpack_from("<I", ph, i * phentsize)[0] == 1)  # PT_LOAD
    page = os.sysconf("SC_PAGE_SIZE")
    if page > align:
        raise UnsupportedHost(f"kernel page size {page} exceeds the SDK libraries' segment alignment "
                              f"{align}: use a 4K- or 16K-page kernel")
    return host


def _bind(lib: C.CDLL, name: str, restype, *argtypes):
    f = getattr(lib, name)
    f.restype, f.argtypes = restype, list(argtypes)
    return f


class Sdk:
    """The loaded SDK. Construct through `load()`, never directly."""

    def __init__(self, libdir: str):
        self.libdir = libdir
        self.arch = preflight(libdir)
        mode = os.RTLD_NOW | os.RTLD_LOCAL
        # Companion first, then dependency order; DT_RUNPATH $ORIGIN resolves siblings on glibc and musl.
        h = {name: C.CDLL(os.path.join(libdir, name), mode=mode) for name in LIBS}
        G, IO, AV = h["libTUTKGlobalAPIs.so"], h["libIOTCAPIs.so"], h["libAVAPIs.so"]
        # The companion's LAN-only egress policy (companion/net.c). Bound so a companion without it
        # fails at load rather than letting the SDK reach ThroughTek's servers.
        self._lan_only_stats = _bind(h["libtk.so"], "tk_lan_only_stats", None,
                                     C.POINTER(C.c_ulong), C.POINTER(C.c_ulong))

        self._set_license = _bind(G, "TUTK_SDK_Set_License_Key", C.c_int, C.c_char_p)
        self._iotc_init = _bind(IO, "IOTC_Initialize2", C.c_int, C.c_uint32)
        self._set_search_port = _bind(IO, "IOTC_Set_LanSearchPort", C.c_int, C.c_int)
        self._av_init = _bind(AV, "avInitialize", C.c_int, C.c_int)
        self._get_sid = _bind(IO, "IOTC_Get_SessionID", C.c_int)
        self._connect = _bind(IO, "IOTC_Connect_ByUIDEx", C.c_int, C.c_char_p, C.c_int, C.c_void_p)
        self._session_close = _bind(IO, "IOTC_Session_Close", C.c_int, C.c_int)
        self._session_check = _bind(IO, "IOTC_Session_Check", C.c_int, C.c_int, C.c_void_p)
        self._client_start = _bind(AV, "avClientStartEx", C.c_int, C.POINTER(AvIn), C.POINTER(AvOut))
        self._client_stop = _bind(AV, "avClientStop", None, C.c_int)
        self._send_ioctrl = _bind(AV, "avSendIOCtrl", C.c_int, C.c_int, C.c_uint32, C.c_void_p, C.c_int)
        self._recv_ioctrl = _bind(AV, "avRecvIOCtrl", C.c_int, C.c_int, C.POINTER(C.c_uint32),
                                  C.c_void_p, C.c_int, C.c_uint32)
        # Media (Phase 5) and talk-back (Phase 6) calls, bound now so a missing symbol fails at load.
        self.recv_frame = _bind(AV, "avRecvFrameData2", C.c_int, C.c_int, C.c_char_p, C.c_int,
                                C.POINTER(C.c_int), C.POINTER(C.c_int), C.c_char_p, C.c_int,
                                C.POINTER(C.c_int), C.POINTER(C.c_int))
        self.recv_audio = _bind(AV, "avRecvAudioData", C.c_int, C.c_int, C.c_char_p, C.c_int,
                                C.c_char_p, C.c_int, C.POINTER(C.c_uint32))
        self._clean_video = _bind(AV, "avClientCleanVideoBuf", C.c_int, C.c_int)
        self._clean_audio = _bind(AV, "avClientCleanAudioBuf", C.c_int, C.c_int)
        self.free_channel = _bind(IO, "IOTC_Session_Get_Free_Channel", C.c_int, C.c_int)
        self.channel_off = _bind(IO, "IOTC_Session_Channel_OFF", C.c_int, C.c_int, C.c_ubyte)
        self.serv_start3 = _bind(AV, "avServStart3", C.c_int, C.c_int, AUTH_FN, C.c_uint, C.c_uint,
                                 C.c_ubyte, C.POINTER(C.c_int))
        self.serv_stop = _bind(AV, "avServStop", None, C.c_int)
        self.serv_exit = _bind(AV, "avServExit", None, C.c_int, C.c_ubyte)
        self.serv_reset = _bind(AV, "avServResetBuffer", C.c_int, C.c_int, C.c_int, C.c_uint)
        self.send_audio = _bind(AV, "avSendAudioData", C.c_int, C.c_int, C.c_char_p, C.c_int,
                                C.c_char_p, C.c_int)
        self.license_key: str | None = None
        self.max_channels = 0

    def _initialise(self, license_key: str, max_channels: int) -> None:
        rc = self._set_license(license_key.encode())
        if rc < 0:
            raise Unlicensed(f"TUTK_SDK_Set_License_Key failed rc={rc}", call="TUTK_SDK_Set_License_Key",
                             code=rc)
        rc = self._iotc_init(0)
        if rc != IOTC_ER_ALREADY_INITIALIZED:
            check(rc, "IOTC_Initialize2")
        check(self._set_search_port(LAN_SEARCH_PORT), "IOTC_Set_LanSearchPort")
        rc = self._av_init(max_channels)
        if rc != AV_ER_ALREADY_INITIALIZED:
            check(rc, "avInitialize")
        self.license_key, self.max_channels = license_key, max_channels

    # ── Session calls (the surface Session uses; mirrored by the test fake) ──
    def get_session_id(self) -> int:
        return self._get_sid()

    def connect(self, uid: str, sid: int, authkey: str, timeout_s: int) -> int:
        """IOTC_Connect_ByUIDEx with the 160-byte input struct. Returns the rc (>= 0 on success)."""
        key = authkey.encode()
        if len(key) > 0x94 - 0x08 - 1:
            raise ValueError("authKey too long")
        inbuf = (C.c_ubyte * 200)()                     # struct is 160 bytes; spare zeroed tail
        struct.pack_into("<II", inbuf, 0x00, 0xA0, 0)   # size, auth type
        inbuf[0x08:0x08 + len(key)] = key
        struct.pack_into("<II", inbuf, 0x94, timeout_s, 0)
        return self._connect(uid.encode(), sid, C.cast(inbuf, C.c_void_p))

    def client_start(self, sid: int, account: str, password: str, timeout_s: int) -> int:
        """avClientStartEx, DTLS + password auth on channel 0. Returns the AV index (or rc < 0)."""
        acct, pwd = account.encode(), password.encode()     # kept alive for the call
        ai = AvIn(size=C.sizeof(AvIn), sid=sid, ch=0, timeout=timeout_s, account=acct, password=pwd,
                  resend=1, security_mode=1, auth_type=0)
        ao = AvOut(size=C.sizeof(AvOut))
        return self._client_start(C.byref(ai), C.byref(ao))

    def client_stop(self, av: int) -> None:
        self._client_stop(av)

    def session_close(self, sid: int) -> None:
        self._session_close(sid)

    # ── Media (Phase 5; media.py) ──
    def clean_media_buffers(self, av: int) -> None:
        """Drop buffered frames before a START, so the first frame is a keyframe (kit 40)."""
        self._clean_video(av)
        self._clean_audio(av)

    def frame_reader(self, av: int) -> "FrameReader":
        return FrameReader(self, av)

    def lan_only_stats(self) -> dict[str, int]:
        """Off-link sends/connects and name lookups the companion has refused since load."""
        sends, lookups = C.c_ulong(), C.c_ulong()
        self._lan_only_stats(C.byref(sends), C.byref(lookups))
        return {"refused_sends": sends.value, "refused_lookups": lookups.value}

    def session_mode(self, sid: int) -> int:
        """IOTC_Session_Check st_SInfo byte 0: 0 = P2P, 1 = relay, 2 = LAN (rc < 0 on error).
        The struct is ~64 bytes; 256 covers the larger SDK variants."""
        info = C.create_string_buffer(256)
        rc = self._session_check(sid, C.cast(info, C.c_void_p))
        return rc if rc < 0 else info.raw[0]

    def send_ioctrl(self, av: int, code: int, payload: bytes) -> int:
        buf = C.create_string_buffer(payload, max(len(payload), 1))
        return self._send_ioctrl(av, code, C.cast(buf, C.c_void_p), len(payload))

    def recv_ioctrl(self, av: int, timeout_ms: int) -> tuple[int, int, bytes]:
        """(rc, opcode, payload). rc >= 0 is the payload length; negative is an SDK error code."""
        typ = C.c_uint32(0)
        buf = C.create_string_buffer(IOCTRL_BUF)
        rc = self._recv_ioctrl(av, C.byref(typ), C.cast(buf, C.c_void_p), IOCTRL_BUF, timeout_ms)
        return rc, typ.value, buf.raw[:rc] if rc > 0 else b""


class FrameReader:
    """Non-blocking reads of the AV channel's video and audio frames into buffers reused per call.
    One reader per media thread; `video()` / `audio()` return (rc, encrypted frame): rc > 0 is the
    frame length, otherwise an SDK code (NO_DATA, BAD_FRAME or a real error)."""

    VIDEO_MAX, AUDIO_MAX, INFO = 1 << 21, 1 << 16, 64

    def __init__(self, sdk: Sdk, av: int):
        self._sdk, self._av = sdk, av
        self._vbuf = C.create_string_buffer(self.VIDEO_MAX)
        self._abuf = C.create_string_buffer(self.AUDIO_MAX)
        self._info = C.create_string_buffer(self.INFO)
        self._ints = [C.c_int() for _ in range(4)]
        self._aidx = C.c_uint32()

    def video(self) -> tuple[int, bytes]:
        actual, expected, info_len, idx = self._ints
        rc = self._sdk.recv_frame(self._av, self._vbuf, self.VIDEO_MAX, C.byref(actual), C.byref(expected),
                                  self._info, self.INFO, C.byref(info_len), C.byref(idx))
        return rc, self._vbuf.raw[:rc] if rc > 0 else b""

    def audio(self) -> tuple[int, bytes]:
        rc = self._sdk.recv_audio(self._av, self._abuf, self.AUDIO_MAX, self._info, self.INFO,
                                  C.byref(self._aidx))
        return rc, self._abuf.raw[:rc] if rc > 0 else b""


# App-wide TUTK license key (TUTK_SDK_Set_License_Key), embedded per the kit
# (vtech-lan-kit/docs/00-prerequisites.md; same value as the reference client's DEFAULT_LICENSE_KEY).
DEFAULT_LICENSE_KEY = ("AQAAAKLYFdlSXIPi3g3PGW+mwX+kQW1343RIBJstfyQuKZSJTOU5sxwi2J67GO31RCngqq8Q"
                       "/6zl6WkQn7hfwUZEP0hbj3spI06VDkAg7AB6MS0SG+0zcieGkORtSpkxxVf+S8TflIpgEldZ"
                       "ykhsLKR+plBToQ9/3CFtgXS/n70OhxyHakx6pXGuvM9xfOsrJnSrBqLdSyHim13rWGvrws4SpZ8b")

_lock = threading.Lock()
_sdk: Sdk | None = None


def load(libdir: str, license_key: str = DEFAULT_LICENSE_KEY, max_channels: int = 16) -> Sdk:
    """The process-wide SDK: loaded and initialised on the first call, returned as-is afterwards.

    A later call with a different library directory or license raises SdkMismatch: one process can
    only hold one SDK, and it is never unloaded (a re-patched set needs a process restart).
    """
    global _sdk
    libdir = os.path.realpath(libdir)
    with _lock:
        if _sdk is not None:
            if _sdk.libdir != libdir or _sdk.license_key != license_key:
                raise SdkMismatch(f"SDK already loaded from {_sdk.libdir} with another license; "
                                  "restart the process to switch")
            return _sdk
        sdk = Sdk(libdir)
        sdk._initialise(license_key, max_channels)
        _sdk = sdk
        return sdk


def loaded() -> Sdk | None:
    return _sdk
