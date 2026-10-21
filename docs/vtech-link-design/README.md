# vtech-link: detailed design

`vtech-link` is a planned replacement for the three TUTK SDK libraries, written in Rust and loaded
into Home Assistant as a Python extension module (PyO3). It talks to the VTech RM5754HD exactly as the
SDK does on the wire, but everything behind the wire is redesigned for the one case we need: one
client, LAN-direct, talking to a known camera.

Today the integration ships `tutk-host/`, a C reimplementation that mirrors the SDK's internal
structure: its exports, structs, FIFOs, threads, task manager and OpenSSL forwarders. That mirror was
the right way to replace the vendor libraries one at a time. It is the wrong shape for what we run now,
which is a single protocol path with no consumer outside this integration.

These documents are the whole design, written before any code. They enumerate what the library must
do, how each layer works down to the byte, and how we get from the current code to this one.

## Status

Design only (2026-10-21). Nothing in this directory is implemented. The current production stack is
`tutk-host/` at `cf561da` (shipped in `custom_components/vtech_baby/native/<arch>-<libc>/`), driven by
`vtech_lan/` through ctypes inside the worker process.

## Documents

Read in order.

| Doc | Contents |
|---|---|
| [`00-requirements.md`](00-requirements.md) | Everything the library must do, with IDs (R1–R40) the other documents cite; non-goals; quality targets. |
| [`05-current-setup.md`](05-current-setup.md) | Analysis of today's stack (C mirror + ctypes + worker): what it carries that we don't need, and its costs. |
| [`10-architecture.md`](10-architecture.md) | Layers, crates, the one-thread-per-link reactor, data flow, ownership, what moves from Python to Rust. |
| [`20-iotc.md`](20-iotc.md) | The IOTC layer: datagram codec, scramble, discovery and connect, data channels, keepalive, close. |
| [`30-dtls.md`](30-dtls.md) | A minimal DTLS 1.2 client for the one cipher suite the camera negotiates (`ECDHE_PSK_WITH_CHACHA20_POLY1305_SHA256`). |
| [`40-av-client.md`](40-av-client.md) | The AV client channel: login, the reliable control stream, ACK/NAK, reports and RTT. |
| [`45-media-receive.md`](45-media-receive.md) | Media fragments, the packet window, frame assembly, parity recovery, the reset handshake, counters. |
| [`50-talkback.md`](50-talkback.md) | The talk-back server channel: the plain login, media send and pacing. |
| [`60-application.md`](60-application.md) | The camera services built on the AV channel: request/reply matching, pushes, the live-view key exchange, frame crypto. |
| [`70-runtime.md`](70-runtime.md) | Threads, the event loop, timers, sockets, the LAN-only rule, queues and limits, errors, logging, panics. |
| [`75-python-api.md`](75-python-api.md) | The PyO3 module's API, and the changes to `vtech_lan/` and the integration. |
| [`80-build.md`](80-build.md) | Crates, dependencies, the four shipped builds, loading, the import check. |
| [`85-testing.md`](85-testing.md) | Unit, simulation, interop, differential, fuzz, live and soak testing. |
| [`90-migration.md`](90-migration.md) | Phases from today's stack to this one, with exit criteria and rollback. |
| [`95-open-questions.md`](95-open-questions.md) | What is still unknown, how each item gets settled, and the risks. |

## Conventions

- **Wire facts come from the kits.** The camera protocol above the SDK is in
  `docs/vtech-lan-kit/docs/` (this repo). The SDK's behaviour is in the TUTK kit,
  `$CAMERA_DIR/tutk-kit/docs/` (the camera repo; cited as `tutk-kit/…`). This design restates
  wire layouts where an implementer needs them in one place, and cites the kit for the source.
- **Code references** are to this repo at `cf561da`, as `path:line` or `path` + function name, e.g.
  `tutk-host/iotc/inbound.c:67` (`handle_lan_reply`). The C in `tutk-host/` is live-proven against the
  camera, so where it and the kit differ, this design follows the C and says so.
- **Requirement IDs** (`R12`) are defined in `00-requirements.md`. Every design section names the
  requirements it serves.
- **Names.** The Rust workspace is `vtech-link`. The Python extension module is `vtech_link`. Inside
  it, a *link* is one authenticated connection to one camera (an IOTC session plus its AV channels).
  `vtech_lan` (the existing pure-Python package) keeps the opcode table and everything above it.
- **Byte order** is little-endian unless a field says otherwise. Offsets are hex from the start of the
  structure named.
- **Live facts recorded here for the first time** carry the command that produced them (the DTLS
  handshake capture in `30-dtls.md`). They have also been added to the kit.

## Decisions at a glance

1. **Rust, memory-safe, no C dependencies.** `#![forbid(unsafe_code)]` in every protocol crate. No
   OpenSSL: DTLS is our own client for one cipher suite, on RustCrypto primitives (`30-dtls.md`).
2. **Sans-IO protocol core.** Every layer is a state machine that takes datagrams, commands and the
   time, and returns datagrams, events and its next deadline. The same code runs on the reactor
   thread, in a deterministic simulator and under fuzzers (`10-architecture.md`).
3. **One reactor thread per link** owns the link's socket and all its protocol state, so the protocol
   needs no locks. Callers talk to it through command channels and bounded queues (`70-runtime.md`).
4. **Higher-level API than the SDK.** The Python side gets `Link.request()`, a push stream, decrypted
   media frames and a paced talk-back sender. There are no session ids, AV indexes, buffer cleans or
   polling loops (`75-python-api.md`).
5. **Rust owns all the crypto**: the scramble, DTLS, the live-view key exchange and per-frame
   ChaCha20-Poly1305. Python never holds a key (`60-application.md`).
6. **Python keeps the meaning**: the opcode table (`ops.py`), the Home Assistant entities, PyAV
   muxing and media decoding. These are already well tested and are not a crash risk the way native
   code is.
7. **In-process in the end.** We first run inside the existing worker process, for an A/B comparison
   with `tutk-host`. Once that is proven, the worker, IPC, patcher, ctypes layer and the C libraries
   are retired (`90-migration.md`).
