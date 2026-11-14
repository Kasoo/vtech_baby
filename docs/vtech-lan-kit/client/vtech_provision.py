#!/usr/bin/env python3
"""vtech_provision.py — offline credential provisioning for the VTech RM5754HD.

Given the parent unit's **"Camera Pairing QR Code"** (the "Connect Mobile Phone"
code), this module recovers the entire LAN-direct credential set needed to connect
(`IOTC_Connect_ByUIDEx` + `avClientStartEx`) — **with no cloud login** — and it
self-heals across credential rotations (re-scan the QR).

The route (specified in `../docs/05-provisioning.md`):

    1. QR string --(base64)--> [ver=5][len:2 big-endian][AES-128-CBC ciphertext]
    2. plaintext = AES-128-CBC(key=QR_AES_KEY, iv=QR_AES_IV).decrypt(ciphertext)   # NoPadding
    3. plaintext is a TLV: 00 <type> <len> <value> ...
         type 2 = device name   type 3 = UID   type 5 = AV password   type 7 = MAC
    4. authKey = base62_8( HKDF-SHA256(salt=HKDF_MASTER_KEY, ikm=AV_password)[:8] each %62 )
    => {uid, account="admin", av_password, authkey}

The three constants below (QR_AES_KEY, QR_AES_IV, HKDF_MASTER_KEY) are **app-wide
static** — the same for every camera of this family and non-rotating. Only the per-camera **UID** and
**AV password** come from the QR; the UID is stable and the AV password rotates (on a
Wi-Fi change or an "update security key" re-pair). Because the authKey is a
deterministic HKDF of the AV password, re-scanning the QR recovers a working set
after any rotation.

Pure-Python crypto (hmac/hashlib) for HKDF; AES-128-CBC via `cryptography` or
`pycryptodome` if present, else a tiny built-in AES-CBC fallback (no third-party dep
required). Reading a QR out of a photo needs `pyzbar` or `opencv-python`; decoding a
QR you have already read as text does not.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass

# --- app-wide static provisioning constants (see ../docs/05-provisioning.md) -----------
# The same for every camera of this family, and not rotating. They are embedded here
# so a user supplies only their own pairing QR.
QR_AES_KEY      = bytes.fromhex("853bc0248503eea465877351f3ae6fe1")  # pairing-QR AES-128 key
QR_AES_IV       = bytes.fromhex("384ce8a20d21a518997189cf62413a0f")  # pairing-QR AES-128 IV
HKDF_MASTER_KEY = bytes.fromhex("45c9d8785a856d4ee1247d91e094175c")  # authKey HKDF salt

AV_ACCOUNT = "admin"  # constant AV-channel account for the RM series
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# TLV field types seen in the pairing QR.
TLV_NAME, TLV_UID, TLV_AVPWD, TLV_MAC = 2, 3, 5, 7


# --- AES-128-CBC (prefer a real lib; fall back to a minimal pure-Python impl) ----------
def _aes_cbc_decrypt(key: bytes, iv: bytes, ct: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        return Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor().update(ct)
    except Exception:
        pass
    try:
        from Crypto.Cipher import AES
        return AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
    except Exception:
        pass
    return _AES(key).cbc_decrypt(iv, ct)


class _AES:
    """Minimal AES-128 (decrypt) + CBC, dependency-free fallback."""
    _SB = None
    _ISB = None
    _RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)

    def __init__(self, key: bytes):
        if _AES._SB is None:
            _AES._build_tables()
        assert len(key) == 16
        self.rk = self._expand(key)

    @classmethod
    def _build_tables(cls):
        p = q = 1
        sbox = [0] * 256
        while True:
            p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
            q ^= q << 1; q ^= q << 2; q ^= q << 4; q &= 0xFF
            if q & 0x80:
                q ^= 0x09
            x = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) \
                  ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4)) ^ 0x63
            sbox[p] = x & 0xFF
            if p == 1:
                break
        sbox[0] = 0x63
        inv = [0] * 256
        for i, v in enumerate(sbox):
            inv[v] = i
        cls._SB, cls._ISB = sbox, inv

    def _expand(self, key):
        w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
        for i in range(4, 44):
            t = list(w[i - 1])
            if i % 4 == 0:
                t = t[1:] + t[:1]
                t = [self._SB[b] for b in t]
                t[0] ^= self._RCON[i // 4 - 1]
            w.append([a ^ b for a, b in zip(w[i - 4], t)])
        return w

    @staticmethod
    def _xtime(a):
        return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else (a << 1)

    def _mul(self, a, b):
        r = 0
        for _ in range(8):
            if b & 1:
                r ^= a
            b >>= 1
            a = self._xtime(a)
        return r

    def _dec_block(self, blk):
        s = [[blk[r + 4 * c] for c in range(4)] for r in range(4)]

        def addrk(rnd):
            for c in range(4):
                for r in range(4):
                    s[r][c] ^= self.rk[rnd * 4 + c][r]

        addrk(10)
        for rnd in range(9, 0, -1):
            for r in range(1, 4):  # inv shiftrows (rotate right by row index)
                s[r] = s[r][-r:] + s[r][:-r]
            for r in range(4):
                for c in range(4):
                    s[r][c] = self._ISB[s[r][c]]
            addrk(rnd)
            for c in range(4):  # inv mixcolumns
                a = [s[r][c] for r in range(4)]
                s[0][c] = self._mul(a[0], 14) ^ self._mul(a[1], 11) ^ self._mul(a[2], 13) ^ self._mul(a[3], 9)
                s[1][c] = self._mul(a[0], 9) ^ self._mul(a[1], 14) ^ self._mul(a[2], 11) ^ self._mul(a[3], 13)
                s[2][c] = self._mul(a[0], 13) ^ self._mul(a[1], 9) ^ self._mul(a[2], 14) ^ self._mul(a[3], 11)
                s[3][c] = self._mul(a[0], 11) ^ self._mul(a[1], 13) ^ self._mul(a[2], 9) ^ self._mul(a[3], 14)
        for r in range(1, 4):  # inv shiftrows for final round
            s[r] = s[r][-r:] + s[r][:-r]
        for r in range(4):
            for c in range(4):
                s[r][c] = self._ISB[s[r][c]]
        addrk(0)
        return bytes(s[r][c] for c in range(4) for r in range(4))

    def cbc_decrypt(self, iv, ct):
        out = bytearray()
        prev = iv
        for i in range(0, len(ct), 16):
            blk = ct[i:i + 16]
            dec = self._dec_block(blk)
            out += bytes(a ^ b for a, b in zip(dec, prev))
            prev = blk
        return bytes(out)


# --- HKDF-SHA256 + base62 (the authKey derivation) -------------------------------------
def hkdf_sha256(salt: bytes, ikm: bytes, info: bytes = b"", length: int = 16) -> bytes:
    """RFC-5869 HKDF-SHA256 (Extract then Expand)."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm, t, ctr = b"", b"", 0
    while len(okm) < length:
        ctr += 1
        t = hmac.new(prk, t + info + bytes([ctr]), hashlib.sha256).digest()
        okm += t
    return okm[:length]


