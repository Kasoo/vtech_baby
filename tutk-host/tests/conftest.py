"""Builds our libraries and loads them in this process, Global, IOTC and AV, each finding the others
beside it."""
import ctypes as C
import os
import pathlib
import platform
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
TUTK = HERE.parent
ROOT = TUTK.parent
sys.path.insert(0, str(HERE))

#: The set under test: TK_BUILD names a prebuilt one (a release set), else the host build, made on demand.
PREBUILT = os.environ.get("TK_BUILD")
BUILD = pathlib.Path(PREBUILT).resolve() if PREBUILT else \
    TUTK / "build" / f"{platform.machine()}-{'musl' if platform.libc_ver()[0] != 'glibc' else 'glibc'}"
#: Each library we build, by file name, and its source directory (whose imports.txt is its surface).
OURS = {"libTUTKGlobalAPIs.so": "global", "libIOTCAPIs.so": "iotc", "libAVAPIs.so": "av"}


def surface(name):
    """The functions our build of library `name` must export."""
    return (TUTK / OURS[name] / "imports.txt").read_text().split()


@pytest.fixture(scope="session")
def libs():
    if not PREBUILT:
        subprocess.run(["make", "-C", str(TUTK)], check=True, capture_output=True)
    mode = os.RTLD_NOW | os.RTLD_LOCAL
    return {name: C.CDLL(str(BUILD / name), mode=mode) for name in OURS}


@pytest.fixture(scope="session")
def g(libs):
    return libs["libTUTKGlobalAPIs.so"]


@pytest.fixture(scope="session")
def iotc(libs):
    return libs["libIOTCAPIs.so"]


@pytest.fixture(scope="session")
def av(libs):
    return libs["libAVAPIs.so"]
