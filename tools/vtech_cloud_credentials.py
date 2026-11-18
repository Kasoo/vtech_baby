#!/usr/bin/env python3
"""Print the LAN credentials of the cameras on a VTech cloud account.

For cameras set up with the MyVTech Baby Pro app, when the pairing QR can't be used. It logs in to the
account the way the app does, reads the account's device list, and prints each camera's UID and AV
password, which the integration's "Type the camera's UID and password" option takes. Protocol:
docs/vtech-lan-kit/docs/06-cloud-credentials.md.

    python3 vtech_cloud_credentials.py [--email EMAIL] [--format table|env|json] [--trace]

Python 3.10 or later, standard library only. Nothing is written to disk. The account password is read
from VTECH_PASSWORD or prompted for; it is never a command-line argument.

The first run for an account adds this helper as a trusted device: VTech emails a verification code,
which the helper asks for (or reads from VTECH_CODE). Later runs on the same computer with the same email
skip that, because the helper's device id is derived from this computer's machine id and the email. The
account then lists one more trusted device. Without a machine id, every run asks for a code.

Environment: VTECH_EMAIL, VTECH_PASSWORD, VTECH_CODE (the emailed code), VTECH_UUID (use this device id
instead of the derived one, e.g. a phone's that is already trusted).
"""
from __future__ import annotations

import argparse
import base64
import getpass
import gzip
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

# App-wide constants (06-cloud-credentials.md, "App-wide constants"): the same for every install.
AM1 = "https://myvtechbaby-am1.vtech.com"
MERCHANT = "https://myvt1080p-api.vtech.com"
VSAAS = "https://vsaas-gateway.kalayservice.com"
MERCHANT_ID = "vt1080p"
USER_AGENT = "okhttp/3.12.13"
IRM_HEADER = ("AQIDAHiXwuowIuwyLk0viMpK/XU7WgY4lImlNyg5wEuWYgMg8AHt5zQhHPBt8iFK4bb2UJ/"
              "KAAAAfjB8BgkqhkiG9w0BBwagbzBtAgEAMGgGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM"
              "Q8b/MB+KOiDqKwOxAgEQgDvhqu5OB21j8KdMa8oUPJrrrgQ+Fz5xWj1aaneWPDr7nH81iCZXS"
              "BwSFRuNeUPrU/OsuvDYRZladh/hmQ==")
TRUST_KEY = ("MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0crgmTCeUJW2O8ERGLuwDKCEjJwTr9K9zVc3W6l"
             "YmtrN75LCzhZ4JSsU+4amHUQXzORx1GNooAFbYa5Q+Whu7NlY0FBfaJCpFnUwoG242JlmITWM6NILApxo6u"
             "xpU5Xl1z7o5jJsNEkcMrvlR5zwP+vx1FzjNA2D9l/diyC2st9CfwXiXnH+AtTyA3NB66uKHeJ25qpm6rMl2"
             "LvtsLQ7FP879u8TVLXyd8nr/Ii57gXJjSpZn89eONp6xTy/9P9aY5VolkDABxjaUTXy9QlYGzrdYW/rGDyH"
             "bD/Z/2aFP6coA07W834vqAZiTItKxFyZ5p3sPuP38q0W8nSu9i8oAwIDAQAB")   # RSA-2048, SPKI
HKDF_MASTER_KEY = bytes.fromhex("45c9d8785a856d4ee1247d91e094175c")         # as in 05-provisioning.md
DEVICE_SALT = b"vtech-baby-creds-helper/v2"
MACHINE_ID_PATHS = ("/etc/machine-id", "/var/lib/dbus/machine-id")
DEVICE_NAME = "creds-helper"
TIMEOUT_S = 30
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


class CloudError(Exception):
    """A failed step, with a message for the user."""


@dataclass(frozen=True)
class Camera:
    name: str
    uid: str
    account: str
    av_password: str
    authkey: str            # for an enhanced camera; a plain one uses none


