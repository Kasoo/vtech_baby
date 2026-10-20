# tutk-sdk: working notes

A replacement for the Kalay SDK 4.3.8.1 libraries, written from the TUTK kit
(`$CAMERA_DIR/tutk-kit/`). Work in progress: not wired into the integration, and not documented
for release until it covers the whole SDK. `libTUTKGlobalAPIs.so` and `libIOTCAPIs.so` are done;
`libAVAPIs.so` is written and live-tested (2026-10-20, uncommitted).
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
- **Import only the libc the stock libraries import.** The patcher and companion are built and
  audited for exactly the libc surface the stock SDK libraries use: they rename and adapt the
  ABI-divergent imports (pthread mutexes and threads go through the companion's side table) and pass
  the rest straight to the host libc. A libc function the stock libraries never import gets no such
  review. It may bypass the side table, as `pthread_cond_*` did (a condition-variable wait never
  released a companion-managed mutex), or differ between bionic and musl in a struct layout, a
  constant or a data symbol. Before using a new libc function, check that a stock library imports
  it (`llvm-nm -D --undefined-only` on the stock set) and that it means the same in each libc; if
  none does, find another way or raise it with the companion first. After each build, compare our
  libraries' undefined libc symbols against the stock ones.
  As of 2026-10-19 the only libc imports outside the stock set are `nanosleep` (Global) and
  `pthread_mutexattr_destroy` (Global, IOTC). Both look harmless (identical `timespec` on 64-bit;
  musl's `pthread_mutexattr_destroy` is a no-op), but they break the rule and should be removed. The
  OpenSSL names beyond the stock set are Global's own calls to the host libssl, which is expected.
- **Unimplemented symbols abort** with `unimplemented: <name>` on stderr.
- **Live probes** may connect and stream (audio, video, decryption). Since 2026-10-20 they may also
  run talk-back with silence (`probe_talk.py --silent`) and briefly switch the LEDs and temperature
  thresholds when every value is restored (`probe_control.py --allow-writes`). Audible talk-back
  (`probe_talk.py --owner-present`) still needs someone at the camera.

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
  PATCHED=$REPO/tutk-sdk/build/x86_64-debug/patched ./run.sh probe_phase1.py 10 1
```

A library built with `make DEBUG=1` (into `build/x86_64-debug`) logs each connection, task, crypto and address call to stderr when
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

## AV status (2026-10-20, uncommitted)

All 14 exports are written, including the talk-back server. All three of our libraries pass, in the
HA image on the NAS (musl), against the camera:
- `probe_phase1`;
- `probe_stream`: snapshots, H.264/AAC, go2rtc RTSP and Opus, no bad frames;
- `probe_sps`: 10 fps, a keyframe every 2 s;
- `probe_restart`: 25 cycles, no bad frames;
- `probe_stall`: none, 731, 790+731 and burst, settings unchanged;
- `probe_talk --silent`: 300 frames, all acknowledged by the camera, 3.3 s per 150 frames as stock,
  camera audio paused while talking and resumed afterwards.
- `probe_control --allow-writes`: the night-light and status-LED switches read back and push 0x7ac,
  and a threshold change pushes 0x738 TOO_HOT (4 s) then IN_RANGE (6 s). Every value is restored.
  Stock gives the same pushes and timings.

Differential runs against stock AV:
- GET latency is the same on the stall probe: median 0.04-0.05 s, max 0.24 s stock and 0.19 s ours.
- `probe_restart` (it now counts lost frames): stock lost 0 frames in 40 cleaned restarts and ours
  lost 19. The cause was not wire loss. The camera also sends frame kind 8 (a P-frame like 7, but
  covered by FEC; kit 3eb8dcf), whose fragments before the
  last can be shorter than 1024 bytes (1021 seen). We rejected those, NAKed them, and rejected the
  camera's resend (same media packet number) too. Fragments are now placed by their actual length.
  Since then, 50 cycles: 0 lost, 0 bad, every cleaned cycle starts on a keyframe, matching stock.
- The ACK now carries the NAK list (count at +0x0e, offsets from +0x16), repeated every 50 ms while a
  packet is missing. Frames are released in frame order: an incomplete frame holds later ones for up to
  500 ms while its resend arrives, then counts as lost.

How it was found, and what is left:
- **Talk-back on LAN is plain IOTC, not DTLS.** The camera opens the channel with a DTLS ClientHello
  (IOTC datatype 1), and stock AV answers in plain data with `recv dtls data!`. The camera then logs
  in plainly (sub-types 0x00/0x20, empty account and password), the server answers 0x21 and sends an
  internal hello (0x10/0x40, payload `00 01 00 00`), and audio flows as kind-3 media. Packet and frame
  numbers start at 0, the FEC group is pn/20 and the index pn%20, and the checksum is a byte sum
  (kit `80-fec.md`; checked on 40 captured packets). We send no parity packets.
- **Our IOTC under stock AV fails talk-back** (`avServStart3` -20011). Stock AV's server read thread
  stops after `IOTC_Session_Check_Ex(0xa0)`. `Check_Ex` now fills the size-prefixed St_SInfoEx (size
  kept, mode at +4; kit cad6715), but -20011 remains, so the cause is elsewhere and still open. +0x54 is
  the camera's long LAN reply +0x30, as stock (0 for this camera; kit cdb7fc7). Our AV does not call
  `Check_Ex`, so all-ours works.
- Debug taps (`TK_TRACE=2`, debug builds in `build/x86_64-debug`): `tk-ssl` (DTLS plaintext),
  `tk-iotc` (channel data in and out), `tk-udp` (every datagram; `build/iotc_decode` unscrambles the
  lines), `tk-iotc-drop` (packets IOTC drops). `TK_TRACE=2` slows the process enough to stretch
  talk-back timing, so use it for captures only.
- AV waits by sleeping 5 ms, not on condition variables (the patcher leaves `pthread_cond_*` going
  straight to libc, which never released a companion-managed mutex: the first live hang).
- Offline tests: `tests/test_av_peer.py` runs our AV against a fake camera (`tests/av_peer.py`) over
  a stand-in IOTC (`tests/fake_iotc.c`). It covers login, IOCtrl both ways, retransmit, media assembly
  and loss, ACK/report cadence, the reset, close, and talk-back. `tests/test_surface.py` checks the
  libc-imports rule.
- Open with the kit agent: the `Check_Ex` layout; the server-side 0x21 fields; the login deadline
  operand; `avClientCleanBuf` (exported by us as clean audio + clean video); FEC; report +0x0a;
  multi-fragment control frames; -20009.
- Not done: FEC (parity) recovery on receive, parity on send, the stats block in media packets. None
  is needed on LAN so far. Also open: our IOTC under stock AV for talk-back (above).
- Next: commit; then archive this build and fork it without the Android build and the
  `libtk` companion (a host-built library loses drop-in testing against stock). Captures in the camera repo's `av-tap/` contain credentials and stay there.
