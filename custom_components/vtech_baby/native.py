"""Install and prepare the user's SDK for this host: extract it from the app package the user uploads,
then patch it with the vendored tooling and the prebuilt companion.

Authority: vtech-lan-kit/docs/85-ha-deployment.md ("Setup flow on the host", "Updates"). Blocking file
I/O and hashing: run in an executor. No Home Assistant imports.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from .tooling import extract_sdk as _extract
from .tooling import patch_sdk as _patch
from .vtech_lan import sdk as _sdk
from .vtech_lan.errors import UnsupportedHost

PKG = os.path.dirname(os.path.abspath(__file__))
PatchError = _patch.PatchError
ExtractError = _extract.ExtractError
NotAPackage, WrongArch = _extract.NotAPackage, _extract.WrongArch
MissingLibs, WrongVersion = _extract.MissingLibs, _extract.WrongVersion
SDK_VERSION = _patch.SDK_VERSION
ABI_OF = _extract.ABI_OF            # our arch name -> Android ABI directory name


@dataclass(frozen=True)
class Prepared:
    libdir: str
    arch: str
    version: str                  # SDK version of the user's libraries (always SDK_VERSION once patched)
    restart_required: bool        # inputs changed but this process already has an SDK loaded


def companion_path(arch: str) -> str:
    return os.path.join(PKG, "native", arch, "libtk.so")


def host_supported() -> str | None:
    """This host's architecture if this build of the integration can run on it, else None."""
    try:
        arch = _sdk.host_arch()
    except UnsupportedHost:
        return None
    return arch if os.path.isfile(companion_path(arch)) else None


def install_sdk(package: str, sdk_dir: str) -> str:
    """Extract this host's SDK libraries from a zip (the app's APK/XAPK, or the libraries zipped up) into
    sdk_dir. Returns the version.

    Raises ExtractError (or a subclass: NotAPackage, WrongArch, MissingLibs, WrongVersion). sdk_dir is
    only replaced once the new libraries have passed every check.
    """
    return _extract.extract_sdk(package, sdk_dir, _sdk.host_arch())["version"]


def sdk_present(sdk_dir: str) -> bool:
    """Whether sdk_dir already holds a usable SDK for this host (all four libraries, right arch and version)."""
    try:
        if not all(os.path.isfile(os.path.join(sdk_dir, lib)) for lib in _patch.LIBS):
            return False
        return _patch.sdk_arch(sdk_dir) == _sdk.host_arch() and _patch.sdk_version(sdk_dir) == SDK_VERSION
    except (PatchError, UnsupportedHost, OSError):
        return False


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
        return Prepared(loaded.libdir, arch, str(manifest.get("version", "")), changed)

    info = _patch.patch_sdk(sdk_dir, libdir, companion=companion)
    _sdk.preflight(libdir)
    return Prepared(libdir, arch, info["version"], False)
