"""native.prepare: patch + cache the user's SDK, arch/companion checks, and the loaded-SDK rules.

Uses the local development SDK (vtech-lan-kit/sdk/) as the user-supplied input; skipped without it.
"""
import os
import shutil
import types

import pytest

from custom_components.vtech_baby import native
from custom_components.vtech_baby.tooling import patch_sdk
from custom_components.vtech_baby.vtech_lan.errors import UnsupportedHost

SDK_ROOT = os.environ.get("VTECH_SDK_DIR",
                          os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vtech-lan-kit", "sdk"))
HOST_SDK = os.path.join(SDK_ROOT, "x86_64")
FOREIGN_SDK = os.path.join(SDK_ROOT, "arm64-v8a")

pytestmark = pytest.mark.skipif(
    os.uname().machine != "x86_64" or not os.path.isdir(HOST_SDK) or not os.path.isdir(FOREIGN_SDK),
    reason="needs an x86_64 host and the development SDK (vtech-lan-kit/sdk/ or $VTECH_SDK_DIR)")


@pytest.fixture(autouse=True)
def not_loaded(monkeypatch):
    monkeypatch.setattr(native._sdk, "loaded", lambda: None)


def fake_loaded(monkeypatch, libdir):
    monkeypatch.setattr(native._sdk, "loaded", lambda: types.SimpleNamespace(libdir=os.path.realpath(libdir)))


def snapshot(libdir):
    return {n: os.stat(os.path.join(libdir, n)).st_ino for n in os.listdir(libdir)}


def test_fresh_patch(tmp_path):
    p = native.prepare(HOST_SDK, str(tmp_path))
    assert p == native.Prepared(str(tmp_path / "x86_64"), "x86_64", "4.3.8.1", False)
    assert sorted(os.listdir(p.libdir)) == sorted([*patch_sdk.LIBS, "libtk.so", patch_sdk.MANIFEST])


def test_second_prepare_is_cached(tmp_path, monkeypatch):
    p = native.prepare(HOST_SDK, str(tmp_path))
    before = snapshot(p.libdir)
    monkeypatch.setattr(patch_sdk, "_patch_one", lambda *a: pytest.fail("re-patched a cached SDK"))
    assert native.prepare(HOST_SDK, str(tmp_path)) == p
    assert snapshot(p.libdir) == before


def test_changed_sdk_is_repatched_when_not_loaded(tmp_path):
    sdk = tmp_path / "sdk"
    shutil.copytree(HOST_SDK, sdk)
    native.prepare(str(sdk), str(tmp_path / "lib"))
    with open(sdk / "libIOTCAPIs.so", "ab") as f:
        f.write(b"\0")
    before = snapshot(str(tmp_path / "lib" / "x86_64"))
    p = native.prepare(str(sdk), str(tmp_path / "lib"))
    assert p.version == "4.3.8.1" and not p.restart_required
    assert snapshot(p.libdir)["libIOTCAPIs.so"] != before["libIOTCAPIs.so"]


def set_version(path, version):
    """Rewrite a library's embedded SDK version string in place (same length)."""
    data = open(path, "rb").read()
    old = patch_sdk.lib_version(path).encode() + b"-"
    assert len(version) == len(old) - 1 and data.count(old) == 1
    open(path, "wb").write(data.replace(old, version.encode() + b"-"))


def test_sdk_version(tmp_path):
    assert patch_sdk.sdk_version(HOST_SDK) == patch_sdk.SDK_VERSION == "4.3.8.1"


def test_other_sdk_version_is_refused(tmp_path):
    sdk = tmp_path / "sdk"
    shutil.copytree(HOST_SDK, sdk)
    for lib in patch_sdk.LIBS:
        set_version(sdk / lib, "4.3.9.0")
    with pytest.raises(native.PatchError, match="version 4.3.9.0; this needs version 4.3.8.1"):
        native.prepare(str(sdk), str(tmp_path / "lib"))
    assert not native.sdk_present(str(sdk))


def test_mixed_sdk_versions_are_refused(tmp_path):
    sdk = tmp_path / "sdk"
    shutil.copytree(HOST_SDK, sdk)
    set_version(sdk / "libAVAPIs.so", "4.3.6.2")
    with pytest.raises(native.PatchError, match="different versions"):
        native.prepare(str(sdk), str(tmp_path / "lib"))


def test_sdk_present(tmp_path):
    assert native.sdk_present(HOST_SDK)
    assert not native.sdk_present(FOREIGN_SDK)
    assert not native.sdk_present(str(tmp_path / "nope"))


def test_loaded_and_unchanged(tmp_path, monkeypatch):
    p = native.prepare(HOST_SDK, str(tmp_path))
    fake_loaded(monkeypatch, p.libdir)
    before = snapshot(p.libdir)
    assert native.prepare(HOST_SDK, str(tmp_path)) == native.Prepared(
        os.path.realpath(p.libdir), "x86_64", "4.3.8.1", False)
    assert snapshot(p.libdir) == before


def test_changed_while_loaded_needs_restart_and_rewrites_nothing(tmp_path, monkeypatch):
    sdk = tmp_path / "sdk"
    shutil.copytree(HOST_SDK, sdk)
    p = native.prepare(str(sdk), str(tmp_path / "lib"))
    fake_loaded(monkeypatch, p.libdir)
    before = snapshot(p.libdir)
    with open(sdk / "libAVAPIs.so", "ab") as f:
        f.write(b"\0")
    monkeypatch.setattr(patch_sdk, "patch_sdk", lambda *a, **k: pytest.fail("patched while loaded"))
    q = native.prepare(str(sdk), str(tmp_path / "lib"))
    assert q.restart_required and q.libdir == os.path.realpath(p.libdir)
    assert snapshot(p.libdir) == before


def test_changed_companion_while_loaded_needs_restart(tmp_path, monkeypatch):
    p = native.prepare(HOST_SDK, str(tmp_path))
    fake_loaded(monkeypatch, p.libdir)
    other = tmp_path / "libtk.so"
    shutil.copyfile(native.companion_path("x86_64"), other)
    with open(other, "ab") as f:
        f.write(b"\0")
    monkeypatch.setattr(native, "companion_path", lambda arch: str(other))
    assert native.prepare(HOST_SDK, str(tmp_path)).restart_required


def test_loaded_from_elsewhere_needs_restart(tmp_path, monkeypatch):
    native.prepare(HOST_SDK, str(tmp_path / "a"))
    p = native.prepare(HOST_SDK, str(tmp_path / "b"))
    fake_loaded(monkeypatch, p.libdir)
    assert native.prepare(HOST_SDK, str(tmp_path / "a")).restart_required


def test_wrong_arch_sdk(tmp_path):
    with pytest.raises(UnsupportedHost, match="aarch64"):
        native.prepare(FOREIGN_SDK, str(tmp_path))
    assert not os.path.exists(tmp_path / "x86_64")


def test_missing_companion(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "companion_path", lambda arch: str(tmp_path / "nope" / "libtk.so"))
    with pytest.raises(UnsupportedHost, match="companion"):
        native.prepare(HOST_SDK, str(tmp_path))


def test_missing_sdk_dir(tmp_path):
    with pytest.raises(native.PatchError, match="does not exist"):
        native.prepare(str(tmp_path / "nope"), str(tmp_path))


@pytest.mark.parametrize("lib", patch_sdk.LIBS)
def test_missing_library(tmp_path, lib):
    sdk = tmp_path / "sdk"
    shutil.copytree(HOST_SDK, sdk)
    os.remove(sdk / lib)
    with pytest.raises(native.PatchError, match=lib):
        native.prepare(str(sdk), str(tmp_path / "lib"))
