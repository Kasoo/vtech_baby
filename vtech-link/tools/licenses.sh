#!/bin/sh
# Write THIRD_PARTY_LICENSES.txt: the licence notices of every crate compiled into the Python module
# (vtech-link-py), for all four targets. Needs cargo-about (`cargo install --locked cargo-about --features cli`).
#
#   tools/licenses.sh [OUT]      OUT default THIRD_PARTY_LICENSES.txt in vtech-link/py/, which the wheel ships
set -e
HERE=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-$HERE/py/THIRD_PARTY_LICENSES.txt}
cd "$HERE"
cargo about generate --locked --fail -c about.toml -m py/Cargo.toml -o "$OUT" tools/licenses.hbs
echo "wrote $OUT ($(grep -c '^  - ' "$OUT") crate entries)"
