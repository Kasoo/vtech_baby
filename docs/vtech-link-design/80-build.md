# 80 — Build, dependencies and packaging

Serves R35, R36. Reference: `tutk-host/release.sh`, `sync_sdk.sh`, `tutk-host/tools/imports-allowed.txt`.

## Workspace

`vtech_baby/vtech-link/` (a Cargo workspace; `10-architecture.md`):

```
vtech-link/
  Cargo.toml          workspace; [profile.release] panic = "unwind", lto = "thin", codegen-units = 1, strip = "symbols"
  rust-toolchain.toml pinned stable (MSRV recorded here)
  deny.toml           cargo-deny: licences, advisories, duplicate crates, banned crates
  core/  py/  cli/  sim/  fuzz/
  release.sh          the four shipped builds, in containers
  tests/interop/      openssl s_server DTLS test (dev only)
```

## Dependencies

All pure Rust. No `build.rs` compiles C, and nothing links a C library beyond libc.

| Crate | Used for | Crate |
|---|---|---|
| `mio` (`net`, `os-poll`) | the reactor | core |
| `socket2` | socket options before handing the socket to mio | core |
| `if-addrs` | `getifaddrs` | core |
| `dimpl` (`"0.8"`, `default-features = false`, `rust-crypto`; from our fork until upstream has the PSK suites, below) | DTLS 1.2 PSK client | core |
| `x25519-dalek` (`static_secrets`) | live-view key exchange (and DTLS ECDHE in the own-client fallback) | core |
| `sha2`, `sha1`, `hmac` | PRF, transcript, connect-record HMAC, PSK | core |
| `chacha20poly1305`, `chacha20` | DTLS records, frame crypto, key unwrap | core |
| `zeroize` | secrets | core |
| `getrandom` | randomness (behind the `Rng` trait) | core |
| `bytes` | owned payload slices | core |
| `bitvec` (or a 64-word array by hand) | packet window | core |
| `log` | logging facade | core |
| `thiserror` | error types | core |
| `pyo3` (`abi3-py313`, `extension-module`) | the Python module | py |
| `clap`, `env_logger` | CLI | cli |
| `proptest`, `libfuzzer-sys`/`cargo-fuzz` | tests | dev |

Each added crate is a reviewed decision, as the import allow-list was for `tutk-host`. `cargo deny`
fails the build on an unlisted licence (MIT, Apache-2.0, BSD-3-Clause and Unicode are allowed), a
RustSec advisory, or a banned crate (`openssl`, `openssl-sys`, `ring` and any `*-sys` that builds C).

## dimpl from our fork

dimpl needs two suites that upstream doesn't have yet (`30-dtls.md`). We use Cargo's standard
mechanism for a pending upstream change: the code depends on the crates.io crate, and the workspace
root swaps in our fork.

```toml
# core/Cargo.toml
dimpl = { version = "0.8", default-features = false, features = ["rust-crypto"] }

# Cargo.toml (workspace root)
[patch.crates-io]
dimpl = { git = "https://github.com/Kasoo/dimpl", tag = "vtech-0.8.0-psk.1" }
```

- **Tag, not branch.** Each fork revision gets a new tag (`vtech-<upstream version>-psk.<n>`), so a
  rebase of `psk-suites` never orphans a commit a build uses. `Cargo.lock` records the commit, and
  builds run with `--locked`.
- **The https URL**, so the release containers need no SSH key.
- **Moving back to upstream**: delete the `[patch]` entry and bump the version, once upstream ships the
  suites.
- **Offline builds**, if wanted: `release.sh` runs `cargo vendor` before starting the containers and
  passes the vendor directory and its `.cargo/config.toml` in. `vendor/` is not committed.
- Proven by the spike (`spikes/dtls-camera/Cargo.toml`), which builds from the tag with `--locked`.

## The four builds (R35)

| Build | Image | Target | Wheel tag (for reference) |
|---|---|---|---|
| `x86_64-glibc` | `quay.io/pypa/manylinux2014_x86_64` + rustup | `x86_64-unknown-linux-gnu` | manylinux2014 (glibc 2.17) |
| `aarch64-glibc` | `quay.io/pypa/manylinux2014_aarch64` + rustup (qemu) | `aarch64-unknown-linux-gnu` | manylinux2014 |
| `x86_64-musl` | `docker.io/library/alpine:3.24` + rustup | `x86_64-unknown-linux-musl`, `-C target-feature=-crt-static` | musllinux_1_2 |
| `aarch64-musl` | `alpine:3.24` + rustup (qemu) | `aarch64-unknown-linux-musl`, `-crt-static` | musllinux_1_2 |

