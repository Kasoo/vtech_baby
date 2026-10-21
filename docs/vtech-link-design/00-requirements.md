# 00 — Requirements

Everything `vtech-link` must do, derived from what the integration does today. The sources are
`vtech_lan/session.py`, `media.py`, `talkback.py`, `worker.py`, `manager.py`, the entity map in
`AGENTS.md`, the live probes in `tests/live/`, and the protocol kit. Each requirement has an ID that the
design documents cite. "Today" means `tutk-host` at `cf561da` plus `vtech_lan`.

## Connect and lifetime

| ID | Requirement | Source today |
|---|---|---|
| R1 | Connect to a camera by **UID** (20 chars, case-insensitive; sent uppercased) and **authKey** (8 alphanumeric chars) over the local L2 segment, by subnet broadcast discovery on UDP **32762**. The port is configurable; 32762 is the default. | `sdk.py` `connect`, `LAN_SEARCH_PORT`; `tutk-host/iotc/session.c:162` |
| R2 | Authenticate the AV channel with **account + password** (account `admin`; the password is the AV password from the QR) over DTLS-PSK on IOTC channel 0. | `sdk.py` `client_start` (`security_mode=1`, `auth_type=0`) |
| R3 | **Connect timeout**: discovery and IOTC connect give up after a caller-given time (default 15 s), and the AV login after its own (default 30 s). | `Session(connect_timeout_s, av_timeout_s)` |
| R4 | **Classify connect failures** so the integration can react: unreachable (no answer); IOTC authKey rejected (`0x602` result 5); device not using key auth (result 6, or a short-form reply); camera full (result 3); rejected by port (result 4); AV credentials rejected (login state 3, or a DTLS PSK failure); local resource failure. | `errors.py` `_CLASSES`; `tutk-host/iotc/inbound.c:50` |
| R5 | **LAN-direct only.** Send only to directly attached IPv4 subnets (host or directed broadcast) or 255.255.255.255. Never resolve names, never contact a server, never fall back to P2P or relay. Count every refused send. | `tutk-host/global/egress.c`; `NotLanDirect` |
| R6 | **Several links at once**: several cameras, and more than one link to the same camera (HA plus a probe; HA during a reconnect). At least 16 per process. | `90-ha-runtime.md` §2; `load(max_channels=16)` |
| R7 | **Detect a dead link** and report why, exactly once: the camera closed it (`0x0418`), 60 s with no inbound traffic, a fatal DTLS alert, or a local close. | `iotc/init.c:89`; `Session._lose` |
| R8 | **Close cleanly**: stop sending, send the IOTC close three times, free everything. Closing is idempotent and safe from any thread. | `iotc/session.c:231`; `Session.close` |
| R9 | **Keep the link alive** at the IOTC level (keepalive datagrams) and the AV level (ACKs, reports, RTT answers) with no caller involvement. | `iotc/init.c:89`; `av/transport.c:164` |

## Control (IOCtrl)

| ID | Requirement | Source today |
|---|---|---|
| R10 | **Send an IOCtrl** (opcode ≥ `0x100`, payload ≤ 1020 bytes) reliably and in order; report success once the camera acknowledges it, and failure if the link dies first. | `av/ioctrl.c:181` |
| R11 | **Concurrent senders** on one link must not fail with "busy". Today the SDK refuses a second concurrent `avSendIOCtrl` (`-20021`), and `Session` serialises and retries around that. | `session.py` `_send_raw`, `SEND_BUSY` |
| R12 | **Request/reply**: send, then wait for a named reply opcode (listed per op, never computed) with a timeout. One outstanding request per reply opcode; others for the same reply opcode queue behind it. | `session.py` `_transact` |
| R13 | **Multi-reply exchanges**: keep collecting replies on one reply opcode until the caller says the exchange is done (the event-list pages). | `session.py` `list_events` |
| R14 | **Pushes**: every inbound IOCtrl that no waiter claims goes to the push stream, in arrival order, with its opcode and payload. | `session.py` `_dispatch` |
| R15 | **No `0x0ff`**: `IOTYPE_INNER_SND_DATA_DELAY` is never sent. The stock SDK refuses it from application code and nothing needs it. | `20-ioctrl-framework.md`; `session.py` `_send_data_delay` |
| R16 | **Inbound control frames** are taken in order, duplicates dropped, and acknowledged through the ACK stream. Multi-fragment inbound frames, which the kit describes but we have not seen, are reassembled rather than dropped. | `av/ioctrl.c:100` |

