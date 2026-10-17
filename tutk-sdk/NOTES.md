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
