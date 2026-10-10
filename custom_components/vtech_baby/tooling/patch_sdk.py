#!/usr/bin/env python3
"""
patch_sdk.py -- produce a host-loadable library set from a user-supplied SDK directory, in pure Python.

This is the patch step of patch.sh as an importable function, for hosts that can run Python but not a
compiler (e.g. inside a Home Assistant installation). It applies the same elfpatch.py edits patch.sh
applies, picks the architecture from the SDK libraries' own ELF headers, verifies the result, and
optionally copies a prebuilt companion (libtk.so) for that architecture next to the patched libraries.
It never builds anything.

    from patch_sdk import patch_sdk
    info = patch_sdk("/path/to/sdk", "/path/to/out", companion="/path/to/libtk.so")
    # -> {"arch": "aarch64", "tested": True, "cached": False, ...}

    python3 patch_sdk.py SDK_DIR OUT_DIR [--companion PATH/TO/libtk.so]

The output directory gets the four patched libraries, libtk.so (if given) and PATCHED.json (a manifest
of the input hashes). A repeat call with the same inputs and tooling returns early with "cached": True.
Errors raise PatchError.
"""
import hashlib
import json
import os
import shutil
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from elfpatch import Elf, EM_X86_64, EM_AARCH64, mrs_tpidr_rt, DT_RUNPATH  # noqa: E402

LIBS = ("libTUTKGlobalAPIs.so", "libNebulaAPIs.so", "libIOTCAPIs.so", "libAVAPIs.so")
ARCH_OF = {EM_X86_64: "x86_64", EM_AARCH64: "aarch64"}
HERE = os.path.dirname(os.path.abspath(__file__))
RENAMES = os.path.join(HERE, "companion", "renames.txt")
MANIFEST = "PATCHED.json"

# The SDK build this kit is characterised against (Kalay 4.3.8.1). Other builds may still patch and load,
# but are untested; the result reports "tested": False so a caller can warn.
TESTED_SHA256 = {
    "x86_64": {
        "libTUTKGlobalAPIs.so": "de59ccf3d543ac45211624bf63a7b98f9f1753ed95ecb27e8334a95bca8e3feb",
        "libNebulaAPIs.so":     "d538aca357d501266362244a4a1444f07819fec09b003218f23e92620db098a7",
        "libIOTCAPIs.so":       "0ebf29b441d9bf52154ea5b9756391da8944c6c7f708ea8443681be2a078b193",
        "libAVAPIs.so":         "43c69cf873e6aa462ac43f7095cc8a1d0edbf96e91fad17760e7bc7db1dc2b90",
    },
    "aarch64": {
        "libTUTKGlobalAPIs.so": "0f367351372eafd5c4663226d6098e517d5cba34797a8e5e0d65e99b0003b1ad",
        "libNebulaAPIs.so":     "a029e3935bf207379a31391c9c748cc6bcbaf31cc8b336a86408d845d7628aef",
        "libIOTCAPIs.so":       "6b4a2c3dd2ffc25617af837777c8360ee235d5b744775c446fda77a221e4fc02",
        "libAVAPIs.so":         "a1f8c268d8ae4ca7e75917841aa67332f13ea30e61ba94d9eb889acf27c94589",
    },
}


class PatchError(Exception):
    pass


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_renames():
    ren = {}
    with open(RENAMES) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                k, v = line.split("=")
                ren[k.strip()] = v.strip()
    return ren


def _tooling_hash():
    h = hashlib.sha256()
    for p in (os.path.join(HERE, "elfpatch.py"), os.path.abspath(__file__), RENAMES):
        with open(p, "rb") as f:
            h.update(f.read())
    return h.hexdigest()


def sdk_arch(sdk_dir):
    """Architecture of an SDK directory, from libAVAPIs.so's e_machine ('x86_64' | 'aarch64')."""
    with open(os.path.join(sdk_dir, "libAVAPIs.so"), "rb") as f:
        hdr = f.read(0x14)
    if hdr[:4] != b"\x7fELF" or hdr[4] != 2 or hdr[5] != 1:
        raise PatchError("libAVAPIs.so is not a little-endian ELF64 library")
    (em,) = struct.unpack_from("<H", hdr, 0x12)
    if em not in ARCH_OF:
        raise PatchError(f"unsupported SDK architecture (e_machine={em}); need x86_64 or aarch64")
    return ARCH_OF[em]


