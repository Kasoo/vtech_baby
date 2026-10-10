"""extract_sdk / native.install_sdk: taking the SDK libraries out of the app package the user uploads.

Synthetic packages: each "library" is just an ELF64 header with the right e_machine and an SDK version
string, which is all the extraction checks. `$VTECH_APP_PACKAGE` (e.g. the APKPure Kalay Mobile .xapk)
adds a test against a real download; it is skipped without it.
"""
import io
import os
import struct
import zipfile

import pytest

from custom_components.vtech_baby import native
from custom_components.vtech_baby.tooling import extract_sdk as x
from custom_components.vtech_baby.tooling.patch_sdk import LIBS, sdk_version

EM = {"x86_64": 62, "arm64-v8a": 183, "armeabi-v7a": 40}
TAG = "-1-gc5f0edc-R2_openssl_android"


def lib(abi, version="4.3.8.1", extra=b""):
    hdr = bytearray(64)
    hdr[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", hdr, 0x12, EM[abi])
    return bytes(hdr) + b"\0junk 10.0.0.0\0" + f"{version}{TAG}".encode() + b"\0" + extra


def apk(abis=(), libs=LIBS, version="4.3.8.1", manifest=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if manifest:
            z.writestr("AndroidManifest.xml", b"\0")
        for abi in abis:
            for name in libs:
                z.writestr(f"lib/{abi}/{name}", lib(abi, version))
            # The logging builds of the same libraries must never be picked.
            z.writestr(f"lib/{abi}/libIOTCAPIsT.so", lib(abi, version, b"logging build"))
    return buf.getvalue()


def bundle(path, splits):
    """An APKPure-style .xapk: base APK, language splits, ABI splits, manifest.json."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", b'{"package_name": "com.tutk.kalaymobile"}')
        z.writestr("icon.png", b"png")
        z.writestr("com.tutk.kalaymobile.apk", apk())
        z.writestr("config.en.apk", apk())
        for name, data in splits.items():
            z.writestr(name, data)
    return str(path)


def test_xapk(tmp_path):
    pkg = bundle(tmp_path / "app.xapk", {"config.x86_64.apk": apk(["x86_64"]),
                                         "config.arm64_v8a.apk": apk(["arm64-v8a"])})
    out = tmp_path / "sdk"
    info = x.extract_sdk(pkg, str(out), "x86_64")
    assert info["version"] == "4.3.8.1" and info["arch"] == "x86_64"
    assert sorted(os.listdir(out)) == sorted(LIBS)
    assert sdk_version(str(out)) == "4.3.8.1"
    assert open(out / "libIOTCAPIs.so", "rb").read() == lib("x86_64")      # not the ...T.so build
    assert [n for n in os.listdir(tmp_path) if n.startswith(".extract-")] == []


def test_single_apk_with_several_abis(tmp_path):
    pkg = tmp_path / "app.apk"
    pkg.write_bytes(apk(["armeabi-v7a", "arm64-v8a"]))
    assert x.extract_sdk(str(pkg), str(tmp_path / "sdk"), "aarch64")["abis"] == ["arm64-v8a", "armeabi-v7a"]


def test_wrong_arch(tmp_path):
    pkg = bundle(tmp_path / "app.xapk", {"config.arm64_v8a.apk": apk(["arm64-v8a"])})
    with pytest.raises(x.WrongArch) as ei:
        x.extract_sdk(pkg, str(tmp_path / "sdk"), "x86_64")
    assert ei.value.found == ["arm64-v8a"]
    assert sorted(os.listdir(tmp_path)) == ["app.xapk"]


def test_apk_without_libraries(tmp_path):
    pkg = tmp_path / "base.apk"
    pkg.write_bytes(apk())
    with pytest.raises(x.WrongArch) as ei:
        x.extract_sdk(str(pkg), str(tmp_path / "sdk"), "x86_64")
    assert ei.value.found == []


@pytest.mark.parametrize("data", [b"not a zip", b""])
def test_not_a_package(tmp_path, data):
    pkg = tmp_path / "app.xapk"
    pkg.write_bytes(data)
    with pytest.raises(x.NotAPackage):
        x.extract_sdk(str(pkg), str(tmp_path / "sdk"), "x86_64")


def test_zip_that_is_not_an_app(tmp_path):
    pkg = tmp_path / "photos.zip"
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("a.jpg", b"jpg")
    with pytest.raises(x.NotAPackage):
        x.extract_sdk(str(pkg), str(tmp_path / "sdk"), "x86_64")


def test_other_app(tmp_path):
    pkg = tmp_path / "other.apk"
    pkg.write_bytes(apk(["x86_64"], libs=("libfoo.so",)))
    with pytest.raises(x.MissingLibs) as ei:
        x.extract_sdk(str(pkg), str(tmp_path / "sdk"), "x86_64")
    assert ei.value.missing == list(LIBS)


def test_wrong_version_keeps_the_existing_sdk(tmp_path):
    out = tmp_path / "sdk"
    good = tmp_path / "good.apk"
    good.write_bytes(apk(["x86_64"]))
    x.extract_sdk(str(good), str(out), "x86_64")
    before = {n: (out / n).read_bytes() for n in os.listdir(out)}
    newer = tmp_path / "newer.apk"
    newer.write_bytes(apk(["x86_64"], version="4.4.0.0"))
    with pytest.raises(x.WrongVersion) as ei:
        x.extract_sdk(str(newer), str(out), "x86_64")
    assert (ei.value.version, ei.value.required) == ("4.4.0.0", "4.3.8.1")
    assert {n: (out / n).read_bytes() for n in os.listdir(out)} == before


def test_replaces_previous_sdk(tmp_path):
    out = tmp_path / "sdk"
    out.mkdir()
    (out / "stale.so").write_bytes(b"old")
    pkg = tmp_path / "app.apk"
    pkg.write_bytes(apk(["x86_64"]))
    x.extract_sdk(str(pkg), str(out), "x86_64")
    assert sorted(os.listdir(out)) == sorted(LIBS)


def test_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(x, "MAX_LIB", 16)
    pkg = tmp_path / "app.apk"
    pkg.write_bytes(apk(["x86_64"]))
    with pytest.raises(x.ExtractError, match="larger than"):
        x.extract_sdk(str(pkg), str(tmp_path / "sdk"), "x86_64")
    assert not (tmp_path / "sdk").exists()


def test_install_sdk_uses_the_host_arch(tmp_path, monkeypatch):
    monkeypatch.setattr(native._sdk, "host_arch", lambda: "aarch64")
    pkg = bundle(tmp_path / "app.xapk", {"config.x86_64.apk": apk(["x86_64"]),
                                         "config.arm64_v8a.apk": apk(["arm64-v8a"])})
    assert native.install_sdk(pkg, str(tmp_path / "sdk")) == "4.3.8.1"
    assert open(tmp_path / "sdk" / "libAVAPIs.so", "rb").read() == lib("arm64-v8a")


@pytest.mark.skipif(not os.environ.get("VTECH_APP_PACKAGE"), reason="set $VTECH_APP_PACKAGE to a real app download")
def test_real_package(tmp_path):
    pkg = os.environ["VTECH_APP_PACKAGE"]
    with zipfile.ZipFile(pkg) as z:
        names = z.namelist()
    arch = "x86_64" if any("x86_64" in n for n in names) else "aarch64"
    info = x.extract_sdk(pkg, str(tmp_path / "sdk"), arch)
    assert info["version"] == "4.3.8.1"
    assert sorted(os.listdir(tmp_path / "sdk")) == sorted(LIBS)