# ── Derivations ──────────────────────────────────────────────────────────────
def derive_authkey(av_password: str) -> str:
    """base62 of the first 8 bytes of HKDF-SHA256(salt=master key, IKM=AV password)."""
    prk = hmac.new(HKDF_MASTER_KEY, av_password.encode(), hashlib.sha256).digest()
    okm = hmac.new(prk, b"\x01", hashlib.sha256).digest()
    return "".join(_BASE62[b % 62] for b in okm[:8])


def machine_secret() -> bytes | None:
    """This computer's install id (systemd machine-id, macOS IOPlatformUUID, Windows MachineGuid), or None.
    It is used only as an HMAC key, never sent."""
    value = ""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                value = str(winreg.QueryValueEx(key, "MachineGuid")[0])
        except OSError:
            pass
    elif sys.platform == "darwin":
        try:
            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True,
                                 text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
        value = m.group(1) if m else ""
    else:
        for path in MACHINE_ID_PATHS:
            try:
                with open(path, encoding="ascii") as f:
                    value = f.read().strip()
            except (OSError, ValueError):
                continue
            if value:
                break
    value = value.strip()
    # Skip an empty, all-zero or not-yet-written id: every such computer would share it.
    if not value.strip("0-") or value == "uninitialized":
        return None
    return value.encode()


def device_uuid(email: str, secret: bytes) -> str:
    """The helper's device id for an account: "AN" + 22 digits, keyed by `secret` (the machine id), so it is
    the same on every run on this computer and can't be computed from the email alone."""
    e = unicodedata.normalize("NFC", email).strip().casefold()
    h = hmac.new(secret, DEVICE_SALT + b"\0" + e.encode(), hashlib.sha256).digest()
    r = str(10_000_000 + int.from_bytes(h[0:6], "big") % 11_111_112)
    when = datetime(2020, 1, 1) + timedelta(seconds=int.from_bytes(h[6:12], "big") % (5 * 365 * 24 * 3600))
    s = when.strftime("%Y%m%d%H%M%S")
    return "AN" + s[0:6] + r[4:8] + s[6:] + r[0:4]


