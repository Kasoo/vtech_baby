#!/bin/sh
# Refresh the vendored tooling and prebuilt companions from vtech-lan-kit/ (HA_INTEGRATION.md §3.1).
#
#   ./sync_vendored.sh [ARCH...]        ARCH: x86_64 (default) and/or aarch64
#
# tooling/ and native/ must always come from the SAME kit revision: patch_sdk.py renames imports to
# exactly the tokens libtk.so exports (companion/renames.txt). native/SOURCE records that revision.
# Building for a foreign arch needs podman + qemu-user binfmt (see vtech-lan-kit/tooling/README.md).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
KIT="$HERE/vtech-lan-kit/tooling"
PKG="$HERE/custom_components/vtech_baby"
[ $# -gt 0 ] || set -- x86_64

mkdir -p "$PKG/tooling/companion"
cp "$KIT/elfpatch.py" "$KIT/patch_sdk.py" "$KIT/extract_sdk.py" "$PKG/tooling/"
cp "$KIT/companion/renames.txt" "$PKG/tooling/companion/"
touch "$PKG/tooling/__init__.py"

for arch in "$@"; do
  case "$arch" in x86_64|aarch64) ;; *) echo "unknown arch $arch" >&2; exit 2 ;; esac
  mkdir -p "$PKG/native/$arch"
  ARCH=$arch sh "$KIT/companion/build.sh" "$PKG/native/$arch"
done

rev=$(git -C "$HERE" log -1 --format=%H -- vtech-lan-kit/tooling)
dirty=$(git -C "$HERE" status --porcelain -- vtech-lan-kit/tooling | head -1)
{
  echo "kit_tooling_commit: $rev${dirty:+ (plus uncommitted changes)}"
  echo "synced: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  (cd "$PKG" && sha256sum tooling/elfpatch.py tooling/patch_sdk.py tooling/extract_sdk.py tooling/companion/renames.txt native/*/libtk.so 2>/dev/null)
} > "$PKG/native/SOURCE"
cat "$PKG/native/SOURCE"
