#!/bin/sh
# Write LIB/imports.txt: the functions a drop-in for one library must export. That is every function
# the other stock SDK libraries import from it, plus the entry points the integration calls on it
# directly (vtech_lan/sdk.py).
#
#   tools/gen_imports.sh LIB SDK_DIR     (LIB: global, iotc or av; SDK_DIR holds the four stock libraries)
set -e
LIB="${1:?usage: gen_imports.sh LIB SDK_DIR}"
SDK="${2:?usage: gen_imports.sh LIB SDK_DIR}"
case "$LIB" in
global) SO=TUTKGlobal
        DIRECT="TUTK_SDK_Set_License_Key" ;;
iotc)   SO=IOTC
        DIRECT="IOTC_Initialize2 IOTC_Set_LanSearchPort IOTC_Get_SessionID IOTC_Connect_ByUIDEx
                IOTC_Session_Check IOTC_Session_Close IOTC_Session_Get_Free_Channel
                IOTC_Session_Channel_OFF" ;;
av)     SO=AV
        DIRECT="avInitialize avClientStartEx avClientStop avSendIOCtrl avRecvIOCtrl
                avClientCleanVideoBuf avClientCleanAudioBuf avRecvFrameData2 avRecvAudioData
                avServStart3 avSendAudioData avServResetBuffer avServStop avServExit" ;;
*)      echo "LIB must be global, iotc or av" >&2; exit 2 ;;
esac
HERE=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM
for l in TUTKGlobal IOTC AV Nebula; do
    [ "$l" = "$SO" ] && continue
    nm -D --undefined-only "$SDK/lib${l}APIs.so" | awk '{print $2}'
done | sed 's/@.*//' | sort -u > "$TMP/undef"
nm -D --defined-only "$SDK/lib${SO}APIs.so" | sed 's/@.*//' > "$TMP/defs"
awk '$2 == "T" {print $3}' "$TMP/defs" | sort -u > "$TMP/def"
awk '$2 != "T" {print $3}' "$TMP/defs" | sort -u > "$TMP/data"
data=$(comm -12 "$TMP/undef" "$TMP/data")
if [ -n "$data" ]; then
    echo "imported data objects, which stubs cannot provide:" $data >&2
    exit 1
fi
{
    printf '%s\n' $DIRECT
    comm -12 "$TMP/undef" "$TMP/def"
} | sort -u > "$HERE/$LIB/imports.txt"
wc -l < "$HERE/$LIB/imports.txt"
