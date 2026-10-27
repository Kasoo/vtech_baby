#!/bin/sh
# Run a live probe inside the real HA image, shipping the vtech_lan package as a zip and the package's
# vtech-link module for the image (musl) next to it.
#
#   VTECH_LIVE_RUNNER=... VTECH_UID=... VTECH_PWD=... VTECH_AUTHKEY=... ./run.sh [probe.py] [probe args...]
#
#   VTECH_LIVE_RUNNER  the script that runs a probe in the HA container (not in this repo). It is called
#            as `$VTECH_LIVE_RUNNER probe.py [args]` with ARCH and EXTRA (the files to copy next to the
#            probe) in the environment.
#   ARCH     amd64 (default) or arm64.
set -e
: "${VTECH_LIVE_RUNNER:?set VTECH_LIVE_RUNNER to the script that runs a probe in the HA image}"
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
ARCH="${ARCH:-amd64}"
case "$ARCH" in amd64) A=x86_64 ;; arm64) A=aarch64 ;; *) echo "ARCH must be amd64 or arm64" >&2; exit 2 ;; esac
export ARCH
MODULE="$ROOT/custom_components/vtech_baby/native/$A-musl/_vtech_link.abi3.so"
[ -f "$MODULE" ] || { echo "no $MODULE: run sync_link.sh $A-musl" >&2; exit 2; }
PROBE="$HERE/probe_phase1.py"
case "$1" in *.py) PROBE=$(cd "$(dirname "$1")" && pwd)/$(basename "$1"); shift ;; esac

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM
(cd "$ROOT/custom_components/vtech_baby" && python3 -c "
import sys, zipfile, pathlib
with zipfile.ZipFile(sys.argv[1], 'w') as z:
    for p in sorted(pathlib.Path('vtech_lan').rglob('*.py')):
        z.write(p)
" "$TMP/vtech_lan.zip")
EXTRA="$TMP/vtech_lan.zip $MODULE" "$VTECH_LIVE_RUNNER" "$PROBE" "$@"
