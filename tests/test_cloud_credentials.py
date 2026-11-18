"""The cloud credentials helper (tools/vtech_cloud_credentials.py), offline: a fake transport stands in for
VTech's servers. Synthetic accounts and cameras only."""
import base64
import importlib.util
import json
import os
import re
import subprocess
import sys
import types
from datetime import datetime

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from vtech_lan import provision

_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools", "vtech_cloud_credentials.py")
_spec = importlib.util.spec_from_file_location("vtech_cloud_credentials", _PATH)
vcc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = vcc                   # dataclasses resolve their module by name
_spec.loader.exec_module(vcc)

EMAIL = "parent@example.com"
CONVERTED = "0" * 64 + "@vtechinterimeu.com"
UID = "TESTUID0000000000001"
SECRET = b"0123456789abcdef0123456789abcdef"          # a synthetic machine id
UUID = vcc.device_uuid(EMAIL, SECRET)


def test_device_uuid_is_stable_per_mailbox_and_machine():
    u = vcc.device_uuid(EMAIL, SECRET)
    assert re.fullmatch(r"AN\d{22}", u)
    assert vcc.device_uuid("  Parent@Example.COM ", SECRET) == u
    assert vcc.device_uuid("other@example.com", SECRET) != u
    assert vcc.device_uuid(EMAIL, b"fedcba9876543210fedcba9876543210") != u
    # The embedded timestamp is a real date and the rand is in range.
    s, r = u[2:8] + u[12:20], u[20:24] + u[8:12]
    datetime.strptime(s, "%Y%m%d%H%M%S")
    assert 10_000_000 <= int(r) < 21_111_112


@pytest.mark.parametrize("content, expected", [
    ("0123456789abcdef0123456789abcdef\n", b"0123456789abcdef0123456789abcdef"),
    ("", None), ("00000000000000000000000000000000\n", None), ("uninitialized\n", None)])
def test_machine_secret_linux(monkeypatch, tmp_path, content, expected):
    path = tmp_path / "machine-id"
    path.write_text(content)
    monkeypatch.setattr(vcc.sys, "platform", "linux")
    monkeypatch.setattr(vcc, "MACHINE_ID_PATHS", (str(tmp_path / "missing"), str(path)))
    assert vcc.machine_secret() == expected


def test_machine_secret_macos(monkeypatch):
    out = '+-o J314sAP  <class IOPlatformExpertDevice>\n    "IOPlatformUUID" = "1A2B3C4D-0000-4000-8000-00000000ABCD"\n'
    monkeypatch.setattr(vcc.sys, "platform", "darwin")
    monkeypatch.setattr(vcc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, out, ""))
    assert vcc.machine_secret() == b"1A2B3C4D-0000-4000-8000-00000000ABCD"


def test_machine_secret_windows(monkeypatch):
    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    winreg = types.SimpleNamespace(HKEY_LOCAL_MACHINE=0, KEY_READ=1, KEY_WOW64_64KEY=2,
                                   OpenKey=lambda *a: Key(),
                                   QueryValueEx=lambda key, name: ("5f2c0d4e-0000-4000-8000-0000000000aa", 1))
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    monkeypatch.setattr(vcc.sys, "platform", "win32")
    assert vcc.machine_secret() == b"5f2c0d4e-0000-4000-8000-0000000000aa"


def test_helper_uuid(monkeypatch):
    said = []
    monkeypatch.setenv("VTECH_UUID", "AN0000000000000000000001")
    assert vcc.helper_uuid(EMAIL, said.append) == "AN0000000000000000000001"
    monkeypatch.delenv("VTECH_UUID")
    monkeypatch.setattr(vcc, "machine_secret", lambda: SECRET)
    assert vcc.helper_uuid(EMAIL, said.append) == UUID and not said
    monkeypatch.setattr(vcc, "machine_secret", lambda: None)
    a, b = vcc.helper_uuid(EMAIL, said.append), vcc.helper_uuid(EMAIL, said.append)
    assert a != b and re.fullmatch(r"AN\d{22}", a) and "every run" in said[0]


def test_authkey_matches_the_integration():
    assert vcc.derive_authkey("Pw0rdXyz") == provision.derive_authkey("Pw0rdXyz")
    assert vcc.HKDF_MASTER_KEY == provision.HKDF_MASTER_KEY


def test_rsa_oaep_decrypts_with_a_standard_implementation():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    spki = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    ct = vcc.rsa_oaep_sha1(spki, b"hello")
    assert len(ct) == 256
    oaep = padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None)
    assert key.decrypt(ct, oaep) == b"hello"
    assert vcc.rsa_oaep_sha1(spki, b"hello") != ct                # random padding


def test_trust_header_shape():
    h = vcc.trust_header(CONVERTED, "secret", UUID)
    assert h.startswith("Custom ") and len(base64.b64decode(h[7:])) == 256
    n, e = vcc._rsa_public_key(base64.b64decode(vcc.TRUST_KEY))
    assert n.bit_length() == 2048 and e == 65537


