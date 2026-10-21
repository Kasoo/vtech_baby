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
| `x25519-dalek` (`static_secrets`) | DTLS ECDHE, live-view key exchange | core |
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

## The four builds (R35)

| Build | Image | Target | Wheel tag (for reference) |
|---|---|---|---|
| `x86_64-glibc` | `quay.io/pypa/manylinux2014_x86_64` + rustup | `x86_64-unknown-linux-gnu` | manylinux2014 (glibc 2.17) |
| `aarch64-glibc` | `quay.io/pypa/manylinux2014_aarch64` + rustup (qemu) | `aarch64-unknown-linux-gnu` | manylinux2014 |
| `x86_64-musl` | `docker.io/library/alpine:3.24` + `rust`, `cargo` | `x86_64-unknown-linux-musl`, `-C target-feature=-crt-static` | musllinux_1_2 |
| `aarch64-musl` | `alpine:3.24` (qemu) | `aarch64-unknown-linux-musl`, `-crt-static` | musllinux_1_2 |

- Built with **maturin** (`maturin build --release --strip --compatibility <tag>`). The `.so` is taken
  out of the wheel: we ship the file, not a wheel (below).
- **abi3, Python ≥ 3.13**: one module per build serves every HA Python from 3.13 onwards (HA runs 3.14
  today: `Python 3.14.6` in the live probe header).
- **Musl must link dynamically** (`-crt-static`), because a Python extension module is loaded into a
  dynamically linked interpreter. Alpine's rust package defaults to that.
- **Reproducible**: `--locked`, `SOURCE_DATE_EPOCH` from the commit, `--remap-path-prefix` for the
  source and cargo-home paths. `release.sh` builds twice and compares hashes, as the current one does.
- **qemu** for the foreign arch, as `tutk-host/release.sh` does. Pass `--arch` explicitly, since the
  local `alpine:3.24` tag flips arch on pull.

## The import check

`release.sh` keeps the rule that `tutk-host/release.sh` enforces today: list the module's undefined
dynamic symbols (`nm -D --undefined-only`) and fail if any is outside
`vtech-link/tools/imports-allowed.txt`. The list holds:
- libc functions std uses (socket, thread, time, memory, `getifaddrs`, `getrandom`/`syscall`, …);
- Python C API symbols (`Py*`, `_Py*`) from the abi3 set;
- on glibc, `__cxa_thread_atexit_impl`, `__tls_get_addr` and similar; on musl, the same names
  where they exist;
- the unwinder's `_Unwind_*` symbols, from **`libgcc_s.so.1`**. Rust's unwinding needs it on both libcs.
  It is in HA's image: on 2026-10-21 `/usr/lib/libgcc_s.so.1` was there, from Alpine's `libgcc`,
  which `ffmpeg` and others require (`podman exec homeassistant apk info -r libgcc`). It is also on
  every glibc host. If it ever went missing, the fallback is `panic = "abort"` plus a reactor that
  never panics, which is a weaker R34.

`DT_NEEDED` must be exactly `libc.so.6` (or `libc.musl-*.so.1`), `libgcc_s.so.1`, and on glibc
`libpthread`/`libdl`/`libm` where the old toolchain splits them out. Nothing else.

## Shipping and loading

- `sync_link.sh` (replacing `sync_sdk.sh`) runs `release.sh` and copies each build to
  `custom_components/vtech_baby/native/<arch>-<libc>/_vtech_link.abi3.so`. It writes
  `native/LINK_SOURCE` with the `vtech-link` revision. These are our own builds, so they may be
  committed; `.gitignore` keeps its exception for `native/*-*/`.
- **Why vendor the `.so` rather than a PyPI requirement in `manifest.json`**: it keeps the integration
  self-contained, with no package index and no network at install. It also keeps the existing
  "built only by the sync script" rule. Publishing wheels to PyPI later is possible without changing the
  code; only the loader would change.
- Size: about 1 MB per build stripped. Today's C sets are about 300 KB each, so the four builds add
  about 3 MB to the HACS download.
- `link.py` loads the module with `ExtensionFileLoader("_vtech_link", path)`. The init symbol is
  `PyInit__vtech_link`, so the module name must stay `_vtech_link`.

## CI

`.github/workflows/validate.yml` gains a Rust job: `cargo fmt --check`, `cargo clippy -D warnings`
(with the parser lints from `70-runtime.md`), `cargo test --workspace`, `cargo deny check`, and
a 60-second fuzz smoke run per target. The four release builds run on tags only.