def derive_authkey(av_password: str, master_key: bytes = HKDF_MASTER_KEY) -> str:
    """authKey = base62_8( HKDF-SHA256(salt=master_key, ikm=av_password)[:8] each %62 )."""
    okm = hkdf_sha256(master_key, av_password.encode())
    return "".join(_BASE62[b % 62] for b in okm[:8])


# --- pairing-QR decode -----------------------------------------------------------------
def decode_pairing_qr(qr: str,
                      key: bytes = QR_AES_KEY,
                      iv: bytes = QR_AES_IV) -> dict:
    """Decode the base64 'Camera Pairing QR Code' string into its TLV fields.

    Returns {uid, av_password, name, mac, fields:{type:bytes}}.
    `qr` is the raw text the QR encodes (whitespace tolerated).
    """
    raw = base64.b64decode("".join(qr.split()) + "===")
    if not raw or raw[0] != 5:
        raise ValueError(f"unexpected QR version byte {raw[:1]!r} (expected 0x05)")
    # bytes 1..2 = declared plaintext length; ciphertext is the rest (block-aligned).
    ct = raw[3:]
    if len(ct) % 16:
        raise ValueError(f"ciphertext not block-aligned ({len(ct)} bytes)")
    pt = _aes_cbc_decrypt(key, iv, ct)
    fields, i = {}, 0
    while i + 3 <= len(pt) and pt[i] == 0:
        t, ln = pt[i + 1], pt[i + 2]
        fields[t] = pt[i + 3:i + 3 + ln]
        i += 3 + ln
    if TLV_UID not in fields or TLV_AVPWD not in fields:
        raise ValueError(f"QR decode produced no UID/AV-password (fields={sorted(fields)})")
    mac = fields.get(TLV_MAC, b"")
    return {
        "uid": fields[TLV_UID].decode("latin1"),
        "av_password": fields[TLV_AVPWD].decode("latin1"),
        "name": fields.get(TLV_NAME, b"").decode("latin1"),
        "mac": ":".join(f"{b:02x}" for b in mac) if mac else "",
        "fields": fields,
    }


