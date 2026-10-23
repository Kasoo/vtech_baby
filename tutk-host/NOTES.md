# tutk-host: working notes

The TUTK Kalay SDK 4.3.8.1 replacement (`libTUTKGlobalAPIs.so`, `libIOTCAPIs.so`, `libAVAPIs.so`) built
for the host's own libc, glibc or musl. Forked on 2026-10-20 from the drop-in build `../tutk-sdk/`,
which is frozen at tag `tutk-sdk-dropin-v1` and keeps the history, the drop-in results and the
side-by-side testing against the stock libraries (its `NOTES.md`). Written from the TUTK kit
(`$CAMERA_DIR/tutk-kit/`). Not yet the integration's default library set.

## Decisions (2026-10-20)

- **Built with the host compiler for the host libc.** No NDK, no SDK patcher, no `libtk` companion and
  no `libNebulaAPIs.so`. The libraries name each other by soname and find each other beside them
  (`DT_RUNPATH $ORIGIN`). Global links the system OpenSSL 3 (`libssl.so.3`, `libcrypto.so.3`); IOTC
  and AV reach it through Global's `tutk_third_*` forwarders. The musl set is built in Alpine 3.24, the
  base of the Home Assistant image.
- **LAN-only egress is Global's own** (`global/egress.c`, ported from the companion's
  `tooling/companion/net.c`). A datagram leaves only for a directly attached IPv4 subnet or
  255.255.255.255 (also IPv4-mapped); anything else fails with `ENETUNREACH`. Names are never resolved
  (numeric-only `getaddrinfo`). Global exports `tk_lan_only_stats(sends, lookups)`, which the
  integration binds in place of the companion's.
- **The libc-imports rule of the drop-in build is retired.** It existed because the patcher only
  adapted what the stock libraries import. Portability between glibc and musl is the concern now:
  `tests/test_surface.py` checks each library depends only on libc, OpenSSL and the other two.
- Kept from the drop-in build: written from the kit only; LAN-direct only; unimplemented symbols abort
  with `unimplemented: <name>`; the live-probe rules (`../tutk-sdk/NOTES.md`).

## Layout

| Path | What |
|---|---|
| `include/` | Generated from the kit by `tools/gen_header.py KIT_DIR`. |
| `common/` | Linked into every library: the abort stub and the debug trace. |
| `global/`, `iotc/`, `av/` | Each library's sources, one file per kit area document. `imports.txt` is its surface. |
| `Makefile`, `lib.mk` | `make` builds the three into `build/ARCH-LIBC` (`-debug` with `DEBUG=1`). |
| `tools/gen_stubs.py` | Abort stubs for surface functions not written by hand, and Global's OpenSSL forwarders. |
| `tests/` | Offline tests: the libraries loaded directly; `test_av_peer.py` runs AV against a fake camera over a stand-in IOTC (`fake_iotc.c`). |

## Build and test

```sh
make                       # build/x86_64-glibc on this workstation
podman run --rm -v "$PWD":/src:z -w /src docker.io/library/alpine:3.24 \
    sh -c 'apk add -q build-base openssl-dev python3 && make'      # build/x86_64-musl
../.venv/bin/pytest        # offline, against the glibc build
```

Live, in the HA image on the NAS (the loader takes a directory without `libtk.so` as a host-built set):

```sh
cd ../tests/live && . $CAMERA_DIR/.camera.env && \
  VTECH_LIVE_RUNNER=$CAMERA_DIR/shim/tests/live_run.sh ARCH=amd64 \
  PATCHED=$REPO/tutk-host/build/x86_64-musl ./run.sh probe_phase1.py 10 1
```

Release sets (what the integration ships), each built in a container for its libc:

```sh
./release.sh                       # build/release/{x86_64,aarch64}-{musl,glibc}/, stripped
TK_BUILD=build/release/x86_64-glibc ../.venv/bin/pytest     # offline tests against a prebuilt set
../sync_sdk.sh                     # release.sh, then install into the package's native/<arch>-<libc>/
```