def _der(buf: bytes, i: int) -> tuple[bytes, int]:
    """One DER TLV at `i`: its value and the index after it."""
    n = buf[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(buf[i:i + k], "big")
        i += k
    return buf[i:i + n], i + n


def _rsa_public_key(spki: bytes) -> tuple[int, int]:
    """(n, e) of an RSA SubjectPublicKeyInfo."""
    outer, _ = _der(spki, 0)
    _, i = _der(outer, 0)                       # AlgorithmIdentifier
    bits, _ = _der(outer, i)
    key, _ = _der(bits[1:], 0)                  # after the unused-bits byte
    n, i = _der(key, 0)
    e, _ = _der(key, i)
    return int.from_bytes(n, "big"), int.from_bytes(e, "big")


def _mgf1(seed: bytes, length: int) -> bytes:
    out = b"".join(hashlib.sha1(seed + c.to_bytes(4, "big")).digest() for c in range(length // 20 + 1))
    return out[:length]


def rsa_oaep_sha1(spki: bytes, message: bytes, seed: bytes | None = None) -> bytes:
    """RSAES-OAEP encryption with SHA-1, MGF1(SHA-1) and an empty label (RFC 8017)."""
    n, e = _rsa_public_key(spki)
    k = (n.bit_length() + 7) // 8
    if len(message) > k - 42:
        raise CloudError("the account email and password are too long to send")
    db = hashlib.sha1(b"").digest() + b"\x00" * (k - len(message) - 42) + b"\x01" + message
    seed = seed if seed is not None else os.urandom(20)
    masked_db = bytes(a ^ b for a, b in zip(db, _mgf1(seed, k - 21)))
    masked_seed = bytes(a ^ b for a, b in zip(seed, _mgf1(masked_db, 20)))
    return pow(int.from_bytes(b"\x00" + masked_seed + masked_db, "big"), e, n).to_bytes(k, "big")


def trust_header(converted_email: str, password: str, uuid: str) -> str:
    """The `Authorization` header that redeems the emailed code for this device."""
    plain = f"{converted_email}:{base64.b64encode(password.encode()).decode()}:{uuid}".encode()
    return "Custom " + base64.b64encode(rsa_oaep_sha1(base64.b64decode(TRUST_KEY), plain)).decode()


# ── HTTP ─────────────────────────────────────────────────────────────────────
# (method, url, headers, body) -> (status, response body)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """None of these endpoints redirect; following one would send the auth headers elsewhere."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()),
                                      _NoRedirect())


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    host = url.split("/")[2]
    try:
        try:
            with _OPENER.open(req, timeout=TIMEOUT_S) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as err:
            status, raw = err.code, err.read()
        return status, gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    except (urllib.error.URLError, OSError, http.client.HTTPException) as err:
        raise CloudError(f"can't reach {host}: {getattr(err, 'reason', err)}") from None
    except (EOFError, zlib.error) as err:           # gzip.BadGzipFile is an OSError
        raise CloudError(f"bad response from {host}: {err}") from None


_SECRET_KEYS = {"password", "access_token", "refresh_token", "token", "code", "authorization", "uuid", "uid"}


def _redact(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: "<redacted>" if k.lower() in _SECRET_KEYS else _redact(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_redact(x) for x in o]
    return o


class Client:
    def __init__(self, transport: Transport = urllib_transport, trace: Callable[[str], None] | None = None):
        self._transport, self._trace = transport, trace

    def call(self, step: str, method: str, url: str, headers: dict[str, str] | None = None,
             body: dict[str, Any] | None = None, ok: tuple[int, ...] = (200,)) -> tuple[int, dict[str, Any]]:
        """One request; returns the status and the JSON body ({} when empty). Any status not in `ok`
        raises CloudError naming the step."""
        h = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip", **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json; charset=utf-8"
        if self._trace:
            self._trace(f">>> {method} {url} {json.dumps(_redact(body)) if body is not None else ''}")
        status, raw = self._transport(method, url, h, data)
        try:
            out = json.loads(raw) if raw.strip() else {}
        except ValueError:
            out = {"_text": raw[:300].decode("latin1")}
        if self._trace:
            self._trace(f"<<< HTTP {status} {json.dumps(_redact(out))[:1000]}")
        if status not in ok:
            raise CloudError(f"{step} failed: HTTP {status} {_message(out)}".rstrip())
        return status, out if isinstance(out, dict) else {"_value": out}


def _message(body: dict[str, Any]) -> str:
    for k in ("message", "error_description", "error", "detail", "_text"):
        if body.get(k):
            return f"({body[k]})"
    return ""


# ── The flow (06-cloud-credentials.md, "The login + fetch sequence") ─────────
def helper_uuid(email: str, say: Callable[[str], None]) -> str:
    """VTECH_UUID if set, else the id keyed by this computer's machine id, else a one-off random id."""
    if uuid := os.environ.get("VTECH_UUID", "").strip():
        return uuid
    secret = machine_secret()
    if secret is None:
        say("No machine id found on this computer, so VTech will ask for a code on every run "
            "(to keep one device id, set VTECH_UUID to a fixed id of your own: \"AN\" and 22 digits).")
        secret = secrets.token_bytes(32)
    return device_uuid(email, secret)


def fetch_cameras(email: str, password: str, ask_code: Callable[[], str], *, uuid: str,
                  client: Client | None = None, say: Callable[[str], None] = lambda _: None) -> list[Camera]:
    client = client or Client()
    merchant = {"x-merchant-id": MERCHANT_ID, "x-mobile-notification-id": uuid,
                "x-mobile-os": f"android9,{DEVICE_NAME}", "authorization": IRM_HEADER}

    _, found = client.call("account lookup", "POST", f"{MERCHANT}/api/account/search", merchant, {"email": email},
                           ok=(200, 404))
    _, account = client.call("account lookup", "POST", f"{MERCHANT}/api/account", merchant, {"email": email},
                             ok=(200, 404))
    data = account.get("converted_data")
    converted = data.get("email") if isinstance(data, dict) else None
    if not converted:
        raise CloudError("no VTech account with that email")

    status, tokens = client.call(
        "login", "POST", f"{AM1}/api/v2/account/login",
        body={"email": converted, "password": password, "uuid": uuid, "deviceName": DEVICE_NAME,
              "platform": "android"}, ok=(200, 204, 400, 401, 403))
    if status in (400, 401, 403):
        raise CloudError(f"login refused, wrong password? HTTP {status} {_message(tokens)}".rstrip())
    if status == 204:
        say("VTech has emailed a verification code to this account, to trust this helper as a device.")
        code = ask_code().strip()
        if not code:
            raise CloudError("no verification code given")
        _, tokens = client.call("verification", "POST", f"{AM1}/api/v2/account/authorize",
                                {"Authorization": trust_header(converted, password, uuid)}, {"code": code})
        if tokens.get("access_token"):
            say("Verified: this helper is now a trusted device on the account.")
    token = tokens.get("access_token")
    if not isinstance(token, str) or not token:
        raise CloudError("login returned no access token")

    _, devices = client.call("device list", "GET", f"{VSAAS}/api/v1/devices/",
                             {"Authorization": f"Bearer {token}"})
    cameras = []
    result = devices.get("result")
    for d in result if isinstance(result, list) else []:
        if not isinstance(d, dict):
            continue
        info = d.get("info") if isinstance(d.get("info"), dict) else {}
        uid = d.get("uuid") or d.get("uid")
        pwd = info.get("password") or d.get("password")
        if not uid or not pwd:
            continue
        cameras.append(Camera(name=d.get("nickname") or info.get("nickname") or "", uid=uid,
                              account=d.get("account") or info.get("username") or "admin",
                              av_password=pwd, authkey=derive_authkey(pwd)))
    return cameras


# ── Command line ─────────────────────────────────────────────────────────────
def render(cameras: Sequence[Camera], fmt: str) -> str:
    if fmt == "json":
        return json.dumps([asdict(c) for c in cameras], indent=2)
    if fmt == "env":
        blocks = [f"# {c.name or c.uid}\nVTECH_UID={c.uid}\nVTECH_ACCOUNT={c.account}\n"
                  f"VTECH_PWD={c.av_password}\nVTECH_AUTHKEY={c.authkey}" for c in cameras]
        return "\n\n".join(blocks)
    rows = [("Camera", "UID", "AV password"), *((c.name or "-", c.uid, c.av_password) for c in cameras)]
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    return "\n".join("  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip() for r in rows)


def main(argv: Sequence[str] | None = None, transport: Transport = urllib_transport) -> int:
    ap = argparse.ArgumentParser(description="Print the LAN credentials of the cameras on a VTech cloud "
                                 "account (MyVTech Baby Pro app).")
    ap.add_argument("--email", help="account email (default: $VTECH_EMAIL, else prompted)")
    ap.add_argument("--format", choices=("table", "env", "json"), default="table")
    ap.add_argument("--trace", action="store_true", help="show each request and response, secrets redacted")
    a = ap.parse_args(argv)

    def ask(prompt: str, secret: bool = False) -> str:
        if not sys.stdin.isatty():
            raise CloudError(f"{prompt.rstrip(': ')} needed: set it in the environment (see --help)")
        if secret:
            return getpass.getpass(prompt)          # prompts on the terminal, not stdout
        print(prompt, end="", file=sys.stderr, flush=True)  # stdout carries only the result
        return input()

    def say(msg: str) -> None:
        print(msg, file=sys.stderr)

    try:
        email = (a.email or os.environ.get("VTECH_EMAIL") or ask("VTech account email: ")).strip()
        password = os.environ.get("VTECH_PASSWORD") or ask("Password: ", secret=True)
        cameras = fetch_cameras(
            email, password, lambda: os.environ.get("VTECH_CODE") or ask("Verification code from the email: "),
            uuid=helper_uuid(email, say),
            client=Client(transport, trace=say if a.trace else None), say=say)
    except CloudError as err:
        say(f"error: {err}")
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130
    if not cameras:
        say("The account lists no cameras.")
        return 3
    print(render(cameras, a.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())
