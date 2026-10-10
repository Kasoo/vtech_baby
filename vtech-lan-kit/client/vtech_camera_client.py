#!/usr/bin/env python3
"""
vtech_camera_client.py — standalone LAN-direct encrypted-video client for the VTech RM5754HD.

Single, self-contained module. It:
  1. loads the ThroughTek/TUTK Kalay SDK libraries + the companion in-process via ctypes;
  2. connects and starts an authenticated AV session;
  3. runs the live-view 0x820/0x821 negotiation with a client X25519 keypair;
  4. derives the session master key and decrypts each avRecvFrameData2 frame to H.264;
  5. (optionally) starts audio (0x300) and decrypts each avRecvAudioData frame — G.711 A-law,
     8 kHz mono — with the same per-frame crypto, and muxes it alongside the video;
  6. (optionally, --talk) plays a WAV file through the camera speaker (talk-back): the client serves an
     AV channel the camera connects to, and sends G.711 A-law frames encrypted with the same scheme.

Audio: codec_id 0x8a = G.711 A-law, 8 kHz mono (decrypted silence = 0xD5, the A-law zero code);
40 ms / 320-byte frames at ~25 fps.
Each audio frame uses the same wire layout as video and decrypts through the identical decrypt_frame()
path.

Everything above the TUTK transport is implemented here in Python: X25519, the 0x820/0x821 handshake,
and the frame_key unwrap + ChaCha20-Poly1305 decrypt. The P2P/DTLS transport is provided by the vendor
SDK libraries.

Scheme (per frame, wire layout [wrapped:32][nonce:12][tag:16][body:N]):
    S      = X25519(n, cam_pubkey@12)                       # n = client ephemeral scalar
    master = HMAC-SHA256(key = salt@44, msg = S)            # key-wrap key, per session
    frame_key = wrapped XOR ChaCha20-IETF-keystream(master, nonce, counter=0)
    plaintext = ChaCha20Poly1305-IETF-decrypt(frame_key, nonce, body || tag)

Env:  VTECH_LIBDIR  (dir with the SDK .so set + companion libtk.so; license embedded, LICENSE_KEY.txt optional override)
      VTECH_UID VTECH_PWD VTECH_AUTHKEY   (required)   VTECH_ACCOUNT (default "admin")
CLI:  --out FILE  --seconds N  --channel N  [--skip-round1] [--dump-raw FILE] [--penc FLAG]
      --talk WAV   play a WAV through the camera speaker, then exit (no video)
Viewing (optional, need ffmpeg/ffplay on this host):
      --rtp HOST [--rtp-port P] [--sdp F]   live RTP to VLC (prints the exact `vlc` command)
      --mp4 FILE   record a playable MP4      --hls DIR   live HLS over HTTP
      --play       live ffplay window         --snapshot PNG   first keyframe to an image
      --audio-out FILE   raw decrypted G.711 A-law (8 kHz mono); play: ffplay -f alaw -ar 8000 -ac 1 -i F
      --no-audio         video only (skip 0x300 + audio decrypt)
Audio (G.711 A-law → AAC) is muxed into --mp4/--hls/--play. --rtp is video-only; for live audio+video
use --hls (open the .m3u8 over HTTP), or --mp4 to record a combined file.

Must run on a host sharing L2 (the camera's LAN segment) with the camera (connect is by-UID, no
broadcast needed). The patched x86_64 lib set loads on both glibc and musl.

Opcode framework: every IOCtrl sent is an `Op` object (numeric code + name + argument encoder +
optional response decoder), one per opcode. `Op.send(t, av, *args)` fires a control/SET;
`Op.request(t, av, *args)` sends a GET and returns the decoded reply as a dict (e.g.
`GET_NIGHT_LIGHT.request(t, av)` -> {"level": 0}). Opcodes that carry a symbolic ThroughTek/VTech
constant name use it; opcodes with no published constant are given a descriptive name by this module.
The minimal stream bring-up is AUDIOSTART (if audio) -> VIDEO_FULLRATE -> START; every other opcode
in the connect burst is an optional GET.
"""
import argparse
import ctypes as C
import hashlib
import hmac
import os
import platform
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

# ─────────────────────────────────────────────────────────────────────────────
# §1  Crypto — self-contained
# ─────────────────────────────────────────────────────────────────────────────
def x25519_keypair():
    """Fresh ephemeral X25519 keypair; returns (priv_obj, 32-byte raw public key)."""
    k = X25519PrivateKey.generate()
    return k, k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def x25519_shared(priv, peer_pub32):
    return priv.exchange(X25519PublicKey.from_public_bytes(peer_pub32))


def session_master(salt, S):
    """master = HMAC-SHA256(salt, S) — the per-session key-wrap key."""
    return hmac.new(salt, S, hashlib.sha256).digest()


def _chacha20_ietf_keystream(key, nonce12, n):
    # cryptography's ChaCha20 takes a 16-byte nonce = 4-byte LE counter (0) || 12-byte IETF nonce.
    full = (0).to_bytes(4, "little") + nonce12
    return Cipher(algorithms.ChaCha20(key, full), mode=None).encryptor().update(b"\x00" * n)


def unwrap_frame_key(wrapped_key, nonce, master):
    ks = _chacha20_ietf_keystream(master, nonce, len(wrapped_key))
    return bytes(a ^ b for a, b in zip(wrapped_key, ks))


def decrypt_frame(frame, master):
    """One wire frame [wrapped:32][nonce:12][tag:16][body] -> plaintext (raises on bad tag)."""
    wrapped_key, nonce, tag, body = frame[0:32], frame[32:44], frame[44:60], frame[60:]
    frame_key = unwrap_frame_key(wrapped_key, nonce, master)
    return ChaCha20Poly1305(frame_key).decrypt(nonce, body + tag, None)


def encrypt_frame(plain, master):
    """Inverse of decrypt_frame() for the talk-back uplink: a fresh random frame_key + nonce per frame,
    ChaCha20-Poly1305-IETF (no AAD), frame_key wrapped with the session master.
    Returns [wrapped:32][nonce:12][tag:16][body]."""
    frame_key, nonce = os.urandom(32), os.urandom(12)
    ct = ChaCha20Poly1305(frame_key).encrypt(nonce, plain, None)          # body || tag
    ks = _chacha20_ietf_keystream(master, nonce, 32)
    return bytes(a ^ b for a, b in zip(frame_key, ks)) + nonce + ct[-16:] + ct[:-16]


# ─────────────────────────────────────────────────────────────────────────────
# §2  ctypes bindings — load the patched TUTK libs + companion in-process
#     (dependency order + struct layouts)
# ─────────────────────────────────────────────────────────────────────────────
class AvIn(C.Structure):
    _fields_ = [("size", C.c_uint32), ("sid", C.c_uint32),
                ("ch", C.c_uint8), ("p", C.c_uint8 * 3), ("tmo", C.c_uint32),
                ("acct", C.c_char_p), ("pwd", C.c_char_p),
                ("resend", C.c_uint32), ("sec", C.c_uint32),
                ("auth", C.c_uint32), ("sync", C.c_uint32),
                ("dtls", C.c_char_p), ("n38", C.c_uint32), ("n3c", C.c_uint32)]


class AvOut(C.Structure):
    _fields_ = [(n, C.c_uint32) for n in ("size", "st", "rs", "tw", "sy", "sec", "x6", "x7")]


# avServStart3's auth callback: int (*)(const char *account, char *password), return 1 = accept.
AUTH_FN = C.CFUNCTYPE(C.c_int, C.c_char_p, C.c_char_p)


_EM = {62: "x86_64", 183: "aarch64"}
_HOST_ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


