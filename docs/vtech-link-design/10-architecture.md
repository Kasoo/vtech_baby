# 10 — Architecture

## The split: a pure Rust core and a thin Python wrapper

```
vtech-link/                         Cargo workspace (repo root: vtech_baby/vtech-link/)
  core/      crate vtech-link-core  pure Rust; no PyO3, no Python. The whole protocol and runtime.
  py/        crate vtech-link-py    PyO3 cdylib `_vtech_link`: type conversion, GIL release, errors.
  cli/       crate vtech-link-cli   `vtech-link` binary: live probes and packet tracing, no Python.
  sim/       crate vtech-link-sim   (dev only) a simulated camera for tests, on the core's sans-IO API.
  fuzz/      cargo-fuzz targets     (dev only) one per parser.
```

**`vtech-link-core`** holds everything: wire codecs, the scramble, DTLS, the IOTC/AV/media/talk-back
state machines, application crypto, the reactor thread, sockets and the LAN-only rule. Its public
Rust API (`Link`, `MediaReceiver`, `TalkBack`, `LinkError`, …) is what the Python wrapper and the CLI
both call. It never imports anything Python, so it can be:

- run by the CLI against the real camera, with no HA image and no interpreter (`85-testing.md`);
- driven by the simulator and the fuzzers in plain `cargo test`;
- reviewed and fuzzed as ordinary safe Rust: `#![forbid(unsafe_code)]` at the crate root.

**`vtech-link-py`** is the only crate that knows about Python. It is deliberately thin, under about 400
lines:

- it converts `bytes`/`str`/`int` to and from Rust types, and `LinkError` into the Python exception
  classes (`75-python-api.md`);
- it wraps every blocking core call in `py.allow_threads` (R37);
- it holds no protocol logic, no threads and no state beyond `Arc` handles to core objects.

