"""Find the integration's `_vtech_link` module for this host: one build per arch and libc, in
native/<arch>-<libc>/. Blocking file I/O and hashing: run in an executor. No Home Assistant imports.
"""
from __future__ import annotations

import glob
import hashlib
import os
import platform
from dataclasses import dataclass

from .vtech_lan.errors import UnsupportedHost

PKG = os.path.dirname(os.path.abspath(__file__))
LINK_MODULE = "_vtech_link.abi3.so"
_HOST_ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


@dataclass(frozen=True)
class Prepared:
    arch: str
    link_path: str                # the _vtech_link module
    fingerprint: str              # the module's sha256; a change restarts the worker


def host_arch() -> str:
    arch = _HOST_ARCH.get(platform.machine().lower())
    if arch is None:
        raise UnsupportedHost(f"unsupported host architecture {platform.machine()!r}: only 64-bit x86_64 "
                              "and aarch64 are supported (32-bit armv7 / i386 installs are not)")
    return arch


def host_libc() -> str | None:
    """This host's C library, "glibc" or "musl", or None if neither is recognised."""
    if platform.libc_ver()[0] == "glibc":
        return "glibc"
    return "musl" if glob.glob("/lib/ld-musl-*.so.1") else None


def bundled_link() -> str | None:
    """The path of our `_vtech_link` module for this host, if the integration ships one for it."""
    try:
        arch = host_arch()
    except UnsupportedHost:
        return None
    libc = host_libc()
    if libc is None:
        return None
    path = os.path.join(PKG, "native", f"{arch}-{libc}", LINK_MODULE)
    return path if os.path.isfile(path) else None


def host_supported() -> str | None:
    """This host's architecture if the integration can run on it, else None."""
    try:
        arch = host_arch()
    except UnsupportedHost:
        return None
    return arch if bundled_link() else None


def prepare() -> Prepared:
    """Our `_vtech_link` module for this host; the worker loads it, never this process.
    Raises UnsupportedHost if the integration has none for this host."""
    arch = host_arch()
    path = bundled_link()
    if path is None:
        raise UnsupportedHost(f"this integration has no vtech-link module for {arch}-{host_libc()}")
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    return Prepared(arch, path, digest)
