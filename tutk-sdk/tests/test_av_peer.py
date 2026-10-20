"""Our libAVAPIs.so against a fake camera (av_peer.py) over a stand-in IOTC (fake_iotc.c): login, IOCtrl
both ways, retransmission, media assembly and loss, the ACK and report cadence, the reset, and session
close. Each scenario runs in its own process, since the stand-in shares IOTC's soname."""
import subprocess
import sys

import pytest
from conftest import BUILD, COMPANION, HERE, SDK, TUTK
from load_test import build_patched

sys.path.insert(0, str(HERE))
from av_peer import SCENARIOS  # noqa: E402

CC = (TUTK.parent.parent / "camera/android-sdk/ndk/28.2.13676358/toolchains/llvm/prebuilt/linux-x86_64/bin"
      / "x86_64-linux-android21-clang")


@pytest.fixture(scope="module")
def fake_set(libs, tmp_path_factory):
    work = tmp_path_factory.mktemp("fake_iotc")
    so = work / "libIOTCAPIs.so"
    subprocess.run([str(CC), "-O1", "-shared", "-fPIC", "-fvisibility=hidden", "-I", str(TUTK / "common"),
                    str(HERE / "fake_iotc.c"), str(TUTK / "common/version.c"), "-Wl,-soname,libIOTCAPIs.so",
                    "-Wl,--no-as-needed", "-ldl", "-o", str(so)], check=True)
    out = work / "patched"
    build_patched(SDK, COMPANION, out, [BUILD / "libTUTKGlobalAPIs.so", so, BUILD / "libAVAPIs.so"])
    return out


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_av_peer(fake_set, scenario):
    r = subprocess.run([sys.executable, str(HERE / "av_peer.py"), str(fake_set), scenario],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip().endswith("ok"), r.stdout + r.stderr