def preflight(libdir):
    """Fail early, with an actionable message, where dlopen would fail opaquely (or not at all):
    unsupported host arch, library set built for the other arch, or a kernel page size larger than the
    SDK libraries' segment alignment (the aarch64 SDK libs are 16K-aligned: no 64K-page kernels)."""
    host = _HOST_ARCH.get(platform.machine().lower())
    if host is None:
        raise RuntimeError(f"unsupported host architecture {platform.machine()!r}: only 64-bit x86_64 and "
                           "aarch64 are supported (32-bit armv7 / i386 installs are not)")
    path = os.path.join(libdir, "libAVAPIs.so")
    with open(path, "rb") as f:
        hdr = f.read(64)
        if hdr[:4] != b"\x7fELF" or hdr[4] != 2:
            raise RuntimeError(f"{path}: not an ELF64 library")
        (machine,) = struct.unpack_from("<H", hdr, 0x12)
        phoff, = struct.unpack_from("<Q", hdr, 0x20)
        phentsize, phnum = struct.unpack_from("<HH", hdr, 0x36)
        f.seek(phoff)
        ph = f.read(phentsize * phnum)
    lib = _EM.get(machine, f"e_machine={machine}")
    if lib != host:
        raise RuntimeError(f"library set in {libdir} is {lib} but this host is {host}: "
                           f"re-run tooling/patch.sh with the {host} SDK directory")
    align = min(struct.unpack_from("<Q", ph, i * phentsize + 0x30)[0]
                for i in range(phnum) if struct.unpack_from("<I", ph, i * phentsize)[0] == 1)   # PT_LOAD
    page = os.sysconf("SC_PAGE_SIZE")
    if page > align:
        raise RuntimeError(f"kernel page size {page} exceeds the SDK libraries' segment alignment {align}: "
                           "this kernel cannot map them (use a 4K- or 16K-page kernel)")
    return host


class TUTK:
    """Loads the four patched libs + companion (RTLD_LOCAL) and binds the API we use."""

    def __init__(self, libdir):
        self.libdir = libdir
        self.arch = preflight(libdir)
        mode = os.RTLD_NOW | os.RTLD_LOCAL
        load = lambda base: C.CDLL(os.path.join(libdir, base), mode=mode)
        # companion first (no TUTK deps), then patched libs in dependency order so each
        # DT_NEEDED (libtk.so + sibling TUTK sonames) resolves to an already-loaded object.
        load("libtk.so")
        self.G = load("libTUTKGlobalAPIs.so")
        load("libNebulaAPIs.so")
        self.IO = load("libIOTCAPIs.so")
        self.AV = load("libAVAPIs.so")

        fn = self._fn
        self.set_license = fn(self.G, "TUTK_SDK_Set_License_Key", C.c_int, C.c_char_p)
        self.iotc_init   = fn(self.IO, "IOTC_Initialize2", C.c_int, C.c_uint32)
        self.set_search_port = fn(self.IO, "IOTC_Set_LanSearchPort", C.c_int, C.c_int)
        self.av_init     = fn(self.AV, "avInitialize", C.c_int, C.c_int)
        self.get_sid     = fn(self.IO, "IOTC_Get_SessionID", C.c_int)
        self.connect     = fn(self.IO, "IOTC_Connect_ByUIDEx", C.c_int, C.c_char_p, C.c_int, C.c_void_p)
        self.av_start    = fn(self.AV, "avClientStartEx", C.c_int, C.POINTER(AvIn), C.POINTER(AvOut))
        self.send_ioctrl = fn(self.AV, "avSendIOCtrl", C.c_int, C.c_int, C.c_uint32, C.c_void_p, C.c_int)
        self.recv_ioctrl = fn(self.AV, "avRecvIOCtrl", C.c_int, C.c_int,
                              C.POINTER(C.c_uint32), C.c_void_p, C.c_int, C.c_uint32)
        # avRecvFrameData2(avIndex, buf, bufMax, *actual, *expected, finfo, finfoMax, *actinfo, *fidx)
        self.recv_frame  = fn(self.AV, "avRecvFrameData2", C.c_int, C.c_int, C.c_char_p, C.c_int,
                              C.POINTER(C.c_int), C.POINTER(C.c_int), C.c_char_p, C.c_int,
                              C.POINTER(C.c_int), C.POINTER(C.c_int))
        # avRecvAudioData(avIndex, buf, bufMax, frameInfo, frameInfoMax, *frameIdx)
        self.recv_audio  = fn(self.AV, "avRecvAudioData", C.c_int, C.c_int, C.c_char_p, C.c_int,
                              C.c_char_p, C.c_int, C.POINTER(C.c_uint32))
        # talk-back (the client is the AV *server* on a second IOTC channel of the session)
        self.free_channel = fn(self.IO, "IOTC_Session_Get_Free_Channel", C.c_int, C.c_int)
        self.channel_off  = fn(self.IO, "IOTC_Session_Channel_OFF", C.c_int, C.c_int, C.c_ubyte)
        # native signature: avServStart3(sid, authFn, timeout_s, servType, ch, int *resend)
        self.serv_start3  = fn(self.AV, "avServStart3", C.c_int, C.c_int, AUTH_FN, C.c_uint, C.c_uint,
                               C.c_ubyte, C.POINTER(C.c_int))
        self.serv_stop    = fn(self.AV, "avServStop", None, C.c_int)
        self.serv_exit    = fn(self.AV, "avServExit", None, C.c_int, C.c_ubyte)
        self.serv_reset   = fn(self.AV, "avServResetBuffer", C.c_int, C.c_int, C.c_int, C.c_uint)
        # avSendAudioData(avIndex, data, size, frameInfo, frameInfoSize)
        self.send_audio   = fn(self.AV, "avSendAudioData", C.c_int, C.c_int, C.c_char_p, C.c_int,
                               C.c_char_p, C.c_int)
        self.session_id = None               # set by open_av_session(); talk-back needs it
        # optional: partial-encryption toggle (our decrypt is app-layer; usually leave OFF)
        try:
            self.set_penc = self._fn(self.IO, "IOTC_Set_Partial_Encryption", C.c_int, C.c_int, C.c_int)
        except AttributeError:
            self.set_penc = None

    @staticmethod
    def _fn(lib, name, restype, *argtypes):
        f = getattr(lib, name)
        f.restype, f.argtypes = restype, list(argtypes)
        return f


# ─────────────────────────────────────────────────────────────────────────────
# §3  Session bring-up — connect + authenticated AV session
# ─────────────────────────────────────────────────────────────────────────────
# App-wide SDK license key (TUTK_SDK_Set_License_Key). Same for every camera of this app
# family; a small static value — a pure numeric fact, shareable under interoperability
# exceptions (see docs/00-prerequisites.md) — so it is embedded here rather than user-supplied.
DEFAULT_LICENSE_KEY = ("AQAAAKLYFdlSXIPi3g3PGW+mwX+kQW1343RIBJstfyQuKZSJTOU5sxwi2J67GO31RCngqq8Q"
                       "/6zl6WkQn7hfwUZEP0hbj3spI06VDkAg7AB6MS0SG+0zcieGkORtSpkxxVf+S8TflIpgEldZ"
                       "ykhsLKR+plBToQ9/3CFtgXS/n70OhxyHakx6pXGuvM9xfOsrJnSrBqLdSyHim13rWGvrws4SpZ8b")


def _read_license(libdir):
    # A LICENSE_KEY.txt in the library directory overrides the embedded default when present.
    try:
        with open(os.path.join(libdir, "LICENSE_KEY.txt")) as f:
            for ln in f:
                if "license_key=" in ln:
                    return ln.split("license_key=", 1)[1].strip()
    except OSError:
        pass
    return DEFAULT_LICENSE_KEY


