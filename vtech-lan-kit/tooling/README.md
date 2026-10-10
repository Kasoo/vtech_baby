# Tooling — load the SDK on a host libc

The ThroughTek/TUTK Kalay SDK libraries in `../sdk/` are Android (bionic) ELFs. To load them in-process
on a normal Linux host (glibc or musl) they need two things: a small load-time ELF patch, and a
companion library that supplies the handful of symbols whose ABI differs between bionic and the host
libc. Everything here is purely a loader-compatibility layer; it does not change SDK behaviour.

## Contents

| Path | What it is |
|---|---|
| `elfpatch.py` | In-place, same-size ELF64 patcher. Strips symbol-version records, fixes `DT_NEEDED` entries, and renames the ABI-divergent imports to companion tokens. Pure stdlib. |
| `patch_sdk.py` | The patch step as an importable, pure-Python function, `patch_sdk(sdk_dir, out_dir, companion=None)`, plus a CLI. It detects the architecture, checks the SDK version (only `SDK_VERSION`, 4.3.8.1, from the libraries' embedded version string), applies the `elfpatch.py` edits, verifies the result, optionally copies in a prebuilt companion, and caches via a `PATCHED.json` manifest. It is used by `patch.sh`, and by hosts with no compiler (e.g. a Home Assistant integration). |
| `extract_sdk.py` | Finds the four SDK libraries for one architecture in any zip (an APK, a split bundle such as `.xapk`, or the libraries zipped up), searching zips inside it one level deep, and keeps only ELF shared objects of that architecture and SDK version. Writes them to an SDK directory: `extract_sdk(package, out_dir, arch)`, plus a CLI. Pure stdlib. |
| `companion/` | Source for the companion library `libtk.so` (a.k.a. `libtutkabi.so`), plus `build.sh`. Build it on a development machine; an integration ships the **prebuilt** result for each architecture (`../docs/85-ha-deployment.md`). |
| `companion/renames.txt` | The old→new symbol rename map shared by the patcher and the companion. |
| `patch.sh` | One-shot development step: `patch_sdk.py` followed by a companion build for the same architecture, into one output directory. |

## Build

```sh
./patch.sh [SDK_DIR] [OUTDIR]        # defaults: ../sdk/x86_64 (../sdk/arm64-v8a on an aarch64 host), ./patched
```

`patch.sh` detects the target architecture itself, by reading `e_machine` out of `libAVAPIs.so` in
`SDK_DIR` (x86_64 or aarch64; anything else is refused). It patches through `patch_sdk.py` and builds the
companion for that architecture. This produces `OUTDIR/` containing the four patched libraries plus
`libtk.so`. Point the client's `VTECH_LIBDIR` at that directory (the SDK license is embedded in the
client; add a `LICENSE_KEY.txt` there only to override it).

On a host with no compiler, patch only, and use a companion built elsewhere for the same architecture:

```sh
python3 patch_sdk.py SDK_DIR OUTDIR --companion path/to/<arch>/libtk.so
```

To build the companions an integration ships:

```sh
ARCH=x86_64  sh companion/build.sh dist/x86_64
ARCH=aarch64 sh companion/build.sh dist/aarch64
```

`../docs/85-ha-deployment.md` describes the full Home Assistant deployment model.

## How it works

1. **`elfpatch.py`** applies byte-level edits to each SDK library, all within existing section spans (no
   program-header move, no section resize):
   - **strip versioning** — remove `DT_VERSYM` / `DT_VERNEED` / `DT_VERNEEDNUM` so imports bind by name
     on glibc (musl ignores versions already);
   - **needed fixups** — drop `DT_NEEDED libc.so` / `libm.so` (those symbols resolve from the host libc
     already in the process) and repoint `libdl.so` in place to the companion soname `libtk.so`;
   - **rename imports** — overwrite the ABI-divergent undefined symbol names in `.dynstr`, in place and
     same-length, to unique tokens the companion defines;
   - **runpath** — add a `DT_RUNPATH` of `$ORIGIN`, its string written over the now-unused `libc.so`
     `DT_NEEDED` name (same length) and the entry itself placed in the `.dynamic` slack the needed-fixup
     drops freed. musl only resolves a `DT_NEEDED` soname (`libtk.so`, sibling SDK libraries) via a path
     search — `LD_LIBRARY_PATH`, runpath, or system directories — never by matching an object already
     loaded under an absolute path, and a host process embedding the loader generally can't set
     `LD_LIBRARY_PATH` after it has already started. `$ORIGIN` lets musl find those libraries next to
     the patched one with no environment changes; glibc resolves either way, so this is harmless there;
   - **canary redirect (aarch64 only)** — bionic's arm64 stack-protector reads its canary from TLS slot
     `TPIDR_EL0+0x28`, a slot glibc/musl don't own. Every `mrs Xt, TPIDR_EL0` in an executable section is
     rewritten to `adrp Xt, <page of vaddr 0>`, so the following `ldr [Xt,#0x28]` instead reads a word
     out of the library's own mapped ELF header — a value that's constant for that library, so the
     canary check always balances. Accidental stack overflows are still caught; only the
     exploit-hardening value of an unpredictable canary is given up.

2. **The companion (`libtk.so`)** defines the renamed tokens. It is built **once per target
   architecture, on glibc**, and loads on both glibc and musl for that architecture (its
   `DT_NEEDED libc.so.6` / `libm.so.6` map to musl's own libc via musl's reserved-soname handling, and
   musl ignores the `GLIBC_*` version tags). It covers:
   - **pthread** — a side table mapping bionic-laid-out pthread objects to host-initialized objects,
     including a stack-size clamp: the bionic caller sizes worker stacks against bionic's tiny
     `PTHREAD_STACK_MIN`, which is smaller than the host minimum on some architectures (and aarch64
     signal frames grow with vector-extension state), so requested sizes are clamped up to a safe floor;
   - **getaddrinfo/freeaddrinfo** — struct-field order differs between bionic and host `struct addrinfo`;
   - **the LAN-only egress policy** (`net.c`) — `connect`/`sendto` refuse any destination outside a
     directly attached IPv4 subnet with `ENETUNREACH`, and `getaddrinfo` resolves numeric literals only, so
     the SDK can never reach ThroughTek's servers; `tk_lan_only_stats` reports the refusal counts
     (`../docs/00-prerequisites.md`, "Host / networking");
   - **host-missing accessors** — bionic `__errno` / `__get_h_errno` / `arc4random` / `res_init` /
     `__system_property_get` (the last answers "not set" — there's no Android property service on a
     host), a libc-independent `sysconf` over bionic `_SC_*` numbers, `stat`/`fstat` forwarders (needed
     because glibc below 2.33 exports no `stat`/`fstat` symbol directly), and the bionic `__sF` stdio
     object;
   - **the bionic `_FORTIFY_SOURCE` `__*_chk` family**;
   - **loud-abort stubs** for a watchlist of ABI-divergent symbols that the session/AV/IOCtrl path does
     not call (renamed so a real call traps loudly instead of silently binding with the wrong ABI).

## Architecture support

Both x86_64 and aarch64 are supported, 64-bit only. An aarch64 host needs a 4K or 16K page-size kernel:
the SDK libraries themselves are 16K-aligned, so a 64K-page kernel is out of scope. Building the
companion for a foreign architecture (e.g. producing an aarch64 companion on an x86_64 host) needs
`podman` plus `qemu-user` binfmt support; a same-architecture build falls back to the native `gcc` if
podman isn't available.
