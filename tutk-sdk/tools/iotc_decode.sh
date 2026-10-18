#!/bin/sh
# Print every UDP datagram in a capture, IOTC payloads unscrambled: time, source, destination, length,
# payload hex. Needs tshark.
#
#   tools/iotc_decode.sh CAPTURE.pcap [DISPLAY_FILTER]
set -eu
here=$(dirname "$0")/..
exe=$here/build/iotc_decode
[ "$exe" -nt "$here/tools/iotc_decode.c" ] && [ "$exe" -nt "$here/iotc/wire.c" ] || \
    cc -O1 -I"$here/iotc" -I"$here/include" -I"$here/common" "$here/tools/iotc_decode.c" \
       "$here/iotc/wire.c" -o "$exe" -lpthread
tshark -r "$1" -Y "udp && !dns && !mdns && !ssdp${2:+ && ($2)}" -T fields -E separator=' ' \
    -e frame.time_relative -e ip.src -e udp.srcport -e ip.dst -e udp.dstport -e udp.payload | "$exe"
