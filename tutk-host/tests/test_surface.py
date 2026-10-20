"""Each of our libraries exports its whole surface and depends only on libc, OpenSSL and each other."""
import subprocess

import pytest
from conftest import BUILD, OURS, surface

ALLOWED = {"libc.so.6", "libpthread.so.0", "libm.so.6", "libssl.so.3", "libcrypto.so.3",
           "libc.musl-x86_64.so.1", "libc.musl-aarch64.so.1", "ld-linux-x86-64.so.2"} | set(OURS)


@pytest.mark.parametrize("name", sorted(OURS))
def test_exports_surface(libs, name):
    for sym in surface(name):
        getattr(libs[name], sym)                 # resolves, or raises AttributeError


@pytest.mark.parametrize("name", sorted(OURS))
def test_dependencies(libs, name):
    out = subprocess.run(["readelf", "-d", str(BUILD / name)], capture_output=True, text=True,
                         check=True).stdout
    needed = {ln.split("[")[1].rstrip("]") for ln in out.splitlines() if "(NEEDED)" in ln}
    assert needed <= ALLOWED, needed - ALLOWED
    assert "$ORIGIN" in out