class FakeCloud:
    """VTech's three hosts. `trusted` decides whether the login needs the emailed code."""

    def __init__(self, trusted=True, password="secret", code="123456", account=True):
        self.trusted, self.password, self.code, self.account = trusted, password, code, account
        self.calls = []

    def __call__(self, method, url, headers, body):
        body = json.loads(body) if body else None
        self.calls.append((method, url, headers, body))
        path = url.split("/", 3)[3]
        if path == "api/account/search":
            return 200, b"{}"
        if path == "api/account":
            return (200, json.dumps({"converted_data": {"email": CONVERTED}}).encode()) if self.account \
                else (404, b'{"message": "not found"}')
        if path == "api/v2/account/login":
            assert body["email"] == CONVERTED and body["uuid"] == UUID
            if body["password"] != self.password:
                return 401, b'{"message": "invalid credentials"}'
            return (200, self._tokens()) if self.trusted else (204, b"")
        if path == "api/v2/account/authorize":
            assert headers["Authorization"].startswith("Custom ")
            if body["code"] != self.code:
                return 400, b'{"message": "bad code"}'
            self.trusted = True
            return 200, self._tokens()
        if path == "api/v1/devices/":
            assert headers["Authorization"] == "Bearer tok"
            return 200, json.dumps({"result": [
                {"uuid": UID, "account": "admin", "nickname": "Nursery", "info": {"password": "Pw0rdXyz"}},
                {"uid": "TESTUID0000000000002", "password": "Tok3nAB1", "info": {"username": "admin"}},
                {"uuid": "TESTUID0000000000003", "info": {}},                  # no password: skipped
            ], "code": 0, "message": ""}).encode()
        raise AssertionError(url)

    @staticmethod
    def _tokens():
        return json.dumps({"token_type": "Bearer", "access_token": "tok", "refresh_token": "rt"}).encode()


def fetch(cloud, password="secret", code="123456"):
    return vcc.fetch_cameras(EMAIL, password, lambda: code, uuid=UUID, client=vcc.Client(cloud))


def test_trusted_device_lists_the_cameras():
    cloud = FakeCloud()
    cams = fetch(cloud)
    assert [(c.name, c.uid, c.av_password, c.authkey) for c in cams] == [
        ("Nursery", UID, "Pw0rdXyz", provision.derive_authkey("Pw0rdXyz")),
        ("", "TESTUID0000000000002", "Tok3nAB1", provision.derive_authkey("Tok3nAB1"))]
    merchant = cloud.calls[0][2]
    assert merchant["x-merchant-id"] == "vt1080p" and merchant["x-mobile-notification-id"] == UUID
    assert all(h["User-Agent"] == "okhttp/3.12.13" for _, _, h, _ in cloud.calls)
    login = {k.lower() for k in cloud.calls[2][2]}
    assert not login & {"x-merchant-id", "x-mobile-notification-id", "x-mobile-os", "authorization"}


def test_untrusted_device_redeems_the_emailed_code():
    cloud = FakeCloud(trusted=False)
    assert [c.uid for c in fetch(cloud)][0] == UID
    method, url, headers, body = cloud.calls[3]
    assert url.endswith("/api/v2/account/authorize") and body == {"code": "123456"}
    assert set(headers) == {"User-Agent", "Accept", "Accept-Encoding", "Content-Type", "Authorization"}
    assert cloud.trusted


def test_wrong_code_and_wrong_password_are_reported():
    with pytest.raises(vcc.CloudError, match="verification failed: HTTP 400"):
        fetch(FakeCloud(trusted=False), code="000000")
    with pytest.raises(vcc.CloudError, match="login refused"):
        fetch(FakeCloud(), password="wrong")
    with pytest.raises(vcc.CloudError, match="no VTech account"):
        fetch(FakeCloud(account=False))


def test_render_formats():
    cams = [vcc.Camera("Nursery", UID, "admin", "Pw0rdXyz", "k3yAuth8")]
    assert vcc.render(cams, "table").splitlines()[1].split() == ["Nursery", UID, "Pw0rdXyz"]
    assert "VTECH_UID=" + UID in vcc.render(cams, "env")
    assert json.loads(vcc.render(cams, "json"))[0]["av_password"] == "Pw0rdXyz"


def test_no_tty_and_no_password_is_an_error(monkeypatch, capsys):
    monkeypatch.setenv("VTECH_EMAIL", EMAIL)
    monkeypatch.delenv("VTECH_PASSWORD", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert vcc.main([]) == 1
    assert "Password needed" in capsys.readouterr().err


def test_malformed_device_list_is_skipped():
    def cloud(method, url, headers, body):
        if url.endswith("/api/v1/devices/"):
            return 200, b'{"result": ["junk", {"uuid": "TESTUID0000000000001", "info": "junk", "password": "p"}]}'
        return FakeCloud()(method, url, headers, body)
    cams = vcc.fetch_cameras(EMAIL, "secret", lambda: "", uuid=UUID, client=vcc.Client(cloud))
    assert [(c.uid, c.av_password) for c in cams] == [(UID, "p")]


def test_prompts_go_to_stderr(monkeypatch, capsys):
    monkeypatch.delenv("VTECH_EMAIL", raising=False)
    monkeypatch.setenv("VTECH_PASSWORD", "secret")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: EMAIL)
    monkeypatch.delenv("VTECH_UUID", raising=False)
    monkeypatch.setattr(vcc, "machine_secret", lambda: SECRET)
    assert vcc.main(["--format", "json"], transport=FakeCloud()) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)[0]["uid"] == UID and "VTech account email" in out.err