# --- QR format inspection (for models whose QR is not the documented v5 format) --------
# The documented, provisionable format is version 5 (decode_pairing_qr above). Other VTech models
# emit other formats. inspect_qr() classifies any pairing QR by its version byte so a user can see
# whether their model can be provisioned from the QR, and gather the facts worth reporting for a
# format that is not yet known. See ../docs/05-provisioning.md, "Other QR formats".
def decode_v1_qr(qr: str, key: bytes = QR_AES_KEY, iv: bytes = QR_AES_IV) -> list[dict]:
    """Decode a version-1 pairing QR into its per-camera records.

    A v1 QR is `[0x01][plaintext_len:1][AES-128-CBC ciphertext]` under the same app-wide key/IV as
    the v5 QR. The plaintext is ASCII, comma-separated, four fields per camera, one or more cameras
    per code:  idx,name,uid,token[,idx,name,uid,token ...].

    The 8-character `token` is a registration token, **not** the AV password: a v1 camera does not
    carry its AV password in the QR, so it cannot be provisioned from the QR alone. The UID is
    still correct. Returns [{idx, name, uid, token}].
    """
    raw = base64.b64decode("".join(qr.split()) + "===")
    if not raw or raw[0] != 1:
        raise ValueError(f"not a version-1 QR (version byte {raw[:1]!r})")
    ct = raw[2:]
    if len(ct) % 16:
        raise ValueError(f"ciphertext not block-aligned ({len(ct)} bytes)")
    pt = _aes_cbc_decrypt(key, iv, ct).rstrip(b"\x00")
    parts = pt.decode("latin1").split(",")
    if not parts or len(parts) % 4:
        raise ValueError(f"v1 payload is not a whole number of 4-field records ({len(parts)} fields)")
    return [{"idx": parts[i], "name": parts[i + 1], "uid": parts[i + 2], "token": parts[i + 3]}
            for i in range(0, len(parts), 4)]


def inspect_qr(qr: str) -> dict:
    """Classify a pairing-QR string without assuming the documented v5 format.

    Returns {version, provisionable, verdict, detail, cameras:[{uid,name}]}. `version` is the
    leading byte (or None if the text is not base64); `provisionable` is whether the full
    credential set can be derived from this QR offline.
    """
    try:
        raw = base64.b64decode("".join(qr.split()) + "===")
    except (ValueError, TypeError) as err:  # binascii.Error is a ValueError
        return {"version": None, "provisionable": False, "verdict": "not-base64",
                "detail": f"the text is not valid base64: {err}", "cameras": []}
    if not raw:
        return {"version": None, "provisionable": False, "verdict": "empty",
                "detail": "the text decoded to no bytes", "cameras": []}
    ver = raw[0]
    if ver == 5:
        try:
            d = decode_pairing_qr(qr)
        except ValueError as err:
            return {"version": 5, "provisionable": False, "verdict": "v5-malformed",
                    "detail": f"version-5 header, but the body did not decode: {err}", "cameras": []}
        return {"version": 5, "provisionable": True, "verdict": "v5-ok",
                "detail": "documented format: the QR carries the UID and the AV password, so the "
                          "full credential set can be provisioned offline from it.",
                "cameras": [{"uid": d["uid"], "name": d["name"]}]}
    if ver == 1:
        try:
            cams = decode_v1_qr(qr)
        except ValueError as err:
            return {"version": 1, "provisionable": False, "verdict": "v1-malformed",
                    "detail": f"version-1 header, but the body did not decode: {err}", "cameras": []}
        return {"version": 1, "provisionable": False, "verdict": "v1-registration-token",
                "detail": "this format carries the UID and an 8-character registration token, not "
                          "the AV password. The AV password is not in the QR, so this model cannot "
                          "be provisioned from the QR alone (the UID below is still correct).",
                "cameras": [{"uid": c["uid"], "name": c["name"]} for c in cams]}
    body = raw[1:]
    printable = sum(1 for b in body if 0x20 <= b <= 0x7e)
    shape = ("mostly ASCII text" if body and printable / len(body) > 0.9
             else "block-aligned (looks like AES ciphertext)" if body and len(body) % 16 == 0
             else "not block-aligned and not ASCII")
    return {"version": ver, "provisionable": False, "verdict": "unknown-version",
            "detail": f"version byte 0x{ver:02x} is not a pairing-QR format this tool knows. "
                      f"Total {len(raw)} bytes; body {len(body)} bytes, {shape}. See COMPATIBILITY.md "
                      "for what to report — report this line, never the QR text itself.",
            "cameras": []}


