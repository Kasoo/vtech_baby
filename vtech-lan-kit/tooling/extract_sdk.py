#!/usr/bin/env python3
"""
extract_sdk.py -- take the four SDK libraries for one architecture out of an Android app package.

The input is an APK, or a bundle of split APKs such as APKPure's .xapk or an .apks: the libraries are in
`lib/<abi>/` of the APK, or of one of the bundle's APKs (e.g. `config.x86_64.apk`). Only the four
libraries patch_sdk.py needs are taken, by exact name; ThroughTek's `...T.so` builds (the same API with
logcat logging) are ignored. Their architecture and SDK version are checked before anything is written,
and OUT_DIR is replaced as a whole.

    from extract_sdk import extract_sdk
    info = extract_sdk("Kalay Mobile.xapk", "/path/to/sdk", "x86_64")
    # -> {"arch": "x86_64", "version": "4.3.8.1", "abis": ["arm64-v8a", "x86_64"]}

    python3 extract_sdk.py PACKAGE OUT_DIR [--arch x86_64|aarch64] [--any-version]

The package is untrusted input: only fixed member names are read, and every read is size-capped.
Errors raise ExtractError (or one of its subclasses below).
"""
import os
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from patch_sdk import LIBS, SDK_VERSION, PatchError, sdk_arch_of_file, sdk_version  # noqa: E402

ABI_OF = {"x86_64": "x86_64", "aarch64": "arm64-v8a"}   # our arch name -> Android ABI directory
MAX_APK = 512 << 20          # one APK inside a bundle
MAX_LIB = 64 << 20           # one SDK library


class ExtractError(Exception):
    pass


class NotAPackage(ExtractError):
    """Not an APK or a bundle of APKs."""


class WrongArch(ExtractError):
    """The package has no libraries for the requested architecture."""

    def __init__(self, arch, found):
        self.arch, self.found = arch, found
        have = ", ".join(found) if found else "none"
        super().__init__(f"the package has no {ABI_OF[arch]} libraries (it has: {have})")


class MissingLibs(ExtractError):
    """The package has libraries for the architecture, but not the SDK's."""

    def __init__(self, missing):
        self.missing = missing
        super().__init__(f"the package is missing {', '.join(missing)}")


class WrongVersion(ExtractError):
    """The SDK libraries are not the required version."""

    def __init__(self, version, required):
        self.version, self.required = version, required
        super().__init__(f"the SDK libraries are version {version}; this needs version {required}")


def _copy_member(zf, name, dst, limit):
    """Copy one zip member to dst, refusing to write more than `limit` bytes (whatever the header says)."""
    with zf.open(name) as src, open(dst, "wb") as out:
        left = limit
        while chunk := src.read(min(1 << 20, left + 1)):
            left -= len(chunk)
            if left < 0:
                raise ExtractError(f"{name} is larger than {limit >> 20} MiB")
            out.write(chunk)


def _abis(zf):
    return {n.split("/")[1] for n in zf.namelist() if n.startswith("lib/") and n.count("/") == 2}


def _find_libs(package, abi, tmp):
    """Return (zip with lib/<abi>/, every ABI seen). Looks in the package itself, then its APKs."""
    seen = set()
    try:
        top = zipfile.ZipFile(package)
    except (zipfile.BadZipFile, OSError) as ex:
        raise NotAPackage(f"not an APK or app bundle: {ex}") from None
    abis = _abis(top)
    seen |= abis
    if abi in abis:
        return top, seen
    apks = [n for n in top.namelist() if n.endswith(".apk") and "/" not in n]
    if not apks and not abis and "AndroidManifest.xml" not in top.namelist():
        top.close()
        raise NotAPackage("not an APK or app bundle")
    # The ABI split first (config.x86_64.apk, config.arm64_v8a.apk), then the rest.
    split = abi.replace("-", "_")
    apks.sort(key=lambda n: split not in n)
    with top:
        for i, name in enumerate(apks):
            path = os.path.join(tmp, f"split{i}.apk")
            _copy_member(top, name, path, MAX_APK)
            try:
                inner = zipfile.ZipFile(path)
            except zipfile.BadZipFile:
                os.remove(path)
                continue
            abis = _abis(inner)
            seen |= abis
            if abi in abis:
                return inner, seen
            inner.close()
            os.remove(path)
    return None, seen


def extract_sdk(package, out_dir, arch, require_version=SDK_VERSION):
    """Extract the SDK libraries for `arch` from `package` into out_dir (see module docstring)."""
    if arch not in ABI_OF:
        raise ExtractError(f"unsupported architecture {arch}; need one of {', '.join(ABI_OF)}")
    abi = ABI_OF[arch]
    out_dir = os.path.abspath(out_dir)
    parent = os.path.dirname(out_dir)
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".extract-", dir=parent)
    try:
        zf, seen = _find_libs(package, abi, tmp)
        if zf is None:
            raise WrongArch(arch, sorted(seen))
        staged = os.path.join(tmp, "sdk")
        os.mkdir(staged)
        with zf:
            names = set(zf.namelist())
            missing = [lib for lib in LIBS if f"lib/{abi}/{lib}" not in names]
            if missing:
                raise MissingLibs(missing)
            for lib in LIBS:
                _copy_member(zf, f"lib/{abi}/{lib}", os.path.join(staged, lib), MAX_LIB)
        for lib in LIBS:
            if sdk_arch_of_file(os.path.join(staged, lib)) != arch:
                raise ExtractError(f"{lib} in lib/{abi}/ is not an {arch} library")
        try:
            version = sdk_version(staged)
        except PatchError as ex:
            raise ExtractError(str(ex)) from None
        if require_version and version != require_version:
            raise WrongVersion(version, require_version)

        old = os.path.join(tmp, "old")
        if os.path.lexists(out_dir):
            os.replace(out_dir, old)
        os.replace(staged, out_dir)
        return {"arch": arch, "version": version, "abis": sorted(seen)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    import argparse
    import platform
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("package")
    ap.add_argument("out_dir")
    ap.add_argument("--arch", choices=sorted(ABI_OF), default=platform.machine().replace("arm64", "aarch64"))
    ap.add_argument("--any-version", action="store_true", help=f"accept SDK versions other than {SDK_VERSION}")
    a = ap.parse_args()
    try:
        info = extract_sdk(a.package, a.out_dir, a.arch, None if a.any_version else SDK_VERSION)
    except ExtractError as ex:
        sys.exit(f"[extract_sdk] error: {ex}")
    print(f"[extract_sdk] arch={info['arch']} sdk_version={info['version']} -> {a.out_dir}")


if __name__ == "__main__":
    main()
