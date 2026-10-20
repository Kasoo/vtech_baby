#!/bin/sh
# Build our SDK replacement libraries (tutk-host/release.sh) and install them into the package.
#
#   ./sync_sdk.sh [SET...]      SET: x86_64-musl aarch64-musl x86_64-glibc aarch64-glibc (default: all)
#
# native/SDK_SOURCE records the tutk-host revision the sets were built from and each library's sha256.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
PKG="$HERE/custom_components/vtech_baby"
[ $# -gt 0 ] || set -- x86_64-musl aarch64-musl x86_64-glibc aarch64-glibc

"$HERE/tutk-host/release.sh" "$@"
for set in "$@"; do
  mkdir -p "$PKG/native/$set"
  cp "$HERE/tutk-host/build/release/$set/"lib*.so "$PKG/native/$set/"
done

rev=$(git -C "$HERE" log -1 --format=%H -- tutk-host)
dirty=$(git -C "$HERE" status --porcelain -- tutk-host | head -1)
{
  echo "tutk_host_commit: $rev${dirty:+ (plus uncommitted changes)}"
  echo "synced: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  (cd "$PKG" && sha256sum native/*-*/lib*.so)
} > "$PKG/native/SDK_SOURCE"
cat "$PKG/native/SDK_SOURCE"