def format_inspection(info: dict) -> str:
    """One-screen human summary of inspect_qr()'s result."""
    head = (f"QR format: version {info['version']}" if info["version"] is not None
            else "QR format: unrecognised")
    lines = [f"{head}  ({info['verdict']})", info["detail"]]
    for c in info["cameras"]:
        lines.append(f"  camera: UID={c['uid']}  name={c['name']}")
    lines.append("provisionable offline from this QR: " + ("yes" if info["provisionable"] else "no"))
    return "\n".join(lines)


def read_qr_image(path: str) -> str:
    """Read the base64 pairing-QR payload from an image file (a photo of the parent unit).

    Tries pyzbar, then OpenCV's QRCodeDetector (with a few preprocessing fallbacks for
    glare/perspective). Needs one of those libs installed; the crypto path above does not.
    """
    try:
        from pyzbar.pyzbar import decode as _zbar
        from PIL import Image
        res = _zbar(Image.open(path))
        if res:
            return res[0].data.decode("latin1")
    except Exception:
        pass
    try:
        import cv2
        im = cv2.imread(path)
        if im is None:
            raise ValueError(f"cannot read image: {path}")
        det = cv2.QRCodeDetector()
        data, _, _ = det.detectAndDecode(im)
        if data:
            return data
        gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        for scale in (1, 2, 3):
            g = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            for prep in ("raw", "otsu", "adaptive"):
                if prep == "raw":
                    t = g
                elif prep == "otsu":
                    _, t = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                else:
                    t = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                              cv2.THRESH_BINARY, 31, 5)
                data, _, _ = det.detectAndDecode(t)
                if data:
                    return data
        raise ValueError("no QR code decoded from image (try a sharper, flatter photo)")
    except ImportError:
        raise RuntimeError("reading a QR image needs 'pyzbar' or 'opencv-python' installed; "
                           "or decode the QR yourself and pass the text via --qr")


@dataclass
class Credentials:
    uid: str
    account: str
    av_password: str
    authkey: str

    def as_env(self) -> str:
        return (f"export VTECH_UID={self.uid}\n"
                f"export VTECH_ACCOUNT={self.account}\n"
                f"export VTECH_PWD={self.av_password}\n"
                f"export VTECH_AUTHKEY={self.authkey}\n")


def provision_from_qr(qr: str) -> Credentials:
    """The complete offline route: pairing QR string -> full LAN-direct credential set."""
    d = decode_pairing_qr(qr)
    return Credentials(uid=d["uid"], account=AV_ACCOUNT,
                       av_password=d["av_password"],
                       authkey=derive_authkey(d["av_password"]))


def provision_from_image(path: str) -> Credentials:
    """End-to-end: a photo of the pairing QR -> full LAN-direct credential set."""
    return provision_from_qr(read_qr_image(path))


