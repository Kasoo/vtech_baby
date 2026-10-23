"""native: finding the `_vtech_link` module a vtech-link build ships, by arch and libc."""
import json

import pytest

from custom_components.vtech_baby import native


@pytest.fixture
def pkg(tmp_path, monkeypatch):
    """An empty package tree on an x86_64 glibc host."""
    monkeypatch.setattr(native, "PKG", str(tmp_path))
    monkeypatch.setattr(native._sdk, "host_arch", lambda: "x86_64")
    monkeypatch.setattr(native, "host_libc", lambda: "glibc")
    return tmp_path


def ship(pkg, data=b"module"):
    d = pkg / "native" / "x86_64-glibc"
    d.mkdir(parents=True, exist_ok=True)
    (d / native.LINK_MODULE).write_bytes(data)
    return str(d / native.LINK_MODULE)


def test_bundled_link_matches_arch_and_libc(pkg, monkeypatch):
    assert native.bundled_link() is None
    path = ship(pkg)
    assert native.bundled_link() == path
    monkeypatch.setattr(native, "host_libc", lambda: "musl")
    assert native.bundled_link() is None
    monkeypatch.setattr(native, "host_libc", lambda: None)
    assert native.bundled_link() is None


def test_link_build_supports_only_hosts_with_a_module(pkg, monkeypatch):
    monkeypatch.setattr(native, "BACKEND", "link")
    assert native.host_supported() is None
    ship(pkg)
    assert native.host_supported() == "x86_64"


def test_prepare_link(pkg):
    with pytest.raises(native.UnsupportedHost):
        native.prepare_link()
    path = ship(pkg)
    p = native.prepare_link()
    assert (p.libdir, p.link_path, p.arch) == (None, path, "x86_64")
    ship(pkg, b"rebuilt")
    assert json.loads(native.prepare_link().fingerprint) != json.loads(p.fingerprint)
