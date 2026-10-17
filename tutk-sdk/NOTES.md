# tutk-sdk: working notes

A replacement for the Kalay SDK 4.3.8.1 libraries, written from the TUTK kit
(`$CAMERA_DIR/tutk-kit/`). Work in progress: not wired into the integration, and not documented
for release until it covers the whole SDK. The first library is `libTUTKGlobalAPIs.so`.

## Decisions (2026-10-17)

- **Built for bionic with the NDK** (r28, API 21), then patched and loaded exactly like the stock
  library: the SDK patcher renames its ABI-divergent libc imports to the companion (`libtk.so`), which
  supplies the pthread side table, the stack-size floor and the LAN-only egress policy. The source is
  plain POSIX and knows nothing of the companion.
- **LAN-only by design.** Global resolves numeric addresses only; every internet-bound path fails the
  way it failed under the companion's policy (`tutk-kit/docs/shared/30-lan-only.md`). Server-only paths
  (TCP, TLS, client connections, curl, NAT64) are stubs or clean failures.
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
| `include/tutk_shared.h` | Generated from the kit's shared docs by `tools/gen_header.py KIT_DIR`. |
| `global/` | Hand-written sources. `imports.txt` is every symbol the stock IOTC/AV/Nebula import from Global plus `TUTK_SDK_Set_License_Key` (`tools/gen_imports.sh SDK_DIR`). |
| `tools/gen_stubs.py` | Run by `make`: abort stubs for every import the hand-written objects don't define, and the OpenSSL forwarders. |
| `tests/load_test.py` | Patches stock IOTC/AV/Nebula with our Global and loads the set with every symbol bound. |

## Build and test

```sh
make                                   # ARCH=x86_64 (default) or aarch64
python3 tests/load_test.py ../docs/vtech-lan-kit/sdk/x86_64 build/x86_64/libTUTKGlobalAPIs.so \
    ../custom_components/vtech_baby/native/x86_64/libtk.so build/x86_64/patched
```

For musl, load `build/x86_64/patched` inside `localhost/home-assistant:2026.9.3-amd64`. The
`ghcr.io/home-assistant/home-assistant:2026.9.3` image on this machine is the arm64 variant.

Offline tests: `../.venv/bin/pytest` from this directory (its `pytest.ini` turns off Home Assistant's
plugins, which block sockets).

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

## Known divergences from the stock library

On paths that never run, where the kit allows any behaviour that keeps state intact:

- `TUTK_Json_Obj_Get_Sub_Obj_String` returns `TJSON_ER_WRONG_TYPE` for a member that is not a string;
  the stock library returns json-c's text form of it.
- `TUTK3rdBase64Decode` rejects bad characters and misplaced padding with `TUTK_ER_INVALID_ARG`; the
  stock library returns whatever OpenSSL's base64 BIO decodes.
