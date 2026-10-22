#!/bin/sh
# Build tools/fixtures/gen_fixtures.c against the tutk-host sources in a temp directory, run it, and
# write core/tests/fixtures/tutk_host.txt. Needs host gcc and OpenSSL headers. tutk-host is not modified:
# its SDK version (0x04030864) is replaced by the design's 0x040308c8 in a patched copy of tk_common.h
# that only exists in the temp directory.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
host="$here/../../../tutk-host"
out="$here/../../core/tests/fixtures/tutk_host.txt"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

mkdir "$tmp/inc"
sed 's/0x04030864/0x040308c8/' "$host/common/tk_common.h" > "$tmp/inc/tk_common.h"
grep -q 0x040308c8 "$tmp/inc/tk_common.h"

gcc -O1 -w -ffunction-sections -Wl,--gc-sections -I "$tmp/inc" -I "$host/iotc" -I "$host/av" -I "$host/include" -I "$host/common" \
    "$here/gen_fixtures.c" "$host/iotc/wire.c" "$host/iotc/handshake.c" "$host/iotc/data.c" \
    -o "$tmp/gen_fixtures" -lpthread -lcrypto
"$tmp/gen_fixtures" > "$tmp/out.txt"
mkdir -p "$(dirname "$out")"
mv "$tmp/out.txt" "$out"
