#!/bin/sh
# Render brand/icon.png (256) and brand/icon@2x.png (512) from icon.svg. Needs inkscape.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
inkscape "$HERE/icon.svg" -w 256 -h 256 -o "$HERE/../custom_components/vtech_baby/brand/icon.png"
inkscape "$HERE/icon.svg" -w 512 -h 512 -o "$HERE/../custom_components/vtech_baby/brand/icon@2x.png"