This crate needs `unsafe` (PyO3's macros generate it). It is the only crate that does, and it contains
nothing that parses network input.

**Why not a pure-Python core?** All of this could be written in Python. It would be memory-safe and
easy to change, but it would put a per-packet hot path (about 1,000 datagrams a second while
streaming, each unscrambled, DTLS-decrypted and AV-parsed, with 10 ms ACK timers) on the GIL in HA's
process. The timer accuracy and the CPU budget (R39) argue against it. The opcode table, which is not
hot, stays in Python.

## Layers inside the core

```
            Python (vtech_lan) ─────────────── Link API (blocking calls, bounded queues)
                                                   │ commands ▲ events / frames / replies
                                     ┌─────────────┴─────────────────────────────┐
  reactor thread "vtech-link-<uid>"  │ LinkDriver: mio Poll, UdpSocket, Waker,     │  70-runtime.md
                                     │ timers, egress rule, trace                  │
                                     ├────────────────────────────────────────────┤
                                     │ Services: request matching, pushes,         │  60-application.md
                                     │ key exchange, frame crypto                  │
                                     ├───────────────────────┬────────────────────┤
                                     │ AvClient (channel 0)  │ AvServer (ch 1–31) │  40, 45, 50
                                     │  ControlStream        │  (talk-back)       │
                                     │  MediaReceiver + Fec  │  MediaSender       │
                                     ├───────────────────────┤                    │
                                     │ DtlsClient            │  (plain channel)   │  30-dtls.md
                                     ├───────────────────────┴────────────────────┤
                                     │ IotcSession: discovery, connect, channels,  │  20-iotc.md
                                     │ keepalive, close; scramble codec            │
                                     └────────────────────────────────────────────┘
                                                   │ UDP datagrams
```

Each box below the driver is a **sans-IO state machine**: it holds protocol state, but does no I/O
and never reads the clock. It looks like this:

```rust
trait Machine {
    type In;   // a datagram or payload from below, or a command from above
    fn handle(&mut self, now: Instant, input: Self::In);
    fn poll_transmit(&mut self) -> Option<Transmit>;   // bytes for the layer below
    fn poll_event(&mut self) -> Option<Event>;         // things for the layer above
    fn poll_deadline(&self) -> Option<Instant>;        // when to call on_timeout
    fn on_timeout(&mut self, now: Instant);
}
```

The layers compose by plain method calls inside the driver: an inbound datagram goes
`IotcSession::handle` → `(channel, datatype, payload)` → `DtlsClient::handle` →
`AvClient::handle(packet)` → events such as `ControlMessage`, `FrameReady`, `Acked(frame)` and
`LinkLost(reason)`. Randomness comes from an injected `Rng` trait (OS `getrandom` in production,
seeded in tests), so a whole connect is deterministic under test (R38).

## Threads

- **One reactor thread per link** (`vtech-link-<uid>`). It owns the link's socket and every state
  machine in the link, including talk-back channels. Protocol state has no locks because only this
  thread touches it.
- **Caller threads** (Python threads, the CLI) never touch protocol state. They:
  - send `Command`s on the link's channel and wake the reactor (`mio::Waker`);
  - block on a per-request `Completion` (a mutex and condvar), with the GIL released;
  - block on the link's bounded output queues (pushes, video, audio), each its own mutex and condvar.
- **No other threads**: there is no global manager, no task thread and no per-channel threads. The
  library spawns a thread only in `Link::connect` and joins it in `Link::close` (or when the link dies
  and the last handle is dropped).

Why one thread per link and not one for all links: links are independent, there are at most a handful,
a stalled or failed link can't delay another, and panic containment is per link (`70-runtime.md`).
With 16 links the cost is 16 mostly idle threads.

## Data flows

**Connect** (`Link::connect(config, timeout)`): the caller's thread spawns the reactor, sends
`Connect`, and waits on its completion. The reactor runs discovery and the IOTC connect
(`20-iotc.md`), the DTLS handshake on channel 0 (`30-dtls.md`) and the AV login (`40-av-client.md`).
It then completes the caller with `Ok(info)` or a classified `LinkError` (R4), and on error tears
itself down.

**Request** (`link.request(op, payload, reply_op, timeout)`): the caller registers a reply waiter and
queues the frame. The reactor sends it on the control stream, which retransmits until the camera ACKs
it. When an inbound control message with `reply_op` arrives, the reactor completes the waiter. An
inbound message that no waiter claims goes to the push queue (R12–R14).

**Media** (`link.media()` → `MediaReceiver`): the reactor assembles frames and pushes complete
**encrypted** frames into the video or audio queue. The consumer's `next(timeout)` pops one and
decrypts it **on the consumer's thread** (GIL released), so the reactor never spends time on
ChaCha20 and decryption runs on as many threads as there are consumers (R18, R22, R39).

**Talk-back** (`link.talkback(timeout)` → `TalkBack`): the reactor opens an AV server on a free
channel, and the caller announces the channel to the camera (`0x350`) and waits for `accept`. The
caller's `send(alaw)` pushes plaintext into a bounded queue. The reactor encrypts and sends one frame
every 20 ms from its own timer, so timing does not depend on Python scheduling (R25–R28).

## What moves where

| Concern | Today | `vtech-link` |
|---|---|---|
| IOTC, DTLS, AV, media assembly, parity | C (`tutk-host`) | core |
| LAN-only egress | C (`global/egress.c`) | core (`70-runtime.md`) |
| Request/reply matching, push routing, send serialisation | Python `session.py` | core (`60-application.md`) |
| Live-view key exchange, frame decrypt/encrypt | Python `crypto.py` | core (`60-application.md`) |
| Talk-back pacing and framing | Python `talkback.py` | core (`50-talkback.md`) |
| Opcode table, encoders, decoders, acks | Python `ops.py` | **stays** |
| Typed helpers (`get_params`, `set_temp_alert`, read-modify-write locks) | Python `session.py` | **stays** (thin `Session`) |
| Application keepalive (`0x733` every 60 s, 3 misses) | Python `session.py` | **stays**: it also feeds params to entities |
| Live-view lifecycle, subscribers, grace period, snapshot | Python `media.py` | **stays**, simplified |
| MPEG-TS mux, JPEG snapshot, talk-back media decode | Python `avmux.py`, `audio.py` (PyAV) | **stays** |
| HTTP stream server | worker process | HA process (after the worker is retired) |
| Reconnect, backoff, stale credentials, entities | `manager.py` | **stays** |

## Ownership and lifetimes

- `Link` (core) is a cheap clonable handle: an `Arc<LinkShared>` holding the command sender, the
  waker, the output queues and the reactor's `JoinHandle`. `MediaReceiver` and `TalkBack` hold their
  own `Arc` to the same shared state plus their own queues.
- The reactor holds the only `LinkState`, the state machines.
- `Link::close()` sends `Close`, waits for the reactor to send its close datagrams and exit (bounded,
  1 s), and marks every queue closed so that blocked callers wake with `LinkError::Closed(Local)`.
  Dropping the last handle without `close()` does the same from `Drop`, without waiting.
- When the link dies on its own, the reactor records the reason, closes every queue and completes
  every waiter with that error. Then it exits. Later calls return the stored reason at once (R7).

## Error model

A single core enum, mapped one-to-one onto the integration's existing exception classes so `manager.py`
does not change (`75-python-api.md`):

```rust
pub enum LinkError {
    Unreachable,                        // no discovery reply, or it timed out (IOTC_ER_TIMEOUT today)
    IotcRejected(IotcReject),           // 0x602 result: MaxSessions(3), Port(4), AuthKey(5), NoKeyAuth(6), Other(u8)
    AvAuthFailed(AvAuthStage),          // DtlsAlert(u8) | LoginRejected(state)
    Closed(CloseReason),                // RemoteClose, RemoteTimeout, DtlsAlert(u8), Local, ThreadFailed
    Timeout,                            // a request/send/accept timeout; the link may still be up
    Protocol(&'static str),             // malformed reply, unexpected state; detail in the log
    Capacity,                           // no free channel / too many links
    InvalidArgument(&'static str),
    Io(std::io::ErrorKind),             // socket setup failed
}
```

## Configuration

Everything has a default and can be overridden per link at connect time (`75-python-api.md`). The
defaults are the values proven live in `tutk-host`.

| Setting | Default | Source |
|---|---|---|
| `search_port` | 32762 | `sdk.py` `LAN_SEARCH_PORT` |
| `connect_timeout` | 15 s | `Session` |
| `av_timeout` | 30 s | `Session` |
| `search_interval` | 100 ms | `iotc/discovery.c:8` |
| `alive_interval` / `silence_limit` | 1 s / 60 s | `iotc/init.c:8–9` |
| `ctrl_retransmit` | 60 ms | `av/ioctrl.c:14` |
| `ctrl_window` | 8 unacknowledged user frames | prototype, `40-av-client.md` |
| `ack_idle` / `report_interval` | 110 ms / 200 ms | `av/transport.c:10–11` |
| `nak_repeat` / `gap_skip` / `partial_wait` / `incomplete_wait` | 50 / 300 / 500 ms / 2 s | `av/transport.c:14`, `av/media_recv.c:20–21`; `incomplete_wait` new (`45-media-receive.md`) |
| `video_queue` / `audio_queue` / `push_queue` | 60 / 200 / 256 | `av/media_recv.c:18–19`; new |
| `talk_queue` / `talk_frame` | 10 frames / 20 ms | new; `talkback.py` `FRAME_S` |
| `trace` | off | R33 |
| `sdk_version` | `0x040308c8` | R40 |