def open_av_session(t, uid, pwd, authkey, account, channel, penc=None):
    """Returns the avClientStartEx channel index, or raises RuntimeError."""
    rc = t.set_license(_read_license(t.libdir).encode())
    log(f"Set_License_Key = {rc}")
    log(f"IOTC_Initialize2(0) = {t.iotc_init(0)}")
    log(f"IOTC_Set_LanSearchPort(32762) = {t.set_search_port(32762)}")   # camera's LAN-search port
    log(f"avInitialize(16) = {t.av_init(16)}")
    sid = t.get_sid()
    log(f"IOTC_Get_SessionID = {sid}")

    inbuf = (C.c_ubyte * 200)()
    struct.pack_into("<I", inbuf, 0x00, 160)          # struct size
    struct.pack_into("<I", inbuf, 0x04, 0)            # auth type
    inbuf[0x08:0x08 + len(authkey)] = authkey         # authKey (enhanced security)
    struct.pack_into("<I", inbuf, 0x94, 15)           # connect timeout (s)
    struct.pack_into("<I", inbuf, 0x98, 0)            # NEWENUM tail
    log(f"connecting to UID={uid.decode()} (needs camera on same L2/br-lan)...")
    ses = t.connect(uid, sid, C.cast(inbuf, C.c_void_p))
    log(f"IOTC_Connect_ByUIDEx = {ses}")
    if ses < 0:
        raise RuntimeError(f"IOTC_Connect_ByUIDEx failed rc={ses} (camera unreachable / wrong authKey?)")

    if penc is not None and t.set_penc:
        log(f"IOTC_Set_Partial_Encryption(ses, {penc}) = {t.set_penc(ses, penc)}")

    ai = AvIn(size=C.sizeof(AvIn), sid=ses, ch=channel, tmo=30,
              acct=account, pwd=pwd, resend=1, sec=1, auth=0)
    ao = AvOut(size=C.sizeof(AvOut))
    av = t.av_start(C.byref(ai), C.byref(ao))
    log(f"avClientStartEx = {av} (serv={ao.st} sec={ao.sec})")
    if av < 0:
        raise RuntimeError(f"avClientStartEx failed rc={av}")
    t.session_id = ses
    return av


# ─────────────────────────────────────────────────────────────────────────────
# §4  Live-view handshake — real X25519 keypair → session master
# ─────────────────────────────────────────────────────────────────────────────
def _send(t, av, op, payload=b""):
    buf = C.create_string_buffer(payload, len(payload)) if payload else C.create_string_buffer(1)
    return t.send_ioctrl(av, op, C.cast(buf, C.c_void_p), len(payload))


def _recv_until(t, av, want_type, tries=14, timeout_ms=500, bufsz=512):
    """Poll avRecvIOCtrl until an IOCtrl of type want_type arrives; return its bytes or None."""
    for _ in range(tries):
        typ = C.c_uint32(0)
        b = C.create_string_buffer(bufsz)
        g = t.recv_ioctrl(av, C.byref(typ), C.cast(b, C.c_void_p), bufsz, timeout_ms)
        if g > 0:
            log(f"  <- IOCtrl type=0x{typ.value:x} len={g}")
            if typ.value == want_type:
                return b.raw[:g]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# §4.1  Opcode framework — one object per TUTK IOCtrl.
#   Each Op bundles: the numeric opcode, a name, an argument ENCODER
#   (args -> request payload bytes), and, for opcodes that return a reply, the
#   response opcode + a DECODER (reply bytes -> dict of named fields).
#
#   NAMING: opcodes with a symbolic ThroughTek/VTech SDK constant use that
#   constant name (IOTYPE_...). Opcodes with no published SDK constant are given
#   a descriptive name by this module.
# ─────────────────────────────────────────────────────────────────────────────
def _enc_channel(ch=0):
    """SMsgAVIoctrlAVStream / GetStreamCtrl / GetAudioOutFormat: 8B, int channel@0."""
    return struct.pack("<i", ch) + b"\0" * 4


def _enc_i32(v=0):
    """VTech GET request arg: 4B little-endian int (0 = channel/reserved)."""
    return struct.pack("<i", v)


def _enc_reserved4(_=0):
    return b"\0" * 4                               # GETSUPPORTSTREAM: reserved[4]


def _enc_toggle(on=0):
    """8B, int@0 = 0/1 (the VIDEO_FULLRATE selector; 0 selects the full-rate stream)."""
    return struct.pack("<i", 1 if on else 0) + b"\0" * 4


def _dec_night_light(r):
    return {"level": r[0]} if r else {}


def _dec_audio_format(r):
    ch, fmt = struct.unpack_from("<ii", r, 0)
    d = {"channel": ch, "format": fmt}             # format = codec id (0x8a = G.711 A-law)
    if len(r) >= 12:                               # talk-back uplink parameters
        d.update(sample_rate_idx=r[8], data_bits=r[9], channels=r[10], serv_channel=r[11])
    return d


def _dec_stream_ctrl(r):
    return {"channel": struct.unpack_from("<i", r, 0)[0], "quality": r[4]}  # quality = resolution


def _dec_motion_detect(r):
    ch, sens = struct.unpack_from("<ii", r, 0)
    return {"channel": ch, "sensitivity": sens}


def _dec_temp_detect(r):
    e, mx, mn = struct.unpack_from("<iii", r, 0)
    return {"enable": e, "max_c": mx / 10.0, "min_c": mn / 10.0}


def _dec_motion_track(r):
    return {"sensitivity": struct.unpack_from("<i", r, 0)[0]} if len(r) >= 4 else {}


def _dec_sound(r):
    return {"sound_detect": r[0]} if r else {}


def _dec_params(r):
    if len(r) < 36:
        return {"raw": r.hex()}
    w = struct.unpack_from("<9i", r, 0)
    # @24 (w[6]) = the motion sensitivity value (same value space as GETMOTIONDETECT /
    # GET_MOTION_TRACKING: 0/25/50/75), not a 0/1 flag.
    # @16 (w[4]) = sound-detect sensitivity {1 low, 2 med, 3 high}; read-only (0x731 ignores this field).
    # @28 (w[7]) is opaque: echo it back unchanged as 0x731 word5.
    return {"temp_c": w[0], "lullaby_track": w[1], "temp_f": w[2],
            "auto_off_s": w[3], "sound_sens": w[4], "volume": w[5], "motion_sens": w[6], "p28": w[7]}


def _dec_devinfo(r):
    # 0x331, 56B: model[16]@0, vendor[16]@16,
    # fw version 4 bytes @32 (little-endian digits: r[35].r[34].r[33].r[32], e.g. "4.2.5.9").
    model  = r[0:16].split(b"\0", 1)[0].decode("latin1", "replace")
    vendor = r[16:32].split(b"\0", 1)[0].decode("latin1", "replace")
    fw = ".".join(str(b) for b in reversed(r[32:36])) if len(r) >= 36 else None
    return {"model": model, "vendor": vendor, "fw": fw, "tail": r[36:].hex()}


def _dec_daynight_mode(r):
    # 0x714: int@0 reserved, int@4 = day/night MODE
    # {0 Auto, 1 On/forced-IR, 2 Off} — the GET counterpart to the 0x715 SET.
    m = struct.unpack_from("<i", r, 4)[0] if len(r) >= 8 else None
    return {"mode": m, "mode_name": {0: "Auto", 1: "On(forced-IR)", 2: "Off"}.get(m, "?")}


class Op:
    """A single TUTK IOCtrl opcode with its argument encoder and response decoder.

    enc(*args) -> request payload bytes.  resp = reply opcode (None = fire-and-forget).
    dec(reply_bytes) -> dict of named fields (None = return raw bytes)."""
    REGISTRY = {}

    def __init__(self, code, name, enc=_enc_channel, resp=None, dec=None, doc=""):
        self.code, self.name, self.enc = code, name, enc
        self.resp, self.dec, self.doc = resp, dec, doc
        Op.REGISTRY[code] = self

    def send(self, t, av, *args):
        """Encode the arguments and fire the opcode (no wait). Returns avSendIOCtrl rc."""
        payload = self.enc(*args)
        log(f"  -> {self.name} (0x{self.code:x}) len={len(payload)}")
        return _send(t, av, self.code, payload)

    def request(self, t, av, *args, tries=14, timeout_ms=500):
        """Send, wait for self.resp, and return the decoded dict (raw bytes if no dec, None on timeout)."""
        if self.resp is None:
            raise ValueError(f"{self.name} is fire-and-forget; use .send()")
        self.send(t, av, *args)
        raw = _recv_until(t, av, self.resp, tries=tries, timeout_ms=timeout_ms)
        if raw is None:
            return None
        return self.dec(raw) if self.dec else raw

    def __repr__(self):
        return f"<Op {self.name} 0x{self.code:x}>"


