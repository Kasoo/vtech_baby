#!/bin/sh
# Write global/imports.txt: every symbol the stock IOTC, AV and Nebula libraries import from
# libTUTKGlobalAPIs.so, plus the entry points the integration calls on Global directly.
#
#   tools/gen_imports.sh SDK_DIR        (a directory of the four stock SDK libraries)
set -e
SDK="${1:?usage: gen_imports.sh SDK_DIR}"
HERE=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM
for l in IOTC AV Nebula; do
    nm -D --undefined-only "$SDK/lib${l}APIs.so" | awk '{print $2}'
done | sed 's/@.*//' | sort -u > "$TMP/undef"
nm -D --defined-only "$SDK/libTUTKGlobalAPIs.so" | awk '{print $3}' | sed 's/@.*//' | sort -u > "$TMP/def"
{
    echo TUTK_SDK_Set_License_Key
    comm -12 "$TMP/undef" "$TMP/def"
} | sort -u > "$HERE/global/imports.txt"
wc -l < "$HERE/global/imports.txt"
