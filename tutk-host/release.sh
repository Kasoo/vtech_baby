#!/bin/sh
# Build the library sets the integration ships, each in a container for its libc:
#
#   ./release.sh [SET...]      SET: x86_64-musl aarch64-musl x86_64-glibc aarch64-glibc (default: all)
#
# musl builds in Alpine 3.24 (Home Assistant's base); glibc builds in manylinux2014 (glibc 2.17), so
# the glibc sets load on any glibc host since. Both link against stand-ins for OpenSSL 3, which the host
# supplies at run time. A foreign arch runs under qemu-user binfmt.
#
# Writes build/release/SET/ (stripped) and fails if a set imports anything outside
# tools/imports-allowed.txt: widening that list is a deliberate, reviewed change.
set -e
export LC_ALL=C              # comm needs one collation order
HERE=$(cd "$(dirname "$0")" && pwd)
ALPINE=docker.io/library/alpine:3.24
LIBS="libTUTKGlobalAPIs.so libIOTCAPIs.so libAVAPIs.so"
[ $# -gt 0 ] || set -- x86_64-musl aarch64-musl x86_64-glibc aarch64-glibc

for set in "$@"; do
  arch=${set%-*}
  libc=${set#*-}
  case $arch in
    x86_64) platform=amd64 ;;
    aarch64) platform=arm64 ;;
    *) echo "unknown arch in $set" >&2; exit 2 ;;
  esac
  case $libc in
    musl) image=$ALPINE; prep="apk add -q build-base python3" ;;
    glibc) image=quay.io/pypa/manylinux2014_$arch; prep="export PATH=/opt/python/cp312-cp312/bin:\$PATH" ;;
    *) echo "unknown libc in $set" >&2; exit 2 ;;
  esac
  echo "== $set ($image)"
  rm -rf "$HERE/build/release-work/$set" "$HERE/build/release/$set"
  podman run --rm --arch "$platform" -v "$HERE":/src:z -w /src "$image" sh -ec "
    $prep
    make -j4 BUILD=build/release-work OSSL_STANDIN=1 >/dev/null
    test -d build/release-work/$set
    mkdir -p build/release/$set
    for lib in $LIBS; do strip --strip-unneeded -o build/release/$set/\$lib build/release-work/$set/\$lib; done"

  # Every undefined dynamic symbol must be one of ours or on the allowed list.
  out="$HERE/build/release/$set"
  for lib in $LIBS; do nm -D --defined-only "$out/$lib"; done | awk '{print $3}' | sed 's/@.*//' |
    sort -u > "$out.ours"
  extra=$(for lib in $LIBS; do nm -D --undefined-only "$out/$lib"; done | awk '{print $2}' | sed 's/@.*//' |
          sort -u | comm -23 - "$HERE/tools/imports-allowed.txt" | comm -23 - "$out.ours")
  rm -f "$out.ours"
  if [ -n "$extra" ]; then
    echo "$set imports symbols not in tools/imports-allowed.txt:" >&2
    echo "$extra" >&2
    exit 1
  fi
  (cd "$out" && sha256sum $LIBS)
done
