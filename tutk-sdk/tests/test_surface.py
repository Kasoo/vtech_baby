"""Every mix of our libraries and the stock ones patches and loads with every symbol bound, and each of
ours exports its whole drop-in surface. Each mix loads in its own process: the libraries share sonames,
so a second set in one process would bind to the first."""
import itertools
import subprocess
import sys

import pytest
from conftest import BUILD, COMPANION, HERE, SDK
from load_test import OURS

MIXES = [m for n in (1, 2, 3) for m in itertools.combinations(OURS, n)]


@pytest.mark.parametrize("ours", MIXES, ids=lambda m: "+".join(OURS[n] for n in m))
def test_mix_loads(libs, tmp_path, ours):
    r = subprocess.run([sys.executable, str(HERE / "load_test.py"), str(SDK), str(COMPANION),
                        str(tmp_path), *(str(BUILD / n) for n in ours)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "loaded:" in r.stdout
