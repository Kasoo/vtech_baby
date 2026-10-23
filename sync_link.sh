#!/bin/sh
# Build vtech-link's Python module (vtech-link/release.sh) and install it into the package.
#
#   ./sync_link.sh [SET...]      SET: x86_64-musl aarch64-musl x86_64-glibc aarch64-glibc (default: all)
#
# The package runs it only when const.BACKEND is "link". native/LINK_SOURCE records the vtech-link
# revision the modules were built from and each module's sha256.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
PKG="$HERE/custom_components/vtech_baby"
[ $# -gt 0 ] || set -- x86_64-musl aarch64-musl x86_64-glibc aarch64-glibc

"$HERE/vtech-link/release.sh" "$@"
for set in "$@"; do
  mkdir -p "$PKG/native/$set"
  cp "$HERE/vtech-link/dist/$set/_vtech_link.abi3.so" "$PKG/native/$set/"
done

rev=$(git -C "$HERE" log -1 --format=%H -- vtech-link)
dirty=$(git -C "$HERE" status --porcelain -- vtech-link | head -1)
{
  echo "vtech_link_commit: $rev${dirty:+ (plus uncommitted changes)}"
  echo "synced: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  (cd "$PKG" && sha256sum native/*-*/_vtech_link.abi3.so)
} > "$PKG/native/LINK_SOURCE"
cat "$PKG/native/LINK_SOURCE"