def sdk_arch_of_file(path):
    with open(path, "rb") as f:
        hdr = f.read(0x14)
    (em,) = struct.unpack_from("<H", hdr, 0x12)
    return ARCH_OF.get(em, f"e_machine={em}")


def _patch_one(src, dst, arch, renames):
    e = Elf(src)
    phdr0 = bytes(e.b[0x20:0x28]) + bytes(e.b[0x38:0x3a])
    e.strip_versioning()
    e.fix_needed(drop={"libc.so", "libm.so"}, repoint={"libdl.so": "libtk.so"})
    e.runpath_origin()
    e.rename_imports(renames)
    if arch == "aarch64":
        e.redirect_tp_canary()
    # verify before writing
    if bytes(e.b[0x20:0x28]) + bytes(e.b[0x38:0x3a]) != phdr0:
        raise PatchError(f"{os.path.basename(src)}: program header table moved")
    if [e._str_at(v) for (t, v) in e._dyn_entries()[1] if t == DT_RUNPATH] != ["$ORIGIN"]:
        raise PatchError(f"{os.path.basename(src)}: DT_RUNPATH not set")
    if arch == "aarch64" and any(mrs_tpidr_rt(w) is not None for (_, _, w) in e.exec_words()):
        raise PatchError(f"{os.path.basename(src)}: thread-pointer canary reads remain")
    tmp = dst + ".tmp"
    e.save(tmp)
    os.replace(tmp, dst)


def patch_sdk(sdk_dir, out_dir, companion=None):
    """Patch the four SDK libraries in sdk_dir into out_dir (see module docstring). Returns a dict."""
    for lib in LIBS:
        if not os.path.isfile(os.path.join(sdk_dir, lib)):
            raise PatchError(f"missing {lib} in {sdk_dir}")
    arch = sdk_arch(sdk_dir)
    for lib in LIBS:
        if sdk_arch_of_file(os.path.join(sdk_dir, lib)) != arch:
            raise PatchError(f"{lib} is not {arch}: the SDK directory mixes architectures")
    if companion and sdk_arch_of_file(companion) != arch:
        raise PatchError(f"companion {companion} is not {arch}")
    inputs = {lib: _sha256(os.path.join(sdk_dir, lib)) for lib in LIBS}
    comp_hash = _sha256(companion) if companion else None
    manifest = {"arch": arch, "inputs": inputs, "companion": comp_hash, "tooling": _tooling_hash(),
                "tested": inputs == TESTED_SHA256[arch]}

    os.makedirs(out_dir, exist_ok=True)
    mpath = os.path.join(out_dir, MANIFEST)
    try:
        with open(mpath) as f:
            have = list(LIBS) + (["libtk.so"] if companion else [])
            if json.load(f) == manifest and all(os.path.isfile(os.path.join(out_dir, n)) for n in have):
                return dict(manifest, cached=True)
    except (OSError, ValueError):
        pass

    renames = _load_renames()
    for lib in LIBS:
        try:
            _patch_one(os.path.join(sdk_dir, lib), os.path.join(out_dir, lib), arch, renames)
        except SystemExit as ex:           # elfpatch reports structural problems via SystemExit
            raise PatchError(f"{lib}: {ex}") from None
    if companion:
        # Atomic (new inode), like the patched libraries: overwriting a loaded, memory-mapped libtk.so in
        # place could crash the process that has it mapped.
        tmp = os.path.join(out_dir, "libtk.so.tmp")
        shutil.copyfile(companion, tmp)
        os.replace(tmp, os.path.join(out_dir, "libtk.so"))
    with open(mpath + ".tmp", "w") as f:
        json.dump(manifest, f, indent=1)
    os.replace(mpath + ".tmp", mpath)
    return dict(manifest, cached=False)


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sdk_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--companion", help="prebuilt libtk.so for the SDK's architecture, copied into OUT_DIR")
    a = ap.parse_args()
    try:
        info = patch_sdk(a.sdk_dir, a.out_dir, a.companion)
    except PatchError as ex:
        sys.exit(f"[patch_sdk] error: {ex}")
    print(f"[patch_sdk] arch={info['arch']} tested_sdk_build={info['tested']} cached={info['cached']} "
          f"-> {a.out_dir}")


if __name__ == "__main__":
    main()