## Media

| ID | Requirement | Source today |
|---|---|---|
| R17 | **Live-view key exchange** (`0x820` round 2 → `0x821`) once per link, on demand; the master key stays inside the library. | `crypto.py` `KeyExchange`; `Session.live_view_master` |
| R18 | **Receive video and audio frames**, decrypt them (ChaCha20-Poly1305 under the per-frame key), and deliver video and audio as two ordered streams, each frame with its kind, frame number and arrival time. | `media.py` `_stream`; `crypto.py` `decrypt_frame` |
| R19 | **Reset the stream** before each start, so packets in flight from the previous run are dropped and the first delivered video frame is the next keyframe (the `0x24`/`0x25` reset handshake plus dropping queued frames). | `av/media_recv.c:303` `clean`; `av/ioctrl.c:156` |
| R20 | **Recover losses**: name missing media packets in the ACK so the camera resends them; rebuild a group's single missing packet from the camera's parity; give up on a frame after a bounded wait and report it. | `av/media_recv.c`, `av/fec.c` |
| R21 | **Bounded buffering**: at most 60 video and 200 audio complete frames queued; frames at most 2 MiB; the oldest dropped and counted when a reader falls behind. | `av/media_recv.c:17–19` |
| R22 | **A blocking read with a timeout** for the next frame, with the GIL released while waiting. No polling. | `media.py` (polls every 5 ms today) |
| R23 | **Receive counters**: resend requests, frames given up/late/overflowed per stream, packets rebuilt from parity, frames that failed to decrypt. | `tk_av_recv_stats`; `media.py` `st["bad"]` |
| R24 | **Kinds**: kind 3 (and 4) is audio; 5, 6, 7 and 8 are video. Kind 8 is delivered exactly as 7. | `tutk-kit/docs/av/90-fifos.md` (FEC envelope) |

## Talk-back

| ID | Requirement | Source today |
|---|---|---|
| R25 | **Open a talk-back channel**: pick a free IOTC channel (1–31), let the caller announce it to the camera (`0x350`), then accept the camera's connection and plain login on that channel within a timeout (default 10 s). | `talkback.py` `_run`; `av/talkback.c:72` |
| R26 | **Send audio**: take 160-byte A-law frames (20 ms), encrypt each under the master (fresh frame key and nonce), frame them as audio media with a 16-byte frame info, and send at real-time pace. Pace in the library, not in Python. | `talkback.py` `_send`; `av/media_send.c` |
| R27 | **Close the talk-back channel** cleanly and free it, whatever state it is in; the link stays up. | `talkback.py` `_teardown` |
| R28 | **Back-pressure**: a bounded input queue (default 10 frames, 200 ms). A caller that runs ahead blocks; one that falls behind gets the pacing clock reset rather than a burst. | `talkback.py` `MAX_LAG_S` |

## Diagnostics

| ID | Requirement | Source today |
|---|---|---|
| R29 | **Link info**: the camera's address and port, its IOTC and AV protocol versions, the negotiated DTLS suite, the connection mode (always LAN), when the link came up, and the close reason once closed. | `Session.connection_mode`, `sdk_info` |
| R30 | **LAN-only counters**: refused sends and refused name lookups (the latter always 0, since nothing resolves names). | `tk_lan_only_stats` |
| R31 | **Transport counters**: datagrams and bytes in and out, keepalives, retransmitted control frames, the smoothed RTT, control-frame acknowledgement latency (for the post-connect stall, `AGENTS.md` "Known issues"). | new |
| R32 | **Logs reach Python logging** without Rust threads calling into Python. | `logging` in `vtech_lan` |
| R33 | **A packet trace**, off by default and enabled per link, recording the decoded plaintext datagrams (IOTC, DTLS plaintext and AV) for differential tests and debugging. It never records keys. | `TK_TRACE=2` taps in debug builds |

