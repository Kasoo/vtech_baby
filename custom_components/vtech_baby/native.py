"""Prepare the user's SDK for this host: patch it with the vendored tooling and the prebuilt companion.

Authority: vtech-lan-kit/docs/85-ha-deployment.md ("Setup flow on the host", "Updates"). Blocking file
I/O and hashing: run in an executor. No Home Assistant imports.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from .tooling import patch_sdk as _patch
from .vtech_lan import sdk as _sdk
from .vtech_lan.errors import UnsupportedHost

PKG = os.path.dirname(os.path.abspath(__file__))
PatchError = _patch.PatchError


@dataclass(frozen=True)
class Prepared:
    libdir: str
    arch: str
    tested: bool                  # the user's SDK is the build the kit is characterised against
    restart_required: bool        # inputs changed but this process already has an SDK loaded


def companion_path(arch: str) -> str:
    return os.path.join(PKG, "native", arch, "libtk.so")


def _sha256(path: str) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def prepare(sdk_dir: str, lib_root: str) -> Prepared:
    """Patch `sdk_dir` into `lib_root/<arch>/` (cached) and check the result can load on this host.

    If an SDK is already loaded in this process, nothing is rewritten: a loaded library must never be
    replaced underneath the process. A changed input is then reported as `restart_required`.
    Raises UnsupportedHost or PatchError with a user-presentable message.
    """
    arch = _sdk.host_arch()
    libdir = os.path.join(lib_root, arch)
    companion = companion_path(arch)
    if not os.path.isfile(companion):
        raise UnsupportedHost(f"this integration build has no prebuilt companion for {arch}")
    if not os.path.isdir(sdk_dir):
        raise PatchError(f"SDK directory {sdk_dir} does not exist")
    missing = [lib for lib in _patch.LIBS if not os.path.isfile(os.path.join(sdk_dir, lib))]
    if missing:
        raise PatchError(f"missing {', '.join(missing)} in {sdk_dir}")
    sdk_arch = _patch.sdk_arch(sdk_dir)
    if sdk_arch != arch:
        raise UnsupportedHost(f"the SDK libraries in {sdk_dir} are {sdk_arch} but this host is {arch}")

    loaded = _sdk.loaded()
    if loaded is not None:
        try:
            with open(os.path.join(loaded.libdir, _patch.MANIFEST)) as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            manifest = {}
        inputs = {lib: _sha256(os.path.join(sdk_dir, lib)) for lib in _patch.LIBS}
        changed = (os.path.realpath(libdir) != loaded.libdir or manifest.get("inputs") != inputs
                   or manifest.get("companion") != _sha256(companion))
        return Prepared(loaded.libdir, arch, bool(manifest.get("tested")), changed)

    info = _patch.patch_sdk(sdk_dir, libdir, companion=companion)
    _sdk.preflight(libdir)
    return Prepared(libdir, arch, bool(info["tested"]), False)
