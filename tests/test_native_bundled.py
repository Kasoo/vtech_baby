"""native: the integration's own library sets, chosen by arch and libc ahead of the patched path."""
import json
import os
import shutil

import pytest

from custom_components.vtech_baby import native
from custom_components.vtech_baby.vtech_lan import sdk as _sdk

try:
    ARCH = _sdk.host_arch()
except native.UnsupportedHost:
    ARCH = None
LIBC = native.host_libc()
SHIPPED = os.path.join(native.PKG, "native", f"{ARCH}-{LIBC}")

pytestmark = pytest.mark.skipif(
    ARCH is None or LIBC is None or not os.path.isfile(os.path.join(SHIPPED, "libAVAPIs.so")),
    reason="needs a host with a bundled set (native/<arch>-<libc>/; ./sync_sdk.sh)")


@pytest.fixture
def pkg(tmp_path, monkeypatch):
    """A copy of the package's native/ tree, so a test can change it."""
    shutil.copytree(SHIPPED, tmp_path / "native" / f"{ARCH}-{LIBC}")
    monkeypatch.setattr(native, "PKG", str(tmp_path))
    return tmp_path / "native" / f"{ARCH}-{LIBC}"


def test_this_host_has_a_bundled_set(monkeypatch):
    monkeypatch.setattr(native, "BACKEND", "sdk")
    assert native.bundled_libdir() == SHIPPED
    assert native.host_supported() == ARCH


def test_prepare_uses_the_bundled_set_whatever_the_sdk_dir(tmp_path):
    p = native.prepare(str(tmp_path / "no-sdk"), str(tmp_path / "lib"))
    assert (p.libdir, p.arch, p.version) == (SHIPPED, ARCH, native.BUNDLED_VERSION)
    assert set(json.loads(p.fingerprint)["bundled"]) == set(_sdk.HOST_LIBS)
    assert not (tmp_path / "lib").exists()


def test_changed_library_changes_the_fingerprint(pkg, tmp_path):
    before = native.prepare("", str(tmp_path)).fingerprint
    with open(pkg / "libAVAPIs.so", "ab") as f:
        f.write(b"\0")
    assert native.prepare("", str(tmp_path)).fingerprint != before


@pytest.mark.parametrize("lib", _sdk.HOST_LIBS)
def test_incomplete_set_is_not_used(pkg, lib):
    os.remove(pkg / lib)
    assert native.bundled_libdir() is None


def test_unknown_libc_has_no_bundled_set(monkeypatch):
    monkeypatch.setattr(native, "host_libc", lambda: None)
    assert native.bundled_libdir() is None