# ── Standard TUTK IPCAM opcodes (ThroughTek SDK constant names) ──────────────
START            = Op(0x1ff, "IOTYPE_USER_IPCAM_START",               _enc_channel, doc="live-view start")
STOP             = Op(0x2ff, "IOTYPE_USER_IPCAM_STOP",                _enc_channel)
AUDIOSTART       = Op(0x300, "IOTYPE_USER_IPCAM_AUDIOSTART",          _enc_channel, doc="camera begins audio frames")
AUDIOSTOP        = Op(0x301, "IOTYPE_USER_IPCAM_AUDIOSTOP",           _enc_channel)
GETSTREAMCTRL    = Op(0x322, "IOTYPE_USER_IPCAM_GETSTREAMCTRL_REQ",   _enc_channel, resp=0x323, dec=_dec_stream_ctrl,
                      doc="quality byte = current resolution (2 = 1080p, 1 = 720p)")
SETSTREAMCTRL    = Op(0x320, "IOTYPE_USER_IPCAM_SETSTREAMCTRL_REQ",
                      enc=lambda quality=2, ch=0: struct.pack("<i", ch) + bytes([quality & 0xFF, 0, 0, 0]),
                      resp=0x321,
                      doc="RESOLUTION selector. quality=2 -> 1920x1080, lower -> 720p. Persists "
                          "camera-side across sessions. Send before START to select the resolution.")
GETMOTIONDETECT  = Op(0x326, "IOTYPE_USER_IPCAM_GETMOTIONDETECT_REQ", _enc_channel, resp=0x327, dec=_dec_motion_detect,
                      doc="standard TUTK motion-detect (distinct from the VTech 0x7a4 family; SET=0x324)")
GETSUPPORTSTREAM = Op(0x328, "IOTYPE_USER_IPCAM_GETSUPPORTSTREAM_REQ", _enc_reserved4, resp=0x329,
                      doc="standard TUTK support-stream query. NOT serviced on the RM5754HD "
                          "(no 0x329 reply); resolutions are not enumerable this way.")
GETAUDIOFORMAT   = Op(0x32a, "IOTYPE_USER_IPCAM_GETAUDIOOUTFORMAT_REQ", _enc_channel, resp=0x32b, dec=_dec_audio_format,
                      doc="format = audio codec id (0x8a = G.711 A-law)")
SPEAKERSTART     = Op(0x350, "IOTYPE_USER_IPCAM_SPEAKERSTART",        _enc_channel,
                      doc="talk-back start: int@0 = the client's AV-server IOTC channel; no reply")
SPEAKERSTOP      = Op(0x351, "IOTYPE_USER_IPCAM_SPEAKERSTOP",         _enc_channel,
                      doc="talk-back stop: int@0 = the same channel")
SND_DATA_DELAY   = Op(0x0ff, "IOTYPE_INNER_SND_DATA_DELAY", _enc_reserved4,
                      doc="first IOCtrl on every new AV channel in the vendor app's traffic; payload int32 0; "
                          "no reply. SDK 4.3.8.1 refuses it from application code (AV_ER_NOT_SUPPORT, "
                          "-20033) and nothing depends on it; send() here ignores that rc.")
PRE_INIT         = SND_DATA_DELAY          # former name, kept for existing callers
DEVINFO          = Op(0x330, "IOTYPE_USER_IPCAM_DEVINFO_REQ",
                      enc=lambda: bytes([0, 1, 0x12, 0x34]) + b"\0" * 16, resp=0x331, dec=_dec_devinfo,
                      doc="20B header {0,1,..}; sending this once is required before the VTech GETs reply")
GET_TIMEZONE     = Op(0x3a0, "IOTYPE_USER_IPCAM_GET_TIMEZONE_REQ", enc=lambda: b"\0" * 268, resp=0x3a1)

# ── VTech vendor opcodes (ThroughTek/VTech SDK constant names) ───────────────
GET_PARAMS          = Op(0x733, "IOTYPE_VTECH_RM_SERIES_GET_PARAMS_REQ",  _enc_i32, resp=0x734, dec=_dec_params,
                         doc="temp/lullaby/volume/motion-tracking params (SET=0x731)")
GET_NIGHT_LIGHT     = Op(0x7a0, "IOTYPE_VTECH_RM_SERIES_GET_NIGHT_LIGHT_REQ", _enc_i32, resp=0x7a1, dec=_dec_night_light,
                         doc="byte0 level; LED honours 0-3 = off/yellow/orange/red (SET=0x7a2)")
GET_MOTION_TRACKING = Op(0x7a4, "IOTYPE_VTECH_GET_MOTION_TRACKING_REQ",   _enc_i32, resp=0x7a5, dec=_dec_motion_track,
                         doc="word0 sensitivity 0/25/50/75 = off/low/med/high (SET=0x7a6)")
GET_SOUND_SETTING   = Op(0x7b0, "IOTYPE_VTECH_GET_SOUND_SETTING_REQ",     _enc_i32, resp=0x7b1, dec=_dec_sound,
                         doc="word0 = sound-detect on/off (SET=0x7b2, 16B, word0). The reply is 16B; "
                             "only word0 is used (word1 stays 0).")
# Track index for STARTSTOP_LULLABY / params @4, in the parent handset's menu order.
LULLABY_TRACKS = ["Play All", "My Bonnie", "Itsy Bitsy Spider", "Hot Cross Buns", "Hush Little Baby",
                  "Brahms' Lullaby", "White Noise", "Crickets", "Stream", "Birds"]
LULLABY             = Op(0x735, "IOTYPE_VTECH_RM_SERIES_STARTSTOP_LULLABY_REQ",
                         enc=lambda track=-1: struct.pack("<ii", 0, track), resp=0x736,
                         doc="Play a melody/soothing sound on the camera speaker: int@4 = track index "
                             "(LULLABY_TRACKS), -1 = stop. Ack 0x736 = [1, track]; camera also pushes "
                             "0x7ac subtype 1. Runs for the params @12 auto-off timer.")
SET_DAYNIGHT_MODE   = Op(0x715, "IOTYPE_VTECH_SET_MODE_REQ",
                         enc=lambda mode=0: struct.pack("<ii", 0, mode), resp=0x716,
                         doc="Day/night (night-vision) MODE, int@4, 3-state: 0 = Auto, 1 = On (forces "
                             "night/IR even in a lit room), 2 = Off. Applying a mode briefly drops video "
                             "(ISP/IR-cut reconfig — set it, wait ~6-8s, then stream). Ack 0x716 = "
                             "int@0=1 (numeric success). GET counterpart = 0x713->0x714 "
                             "(GET_DAYNIGHT_MODE). The current day/night STATE is camera-pushed on "
                             "0x785 DAYNIGHTMODE_NOTIFY.")

# ── VTech vendor opcodes with no published SDK constant — names assigned here ─
GET_TEMP_DETECT = Op(0x74b, "GET_TEMP_DETECT",   _enc_i32, resp=0x74c, dec=_dec_temp_detect,
                         doc="word0 enable, word1 max c*10, word2 min c*10 (SET=0x74d)")
GET_LED_STATUS  = Op(0x709, "GET_LED_STATUS",   _enc_channel, resp=0x710,
                         dec=lambda r: {"led_off": bool(struct.unpack_from("<i", r, 4)[0])} if len(r) >= 8 else {},
                         doc="GET the camera's physical online/status LED (distinct from the night-light "
                             "0x7a2). int@4: 0 = LED on/shown, 1 = LED off/hidden. "
                             "SET = 0x711; state-change push = 0x7ac subtype 8.")
SET_LED_STATUS  = Op(0x711, "SET_LED_STATUS",
                         enc=lambda off=0: struct.pack("<ii", 0, 1 if off else 0), resp=0x712,
                         doc="SET the status LED. int@4 = 1 turns the LED OFF, 0 turns it ON. "
                             "Ack 0x712 (int@0=0); camera also pushes 0x7ac subtype 8 with the new value.")
