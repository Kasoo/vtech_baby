#!/bin/sh
# patch.sh -- produce a host-loadable library set from the sourced ThroughTek/TUTK Kalay SDK ELFs.
#
# The SDK libraries are Android (bionic) ELFs. This step makes them load in-process against the host
# libc (glibc or musl) by:
#   1. patching each library with elfpatch.py (strip version records; drop DT_NEEDED libc.so/libm.so;
#      repoint DT_NEEDED libdl.so -> the companion soname libtk.so; rename the ABI-divergent imports
#      to the companion's tokens; add a DT_RUNPATH "$ORIGIN" so musl can resolve the companion and
#      sibling libraries without LD_LIBRARY_PATH; on aarch64 also redirect the bionic TLS stack-canary
#      read to a stable per-library constant), and
#   2. building the companion library (libtk.so) that defines those tokens, for the same architecture
#      as the SDK libraries being patched.
#
# Architecture is auto-detected per SDK_DIR by reading e_machine out of libAVAPIs.so (x86_64 or
# aarch64; anything else is refused). SDK_DIR defaults to ../sdk/x86_64, unless this host itself is
# aarch64, in which case it defaults to ../sdk/arm64-v8a.
#
# Output: OUTDIR (default ./patched/) containing the four patched libraries + libtk.so. The client
# then just needs OUTDIR; the SDK license is embedded in the client (override with a LICENSE_KEY.txt
# in OUTDIR if needed — see docs/00-prerequisites.md).
#
#   ./patch.sh [SDK_DIR] [OUTDIR]      # SDK_DIR default ../sdk/x86_64 (../sdk/arm64-v8a on an aarch64
#                                         host), OUTDIR default ./patched
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
DEFAULT_SDK="$HERE/../sdk/x86_64"
[ "$(uname -m)" = "aarch64" ] && DEFAULT_SDK="$HERE/../sdk/arm64-v8a"
SDK="${1:-$DEFAULT_SDK}"
OUT="${2:-$HERE/../patched}"
mkdir -p "$OUT"

# patch_sdk.py detects the architecture from the SDK libraries' ELF headers, applies the elfpatch.py
# edits (incl. the aarch64-only canary redirect) and verifies the result.
python3 "$HERE/patch_sdk.py" "$SDK" "$OUT"
ARCH=$(python3 -c "import sys; sys.path.insert(0, '$HERE'); from patch_sdk import sdk_arch; print(sdk_arch('$SDK'))")

echo "[build] companion libtk.so ($ARCH)"
ARCH="$ARCH" sh "$HERE/companion/build.sh" "$OUT"

echo "== library set ready in: $OUT =="
echo "   (point VTECH_LIBDIR at this directory; license is embedded, drop a LICENSE_KEY.txt here to override)"
