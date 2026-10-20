"""Our libAVAPIs.so against a fake camera (av_peer.py) over a stand-in IOTC (fake_iotc.c): login, IOCtrl
both ways, retransmission, media assembly and loss, the ACK and report cadence, the reset, and session
close. Each scenario runs in its own process, since the stand-in shares IOTC's soname."""
import shutil
import subprocess
import sys

import pytest
from conftest import BUILD, HERE

sys.path.insert(0, str(HERE))
from av_peer import SCENARIOS  # noqa: E402



@pytest.fixture(scope="module")
def fake_set(libs, tmp_path_factory):
    """Our Global and AV beside the stand-in IOTC: AV finds whichever libIOTCAPIs.so is next to it."""
    out = tmp_path_factory.mktemp("fake_iotc")
    subprocess.run(["cc", "-O1", "-shared", "-fPIC", "-fvisibility=hidden", str(HERE / "fake_iotc.c"),
                    "-Wl,-soname,libIOTCAPIs.so", "-lpthread", "-o", str(out / "libIOTCAPIs.so")], check=True)
    for name in ("libTUTKGlobalAPIs.so", "libAVAPIs.so"):
        shutil.copy(BUILD / name, out / name)
    return out


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_av_peer(fake_set, scenario):
    r = subprocess.run([sys.executable, str(HERE / "av_peer.py"), str(fake_set), scenario],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip().endswith("ok"), r.stdout + r.stderr