GET_DAYNIGHT_MODE = Op(0x713, "GET_DAYNIGHT_MODE", _enc_i32, resp=0x714, dec=_dec_daynight_mode,
                         doc="GET day/night mode (int@4: 0 Auto/1 On-forced/2 Off) — the read "
                             "counterpart to SET_DAYNIGHT_MODE 0x715.")
GET_PARENT_ACTIVE = Op(0x75a, "GET_PARENT_ACTIVE", _enc_i32, resp=0x75b,
                         dec=lambda r: {"parent_active": bool(struct.unpack_from("<i", r, 0)[0])},
                         doc="int@0 = physical parent unit active (1 = parent handset on, 0 = off). This "
                             "is the signal behind the camera-enforced 720p cap: while parent_active=1 "
                             "the camera forces 720p and rejects quality=2. Available both ways: poll "
                             "this, or receive the async 0x74F push (len4: OFF->0, ON->1; shares the "
                             "0x75B handler).")
CLIENT_ANNOUNCE = Op(0x7be, "CLIENT_ANNOUNCE",
                         enc=lambda v=0: struct.pack("<i", v) + b"\0" * 4,
                         doc="first opcode in the connect burst; 8B int; no reply")
VIDEO_FULLRATE  = Op(0x719, "VIDEO_FULLRATE", _enc_toggle,
                         doc="0/1 toggle; sending it flips 1fps preview -> ~10fps (fire-and-forget)")


def negotiate_master(t, av, do_round1=True):
    """Run the 0x820/0x821 negotiation with a client X25519 keypair; return the session master."""
    SND_DATA_DELAY.send(t, av)          # 0x0ff — first IOCtrl on the channel, before any app command
    DEVINFO.send(t, av)                 # 0x330 prep — required before the VTech GETs reply

    # ROUND 1 (empty) — an initial empty round; camera replies 0x821 int@0=-1 ("not done").
    if do_round1:
        log("--- ROUND 1: 0x820 (empty) ---")
        _send(t, av, 0x820, b"")
        r1 = _recv_until(t, av, 0x821)
        if r1 is not None and len(r1) >= 8:
            log(f"    round1 0x821 int@0={struct.unpack_from('<i', r1, 0)[0]} "
                f"int@4={struct.unpack_from('<i', r1, 4)[0]}")

    # ROUND 2 — send word0=1 || our X25519 public key (36 bytes). Camera ECDHs against it.
    priv, pub = x25519_keypair()
    r2 = struct.pack("<I", 1) + pub
    log(f"--- ROUND 2: 0x820 word0=1 || X25519 pubkey (36B) ---")
    log(f"    our pubkey = {pub.hex()}")
    _send(t, av, 0x820, r2)
    go = _recv_until(t, av, 0x821)
    if go is None:
        raise RuntimeError("no 0x821 GO response to round-2 0x820")
    if len(go) < 76:
        raise RuntimeError(f"0x821 GO too short (len={len(go)}); expected >=76 with @12/@44 fields")
    i0, i4 = struct.unpack_from("<i", go, 0)[0], struct.unpack_from("<i", go, 4)[0]
    log(f"    round2 0x821 int@0={i0} int@4={i4} ({'GO' if i0 == 0 else 'NOT-GO'})")
    if i0 != 0:
        raise RuntimeError(f"0x821 did not go to GO state (int@0={i0}); handshake rejected")

    p = go[12:44]        # camera X25519 public key
    salt = go[44:76]     # HKDF-Extract salt (== HMAC key)
    if p == b"\0" * 32 or salt == b"\0" * 32:
        raise RuntimeError("0x821 @12/@44 are zero — no ECDH material (camera didn't accept our pubkey)")
    log(f"    cam pubkey @12 = {p.hex()}")
    log(f"    salt       @44 = {salt.hex()}")

    S = x25519_shared(priv, p)
    master = session_master(salt, S)
    log(f"    S      = {S.hex()}")
    log(f"    master = {master.hex()}")
    return master


# ─────────────────────────────────────────────────────────────────────────────
# §4b  Viewing sinks — pipe the decrypted Annex-B H.264 into ffmpeg/ffplay
#      (all optional; each degrades to a no-op with a warning if the tool is absent)
# ─────────────────────────────────────────────────────────────────────────────
# Audio format the camera emits (see module header): G.711 A-law, 8 kHz, mono.
AUDIO_CODEC = "alaw"
AUDIO_RATE = 8000
AUDIO_CH = 1


class _MuxProc:
    """One ffmpeg fed decrypted video on stdin and (optionally) decrypted A-law audio on a named
    FIFO, muxing to `out_args`. Both .write_video/.write_audio are broken-pipe-safe. When `downstream`
    is given, ffmpeg writes to a pipe consumed by that second process (used for combined-A/V --play)."""

    # Bounded audio backlog (~8 s at 25 fps). If ffmpeg momentarily stops draining the audio FIFO
    # we drop the oldest audio rather than block — the video loop must never stall on an audio write.
    AQ_MAX = 200

    def __init__(self, name, out_args, fps, audio=False, downstream=None, direct_cmd=None):
        self.name = name
        self.dead = False
        self.fifo = None
        self.down = None
        self.aq = None
        if direct_cmd is not None:
            # No ffmpeg: feed decrypted video straight to this process on stdin (the --play path).
            self.p = subprocess.Popen(direct_cmd, stdin=subprocess.PIPE)
            return
        # AUDIO input goes FIRST when present: ffmpeg opens+probes inputs in order. Raw A-law needs
        # ~no analysis so the FIFO connects immediately (no startup audio lost), and the small probe
        # window below lets the raw-H.264 input reach its first SPS (needed for MPEG-TS muxing, which
        # requires the video dimensions up front — a bare pipe can otherwise probe as size 0).
        base = ["ffmpeg", "-loglevel", "warning", "-fflags", "+genpts"]
        video_in = ["-r", str(fps), "-f", "h264", "-i", "pipe:0"]
        cmd = base
        if audio:
            video_in = ["-analyzeduration", "2M", "-probesize", "2M"] + video_in
            self.fifo = os.path.join(tempfile.mkdtemp(prefix="vvc_"), f"audio.{AUDIO_CODEC}")
            os.mkfifo(self.fifo)
            cmd = cmd + ["-thread_queue_size", "1024", "-f", AUDIO_CODEC, "-ar", str(AUDIO_RATE),
                         "-ac", str(AUDIO_CH), "-i", self.fifo]
        cmd = cmd + video_in + out_args
        if downstream is not None:
            self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            self.down = subprocess.Popen(downstream, stdin=self.p.stdout)
            self.p.stdout.close()   # let the downstream own the read end
        else:
            self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        if audio:
            # A dedicated thread owns the FIFO: opening it for write blocks until ffmpeg opens the
            # read end, and writes to it can block whenever ffmpeg is busy — neither must ever stall
            # the caller's video loop, so all of it happens here, fed by a bounded queue.
            self.aq = queue.Queue(maxsize=self.AQ_MAX)
            threading.Thread(target=self._audio_writer, daemon=True).start()

    def _audio_writer(self):
        try:
            afh = open(self.fifo, "wb", buffering=0)     # blocks until ffmpeg opens the read end
        except OSError:
            return
        while True:
            data = self.aq.get()
            if data is None:                              # sentinel from close()
                break
            try:
                afh.write(data)
            except (BrokenPipeError, ValueError, OSError):
                break
        try:
            afh.close()
        except OSError:
            pass

    def write_video(self, data):
        if self.dead:
            return
        try:
            self.p.stdin.write(data)
        except (BrokenPipeError, ValueError, OSError):
            self.dead = True
            log(f"sink '{self.name}' closed early (player quit?)")

    def write_audio(self, data):
        # Non-blocking hand-off to the writer thread; drop the oldest frame if the backlog is full.
        if self.dead or self.aq is None:
            return
        try:
            self.aq.put_nowait(data)
        except queue.Full:
            try:
                self.aq.get_nowait()
                self.aq.put_nowait(data)
            except (queue.Empty, queue.Full):
                pass

    def close(self):
        if self.aq is not None:
            try:
                self.aq.put_nowait(None)                  # tell the writer thread to finish
            except queue.Full:
                pass
        try:
            if self.p.stdin:
                self.p.stdin.close()
        except OSError:
            pass
        for proc in (self.p, self.down):
            if proc is None:
                continue
            try:
                proc.wait(timeout=15)
            except Exception:
                proc.kill()
        if self.fifo and os.path.exists(self.fifo):
            try:
                os.unlink(self.fifo)
                os.rmdir(os.path.dirname(self.fifo))
            except OSError:
                pass


