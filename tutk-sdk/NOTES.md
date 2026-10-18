# tutk-sdk: working notes

A replacement for the Kalay SDK 4.3.8.1 libraries, written from the TUTK kit
(`$CAMERA_DIR/tutk-kit/`). Work in progress: not wired into the integration, and not documented
for release until it covers the whole SDK. `libTUTKGlobalAPIs.so` and `libIOTCAPIs.so` are done;
`libAVAPIs.so` is scaffolded (every export a stub).
`libNebulaAPIs.so` stays stock: nothing calls into it.

## Decisions (2026-10-17)

- **Built for bionic with the NDK** (r28, API 21), then patched and loaded exactly like the stock
  library: the SDK patcher renames its ABI-divergent libc imports to the companion (`libtk.so`), which
  supplies the pthread side table, the stack-size floor and the LAN-only egress policy. The source is
  plain POSIX and knows nothing of the companion.
- **LAN-only by design.** Global resolves numeric addresses only; every internet-bound path fails the
  way it failed under the companion's policy (`tutk-kit/docs/shared/30-lan-only.md`). Server-only paths
  (TCP, TLS, client connections, curl, NAT64) are stubs or clean failures.
- **IOTC connects LAN-direct only.** `IOTC_Connect_ByUIDEx` runs just the LAN-local sequence; the
  master-name resolution, knock-retry task, ECDH key pair, TryPort and relay send paths are left out.
- **OpenSSL from the host.** The 42 `tutk_third_*` imports are tail-jumps to the host's `libssl.so.3` /
  `libcrypto.so.3` (`DT_NEEDED libssl.so.3`). Android's system libssl is BoringSSL, so running on
  Android would need an OpenSSL 3 shipped alongside.
- **Written from the kit only**, not from the decompiled library. A gap in the kit is raised and fixed
  in the kit.
- **Unimplemented symbols abort** with `unimplemented: <name>` on stderr.
- **Live probes** may connect and stream (audio, video, decryption). They must not change camera
  settings, switch LEDs or play audio, so `probe_talk.py` is excluded.

## Layout

