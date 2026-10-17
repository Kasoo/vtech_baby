#!/usr/bin/env python3
"""Patch the stock IOTC, AV and Nebula libraries together with our Global, then load them all with
every symbol bound, as the integration does.

    load_test.py SDK_DIR GLOBAL_SO COMPANION_SO [OUT_DIR]

SDK_DIR holds the four stock libraries; GLOBAL_SO is our build. OUT_DIR (default: a temporary
directory) receives the patched set, so it can be reused for live runs.
"""
import ctypes
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "docs/vtech-lan-kit/tooling"))
from patch_sdk import patch_sdk  # noqa: E402

LOAD_ORDER = ("libtk.so", "libTUTKGlobalAPIs.so", "libNebulaAPIs.so", "libIOTCAPIs.so", "libAVAPIs.so")


def build_patched(sdk, ours, companion, out):
    """Patch the stock libraries in `sdk` with `ours` as Global into `out`; returns patch_sdk's info."""
    with tempfile.TemporaryDirectory() as src:
        for name in ("libIOTCAPIs.so", "libAVAPIs.so", "libNebulaAPIs.so"):
            shutil.copy(pathlib.Path(sdk) / name, src)
        shutil.copy(ours, pathlib.Path(src) / "libTUTKGlobalAPIs.so")
        return patch_sdk(src, str(out), str(companion))


def load(out):
    """Load a patched set in the integration's order; returns the handles by name."""
    mode = os.RTLD_NOW | os.RTLD_LOCAL
    return {name: ctypes.CDLL(str(pathlib.Path(out) / name), mode=mode) for name in LOAD_ORDER}


def main(argv):
    sdk, ours, companion = map(pathlib.Path, argv[1:4])
    out = pathlib.Path(argv[4]) if len(argv) > 4 else pathlib.Path(tempfile.mkdtemp(prefix="tutk-load-"))
    print(f"patched into {out}: {build_patched(sdk, ours, companion, out)}")
    libs = load(out)
    libs["libTUTKGlobalAPIs.so"].TUTK_SDK_Set_License_Key  # resolves, or raises AttributeError
    print("loaded:", ", ".join(LOAD_ORDER))


if __name__ == "__main__":
    main(sys.argv)
