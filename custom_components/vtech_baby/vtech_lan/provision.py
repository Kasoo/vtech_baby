"""Per-camera credentials from the parent unit's pairing QR (docs/vtech-lan-kit/docs/05-provisioning.md).

The QR carries the UID and AV password; the authKey is derived from the AV password. Reference
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

QR_VERSION = 0x05
TLV_NAME, TLV_UID, TLV_AVPWD, TLV_MAC = 2, 3, 5, 7
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


class InvalidQr(ValueError):
    """The text is not a pairing QR this module understands."""


@dataclass(frozen=True)
class Credentials:
    uid: str
    av_password: str
    authkey: str
    account: str = AV_ACCOUNT
    name: str = ""
    mac: str = ""


def derive_authkey(av_password: str) -> str:
    """authKey = base62 of the first 8 bytes of HKDF-SHA256(salt=master key, IKM=AV password)."""
    prk = hmac.new(HKDF_MASTER_KEY, av_password.encode(), hashlib.sha256).digest()
    okm = hmac.new(prk, b"\x01", hashlib.sha256).digest()
    return "".join(_BASE62[b % 62] for b in okm[:8])


def decode_qr(text: str) -> dict[int, bytes]:
    """The QR text -> its TLV fields {type: value}. Raises InvalidQr."""
    try:
        raw = base64.b64decode("".join(text.split()) + "===")
    except (binascii.Error, ValueError) as err:
        raise InvalidQr(f"not base64: {err}") from None
    if len(raw) < 3 or raw[0] != QR_VERSION:
        raise InvalidQr(f"unexpected QR version byte {raw[:1].hex() or 'none'} (expected 05)")
    ct = raw[3:]
    if not ct or len(ct) % 16:
        raise InvalidQr(f"ciphertext not block-aligned ({len(ct)} bytes)")
    dec = Cipher(algorithms.AES(QR_AES_KEY), modes.CBC(QR_AES_IV)).decryptor()
    pt = dec.update(ct) + dec.finalize()
    fields, i = {}, 0
    while i + 3 <= len(pt) and pt[i] == 0:
        typ, ln = pt[i + 1], pt[i + 2]
        fields[typ] = pt[i + 3:i + 3 + ln]
        i += 3 + ln
    if not fields.get(TLV_UID) or not fields.get(TLV_AVPWD):
        raise InvalidQr(f"no UID / AV password in the QR (fields {sorted(fields)})")
    return fields


def provision(text: str) -> Credentials:
    """The full credential set from the pairing-QR text."""
    f = decode_qr(text)
    pwd = f[TLV_AVPWD].decode("latin1")
    mac = f.get(TLV_MAC, b"")
    return Credentials(uid=f[TLV_UID].decode("latin1"), av_password=pwd, authkey=derive_authkey(pwd),
                       name=f.get(TLV_NAME, b"").decode("latin1"),
                       mac=":".join(f"{b:02x}" for b in mac))
