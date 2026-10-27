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

