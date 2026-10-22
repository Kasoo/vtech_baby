# 85 — Testing and verification

Serves R38 and every functional requirement. Reference: `tutk-host/tests/` (`av_peer.py`,
`fake_iotc.c`, the offline suites), `tests/live/` and its probes, `$CAMERA_DIR/shim/tests/live_run.sh`.

The levels below run from cheapest to most realistic. Every level except live and soak runs in CI.

## T1 — Unit and known-answer tests (`cargo test`, per module)

| ID | What | Vector source |
|---|---|---|
| T-IOTC-1 | Scramble one block | the kit's worked example (`tutk-kit/docs/iotc/15-wire-format.md`: plaintext `04 03 02 01 …`, key `44 33 22 11 …`, output `c3 27 b9 f4 …`) |
| T-IOTC-2 | Scramble/unscramble round trip for every length 0–2048 and both prefixes; tails of 2/4/8 bytes | proptest |
| T-IOTC-3 | Our key equals the licence-derived key | a one-time fixture from `tutk-host` `iotc_build_key` output |
| T-IOTC-4 | Probe, record (with HMAC), connect ack, data, keepalive and close are byte-identical to `tutk-host` for the same UID, authKey, token and version | fixtures generated once from `tutk-host` (a small C harness linking `iotc/wire.c` and `handshake.c`, as `tests/wire_check.c` does) |
| T-IOTC-5 | `0x602` parse: long and short forms, every result code, truncated datagrams | hand-built, plus the trace's real reply |
| T-DTLS-1 | P_SHA256 PRF | RFC 5246 test vector |
| T-DTLS-2 | Key schedule (EMS and non-EMS), record nonce and AAD | computed independently in Python with `cryptography` (a script in `tests/vectors/`) |
| T-DTLS-3 | ClientHello bytes; parsing the camera's real server flight (the 2026-10-21 trace) | the trace, `30-dtls.md` |
| T-AV-1 | Login packets byte-identical to `tutk-host` `build_login` for a fixed nonce | fixture |
| T-AV-2 | ACK/report/RTT/control-frame encodings | fixtures from `tutk-host` |
| T-MEDIA-1 | Window: contiguous, gaps, gap skip at 300 ms, NAK list contents and the 50 ms repeat, serial wrap at 0xffff | ported from `av_peer.py` scenarios |
| T-MEDIA-2 | Assembly: variable `plen`, out-of-order fragments, the frame-info trailer, give-up at 500 ms, overflow, the late count | ported |
| T-FEC-1 | Rebuild for k = 20 and k = 25, any missing index, all kinds; 2 missing → nothing | ported from `av_peer.py` `s_parity`, plus 10 real groups from a trace |
| T-APP-1 | Live-view master and frame decrypt/encrypt | `crypto.py` round trip vectors + a recorded `0x821` and frame (no real keys in the repo: synthetic master) |

## T2 — Simulation (`vtech-link-sim`)

A **simulated camera**, built on the same codecs, that speaks the camera's side: it answers discovery,
runs a DTLS server for `0xCCAC` (a sans-IO server, test-only), handles the AV login, ACKs control
frames, replies to IOCtrls from a script, emits pushes, and streams synthetic H.264 and A-law media
encrypted with a known master, including parity and resends for NAKed packets. It also accepts a
talk-back connection.

The simulator and the link run in **one thread with a virtual clock and an in-memory network**:
`Network { delay, jitter, loss, duplicate, reorder }` per direction, seeded. No sockets, no sleeps:
a 60-second scenario runs in milliseconds and replays exactly (R38).

Scenarios (each also runs with 2% loss, 1% duplication and 5 ms of jitter-induced reordering):

| ID | Scenario | Pass |
|---|---|---|
| S1 | Connect, DEVINFO, 100 requests, close | every reply matched; 3 close datagrams |
| S2 | Each connect failure in R4 | the right `LinkError` within its deadline |
| S3 | Wrong AV password | `AvAuthFailed(DtlsAlert(51))` (the simulated server sends `decrypt_error`) |
| S4 | 5 minutes of 1080p at 12 fps with audio | 0 frames lost at 0% loss; at 2% loss all recovered by NAK or parity |
| S5 | `probe_restart` equivalent: 25 × (reset, START, 5 s, STOP) | first frame after each reset is a keyframe; no stale frame |
| S6 | The camera goes silent | `Closed(RemoteTimeout)` after 61 s of virtual time |
| S7 | The camera sends `0x418` | `Closed(RemoteClose)` |
| S8 | Talk-back 30 s | 1,500 frames in 30 s ± 20 ms; the decrypted payload matches |
| S9 | 16 concurrent links (simulated cameras) | independent; one panicking (an injected fault) leaves the others up |
| S10 | Concurrent requests: 8 threads × 50 requests, mixed reply opcodes | no "busy", one user frame in flight, replies matched |
| S11 | A multi-page `0x788` exchange | pages in order; the exchange ends on `end` |
| S12 | DTLS flight loss: drop each flight once | the handshake completes through retransmission |

## T3 — Interoperability

- **DTLS against OpenSSL**: `openssl s_server -dtls1_2 -listen -psk <hex sha256(pwd)> -psk_identity
  AUTHPWD_admin -cipher ECDHE-PSK-CHACHA20-POLY1305 -groups X25519` on loopback UDP, with a test
  adapter that carries our records straight over UDP (no IOTC). It runs with and without
  `-no_ticket`, and with `-cookie_exchange` to exercise HelloVerifyRequest. Echo application data both
  ways. Dev only, gated on `openssl` being on the PATH.