| Path | What |
|---|---|
| `include/` | Generated from the kit by `tools/gen_header.py KIT_DIR`: `tutk_shared.h` (shared errors and types), `iotc_types.h` and `av_types.h` (each library's `01-types.md`). |
| `common/` | Linked into every library: the version string the patcher checks, the abort stub, the debug trace. |
| `global/`, `iotc/`, `av/` | Each library's hand-written sources, one file per kit area document. `imports.txt` is its drop-in surface: every function the other stock libraries import from it, plus the entry points the integration calls on it (`tools/gen_imports.sh LIB SDK_DIR`). |
| `Makefile`, `lib.mk` | `make` builds the three in order; `lib.mk` builds one. |
| `tools/gen_stubs.py` | Run by `make`: abort stubs for every import the hand-written objects don't define, and (Global only) the OpenSSL forwarders. It prints how many of each library's imports are implemented. |
| `tools/iotc_decode.sh` | Prints a capture's UDP datagrams with the IOTC payloads unscrambled (`iotc/wire.c` via `tools/iotc_decode.c`). The licence in use has no keyMask, so the key is the bare constant. |
| `tests/load_test.py` | Patches any mix of our libraries and the stock ones, and loads the set with every symbol bound. |

The drop-in surfaces (the x86_64 and arm64 SDKs agree): Global 151 (108 TUTK functions, 42
`tutk_third_*`, the licence call); IOTC 37 (the 30 functions stock AV imports and the integration's 8
calls, one shared); AV 14 (the integration's calls). IOTC links against Global and AV against both,
by soname, so each of ours runs over either the stock build or ours of the libraries below it. IOTC
and AV reach OpenSSL as the stock ones do, through Global's `tutk_third_*` forwarders.

## Build and test

```sh
make                     # all three, or make global|iotc|av; ARCH=x86_64 (default) or aarch64
python3 tests/load_test.py ../docs/vtech-lan-kit/sdk/x86_64 \
    ../custom_components/vtech_baby/native/x86_64/libtk.so build/x86_64/patched \
    build/x86_64/libTUTKGlobalAPIs.so    # name libIOTCAPIs.so / libAVAPIs.so too to swap those in
```

For musl, load `build/x86_64/patched` inside `localhost/home-assistant:2026.9.3-amd64`. The
`ghcr.io/home-assistant/home-assistant:2026.9.3` image on this machine is the arm64 variant.

Offline tests: `../.venv/bin/pytest` from this directory (its `pytest.ini` turns off Home Assistant's
plugins, which block sockets). They load all three of ours in-process (fixtures `g`, `iotc`, `av`,
`tk`), and check that every mix of ours and stock loads, each mix in its own process.

Live (on the NAS, in the HA image; read-only probes only):

```sh
cd ../tests/live && . $CAMERA_DIR/.camera.env && TK_TRACE=1 \
  VTECH_LIVE_RUNNER=$CAMERA_DIR/shim/tests/live_run.sh ARCH=amd64 \
  PATCHED=$REPO/tutk-sdk/build/x86_64/patched ./run.sh probe_phase1.py 10 1
```

A library built with `make DEBUG=1` logs each connection, task, crypto and address call to stderr when
`TK_TRACE=1`; the default build has no trace code. A companion built from the
current source (`../docs/vtech-lan-kit/tooling/companion/build.sh`) also logs every `sendto`/`connect`
under the same variable; build one into a scratch directory and pass it to `tests/load_test.py` to see
what either Global sends. That is how the stock library's traffic was compared with ours.

## Status

- 2026-10-17: Global's observed paths are implemented. `probe_phase1.py` (connect, every GET, events,
  live-view key exchange, keepalive, two connect cycles) and `probe_stream.py` (snapshots, H.264/AAC
  streaming, go2rtc RTSP and Opus) pass against the camera with our Global under the stock
  IOTC/AV/Nebula, in the HA image (musl). `probe_talk.py` plays audio and is not run.
- 2026-10-17: also passing: `probe_sps.py` (30 s, steady 10 fps, a keyframe every 2 s, one SPS),
  `probe_restart.py` (15 stop/start cycles, each starting on a keyframe, no bad frames) and
  `probe_stall.py` in the scenarios that write current values only (`none`, `731`, `790+731`,
  `burst`: no stall, every GET under 0.11 s, settings unchanged). Still to do: talk-back (needs
  someone at the camera) and a first arm64 run.
- 2026-10-18: IOTC and AV scaffolded from the kit's skeleton: per-area source files, generated type
  headers, stubbed surfaces. All seven mixes of ours and stock load (glibc offline; the all-ours set
  on musl in the HA image too); all three build for aarch64.
- 2026-10-18: IOTC: 34 of 37 exports written from the kit (init, session table and guards, channels,
  receive queues and reads, DTLS with its BIO and PSK registries, NAT, task lists, addresses, error
  mapping, the `99-stubs.md` entries), plus the scramble, checked against the kit's worked example and
  inverted for every length. Still stubs: `IOTC_Connect_ByUIDEx`, `IOTC_Session_Write` and
  `IOTC_Session_Write_DTLS`, waiting on kit gaps (credentials on the wire, connect sequence, data
  header, acks, keepalive and close); the close datagrams and the keepalive task body wait on the same.
- 2026-10-18: IOTC complete: all 37 exports. The LAN-direct connect (signed `0xcc51` record and probe
  broadcast every 100 ms, the camera's long-form `0x602`, our probe and `0x0402` ack), `0x0407` data
  out and `0x0408` in, keepalive (`0x0427`, 1 s tick, 60 s silence → -23) and the 3× `0x0417` close.
  `tests/test_iotc_peer.py` drives it against a fake camera on a local socket (connect, -68, -13
  timeout, data both ways, close). Live, with our Global and IOTC under stock AV/Nebula in the HA image
  (musl): `probe_phase1.py` (10 s × 1 and 70 s × 2 cycles, every GET, events, live-view key exchange)
  and `probe_stream.py` (snapshots, H.264/AAC, go2rtc RTSP and Opus, no bad frames) pass. `tools/
  iotc_decode.sh` decodes captures (both version bytes, `0x1d` ours and `0x1b` the camera's).

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
