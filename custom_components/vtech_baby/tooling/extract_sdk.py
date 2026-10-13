#!/usr/bin/env python3
"""
extract_sdk.py -- find the four SDK libraries for one architecture in a zip file, and copy them out.

The input is any zip: an APK, a bundle of split APKs (APKPure's .xapk, .apks), the right split on its
own, or a zip the user made of the libraries. A file is a candidate if its name (in any folder) is one
of the four libraries patch_sdk.py needs; members that are themselves zips (the APKs inside a bundle)
are searched one level deep. A candidate is used only if it is an ELF shared object for the requested
architecture with SDK version SDK_VERSION, so ThroughTek's `...T.so` logging builds, other ABIs and
other versions are never picked. Nothing is written unless all four are found; OUT_DIR is then replaced
as a whole.

    from extract_sdk import extract_sdk
    info = extract_sdk("Kalay Mobile.xapk", "/path/to/sdk", "x86_64")
    # -> {"arch": "x86_64", "version": "4.3.8.1", "abis": ["arm64-v8a", "x86_64"]}

    python3 extract_sdk.py ZIPFILE OUT_DIR [--arch x86_64|aarch64] [--any-version]

The zip is untrusted input: member paths are never used to build output paths, and every read is
size-capped. Errors raise ExtractError (or one of its subclasses below).
"""
import os
import shutil
import struct
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from patch_sdk import LIBS, SDK_VERSION, PatchError, lib_version  # noqa: E402

ABI_OF = {"x86_64": "x86_64", "aarch64": "arm64-v8a"}   # our arch name -> Android ABI name
# (ELF class, e_machine) -> Android ABI name, for telling the user what a file does contain.
_ABI_OF_ELF = {(2, 62): "x86_64", (2, 183): "arm64-v8a", (1, 40): "armeabi-v7a", (1, 3): "x86"}
_ET_DYN = 3
MAX_NESTED = 512 << 20       # one zip inside the upload (e.g. an APK in an .xapk)
MAX_LIB = 64 << 20           # one SDK library


class ExtractError(Exception):
    pass


class NotAPackage(ExtractError):
    """Not a zip file."""


class WrongArch(ExtractError):
    """SDK libraries were found, but none for the requested architecture."""

    def __init__(self, arch, found):
        self.arch, self.found = arch, found
        have = ", ".join(found) if found else "none"
        super().__init__(f"the file has no {ABI_OF[arch]} SDK libraries (it has: {have})")


class MissingLibs(ExtractError):
    """Some (or all) of the four libraries aren't in the file."""

    def __init__(self, missing):
        self.missing = missing
        super().__init__(f"the file doesn't contain {', '.join(missing)}")


class WrongVersion(ExtractError):
    """The libraries for the architecture are not the required SDK version."""

    def __init__(self, version, required):
        self.version, self.required = version, required
        super().__init__(f"the SDK libraries are version {version}; this needs version {required}")


def _copy_member(zf, info, dst, limit):
    """Copy one zip member to dst, refusing to write more than `limit` bytes (whatever the header says)."""
    with zf.open(info) as src, open(dst, "wb") as out:
        left = limit
        while chunk := src.read(min(1 << 20, left + 1)):
            left -= len(chunk)
            if left < 0:
                raise ExtractError(f"{info.filename} is larger than {limit >> 20} MiB")
            out.write(chunk)


def _elf_abi(head):
    """Android ABI name of an ELF shared object from its first bytes, or None if it isn't one."""
    if len(head) < 0x14 or head[:4] != b"\x7fELF" or head[5] != 1:     # little-endian ELF only
        return None
    e_type, e_machine = struct.unpack_from("<HH", head, 0x10)
    if e_type != _ET_DYN:
        return None
    return _ABI_OF_ELF.get((head[4], e_machine), f"e_machine={e_machine}")


def _head(zf, info):
    with zf.open(info) as f:
        return f.read(64)


def _zips(package, tmp):
    """Yield the upload and each zip directly inside it (one level), as open ZipFiles."""
    try:
        top = zipfile.ZipFile(package)
    except (zipfile.BadZipFile, OSError) as ex:
        raise NotAPackage(f"not a zip file: {ex}") from None
    with top:
        yield top
        for i, info in enumerate(top.infolist()):
            if info.is_dir() or os.path.basename(info.filename) in LIBS or _head(top, info)[:4] != b"PK\x03\x04":
                continue
            path = os.path.join(tmp, f"nested{i}.zip")
            _copy_member(top, info, path, MAX_NESTED)
            try:
                inner = zipfile.ZipFile(path)
            except zipfile.BadZipFile:
                os.remove(path)
                continue
            with inner:
                yield inner
            os.remove(path)


def extract_sdk(package, out_dir, arch, require_version=SDK_VERSION):
    """Extract the SDK libraries for `arch` from the zip `package` into out_dir (see module docstring)."""
    if arch not in ABI_OF:
        raise ExtractError(f"unsupported architecture {arch}; need one of {', '.join(ABI_OF)}")
    want = ABI_OF[arch]
    out_dir = os.path.abspath(out_dir)
    parent = os.path.dirname(out_dir)
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".extract-", dir=parent)
    try:
        staged = os.path.join(tmp, "sdk")
        os.mkdir(staged)
        chosen = set()
        abis = set()               # ABIs of every SDK-named ELF library seen
        wrong = {}                 # lib -> version, for right-arch candidates of another version
        n = 0
        zips = _zips(package, tmp)
        for zf in zips:
            for info in zf.infolist():
                lib = os.path.basename(info.filename)
                if info.is_dir() or lib not in LIBS or lib in chosen:
                    continue
                abi = _elf_abi(_head(zf, info))
                if abi is None:
                    continue
                abis.add(abi)
                if abi != want:
                    continue
                n += 1
                path = os.path.join(tmp, f"candidate{n}.so")
                _copy_member(zf, info, path, MAX_LIB)
                try:
                    version = lib_version(path)
                except PatchError:
                    version = "unknown"
                if require_version and version != require_version:
                    wrong.setdefault(lib, version)
                    os.remove(path)
                    continue
                os.replace(path, os.path.join(staged, lib))
                chosen.add(lib)
            if len(chosen) == len(LIBS):
                break
        zips.close()

        missing = [lib for lib in LIBS if lib not in chosen]
        if missing:
            if any(lib in wrong for lib in missing):
                raise WrongVersion(next(wrong[lib] for lib in missing if lib in wrong), require_version)
            if not chosen and want not in abis and abis:
                raise WrongArch(arch, sorted(abis))
            raise MissingLibs(missing)
        versions = {lib_version(os.path.join(staged, lib)) for lib in LIBS}
        if len(versions) != 1:      # only possible with require_version=None
            raise ExtractError(f"the SDK libraries are from different versions: {', '.join(sorted(versions))}")

        old = os.path.join(tmp, "old")
        if os.path.lexists(out_dir):
            os.replace(out_dir, old)
        os.replace(staged, out_dir)
        return {"arch": arch, "version": versions.pop(), "abis": sorted(abis)}
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