- **Python ↔ Rust frame crypto**: `crypto.py` (kept in tests only) decrypts what Rust encrypts and
  the other way round.

## T4 — Differential testing against `tutk-host`

Both stacks are driven by the same probe script against the same peer, and their behaviour compared:

- **AV layer, offline**: `tutk-host/tests/av_peer.py`'s fake camera speaks plain AV over a stand-in
  IOTC. A small adapter runs `vtech-link-core`'s `AvClient` against the same scripted peer, and the
  ACK sequences, NAK lists, delivered frames and counters are compared scenario by scenario.
- **Live, trace-level**: run `probe_stream` once with `tutk-host`'s debug build (`TK_TRACE=2`) and
  once with `vtech-link` (`trace=`). Then compare, by a script, the decoded AV packet streams: the
  same classes and cadence (ACK ~110 ms, report ~200 ms, RTT per report), control frames
  acknowledged, and the same frames delivered.

## T5 — Fuzzing (`vtech-link/fuzz/`)

One `cargo-fuzz` target per parser that sees network input: IOTC datagram (unscramble + header +
`0x602` + data), DTLS record layer, DTLS handshake messages, AV packet dispatch, media fragment +
assembly (a sequence of fragments), FEC (sequences of shards), control frame and reassembly, and the
talk-back login. Each target asserts no panic and bounded allocation (with a tracking allocator). CI
runs 60 s per target; before each release, 1 hour per target.

## T6 — Python integration tests (`pytest`)

The existing 234 tests run against `tests/fake_link.py` (`75-python-api.md`). New tests cover:
`from_link` error mapping; `LinkSession` request/push/lost paths; `media.py` with a fake
`MediaReceiver` (keyframe join, `lost_before` accounting, stats); `talkback.py` with a fake `TalkBack`
(stop mid-stream, drain vs drop); and `link.py` loading (a missing module, the wrong directory).

A **smoke test with the real module** (`tests/test_link_native.py`, skipped unless a build is present)
runs the module against the simulator through a test-only `_vtech_link._sim_connect()` hook. That hook
exists only in debug builds, behind a cargo feature.

## T7 — Live (`vtech-link-cli` and `tests/live/`)

- **The CLI** (`vtech-link probe …`) runs against the real camera with no Python. It reads credentials
  from `VTECH_UID`/`VTECH_AUTHKEY`/`VTECH_PWD` as the probes do, and never from arguments or files in
  the repo. Subcommands:
  - `connect` (read-only): connect, DEVINFO, close; prints `info()`.
  - `watch` (read-only): pushes for N seconds.
  - `stream` (read-only): N seconds of media; counters; optional `--out file.h264`.
  - `restart` (read-only): reset/START/STOP cycles.
  - `talk --silent`: A-law silence; still opens the speaker path, so it needs the owner's OK.
  - `--drop N` (debug builds only): drop every Nth inbound media or parity packet, like `TK_DROP`.
- **The live probes** (`tests/live/probe_*.py`) gain a `BACKEND=link` switch. The runner script already
  ships a library directory into the HA image (`PATCHED=`). It ships the module the same way.
- The camera is a live baby monitor: everything that makes light or sound needs the owner present
  (`AGENTS.md` "Rules"). `connect`, `watch`, `stream` and `restart` are read-only and safe headless.

### First live runs (2026-10-22, read-only)

The whole Rust stack, static musl `vtech-link` built on the workstation, run on the NAS against the
camera while HA's own session and the parent unit were up:

```sh
cd vtech_baby/vtech-link && cargo build --release --locked --target x86_64-unknown-linux-musl -p vtech-link-cli
scp target/x86_64-unknown-linux-musl/release/vtech-link $HOST:/tmp/
. $CAMERA_DIR/.camera.env
ssh $HOST "export VTECH_UID=… VTECH_AUTHKEY=… VTECH_PWD=…; /tmp/vtech-link stream 192.0.2.10 20"
```

| Run | Result |
|---|---|
| `connect` ×4 | Up in 84–108 ms with suite `0xccac` and partial encryption; DEVINFO `RM5754`/`vtech` in 110–170 ms; close at once. |
| `stream 20` | Key exchange 19 ms; reset confirmed; first frame after 1 ms, first keyframe after 1.3 s; 189 video frames (9.4/s, 10 keyframes), 502 audio, 1.1 Mbit/s; **0 lost, 0 decrypt failures, 0 retransmits, 0 NAKs**; RTT 23 ms; 7 of 8 control frames ACKed within 50 ms. |

Found by the first run and fixed: the reactor sent its first search round only after its first wait (a
100 ms search interval), so the IOTC connect took 114 ms instead of about 10 ms.

Open: DEVINFO takes 110–170 ms. Compare with `tutk-host` on the same request before reading anything
into it.

## T8 — Soak

The production HA runs the new backend for two weeks (`90-migration.md`, phase 4). Daily: the HA log for
`vtech_baby`/`link` warnings, the reconnect count, `lan_only_stats`, the receive counters and the
ack-latency histogram from diagnostics. The exit criterion is in `00-requirements.md` ("Soak").
