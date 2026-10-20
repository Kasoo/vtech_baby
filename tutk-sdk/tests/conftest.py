"""Builds our libraries, patches all three with the stock Nebula, and loads the set in this process. The stock libraries must be in docs/vtech-lan-kit/sdk/x86_64."""
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
TUTK = HERE.parent
ROOT = TUTK.parent
sys.path.insert(0, str(HERE))
from load_test import OURS, build_patched, load  # noqa: E402

SDK = ROOT / "docs/vtech-lan-kit/sdk/x86_64"
COMPANION = ROOT / "custom_components/vtech_baby/native/x86_64/libtk.so"
BUILD = TUTK / "build/x86_64"


@pytest.fixture(scope="session")
def libs(tmp_path_factory):
    if not (SDK / "libIOTCAPIs.so").exists():
        pytest.skip(f"stock SDK libraries not found in {SDK}")
    subprocess.run(["make", "-C", str(TUTK), "ARCH=x86_64"], check=True, capture_output=True)
    out = tmp_path_factory.mktemp("patched")
    build_patched(SDK, COMPANION, out, [BUILD / n for n in OURS])
    return load(out)


@pytest.fixture(scope="session")
def g(libs):
    return libs["libTUTKGlobalAPIs.so"]


@pytest.fixture(scope="session")
def tk(libs):
    return libs["libtk.so"]


@pytest.fixture(scope="session")
def iotc(libs):
    return libs["libIOTCAPIs.so"]


@pytest.fixture(scope="session")
def av(libs):
    return libs["libAVAPIs.so"]