# Video-input note (`-r fps -fflags +genpts`): we assume a constant `fps` and let ffmpeg generate
# monotonic PTS from it — the best of the measured options.
# It yields MONOTONIC RTP timestamps (what VLC's live555 receiver needs) and emits exactly ONE benign
# "Timestamps are unset" warning: the first SPS/PPS packet reaches the muxer before genpts stamps it;
# every frame after is timestamped, so for live video there is no downstream effect. The warning is
# left VISIBLE, not suppressed. (Rejected: `setts` BSF = 0 warnings but stalls live555;
# `use_wallclock_as_timestamps` = per-AU non-monotonic timestamps, since we write a whole access unit
# at once, which makes VLC stutter.)


def _write_sdp(path, host, port, ipver):
    """A minimal H.264/RTP SDP VLC can open (SPS/PPS arrive in-band with each keyframe)."""
    sdp = "\n".join(["v=0", f"o=- 0 0 IN {ipver} {host}", "s=VTech RM5754HD",
                     f"c=IN {ipver} {host}", "t=0 0",
                     f"m=video {port} RTP/AVP 96", "a=rtpmap:96 H264/90000",
                     "a=fmtp:96 packetization-mode=1", ""])
    with open(path, "w") as f:
        f.write(sdp)
    return sdp


def build_sinks(mp4=None, hls=None, play=False, rtp_host=None, rtp_port=5004,
                sdp_path=None, fps=15, audio=False):
    """Return a list of live writers (each has .write_video/.write_audio/.close) for the decrypt loop.
    When `audio` is True the muxing sinks take a 2nd (A-law) input and add an AAC track: --mp4/--hls/
    --play carry combined A/V. --rtp stays H.264-only — it uses the low-latency raw-H.264/RTP recipe
    that VLC's live555 receiver is known to accept here; for live audio+video to VLC use --hls (open
    the .m3u8 over HTTP) or record with --mp4. (Audio-over-RTP is a possible future add — see docs.)"""
    sinks = []
    have_ffmpeg = shutil.which("ffmpeg") is not None
    if (mp4 or hls or rtp_host) and not have_ffmpeg:
        log("!! ffmpeg not on PATH — --mp4/--hls/--rtp disabled (raw .h264 still written)")
    if rtp_host and have_ffmpeg:
        ipv6 = ":" in rtp_host
        ipver = "IP6" if ipv6 else "IP4"
        url_host = f"[{rtp_host}]" if ipv6 else rtp_host
        out = ["-an", "-c:v", "copy", "-payload_type", "96",
               "-flush_packets", "1", "-max_delay", "0",
               "-f", "rtp", f"rtp://{url_host}:{rtp_port}"]
        sinks.append(_MuxProc("rtp", out, fps, audio=False))   # RTP path is video-only by design
        sdp_path = sdp_path or os.path.abspath("vtech.sdp")
        sdp = _write_sdp(sdp_path, rtp_host, rtp_port, ipver)
        log("──── RTP/H.264 live (video only) → VIEW IN VLC ────")
        log(f"    vlc {sdp_path}")
        log(f"    (VLC on another host: save the SDP below as vtech.sdp, then: vlc vtech.sdp)")
        log(f"    start VLC as soon as you see this — it catches the next keyframe")
        if audio:
            log(f"    NOTE: audio is NOT on the RTP stream — for live A/V use --hls, or --mp4 to record")
        for ln in sdp.strip().splitlines():
            log(f"    | {ln}")
        log("──────────────────────────────────────")
    if mp4 and have_ffmpeg:
        out = (["-c:v", "copy", "-c:a", "aac", "-b:a", "48k"] if audio else ["-c", "copy"]) + \
              ["-movflags", "+faststart", "-y", mp4]
        sinks.append(_MuxProc("mp4", out, fps, audio=audio))
        log(f"sink: MP4 ({'H.264+AAC' if audio else 'H.264'}) -> {mp4}")
    if hls and have_ffmpeg:
        os.makedirs(hls, exist_ok=True)
        m3u8 = os.path.join(hls, "stream.m3u8")
        out = (["-c:v", "copy", "-c:a", "aac", "-b:a", "48k"] if audio else ["-c", "copy"]) + \
              ["-f", "hls", "-hls_time", "2", "-hls_list_size", "6",
               "-hls_flags", "delete_segments", "-y", m3u8]
        sinks.append(_MuxProc("hls", out, fps, audio=audio))
        log(f"sink: HLS -> {m3u8}  (serve the dir over HTTP and open the .m3u8)")
    if play:
        if not shutil.which("ffplay"):
            log("!! ffplay not on PATH — --play disabled")
        elif audio:
            # ffplay can't take two inputs; ffmpeg muxes A/V to MPEG-TS on a pipe that ffplay plays.
            out = ["-c:v", "copy", "-c:a", "aac", "-f", "mpegts", "pipe:1"]
            down = ["ffplay", "-loglevel", "warning", "-autoexit", "-fflags", "nobuffer",
                    "-i", "pipe:0"]
            sinks.append(_MuxProc("play", out, fps, audio=True, downstream=down))
            log("sink: ffplay window (H.264+AAC via MPEG-TS)")
        else:
            sinks.append(_MuxProc("play", None, fps, direct_cmd=[
                "ffplay", "-loglevel", "warning", "-autoexit", "-fflags", "nobuffer",
                "-f", "h264", "-i", "pipe:0"]))
            log("sink: ffplay window")
    return sinks


def make_snapshot(raw_h264, png_path):
    """Post-capture: decode the first keyframe of the raw stream to a PNG."""
    if not shutil.which("ffmpeg"):
        log("!! ffmpeg absent — cannot make snapshot")
        return False
    rc = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "h264", "-i", raw_h264,
                         "-frames:v", "1", "-y", png_path]).returncode
    if rc == 0 and os.path.exists(png_path):
        log(f"snapshot -> {png_path}")
        return True
    log("snapshot failed (no decodable keyframe in the stream?)")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# §5  START + decrypt loop
# ─────────────────────────────────────────────────────────────────────────────
def _finfo_ts(fi):
    """Best-guess the camera frame timestamp (ms) from FRAMEINFO_t: try u32 @8/@12/@16."""
    return {off: struct.unpack_from("<I", fi, off)[0] for off in (8, 12, 16) if off + 4 <= len(fi)}


def _drain_audio(t, av, master, abuf, afinfo, aidx, sinks, aout, stats):
    """Drain all currently-available audio frames: decrypt each and route to sinks + raw file.
    `stats` = [frames, ok, bad, plain_bytes]. Returns when avRecvAudioData has nothing ready."""
    for _ in range(64):                     # bounded so we never starve the video pump
        r = t.recv_audio(av, abuf, len(abuf), afinfo, 64, C.byref(aidx))
        if r <= 0:
            break
        stats[0] += 1
        frame = abuf.raw[:r]
        if r < 60:
            stats[2] += 1
            continue
        try:
            pt = decrypt_frame(frame, master)      # same per-frame crypto as video
        except Exception:
            stats[2] += 1
            continue
        if aout:
            aout.write(pt)
        for s in sinks:
            s.write_audio(pt)
        stats[1] += 1; stats[3] += len(pt)
        if stats[1] == 1:
            log(f"  *** first AUDIO frame DECRYPTED ok: {len(pt)}B G.711 A-law "
                f"(codec_id=0x{struct.unpack_from('<H', afinfo.raw, 0)[0]:x}) ***")