# --- self-test -------------------------------------------------------------------------
# Synthetic vectors (no real camera's credentials belong in this repo). The QR is built with the app-wide
# QR key above; the authKey is this module's own derivation of the made-up AV password, which was checked
# against a real camera when the derivation was worked out.
_SELFTEST_QR = ("BQBAnKyf45uQj6cMJVH0WNRYgaghpJ8gJgVv7vpkFYAp84R070RCMv02XQkQjX+dd5jaHj2tIygYhCWru7q9kj4JNg==")
_EXPECT_UID = "TESTUID0000000000001"
_EXPECT_AVPWD = "Pw0rdXyz"
_DERIV_VECTOR = ("Pw0rdXyz", "tPVrrfrL")  # AV password -> authKey
# Synthetic version-1 QR (fabricated: two cameras, 8-char registration tokens, not AV passwords).
# Built with the app-wide QR key/IV above; exercises the non-v5 inspection path only.
_SELFTEST_V1_QR = ("AV9CTHbs9uYqrsCWjQlhQ11bqghEzSpBAYJ9aj6/n9TVGUmSXuXkqffEEKbASGvzuyNd2YGYEqctlX9K"
                   "GDo8q2nJyncaY2xKR1YjY6QFpcNtvyOMXXqJVrdk5ULGK7Wfj3M=")


def _selftest() -> int:
    ok = True

    ak = derive_authkey(_DERIV_VECTOR[0])
    print(f"[derive] HKDF({_DERIV_VECTOR[0]}) = {ak}  (expect {_DERIV_VECTOR[1]})",
          "OK" if ak == _DERIV_VECTOR[1] else "FAIL")
    ok &= ak == _DERIV_VECTOR[1]

    d = decode_pairing_qr(_SELFTEST_QR)
    print(f"[qr] uid={d['uid']} avpwd={d['av_password']} name={d['name']} mac={d['mac']}")
    uid_ok = d["uid"] == _EXPECT_UID
    pwd_ok = d["av_password"] == _EXPECT_AVPWD
    print(f"[qr] UID {'OK' if uid_ok else 'FAIL'}, AV-pwd {'OK' if pwd_ok else 'FAIL'}")
    ok &= uid_ok and pwd_ok

    c = provision_from_qr(_SELFTEST_QR)
    print(f"[provision] {c.uid} / {c.account} / {c.av_password} / authkey={c.authkey}")
    ak_b62 = all(ch in _BASE62 for ch in c.authkey) and len(c.authkey) == 8
    print(f"[provision] authkey well-formed base62x8: {'OK' if ak_b62 else 'FAIL'}")
    ok &= ak_b62

    i5 = inspect_qr(_SELFTEST_QR)
    print(f"[inspect] v5 -> version={i5['version']} verdict={i5['verdict']} provisionable={i5['provisionable']}")
    ok &= i5["version"] == 5 and i5["provisionable"]
    i1 = inspect_qr(_SELFTEST_V1_QR)
    v1_uids = [c["uid"] for c in i1["cameras"]]
    print(f"[inspect] v1 -> version={i1['version']} verdict={i1['verdict']} "
          f"provisionable={i1['provisionable']} uids={v1_uids}")
    ok &= (i1["version"] == 1 and not i1["provisionable"]
           and v1_uids == ["TESTUID0000000000001", "TESTUID0000000000002"])

    print("\nSELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="VTech RM5754HD offline credential provisioning")
    ap.add_argument("--qr", help="pairing-QR text (base64) to provision from")
    ap.add_argument("--image", help="photo of the pairing QR to provision from (needs pyzbar/opencv)")
    ap.add_argument("--avpwd", help="derive authKey from this AV password only")
    ap.add_argument("--inspect", action="store_true",
                    help="classify the QR's format (works for non-v5 QRs) instead of provisioning")
    ap.add_argument("--selftest", action="store_true", help="run built-in known-vector self-test")
    ap.add_argument("--env", action="store_true", help="print as VTECH_* exports")
    a = ap.parse_args()

    if a.selftest:
        sys.exit(_selftest())
    if a.inspect:
        text = a.qr or (read_qr_image(a.image) if a.image else None)
        if not text:
            ap.error("--inspect needs --qr TEXT or --image FILE")
        print(format_inspection(inspect_qr(text)))
        sys.exit(0)
    if a.avpwd:
        print(derive_authkey(a.avpwd))
        sys.exit(0)
    if a.qr or a.image:
        c = provision_from_qr(a.qr) if a.qr else provision_from_image(a.image)
        print(c.as_env() if a.env else c)
        sys.exit(0)
    ap.print_help()
