"""Real-SDK smoke test: preflight, load, initialise, singleton rules. No camera connection.

Needs a patched library set for this host in .build/<arch>/ (build it with
`vtech-lan-kit/tooling/patch.sh vtech-lan-kit/sdk/<arch> .build/<arch>`); skipped without it. The
license is the embedded sdk.DEFAULT_LICENSE_KEY unless $VTECH_LICENSE_FILE overrides it.
"""
import os

import pytest

from vtech_lan import sdk
from vtech_lan.errors import SdkMismatch, UnsupportedHost

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LICENSE_FILE = os.environ.get("VTECH_LICENSE_FILE", "")
SDK_DIR = os.environ.get("VTECH_SDK_DIR", os.path.join(ROOT, "vtech-lan-kit", "sdk"))


def _license():
    try:
        with open(LICENSE_FILE) as f:
            for line in f:
                if "license_key=" in line:
                    return line.split("license_key=", 1)[1].strip()
    except OSError:
        pass
    return sdk.DEFAULT_LICENSE_KEY


def _libdir():
    try:
        return os.path.join(ROOT, ".build", sdk.host_arch())
    except UnsupportedHost:
        return None


LIBDIR = _libdir()
pytestmark = pytest.mark.skipif(
    not (LIBDIR and os.path.isfile(os.path.join(LIBDIR, "libtk.so"))),
    reason="no patched SDK build in .build/<arch>")


def test_preflight_rejects_other_arch():
    other = "arm64-v8a" if sdk.host_arch() == "x86_64" else "x86_64"
    with pytest.raises(UnsupportedHost):
        sdk.preflight(os.path.join(SDK_DIR, other))


def test_load_initialise_and_singleton():
    lic = _license()
    s = sdk.load(LIBDIR, lic)
    assert s.arch == sdk.host_arch()
    assert s.max_channels == 16
    assert sdk.load(LIBDIR, lic) is s
    with pytest.raises(SdkMismatch):
        sdk.load(LIBDIR, lic + "x")
    sid = s.get_session_id()
    assert sid >= 0
    s.session_close(sid)
