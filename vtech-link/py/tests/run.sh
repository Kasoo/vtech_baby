#!/bin/sh
# Build the module and the simulated camera, then run the Python tests against them.
#
#   py/tests/run.sh [unittest args]
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE/../.."
PATH=$HOME/.cargo/bin:$PATH cargo build -q -p vtech-link-py -p vtech-link-sim --bin sim-camera
exec python3 -m unittest discover -s "$HERE" -t "$HERE" "$@"