def stream_and_decrypt(t, av, master, out_path, seconds, dump_raw=None, sinks=None, debug=False,
                       audio=False, audio_out=None, quality=None):
    # quality (0x320 SETSTREAMCTRL) selects the resolution and persists camera-side: 2 -> 1920x1080,
    # lower -> 720p. Sent before START. Omit to keep whatever the camera is currently set to.
    if quality is not None:
        SETSTREAMCTRL.send(t, av, quality)
    # 0x719 selects the FULL-RATE video stream and is sent just before START. Without it the camera
    # streams a 1 fps keyframe-only preview; with it, ~10 fps with P-frames.
    if audio:
        AUDIOSTART.send(t, av)             # 0x300 — camera begins delivering audio frames
    VIDEO_FULLRATE.send(t, av, 0)      # 0x719 — flip 1fps preview -> ~10fps (see Op doc)
    src = START.send(t, av)                # 0x1ff — live-view START
    log(f"--- {'AUDIOSTART + ' if audio else ''}full-rate + START = {src}; "
        f"pumping for {seconds}s -> {out_path} ---")

    bufmax = 1 << 21                        # 2 MiB
    fbuf = C.create_string_buffer(bufmax)
    finfo = C.create_string_buffer(64)
    actual = C.c_int(0); expected = C.c_int(0); actinfo = C.c_int(0); fidx = C.c_int(0)
    # audio scratch (frames are ~380 B; 64 KiB is ample)
    abuf = C.create_string_buffer(1 << 16)
    afinfo = C.create_string_buffer(64)
    aidx = C.c_uint32(0)
    astats = [0, 0, 0, 0]                    # [frames, ok, bad, plain_bytes]
    aout = open(audio_out, "wb") if audio_out else None

    sinks = sinks or []
    out = open(out_path, "wb") if out_path else None
    raw = open(dump_raw, "wb") if dump_raw else None
    frames = ok = bad = 0
    plain_bytes = 0
    rc_hist = {}                            # recv_frame return-code histogram
    last_wall = None                        # for wall-clock inter-frame gap
    last_ts = {}                            # for camera finfo timestamp deltas
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            r = t.recv_frame(av, fbuf, bufmax, C.byref(actual), C.byref(expected),
                             finfo, 64, C.byref(actinfo), C.byref(fidx))
            if debug:
                key = "frame" if r > 0 else r
                rc_hist[key] = rc_hist.get(key, 0) + 1
            if r > 0:
                frame = fbuf.raw[:r]
                now = time.time()
                if frames == 0:
                    log(f"FIRST FRAME r={r} actual={actual.value} expected={expected.value} "
                        f"idx={fidx.value} finfo={finfo.raw[:24].hex()}")
                if debug:
                    ts = _finfo_ts(finfo.raw)
                    dts = {off: ts[off] - last_ts.get(off, ts[off]) for off in ts}
                    gap = "" if last_wall is None else f" wall+{(now-last_wall)*1000:.0f}ms"
                    if frames < 40 or frames % 15 == 0:
                        log(f"  f#{frames} idx={fidx.value} r={r}{gap} "
                            f"ts@8={ts.get(8)}(+{dts.get(8)}) ts@12={ts.get(12)}(+{dts.get(12)}) "
                            f"flags={finfo.raw[2]:#x}")
                    last_ts = ts
                last_wall = now
                if raw:
                    raw.write(frame)
                frames += 1
                if len(frame) < 60:
                    bad += 1
                    if bad <= 3:
                        log(f"  frame {fidx.value}: too short ({len(frame)}B) to be a wire frame")
                else:
                    try:
                        pt = decrypt_frame(frame, master)
                        if out:
                            out.write(pt)
                        for s in sinks:
                            s.write_video(pt)
                        plain_bytes += len(pt); ok += 1
                        if ok == 1:
                            log(f"  *** first frame DECRYPTED ok: {len(pt)}B plaintext, "
                                f"starts {pt[:5].hex()} (H.264 start code = 00000001) ***")
                    except Exception as e:  # bad Poly1305 tag / wrong layer / offset
                        bad += 1
                        if bad <= 3:
                            log(f"  frame {fidx.value}: decrypt FAILED ({e}); "
                                f"head={frame[:16].hex()}")
            elif expected.value > bufmax:
                log(f"  frame buffer too small (expected={expected.value}); skipping")
            elif r in (-20013, -20014):   # INCOMPLETE / LOSED_THIS_FRAME -> retry immediately
                pass
            else:                         # NOREADY (-20012) etc. -> brief backoff
                time.sleep(0.003)
            # audio arrives on its own FIFO at ~25 fps — drain whatever is ready each iteration
            if audio:
                _drain_audio(t, av, master, abuf, afinfo, aidx, sinks, aout, astats)
            # drain any interleaved IOCtrl notifies so the recv queue doesn't back up
            typ = C.c_uint32(0); ib = C.create_string_buffer(128)
            t.recv_ioctrl(av, C.byref(typ), C.cast(ib, C.c_void_p), 128, 0)
    finally:
        if out:
            out.close()
        if raw:
            raw.close()
        if aout:
            aout.close()
        for s in sinks:
            s.close()
    elapsed = time.time() - t0
    log(f"=== DONE: frames={frames} decrypted_ok={ok} failed={bad} "
        f"plaintext={plain_bytes}B in {elapsed:.1f}s = {frames/elapsed:.1f} fps -> {out_path} ===")
    if audio:
        log(f"=== AUDIO: frames={astats[0]} decrypted_ok={astats[1]} failed={astats[2]} "
            f"plaintext={astats[3]}B = {astats[0]/elapsed:.1f} fps (G.711 A-law 8kHz mono)"
            + (f" -> {audio_out}" if audio_out else "") + " ===")
    if debug:
        log(f"    recv_frame return-code histogram: {rc_hist}  "
            f"(TUTK: -20012=DATA_NOREADY -20013=INCOMPLETE_FRAME -20014=LOSED_THIS_FRAME -20015=SESSION_CLOSE_BY_REMOTE)")
    return ok, bad



# ─────────────────────────────────────────────────────────────────────────────
# §6  Talk-back — play audio through the camera speaker
#     The client runs an AV SERVER on a free IOTC channel of its session; SPEAKERSTART (0x350) tells the
#     camera which channel, the camera connects to it as an AV client, and the client sends 20 ms
#     G.711 A-law frames with avSendAudioData, each wrapped by encrypt_frame(master). Unencrypted frames
#     are accepted by the SDK but the camera plays near-silence.
# ─────────────────────────────────────────────────────────────────────────────
_ALAW_SEG_END = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def lin2alaw(sample):
    """One signed 16-bit PCM sample -> one G.711 A-law byte (ITU-T G.711 segment encoder)."""
    s = sample >> 3
    if s >= 0:
        mask = 0xD5
    else:
        mask, s = 0x55, -s - 1
    seg = next((i for i, e in enumerate(_ALAW_SEG_END) if s <= e), 8)
    if seg >= 8:
        return 0x7F ^ mask
    return ((seg << 4) | ((s >> 1) & 0xF if seg < 2 else (s >> seg) & 0xF)) ^ mask


