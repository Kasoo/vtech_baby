#!/bin/sh
# Load dist/SET/_vtech_link.abi3.so the way the integration will, in a throwaway container per set:
#
#   tools/ha_import_check.sh [SET...]      SET as for release.sh (default: all)
#
# The musl sets load in the Home Assistant image (arm64 under qemu on an amd64 host); the glibc sets in
# python:3.13-slim. Never touches a running container.
set -e
HERE=$(cd "$(dirname "$0")/.." && pwd)
HA=${HA_IMAGE:-ghcr.io/home-assistant/home-assistant:stable}
PY=${PY_IMAGE:-docker.io/library/python:3.13-slim}
[ $# -gt 0 ] || set -- x86_64-musl aarch64-musl x86_64-glibc aarch64-glibc

CHECK='
import importlib.machinery, importlib.util, sys
print(sys.version.split()[0], sys.platform)
loader = importlib.machinery.ExtensionFileLoader("_vtech_link", "/dist/_vtech_link.abi3.so")
spec = importlib.util.spec_from_file_location("_vtech_link", "/dist/_vtech_link.abi3.so", loader=loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)
print("version", m.__version__, hex(m.SDK_VERSION))
try:
    m.connect("SIMCAM0123456789ABCD", "short", "pw")
except m.LinkError as e:
    assert (e.kind, e.code) == ("invalid_argument", None), (e.kind, e.code)
else:
    raise SystemExit("connect with a short authkey did not raise")
assert m.lan_only_stats() == {"refused_sends": 0, "refused_lookups": 0}
assert m.next_logs(0.0) == [] and m.links() == []
try:
    m._panic()
except BaseException as e:
    assert type(e).__name__ == "PanicException", type(e)
    print("panic ->", type(e).__module__, type(e).__name__, e)
else:
    raise SystemExit("_panic() did not raise")
print("OK")'

for set in "$@"; do
  arch=${set%-*}
  libc=${set#*-}
  case $arch in x86_64) platform=amd64 ;; aarch64) platform=arm64 ;; *) echo "unknown set $set" >&2; exit 2 ;; esac
  case $libc in musl) image=$HA ;; glibc) image=$PY ;; *) echo "unknown set $set" >&2; exit 2 ;; esac
  echo "== $set ($image, $platform)"
  podman run --rm --arch "$platform" --entrypoint sh -v "$HERE/dist/$set":/dist:ro,z "$image" -c "
    uname -m
    python3 -V
    (ldd --version 2>&1 || true) | head -1
    python3 -c '$CHECK'"
done
