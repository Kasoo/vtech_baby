"""Every mix of our libraries and the stock ones patches and loads with every symbol bound, and each of
ours exports its whole drop-in surface. Each mix loads in its own process: the libraries share sonames,
so a second set in one process would bind to the first."""
import itertools
import pathlib
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


def libc_imports(path):
    nm = (pathlib.Path.home() / "android-sdk/ndk/28.2.13676358"
          / "toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-nm")
    out = subprocess.run([str(nm), "-D", "--undefined-only", str(path)], capture_output=True, text=True,
                         check=True).stdout
    return {ln.split()[-1] for ln in out.splitlines() if ln.endswith("@LIBC")}


def test_libc_imports_within_stock(libs):
    """Ours import no libc function the stock libraries do not: the patcher and companion are audited
    for exactly that surface (NOTES.md, the libc-imports decision)."""
    stock = set().union(*(libc_imports(p) for p in SDK.glob("*.so")))
    assert "pthread_create@LIBC" in stock
    for n in OURS:
        assert libc_imports(BUILD / n) - stock == set(), n