- Built with **maturin** 1.15.0 from pip (`maturin build --release --strip --locked --compatibility <tag>`),
  with the toolchain pinned by `rust-toolchain.toml` installed by rustup in every container (Alpine's own
  rust is not used). Musl passes `--auditwheel skip`, since maturin would otherwise graft `libgcc_s` into
  the wheel; `release.sh`'s own `DT_NEEDED` check replaces it. The `.so` is taken out of the wheel: we ship the file, not a wheel (below).
- **abi3, Python ≥ 3.13**: one module per build serves every HA Python from 3.13 onwards (HA runs 3.14
  today: `Python 3.14.6` in the live probe header).
- **Musl must link dynamically** (`-crt-static`), because a Python extension module is loaded into a
  dynamically linked interpreter. `release.sh` sets `RUSTFLAGS` for it.
- **Reproducible**: `--locked`, `SOURCE_DATE_EPOCH` from the commit, `--remap-path-prefix` for the
  source and cargo-home paths. `release.sh` builds twice and compares hashes; all four sets matched on 2026-10-22. It builds a `git archive`
  of one revision, not the working tree.
- **qemu** for the foreign arch, as `tutk-host/release.sh` does. Pass `--arch` explicitly, since the
  local `alpine:3.24` tag flips arch on pull.

## The import check

`release.sh` checks only what each build links against. `DT_NEEDED` must be `libc.so.6` (or
`libc.musl-*.so.1`), `libgcc_s.so.1`, and on glibc the split-outs (`libpthread`, `libdl`, `librt`,
`libm`, `ld-linux-*`) where the old toolchain uses them. Nothing else is allowed. maturin's auditwheel
check covers this on the glibc sets. The musl sets skip auditwheel (`--auditwheel skip`, because a
repair would graft `libgcc_s`, which the host already has), so this check stands in for it there.

`libgcc_s.so.1` provides the unwinder's `_Unwind_*` symbols, which Rust's unwinding needs on both libcs.
It is in HA's image: on 2026-10-21 `/usr/lib/libgcc_s.so.1` was there, from Alpine's `libgcc`, which
`ffmpeg` and others require (`podman exec homeassistant apk info -r libgcc`). It is also on every glibc
host. If it ever went missing, the fallback is `panic = "abort"` plus a reactor that never panics,
which is a weaker R34.

**No symbol allow-list (decided 2026-10-22).** `tutk-host`'s `release.sh` fails on any imported symbol
outside `tools/imports-allowed.txt`. vtech-link carried that over in Phase 0, then dropped it. The list
mattered for `tutk-host` because it borrows the host's OpenSSL through stand-ins, and because its imports
were meant to match the stock libraries'. Neither applies here: the module is pure Rust, and auditwheel
plus `tools/ha_import_check.sh` (loading the module in the HA image) cover loadability. The last review
(at `40e3d9f`) found every libc import came from std, `socket2`, `if-addrs`, `getrandom` or dimpl's
`der`/`x509_cert`.

## Shipping and loading

- `sync_link.sh` (replacing `sync_sdk.sh`) runs `release.sh` and copies each build to
  `custom_components/vtech_baby/native/<arch>-<libc>/_vtech_link.abi3.so`. It writes
  `native/LINK_SOURCE` with the `vtech-link` revision. These are our own builds, so they may be
  committed; `.gitignore` keeps its exception for `native/*-*/`.
- **Why vendor the `.so` rather than a PyPI requirement in `manifest.json`**: it keeps the integration
  self-contained, with no package index and no network at install. It also keeps the existing
  "built only by the sync script" rule. Publishing wheels to PyPI later is possible without changing the
  code; only the loader would change.
- **Licence notices**: the module compiles in dimpl and the RustCrypto crates (MIT/Apache), whose
  notices must travel with the binaries. `sync_link.sh` writes
  `native/THIRD_PARTY_LICENSES` with `cargo about generate`.
- Size: the hello-world is 366-398 KB per build stripped (more once the core's crypto is linked in); expect about 1 MB. Today's C sets are about 300 KB each, so the four builds add
  about 3 MB to the HACS download.
- `link.py` loads the module with `ExtensionFileLoader("_vtech_link", path)`. The init symbol is
  `PyInit__vtech_link`, so the module name must stay `_vtech_link`.

## CI

`.github/workflows/validate.yml` gains a Rust job: `cargo fmt --check`, `cargo clippy -D warnings`
(with the parser lints from `70-runtime.md`), `cargo test --workspace`, `cargo deny check`, and
a 60-second fuzz smoke run per target. The four release builds run on tags only.
