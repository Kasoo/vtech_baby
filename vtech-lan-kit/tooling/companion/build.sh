#!/bin/sh
# build.sh -- build the companion libtutkabi.so ONCE on low-floor glibc.
#
# The single glibc-built companion runs on BOTH glibc and musl: it carries
# DT_NEEDED libc.so.6/libm.so.6 (+ libpthread.so.0 on an old floor) -- all of which musl maps to
# its own libc via the reserved-soname list (c./m./pthread.), and musl ignores the GLIBC_* version
# tags, so its imports bind to musl by name. A musl-built .so would NOT run on glibc, so we only
# ever build on glibc.
#
#   [ARCH=x86_64|aarch64] ./build.sh [OUTDIR]
#       OUTDIR default: this dir. ARCH default: this machine (uname -m). Prefers the manylinux2014
#       container for ARCH (glibc 2.17 floor) via podman; falls back to native gcc (same-arch only).
#       A foreign ARCH (e.g. aarch64 on an x86_64 box) needs podman + qemu-user binfmt.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="${1:-$HERE}"
SRCS="pthread.c net.c fortify.c misc.c stubs.c"
# -fvisibility=hidden + EXPORT(default) => only the renamed tokens are exported.
# --no-as-needed -lm forces DT_NEEDED libm.so.6. -pthread pulls pthread on an old floor.
ARCH="${ARCH:-$(uname -m)}"
case "$ARCH" in x86_64|aarch64) ;; arm64) ARCH=aarch64 ;; *) echo "unsupported ARCH=$ARCH" >&2; exit 2 ;; esac
# -D__NO_STRING_INLINES: on glibc<2.26 headers (the manylinux2014 floor) these otherwise rewrite
# strdup() into a call to the glibc-internal __strdup, which musl doesn't export -> the companion
# fails to load on musl. A native modern-glibc build never hits this class of issue.
CFLAGS="-O2 -fPIC -fvisibility=hidden -D__NO_STRING_INLINES -Wall -Wextra -Wno-unused-parameter"
# Soname is libtk.so (<=8 chars) so a patched TUTK lib can name it in-place by reusing the
# freed "libdl.so" DT_NEEDED slot (same-size edit). "libtk" == the companion, a.k.a. libtutkabi.
LDFLAGS="-shared -Wl,-soname,libtk.so -Wl,--no-as-needed -lm -pthread"
# aarch64 kernels run with 4K, 16K or 64K pages: align segments for the largest of those actually in
# use so the companion never becomes the load blocker (the TUTK libs themselves are 16K-aligned, so
# 64K kernels are out of scope either way).
[ "$ARCH" = aarch64 ] && LDFLAGS="$LDFLAGS -Wl,-z,max-page-size=0x10000"
IMG="quay.io/pypa/manylinux2014_$ARCH"   # glibc 2.17 floor

build_native() {
    [ "$(uname -m)" = "$ARCH" ] || { echo "[build] native build can't target $ARCH on $(uname -m)" >&2; exit 2; }
    echo "[build] native gcc ($(gcc -dumpversion), glibc $(getconf GNU_LIBC_VERSION 2>/dev/null || echo '?'))"
    ( cd "$HERE" && gcc $CFLAGS $SRCS $LDFLAGS -o "$OUT/libtk.so" )
}

build_manylinux() {
    echo "[build] manylinux2014 (glibc 2.17) via podman: $IMG"
    podman run --rm --arch "$([ "$ARCH" = aarch64 ] && echo arm64 || echo amd64)" -v "$HERE":/s:z -v "$OUT":/o:z -w /s "$IMG" \
        gcc $CFLAGS $SRCS $LDFLAGS -o /o/libtk.so
}

if command -v podman >/dev/null 2>&1 && [ -z "$NATIVE" ] && \
   podman image exists "$IMG" 2>/dev/null; then
    build_manylinux
elif [ -z "$NATIVE" ] && command -v podman >/dev/null 2>&1; then
    echo "[build] pulling $IMG (one-time)…"; build_manylinux || { echo "[build] container failed -> native"; build_native; }
else
    build_native
fi

echo "=== libtk.so (companion, $ARCH) built: $OUT/libtk.so ==="
echo "--- DT_NEEDED ---"; readelf -dW "$OUT/libtk.so" | grep NEEDED || true
echo "--- glibc version floor demanded (want nothing above the target's libc) ---"
objdump -T "$OUT/libtk.so" 2>/dev/null | grep -oE 'GLIBC_[0-9.]+' | sort -uV || true
echo "--- exported tokens (should be exactly the renamed set) ---"
readelf -sW --dyn-syms "$OUT/libtk.so" | awk '$4=="FUNC"||$4=="OBJECT"{print $8}' \
    | grep -E '^(Pthread_|Getaddrinfo|Freeaddrinfo|Connect|Sendto|tk_lan_only_stats|__Errno|__Get_h_errno|__SF|Arc4random|Res_init|Sysconf|__System_property_get|F?[Ss]tat$|__fD_SET_chk|__[MS]|__Vsprintf_chk|Basename|Get|Gmtime_r|Localtime_r|Readdir|Sigaction|Strerror_r|Strftime)' \
    | sort | tr '\n' ' '; echo
