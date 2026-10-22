#!/bin/sh
# Build the _vtech_link Python module for each target, each in a container for its libc:
#
#   ./release.sh [SET...]      SET: x86_64-glibc aarch64-glibc x86_64-musl aarch64-musl (default: all)
#
# glibc builds in manylinux2014 (glibc 2.17), so the module loads on any glibc host since; musl builds in
# Alpine 3.24 (Home Assistant's base), linked dynamically. The pinned Rust toolchain comes from rustup and
# maturin from pip, both cached under build/cache/SET. A foreign arch runs under qemu-user binfmt.
#
# Builds the committed tree (git archive of REV, default HEAD at start), not the working tree, unless
# WORKTREE=1. Each set is built twice and the two hashes compared. Writes dist/SET/_vtech_link.abi3.so (stripped) and fails if it
# imports anything outside tools/imports-allowed.txt or needs a library other than libc, libgcc_s and the glibc split-outs:
# widening either is a deliberate, reviewed change.
set -e
export LC_ALL=C
HERE=$(cd "$(dirname "$0")" && pwd)
ALPINE=docker.io/library/alpine:3.24
MATURIN=${MATURIN:-1.15.0}
[ $# -gt 0 ] || set -- x86_64-glibc aarch64-glibc x86_64-musl aarch64-musl
TOP=$(git -C "$HERE" rev-parse --show-toplevel)
REV=${REV:-$(git -C "$HERE" rev-parse HEAD)}   # one revision for every set
EPOCH=$(git -C "$HERE" log -1 --format=%ct "$REV")

# Runs inside the container: /src is the source, /cache the per-set cache, /dist the output.
INNER='
set -eu
export CARGO_HOME=/cache/cargo RUSTUP_HOME=/cache/rustup PIP_CACHE_DIR=/cache/pip PATH=/cache/cargo/bin:$PATH
if [ "$LIBC" = musl ]; then
  apk add -q build-base python3 py3-pip curl git binutils
  export RUSTFLAGS="-C target-feature=-crt-static"
  compat="musllinux_1_2 --auditwheel skip"  # maturin would graft libgcc_s; the host has it
else
  export PATH=/opt/python/cp313-cp313/bin:$PATH
  compat=manylinux2014
fi
export RUSTFLAGS="${RUSTFLAGS:-} --remap-path-prefix=/src=. --remap-path-prefix=/cache/cargo=/cargo"
chan=$(sed -n "s/^channel *= *\"\(.*\)\"/\1/p" /src/rust-toolchain.toml)
[ -x /cache/cargo/bin/rustup ] || curl -sSf https://sh.rustup.rs | sh -s -- -y -q --profile minimal --default-toolchain none >/dev/null 2>&1
rustup -q toolchain install "$chan" --profile minimal
export RUSTUP_TOOLCHAIN=$chan
python3 -m venv /tmp/venv
/tmp/venv/bin/pip install -q "maturin==$MATURIN"
export PATH=/tmp/venv/bin:$PATH
echo "$(rustc -V), $(maturin -V), $(python3 -V)"
build() {
  rm -rf /src/target
  (cd /src && maturin build --release --strip --locked --compatibility $compat -m py/Cargo.toml -o /src/target/wheels) >&2
  python3 -c "import sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); n=[n for n in z.namelist() if n.endswith(\"_vtech_link.abi3.so\")]; assert len(n)==1, z.namelist(); open(sys.argv[2],\"wb\").write(z.read(n[0]))" \
    /src/target/wheels/*.whl "$1"
}
mkdir -p /dist
build /dist/_vtech_link.abi3.so
build /tmp/second.so
if cmp -s /dist/_vtech_link.abi3.so /tmp/second.so; then echo "reproducible: yes"; else echo "reproducible: NO"; fi
' 

for set in "$@"; do
  arch=${set%-*}
  libc=${set#*-}
  case $arch in
    x86_64) platform=amd64 ;;
    aarch64) platform=arm64 ;;
    *) echo "unknown arch in $set" >&2; exit 2 ;;
  esac
  case $libc in
    musl) image=$ALPINE ;;
    glibc) image=quay.io/pypa/manylinux2014_$arch ;;
    *) echo "unknown libc in $set" >&2; exit 2 ;;
  esac
  echo "== $set ($image)"
  work="$HERE/build/src/$set"
  out="$HERE/dist/$set"
  rm -rf "$work" "$out"
  mkdir -p "$work" "$out" "$HERE/build/cache/$set"
  if [ -n "$WORKTREE" ]; then
    tar -C "$HERE" --exclude=./target --exclude=./dist --exclude=./build -cf - . | tar -C "$work" -xf -
  else
    git -C "$TOP" archive "$REV:${HERE#$TOP/}" | tar -C "$work" -xf -
  fi
  podman run --rm --arch "$platform" -e LIBC="$libc" -e MATURIN="$MATURIN" -e SOURCE_DATE_EPOCH="$EPOCH" \
    -v "$work":/src:z -v "$HERE/build/cache/$set":/cache:z -v "$out":/dist:z -w /src "$image" sh -c "$INNER"
  rm -rf "$work"

  # Check the imports and DT_NEEDED with the container's own binutils (the host's may not read the foreign arch).
  podman run --rm --arch "$platform" -v "$out":/dist:z -v "$HERE/tools":/tools:ro,z "$image" sh -c '
    export LC_ALL=C
    f=/dist/_vtech_link.abi3.so
    command -v nm >/dev/null || apk add -q binutils
    nm -D --undefined-only $f | awk "{print \$2}" | sed "s/@.*//" | sort -u > /tmp/imports
    nm -D --defined-only $f | awk "{print \$3}" | sed "s/@.*//" | sort -u > /tmp/ours
    cp /tmp/imports /dist/imports.txt
    readelf -d $f | sed -n "s/.*(NEEDED).*\[\(.*\)\]/\1/p" | sort > /dist/needed.txt
    extra=$(comm -23 /tmp/imports /tools/imports-allowed.txt | comm -23 - /tmp/ours)
    [ -z "$extra" ] || { echo "imports not in tools/imports-allowed.txt:"; echo "$extra"; bad=1; }
    badneed=$(grep -Ev "^(libc\.so\.6|libc\.musl-.*\.so\.1|libgcc_s\.so\.1|libpthread\.so\.0|libdl\.so\.2|libm\.so\.6|ld-linux-.*\.so\.[0-9])$" /dist/needed.txt || true)
    [ -z "$badneed" ] || { echo "unexpected DT_NEEDED:"; echo "$badneed"; bad=1; }
    exit ${bad:-0}' || { echo "$set failed the import check" >&2; exit 1; }
  echo "needed: $(tr '\n' ' ' < "$out/needed.txt")"
  (cd "$out" && ls -l _vtech_link.abi3.so | awk '{print "size:", $5}' && sha256sum _vtech_link.abi3.so)
done