def load_wav_8k(path, gain=1.0):
    """Read a PCM WAV (8/16-bit, any rate, mono or stereo) -> list of int16 samples, mono, 8 kHz."""
    with wave.open(path) as w:
        nch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        x = struct.unpack(f"<{len(raw) // 2}h", raw)
    elif width == 1:
        x = [(b - 128) << 8 for b in raw]
    else:
        raise ValueError(f"{path}: {8 * width}-bit WAV not supported (use 8- or 16-bit PCM)")
    x = [sum(x[i:i + nch]) // nch for i in range(0, len(x), nch)]          # downmix to mono
    if rate != AUDIO_RATE:                                                # linear-interpolation resample
        n_out = int(len(x) * AUDIO_RATE / rate)
        out = []
        for k in range(n_out):
            pos = k * rate / AUDIO_RATE
            i = int(pos); f = pos - i
            a = x[i]; b = x[i + 1] if i + 1 < len(x) else a
            out.append(a + (b - a) * f)
        x = out
    return [max(-32768, min(32767, int(v * gain))) for v in x]


def talkback(t, av, master, pcm, frame_ms=20, serv_timeout=10, resume_audio=False):
    """Play int16 8 kHz mono samples through the camera speaker. Needs an open AV session (t.session_id)
    and the session master from negotiate_master(). Camera audio is stopped while talking (half-duplex);
    resume_audio=True restarts it (AUDIOSTART) afterwards. Returns the number of frames sent."""
    sid = t.session_id
    fmt = GETAUDIOFORMAT.request(t, av, 0) or {}
    codec = fmt.get("format", 0x8a)
    log(f"talk-back uplink format: {fmt}")
    if codec != 0x8a or fmt.get("sample_rate_idx", 0) != 0:
        raise RuntimeError(f"unsupported uplink format {fmt}: this client sends G.711 A-law 8 kHz only")
    flags = 0x02                                   # mono | 8 kHz << 2 | 16-bit capture << 1
    spf = AUDIO_RATE * frame_ms // 1000

    AUDIOSTOP.send(t, av)                          # talk-back is half-duplex: stop camera audio first
    ch = t.free_channel(sid)
    if ch < 0:
        raise RuntimeError(f"IOTC_Session_Get_Free_Channel failed rc={ch}")
    auth = AUTH_FN(lambda acc, pwd: 1)             # the camera logs in with an empty account/password
    idx, sent = -1, 0
    try:
        SPEAKERSTART.send(t, av, ch)
        resend = C.c_int(0)
        idx = t.serv_start3(sid, auth, serv_timeout, 0, ch, C.byref(resend))
        log(f"avServStart3(ch={ch}) = {idx}")
        if idx < 0:
            raise RuntimeError(f"camera did not connect to the talk-back channel (rc={idx})")
        t0 = time.time()
        for n, i in enumerate(range(0, len(pcm) - spf + 1, spf)):
            body = bytes(lin2alaw(v) for v in pcm[i:i + spf])
            frame = encrypt_frame(body, master)
            finfo = struct.pack("<HBBB7xI", codec, flags, 0, 0, int(time.time() * 1000) & 0xFFFFFFFF)
            rc = t.send_audio(idx, frame, len(frame), finfo, len(finfo))
            if rc == -20006:                       # AV_ER_EXCEED_MAX_SIZE: send buffer full
                t.serv_reset(idx, 1, 30)
            elif rc < 0:
                raise RuntimeError(f"avSendAudioData rc={rc}")
            else:
                sent += 1
            delay = t0 + (n + 1) * frame_ms / 1000 - time.time()
            if delay > 0:
                time.sleep(delay)
        log(f"talk-back: {sent} frames ({sent * frame_ms / 1000:.1f}s) sent")
    finally:
        if idx >= 0:
            t.serv_stop(idx)
        t.channel_off(sid, ch)
        t.serv_exit(sid, ch)
        SPEAKERSTOP.send(t, av, ch)
        if resume_audio:
            AUDIOSTART.send(t, av)
        auth = None
    return sent


# ─────────────────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[vvc] {msg}", file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser(description="Standalone LAN-direct encrypted-video client (VTech RM5754HD)")
    ap.add_argument("--out", default="video.h264",
                    help="output plaintext H.264 elementary stream ('' to skip)")
    ap.add_argument("--seconds", type=int, default=8, help="frame-pump duration")
    ap.add_argument("--channel", type=int, default=0, help="AV channel index")
    ap.add_argument("--quality", type=int, default=None, metavar="N",
                    help="set resolution via SETSTREAMCTRL 0x320 before START: 2 = 1080p, lower = 720p "
                         "(persists camera-side; omit to keep the camera's current setting)")
    ap.add_argument("--skip-round1", action="store_true", help="skip the empty round-1 0x820")
    ap.add_argument("--dump-raw", help="also write the raw (still-encrypted) frames here")
    # viewing sinks (all optional; need ffmpeg/ffplay on the host that runs this)
    ap.add_argument("--mp4", help="also record a playable MP4 (via ffmpeg)")
    ap.add_argument("--hls", help="also produce live HLS in this dir (serve over HTTP, open stream.m3u8)")
    ap.add_argument("--play", action="store_true", help="show a live ffplay window")
    ap.add_argument("--rtp", metavar="HOST",
                    help="live RTP/H.264 to HOST (where VLC runs); writes an SDP + prints the VLC command")
    ap.add_argument("--rtp-port", type=int, default=5004, help="RTP destination port (default 5004)")
    ap.add_argument("--sdp", help="path to write the RTP SDP (default ./vtech.sdp)")
    ap.add_argument("--snapshot", help="after capture, decode the first keyframe to this PNG")
    ap.add_argument("--audio-out", dest="audio_out",
                    help="also write raw decrypted G.711 A-law (8kHz mono); "
                         "play with: ffplay -f alaw -ar 8000 -ac 1 -i FILE")
    ap.add_argument("--no-audio", action="store_true",
                    help="video only: skip 0x300 + audio decrypt (RTP uses the raw-H.264 recipe)")
    ap.add_argument("--fps", type=int, default=10,
                    help="assumed framerate for MP4/HLS/RTP timing (camera streams ~10 fps)")
    ap.add_argument("--debug", action="store_true",
                    help="per-frame return codes, wall-clock gaps, and finfo timestamp deltas")
    ap.add_argument("--penc", type=int, default=None,
                    help="call IOTC_Set_Partial_Encryption(flag) before start (default: don't)")
    ap.add_argument("--talk", metavar="WAV",
                    help="talk-back: play this PCM WAV through the camera speaker, then exit (no video)")
    ap.add_argument("--talk-gain", type=float, default=1.0, help="linear gain applied to --talk audio")
    ap.add_argument("--libdir", default=os.environ.get("VTECH_LIBDIR"),
                    help="dir with patched .so + libtk.so (or $VTECH_LIBDIR); license embedded, LICENSE_KEY.txt optional")
    args = ap.parse_args()

    uid = os.environ.get("VTECH_UID", "").encode()
    pwd = os.environ.get("VTECH_PWD", "").encode()
    ak = os.environ.get("VTECH_AUTHKEY", "").encode()
    acct = os.environ.get("VTECH_ACCOUNT", "admin").encode()
    if not (uid and pwd and ak):
        sys.exit("need VTECH_UID / VTECH_PWD / VTECH_AUTHKEY in env")
    if not args.libdir:
        sys.exit("need --libdir or $VTECH_LIBDIR (patched TUTK libs + companion)")

    t = TUTK(args.libdir)
    log("all patched libs + companion loaded in-process (RTLD_LOCAL)")
    av = open_av_session(t, uid, pwd, ak, acct, args.channel, penc=args.penc)
    master = negotiate_master(t, av, do_round1=not args.skip_round1)
    if args.talk:
        talkback(t, av, master, load_wav_8k(args.talk, args.talk_gain))
        return
    # Audio is on by default whenever a sink will consume it; --no-audio forces video-only.
    # (--rtp is intentionally excluded: the RTP path is video-only.)
    audio = (not args.no_audio) and bool(
        args.audio_out or args.mp4 or args.hls or args.play)
    sinks = build_sinks(mp4=args.mp4, hls=args.hls, play=args.play,
                        rtp_host=args.rtp, rtp_port=args.rtp_port, sdp_path=args.sdp,
                        fps=args.fps, audio=audio)
    out_path = args.out or None
    ok, bad = stream_and_decrypt(t, av, master, out_path, args.seconds,
                                 dump_raw=args.dump_raw, sinks=sinks, debug=args.debug,
                                 audio=audio, audio_out=args.audio_out, quality=args.quality)
    if args.snapshot:
        if out_path:
            make_snapshot(out_path, args.snapshot)
        else:
            log("!! --snapshot needs --out (the raw stream to decode from)")
    if ok == 0:
        log("no frames decrypted — see the notes in the module header")
        sys.exit(2)


if __name__ == "__main__":
    main()
