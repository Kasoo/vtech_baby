#!/bin/sh
# Render brand/icon.png (256) and brand/icon@2x.png (512) from icon.svg, and images/social-preview.png
# (1280x640) from social.svg. Needs inkscape and the Open Sans font.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
inkscape "$HERE/icon.svg" -w 256 -h 256 -o "$HERE/../custom_components/vtech_baby/brand/icon.png"
inkscape "$HERE/icon.svg" -w 512 -h 512 -o "$HERE/../custom_components/vtech_baby/brand/icon@2x.png"
inkscape "$HERE/social.svg" -w 1280 -h 640 -o "$HERE/../images/social-preview.png"