musl sets build in Alpine 3.24, glibc sets in manylinux2014 (glibc 2.17 floor). Both link against
generated stand-ins for `libssl.so.3`/`libcrypto.so.3` carrying the `OPENSSL_3.0.0` symbol version
(`OSSL_STANDIN=1`), so no container needs OpenSSL 3. The old linker there writes `DT_RPATH $ORIGIN`
instead of `DT_RUNPATH`, which loads the same way.
`release.sh` fails if a set imports anything outside `tools/imports-allowed.txt`. Beyond the stock
libraries' imports, the list has only toolchain references (`__register_frame_info`, `_ITM_*`,
`__gmon_start__`, `__getauxval` for arm64 outline atomics, `__stack_chk_guard`), libc spellings of the
same calls (`__errno_location`, glibc's `__ctype_*_loc` and `__res_init`), byte-order and ctype helpers
bionic inlines, `in6addr_any`, and `getifaddrs`/`freeifaddrs` for the LAN-only rule.

`DEBUG=1` builds the trace and the `TK_TRACE=2` taps described in `../tutk-sdk/NOTES.md`.

## Status

- 2026-10-20: forked. glibc and musl builds; 59 offline tests pass on glibc. The build advertises SDK
  version 4.3.8.100 (`TK_SDK_VERSION`, `common/tk_common.h`) in the LAN probe, the signed connect
  record and the connect ack, where the stock libraries send 4.3.8.1. Live, the musl set in the HA
  image passes probe_phase1 (2 x 70 s), probe_stream (0 bad), probe_restart (25 cycles, 0 lost),
  probe_talk --silent, probe_control --allow-writes and probe_stall none.
- 2026-10-20: release sets shipped in the integration (`../sync_sdk.sh`), with AV receive counters
  (`tk_av_recv_stats`, shown in live-view diagnostics). Live: probe_stream on the x86_64-musl set, 0 bad,
  all counters 0. Next: a soak test in the production HA, then retire the patcher path.
- 2026-10-20: parity recovery (`av/fec.c`). The camera sends one Reed-Solomon parity packet per group
  of 20 or 25 (generator rows from the kit's `docs/av/80-fec.md`), and the single missing packet of a
  group is rebuilt and fed to reassembly; kind 7 is covered too, unlike stock. Count in
  `tk_av_recv_stats` (`packets_recovered`). Offline: `av_peer.py parity`. Live, with a debug build
  dropping 1 packet in 50 (`TK_DROP=50`, debug builds only): 35 packets rebuilt (25 from 20-groups,
  10 from 25-groups), 0 bad frames. **Corrected 2026-10-22**: "0 bad" counted only partial frames given
  up. A frame none of whose packets arrives (every audio frame is one packet) is neither counted nor
  reported, and the camera ignores some NAKs, so frames were lost all the same
  (`../docs/vtech-link-design/86-live-losses.md`, findings 2 and 3).
- Earlier: make the host build the integration's library set (packaging, install, arm64 and a portable
  glibc build, e.g. in the manylinux2014 image), then retire the patcher path. Open from the drop-in
  build: the stats block in media packets (camera to client only; not needed).

## Known divergences from the stock library

On paths that never run, where the kit allows any behaviour that keeps state intact:

- `TUTK_Json_Obj_Get_Sub_Obj_String` returns `TJSON_ER_WRONG_TYPE` for a member that is not a string;
  the stock library returns json-c's text form of it.
- `TUTK3rdBase64Decode` rejects bad characters and misplaced padding with `TUTK_ER_INVALID_ARG`; the
  stock library returns whatever OpenSSL's base64 BIO decodes.

In IOTC, where the kit leaves a choice open (to revisit when it is pinned):

- The session slot layout is our own: nothing outside IOTC reads it.
- The DTLS handshake loop treats `SSL_ERROR_SYSCALL` (the BIO's `-13` read timeout, which sets no retry
  flag) as a retry alongside want-read/want-write.
- The broadcast search repeats every 100 ms (the task manager's tick); the stock library's interval is
  about 105 ms.
- `translate_Error` maps reasons `0x119` and `0x3fc` to `-50002` and every other reason to
  `-52000 - reason`; the kit does not say which reasons give `-50003`.

