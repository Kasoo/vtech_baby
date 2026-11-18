"""Per-camera credentials from the parent unit's pairing QR (docs/vtech-lan-kit/docs/05-provisioning.md).

The QR carries each camera's UID and AV password, and its security mode: an enhanced camera's authKey
is derived from the AV password, a plain camera has none. Reference
implementation: docs/vtech-lan-kit/client/vtech_provision.py (tests check parity with it). Reading the QR
out of a photo is left to the caller: this module takes the text the QR encodes.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# App-wide constants (05-provisioning.md): not per-camera, not rotating.
QR_AES_KEY = bytes.fromhex("853bc0248503eea465877351f3ae6fe1")
QR_AES_IV = bytes.fromhex("384ce8a20d21a518997189cf62413a0f")
HKDF_MASTER_KEY = bytes.fromhex("45c9d8785a856d4ee1247d91e094175c")
AV_ACCOUNT = "admin"  # fixed for the RM series

QR_VERSION = 0x05             # TLV records, several cameras, enhanced or plain
QR_VERSION_V1 = 0x01          # comma-separated records; every camera plain
TLV_NAME, TLV_UID, TLV_PWD_PLAIN, TLV_PWD_ENHANCED, TLV_MAC, TLV_UNSUPPORTED = 2, 3, 4, 5, 7, 8
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


class InvalidQr(ValueError):
    """The text is not a pairing QR this module understands."""


class UnsupportedModel(InvalidQr):
    """Every camera in the QR is flagged as a model the pairing QR marks unsupported."""


@dataclass(frozen=True)
class Credentials:
    uid: str
    av_password: str
    authkey: str              # "" for a plain camera
    account: str = AV_ACCOUNT
    name: str = ""
    mac: str = ""
    plain: bool = False       # plain security mode: no authKey, no DTLS


def derive_authkey(av_password: str) -> str:
    """authKey = base62 of the first 8 bytes of HKDF-SHA256(salt=master key, IKM=AV password)."""
    prk = hmac.new(HKDF_MASTER_KEY, av_password.encode(), hashlib.sha256).digest()
    okm = hmac.new(prk, b"\x01", hashlib.sha256).digest()
    return "".join(_BASE62[b % 62] for b in okm[:8])


def _qr_bytes(text: str) -> bytes:
    """The QR text's bytes: version byte first."""
    try:
        raw = base64.b64decode("".join(text.split()) + "===")
    except (binascii.Error, ValueError) as err:
        raise InvalidQr(f"not base64: {err}") from None
    if not raw:
        raise InvalidQr("empty QR")
    return raw


def _decrypt(ct: bytes) -> bytes:
    """The plaintext, with its zero padding to the block size."""
    if not ct or len(ct) % 16:
        raise InvalidQr(f"ciphertext not block-aligned ({len(ct)} bytes)")
    dec = Cipher(algorithms.AES(QR_AES_KEY), modes.CBC(QR_AES_IV)).decryptor()
    return dec.update(ct) + dec.finalize()


def _v5_cameras(raw: bytes) -> list[Credentials]:
    """`[0x05][len:2 BE][AES-CBC]`; the plaintext is TLV records `<camera index><type><len><value>`,
    grouped by camera index."""
    if len(raw) < 3:
        raise InvalidQr("version-5 QR too short")
    pt = _decrypt(raw[3:])
    by_index: dict[int, dict[int, bytes]] = {}
    i = 0
    while i + 3 <= len(pt):
        idx, typ, ln = pt[i], pt[i + 1], pt[i + 2]
        if typ:                                       # type 0: the zero padding
            by_index.setdefault(idx, {})[typ] = pt[i + 3:i + 3 + ln]
        i += 3 + ln
    cameras, unsupported = [], 0
    for f in by_index.values():
        plain = TLV_PWD_PLAIN in f and TLV_PWD_ENHANCED not in f
        pwd = f.get(TLV_PWD_PLAIN if plain else TLV_PWD_ENHANCED)
        if not f.get(TLV_UID) or not pwd:
            continue
        if TLV_UNSUPPORTED in f:
            unsupported += 1
            continue
        pw = pwd.decode("latin1")
        cameras.append(Credentials(
            uid=f[TLV_UID].decode("latin1"), av_password=pw, authkey="" if plain else derive_authkey(pw),
            name=f.get(TLV_NAME, b"").decode("latin1"),
            mac=":".join(f"{b:02x}" for b in f.get(TLV_MAC, b"")), plain=plain))
    if not cameras and unsupported:
        raise UnsupportedModel("the QR marks its camera as an unsupported model")
    if not cameras:
        raise InvalidQr(f"no UID / AV password in the QR (fields {sorted(set().union(*by_index.values()))})")
    return cameras


def _v1_cameras(raw: bytes) -> list[Credentials]:
    """`[0x01][len:1][AES-CBC]`; the plaintext is ASCII, four comma-separated fields per camera
    (idx, name, uid, token). The token is the AV password of a plain camera."""
    if len(raw) < 2:
        raise InvalidQr("version-1 QR too short")
    pt = _decrypt(raw[2:]).rstrip(b"\x00")
    try:
        parts = pt.decode("ascii").split(",")
    except UnicodeDecodeError:
        raise InvalidQr("version-1 payload is not ASCII") from None
    if len(parts) < 4 or len(parts) % 4:
        raise InvalidQr("version-1 payload is not four fields per camera")
    cameras = []
    for i in range(0, len(parts), 4):
        _idx, name, uid, token = parts[i:i + 4]
        if "VTECH_" not in name or len(uid) != 20 or not uid.isalnum() or not token:
            raise InvalidQr("version-1 record is not a VTech camera")
        cameras.append(Credentials(uid=uid, av_password=token, authkey="", name=name, plain=True))
    return cameras


def cameras(text: str) -> list[Credentials]:
    """Every camera in the pairing-QR text, in QR order. Raises InvalidQr."""
    raw = _qr_bytes(text)
    if raw[0] == QR_VERSION:
        return _v5_cameras(raw)
    if raw[0] == QR_VERSION_V1:
        return _v1_cameras(raw)
    raise InvalidQr(f"unexpected QR version byte {raw[:1].hex()} (expected 05 or 01)")


def provision(text: str, uid: str | None = None) -> Credentials:
    """The camera with `uid` from the pairing-QR text, or the first. Raises InvalidQr, and KeyError
    when no camera has `uid`."""
    found = cameras(text)
    if uid is None:
        return found[0]
    for c in found:
        if c.uid == uid:
            return c
    raise KeyError(uid)
