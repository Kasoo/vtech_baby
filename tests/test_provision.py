"""Pairing-QR provisioning (docs/vtech-lan-kit/docs/05-provisioning.md), on synthetic QRs only.

Parity with the kit's reference implementation is checked by importing it from the kit.
"""
import base64
import importlib.util
import os
import struct
import sys

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from vtech_lan import provision
from vtech_lan.provision import InvalidQr

KIT_CLIENT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "vtech-lan-kit", "client")


def _kit(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(KIT_CLIENT, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve their module by name
    spec.loader.exec_module(mod)
    return mod


def make_qr(fields, version=0x05):
    pt = b"".join(b"\x00" + bytes([t, len(v)]) + v for t, v in fields)
    pt += b"\x00" * (-len(pt) % 16)
    enc = Cipher(algorithms.AES(provision.QR_AES_KEY), modes.CBC(provision.QR_AES_IV)).encryptor()
    ct = enc.update(pt) + enc.finalize()
    return base64.b64encode(bytes([version]) + struct.pack(">H", len(pt)) + ct).decode()


FIELDS = [(2, b"VTECH_5754_test"), (3, b"TESTUID0000000000001"), (5, b"Pw0rdXyz"),
          (7, bytes.fromhex("0011223344ff"))]


def make_v1_qr(cameras):
    """A synthetic version-1 QR: [0x01][len:1][AES-CBC CSV], four fields per camera."""
    pt = ",".join(f"{i + 1},{name},{uid},{tok}" for i, (name, uid, tok) in enumerate(cameras)).encode()
    padded = pt + b"\x00" * (-len(pt) % 16)
    enc = Cipher(algorithms.AES(provision.QR_AES_KEY), modes.CBC(provision.QR_AES_IV)).encryptor()
    ct = enc.update(padded) + enc.finalize()
    return base64.b64encode(bytes([0x01, len(pt) & 0xFF]) + ct).decode()


V1_CAMERAS = [("VTECH_5764_abcd", "TESTUID0000000000001", "Tok3nAB1"),
              ("VTECH_5764_ef01", "TESTUID0000000000002", "Tok3nCD2")]


def test_provision_roundtrip():
    c = provision.provision(make_qr(FIELDS))
    assert (c.uid, c.av_password, c.account) == ("TESTUID0000000000001", "Pw0rdXyz", "admin")
    assert c.name == "VTECH_5754_test" and c.mac == "00:11:22:33:44:ff"
    assert c.authkey == provision.derive_authkey("Pw0rdXyz")
    assert len(c.authkey) == 8 and c.authkey.isalnum()


def test_whitespace_and_line_breaks_tolerated():
    qr = make_qr(FIELDS)
    assert provision.provision(" " + qr[:20] + "\n" + qr[20:] + "\n").uid == "TESTUID0000000000001"


@pytest.mark.parametrize("text", ["", "not base64 !!!", base64.b64encode(b"\x05\x00\x10short").decode()])
def test_garbage_rejected(text):
    with pytest.raises(InvalidQr):
        provision.provision(text)


def test_wrong_version_rejected():
    with pytest.raises(InvalidQr, match="version"):
        provision.provision(make_qr(FIELDS, version=0x03))


def test_v1_qr_is_a_plain_camera_first_record():
    c = provision.provision(make_v1_qr(V1_CAMERAS))
    assert (c.uid, c.av_password, c.authkey, c.plain) == ("TESTUID0000000000001", "Tok3nAB1", "", True)
    assert c.name == "VTECH_5764_abcd"


def test_v1_qr_lists_every_camera():
    found = provision.cameras(make_v1_qr(V1_CAMERAS))
    assert [(c.uid, c.av_password, c.plain) for c in found] == [
        ("TESTUID0000000000001", "Tok3nAB1", True), ("TESTUID0000000000002", "Tok3nCD2", True)]


def test_provision_picks_by_uid():
    c = provision.provision(make_v1_qr(V1_CAMERAS), uid="TESTUID0000000000002")
    assert (c.uid, c.av_password) == ("TESTUID0000000000002", "Tok3nCD2")
    with pytest.raises(KeyError):
        provision.provision(make_v1_qr(V1_CAMERAS), uid="TESTUID0000000000009")


def make_qr_cameras(cameras):
    """A synthetic version-5 QR with several cameras: [(index, fields)]."""
    pt = b"".join(bytes([idx, t, len(v)]) + v for idx, fields in cameras for t, v in fields)
    pt += b"\x00" * (-len(pt) % 16)
    enc = Cipher(algorithms.AES(provision.QR_AES_KEY), modes.CBC(provision.QR_AES_IV)).encryptor()
    ct = enc.update(pt) + enc.finalize()
    return base64.b64encode(bytes([0x05]) + struct.pack(">H", len(pt)) + ct).decode()


V5_CAMERAS = [
    (0, [(2, b"VTECH_5754_aaaa"), (3, b"TESTUID0000000000001"), (5, b"Pw0rdXyz"), (7, b"\x00\x11\x22\x33\x44\x55")]),
    (1, [(2, b"VTECH_5754_bbbb"), (3, b"TESTUID0000000000002"), (4, b"Pl4inPwd")]),
]


def test_v5_qr_with_several_cameras_and_both_modes():
    a, b = provision.cameras(make_qr_cameras(V5_CAMERAS))
    assert (a.uid, a.av_password, a.authkey, a.plain, a.mac) == (
        "TESTUID0000000000001", "Pw0rdXyz", provision.derive_authkey("Pw0rdXyz"), False, "00:11:22:33:44:55")
    assert (b.uid, b.av_password, b.authkey, b.plain, b.name) == (
        "TESTUID0000000000002", "Pl4inPwd", "", True, "VTECH_5754_bbbb")


def test_v5_unsupported_model_is_skipped():
    flagged = (2, [(3, b"TESTUID0000000000003"), (5, b"Xx9Yy8Zz"), (8, b"\x01")])
    assert [c.uid for c in provision.cameras(make_qr_cameras([*V5_CAMERAS, flagged]))] == [
        "TESTUID0000000000001", "TESTUID0000000000002"]
    with pytest.raises(provision.UnsupportedModel):
        provision.cameras(make_qr_cameras([flagged]))


def test_v5_both_password_types_is_enhanced():
    c = provision.provision(make_qr([(3, b"TESTUID0000000000001"), (4, b"Pl4inPwd"), (5, b"Pw0rdXyz")]))
    assert (c.av_password, c.plain) == ("Pw0rdXyz", False)


def test_parity_with_kit_several_cameras():
    kit = _kit("vtech_provision")
    for qr in (make_qr_cameras(V5_CAMERAS), make_v1_qr(V1_CAMERAS)):
        ours, theirs = provision.cameras(qr), kit.provision_all_from_qr(qr)
        assert [(c.uid, c.av_password, c.authkey, c.plain) for c in ours] == \
            [(c.uid, c.av_password, c.authkey, c.plain) for c in theirs]


def test_v1_shaped_but_implausible_is_invalid():
    bogus = base64.b64encode(bytes([0x01, 0x10]) + b"\x00" * 16).decode()
    with pytest.raises(InvalidQr):
        provision.provision(bogus)


def test_missing_password_rejected():
    with pytest.raises(InvalidQr, match="no UID"):
        provision.provision(make_qr([(3, b"TESTUID0000000000001")]))


@pytest.mark.parametrize("pwd", ["Pw0rdXyz", "a", "x" * 32])
def test_parity_with_kit(pwd):
    kit = _kit("vtech_provision")
    qr = make_qr([(3, b"TESTUID0000000000001"), (5, pwd.encode())])
    ours, theirs = provision.provision(qr), kit.provision_from_qr(qr)
    assert (ours.uid, ours.av_password, ours.authkey, ours.account) == \
        (theirs.uid, theirs.av_password, theirs.authkey, theirs.account)
    assert (provision.QR_AES_KEY, provision.QR_AES_IV, provision.HKDF_MASTER_KEY) == \
        (kit.QR_AES_KEY, kit.QR_AES_IV, kit.HKDF_MASTER_KEY)