## Platform and safety

| ID | Requirement | Source today |
|---|---|---|
| R34 | **A native fault must not take Home Assistant down.** Memory-safe code; no `unsafe` in protocol code; no panic crosses into Python; every buffer is bounded by a protocol limit; a link whose thread fails is reported lost and leaves the process healthy. | `90-ha-runtime.md` §6; the worker exists for this |
| R35 | **Four builds**: x86_64 and aarch64, each for glibc (glibc 2.17 floor) and musl (Home Assistant's Alpine image). Python ≥ 3.13 (HA's floor), one abi3 module per build. | `tutk-host/release.sh` |
| R36 | **No host libraries** beyond libc (and the unwinder, see `80-build.md`). In particular no OpenSSL: today Global links the host's `libssl.so.3`. | `tutk-host/lib.mk` |
| R37 | **Nothing blocks the event loop.** Every call that can wait releases the GIL and takes a timeout. No Python callbacks are called from Rust threads. | `90-ha-runtime.md` §3 |
| R38 | **Deterministic tests**: the protocol runs against an injected clock and randomness, so whole connects, losses and timeouts replay exactly. | new |
| R39 | **Cheap when idle**: an idle link (no media) uses no CPU beyond its timers (about 10 wakeups a second); a streaming link costs less CPU than today's Python decrypt plus 5 ms polling. | new |
| R40 | **Advertise a recognisable SDK version** in the probe, the connect record and the connect ack (today `4.3.8.100`, `common/tk_common.h`) that still sorts above stock 4.3.8.1. This design uses `4.3.8.200` (`0x040308c8`). | `TK_SDK_VERSION` |

## Non-goals

The library does **not** do these, and the design spends nothing on them:

- **The TUTK API.** No `IOTC_*`/`av*` exports, session ids, AV indexes, struct layouts, error-code
  space or ABI compatibility. Nothing outside this integration links against it.
- **Anything off the LAN.** No master servers, P2P punching, NAT detection, relay, TCP, the
  AES/ECDH handshake (`tutk-kit/docs/iotc/16-handshake.md`, last section), Nebula, or a licence
  check. The licence key's only effect on the wire is the scramble key, and for our licence that is the
  bare constant (`20-iotc.md`).
- **The device role.** We never listen for clients, except that the talk-back channel is an AV server
  inside our own link.
- **The old AV protocol** (bare class 3–8 media, bare class 0 `0x10`/`0x70` control). The camera uses
  the new protocol.
- **BBR, DASA, adaptive bitrate.** The camera paces its own sending. Our only outbound media is
  8 kB/s of talk-back audio.
- **Encoding parity.** We never send parity, and we never resend talk-back audio.
- **IPv6.** The camera is IPv4-only on the LAN.
- **The opcode table.** It stays in Python (`vtech_lan/ops.py`).
- **Media decoding and muxing** (PyAV) and **talk-back media decoding**. These stay in Python.
- **Provisioning from the QR** (`provision.py`). It stays in Python. It runs once per setup and uses
  `cryptography`, which Home Assistant already ships.

## Quality targets

- **Correctness:** the live probes pass as they do on `tutk-host` today: `probe_phase1` (2 × 70 s),
  `probe_stream` (0 bad frames), `probe_restart` (25 cycles, 0 lost), `probe_talk --silent`,
  `probe_control --allow-writes`, `probe_stall`. They must also pass with 1-in-50 induced loss.
- **Soak:** two weeks in the production HA with no lost-session reconnects beyond what `tutk-host`
  shows over the same period.
- **Latency:** connect in under 1 s when the camera is reachable. Frame delivery adds under 1 ms
  over network arrival, against up to 5 ms of polling plus a Python decrypt today.
- **Resources:** one OS thread per link, plus one per talk-back channel only if `70-runtime.md`
  calls for it (it does not). Per-link memory is bounded by the queue limits (R21): about 6 MiB worst
  case with 60 frames of 1080p video queued.
