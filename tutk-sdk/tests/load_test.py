#!/usr/bin/env python3
"""Patch a set of SDK libraries, ours in place of any of the stock ones, then load them all with every
symbol bound, as the integration does.

    load_test.py SDK_DIR COMPANION_SO OUT_DIR [OURS_SO ...]

SDK_DIR holds the four stock libraries. Each OURS_SO is one of our builds (libTUTKGlobalAPIs.so,
libIOTCAPIs.so or libAVAPIs.so) and replaces the stock library of that name. OUT_DIR receives the
patched set, so it can be reused for live runs.
"""
import ctypes
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
TUTK = ROOT / "tutk-sdk"
sys.path.insert(0, str(ROOT / "docs/vtech-lan-kit/tooling"))
from patch_sdk import patch_sdk  # noqa: E402

LOAD_ORDER = ("libtk.so", "libTUTKGlobalAPIs.so", "libNebulaAPIs.so", "libIOTCAPIs.so", "libAVAPIs.so")
STOCK = LOAD_ORDER[1:]
# The source directory of each library we build; its imports.txt is the drop-in surface.
OURS = {"libTUTKGlobalAPIs.so": "global", "libIOTCAPIs.so": "iotc", "libAVAPIs.so": "av"}


def build_patched(sdk, companion, out, ours):
    """Patch the stock libraries in `sdk` into `out`, with each path in `ours` replacing the stock
    library of the same name; returns patch_sdk's info."""
    ours = {pathlib.Path(p).name: p for p in ours}
    if unknown := set(ours) - set(OURS):
        raise ValueError(f"not a library we build: {', '.join(sorted(unknown))}")
    with tempfile.TemporaryDirectory() as src:
        for name in STOCK:
            shutil.copy(ours.get(name, pathlib.Path(sdk) / name), pathlib.Path(src) / name)
        return patch_sdk(src, str(out), str(companion))


def load(out):
    """Load a patched set in the integration's order; returns the handles by name."""
    mode = os.RTLD_NOW | os.RTLD_LOCAL
    return {name: ctypes.CDLL(str(pathlib.Path(out) / name), mode=mode) for name in LOAD_ORDER}


def surface(name):
    """The functions our build of library `name` must export."""
    return (TUTK / OURS[name] / "imports.txt").read_text().split()


def main(argv):
    if len(argv) < 4:
        sys.exit(__doc__)
    sdk, companion, out = map(pathlib.Path, argv[1:4])
    ours = [pathlib.Path(p) for p in argv[4:]]
    print(f"patched into {out}: {build_patched(sdk, companion, out, ours)}")
    libs = load(out)
    for p in ours:
        for sym in surface(p.name):
            getattr(libs[p.name], sym)   # resolves, or raises AttributeError
    print("loaded:", ", ".join(f"{n} ({'ours' if n in {p.name for p in ours} else 'stock'})"
                               for n in STOCK))


if __name__ == "__main__":
    main(sys.argv)
