# 70 — Runtime: threads, I/O, limits, errors, logs

Serves R5–R9, R22, R30–R34, R37–R39. Reference: `tutk-host/global/egress.c` (LAN-only),
`global/connections.c` (socket options), `vtech_lan/worker.py` (fault isolation today).

## The link thread (`core/src/runtime/driver.rs`)

One OS thread per link, named `vtech-link-<last 6 of UID>`, spawned by `Link::connect` and owning:

- a non-blocking `mio::net::UdpSocket`;
- a `mio::Poll` with two tokens, the socket and a `mio::Waker`;
- the command receiver (`std::sync::mpsc::Receiver<Command>`);
- `LinkState`, all the sans-IO machines (`IotcSession`, `DtlsClient`, `AvClient`, `MediaRx`,
  `Requests`, `MediaKey`, `AvServer`s);
- the trace writer, if any.

```text
loop {
    let deadline = state.poll_deadline();                    // min over every machine; None = idle
    poll.poll(&mut events, deadline.map(|d| d.saturating_duration_since(now()).ceil_ms()))?;
    let now = Instant::now();
    if socket readable { while let Ok((n, src)) = socket.recv_from(&mut buf) { state.on_datagram(now, src, &buf[..n]) } }
    while let Ok(cmd) = commands.try_recv() { state.on_command(now, cmd) }
    state.on_timeout(now);                                   // each machine checks its own deadlines
    while let Some(tx) = state.poll_transmit() { send(tx) }  // egress-checked
    state.deliver();                                         // completions, queues
    if state.finished() { break }
}
```

- **Wake-ups**: datagrams, commands (each `Command` send is followed by `waker.wake()`), and the
  earliest deadline. An idle established link wakes for the keepalive tick (1 s), the ACK idle
  deadline (110 ms) and the report (200 ms): about 15 wake-ups a second. That replaces today's two AV
  threads waking every 10 ms plus 5 ms polling waits (R39).
- **Receive buffer**: one 2048-byte buffer per link, reused. Datagrams are decoded and copied into
  owned `Bytes` only where something keeps them (a fragment slot, a control payload).
- **Deadline granularity**: rounded up to whole milliseconds; timers fire late by at most one
  scheduler quantum, never early.

## Commands

```rust
enum Command {
    Connect { done: Arc<Completion<LinkInfo>> },
    Send { ty: u32, payload: Bytes, done: Arc<Completion<()>> },
    Request { ty: u32, payload: Bytes, reply: u32, multi: bool, deadline: Instant, waiter: Arc<Waiter> },
    EndExchange { id: u64 },
    MediaStart { done: Arc<Completion<()>> },    // key exchange if needed
    MediaReset { done: Arc<Completion<()>> },
    TalkOpen { done: Arc<Completion<u8>> },      // channel
    TalkAccept { ch: u8, deadline: Instant, done: Arc<Completion<()>> },
    TalkClose { ch: u8, drain: bool, done: Arc<Completion<()>> },
    Stats { done: Arc<Completion<Stats>> },
    Close { done: Arc<Completion<()>> },
}
```

`Completion<T>` is a `Mutex<Option<Result<T, LinkError>>>` with a `Condvar`. A caller blocks in
`wait(timeout)`, with the GIL released by the wrapper. A completion that times out on the caller's
side is abandoned: the reactor's later `complete()` finds no one waiting, and nothing breaks.

## Output queues (`core/src/runtime/queue.rs`)

One generic bounded queue:

```rust
struct Queue<T> { inner: Mutex<QState<T>>, cv: Condvar }
struct QState<T> { items: VecDeque<T>, cap: usize, closed: Option<LinkError>, dropped: u64 }
```

| Queue | Producer → consumer | Cap | When full |
|---|---|---|---|
| pushes | reactor → `Link::next_push` | 256 | drop oldest, count |
| video | reactor → `MediaReceiver::next` | 60 | drop oldest, count `video_overflow` |
| audio | reactor → `MediaReceiver::next` | 200 | drop oldest, count `audio_overflow` |
| talk input | `TalkBack::send` → reactor | 10 | the producer blocks (back-pressure) |
| multi-reply inbox | reactor → `Exchange::next` | 64 | drop oldest, count |

Consumers wait on the condvar with a timeout. Closing a queue stores the link's error and wakes
everyone; `next()` on a closed, empty queue returns that error. The reactor never blocks on a queue: it
drops instead. The talk-input queue is the only one where the producer blocks, and its producer is a
caller thread.

## The socket

Created with `socket2`, then converted to `mio`:

- IPv4 UDP, bound to `0.0.0.0:0` (an ephemeral port), or to a configured local address;
- `SO_BROADCAST` on (discovery); `SO_REUSEADDR` is not needed with an ephemeral port;
- `SO_RCVBUF` and `SO_SNDBUF` 1,024,000 bytes, as today (`global/connections.c` `SOCKET_BUFFER`). A
  1080p keyframe is up to ~200 fragments, which arrive in a burst;
- non-blocking. A send that would block is **dropped and counted** (`send_would_block`). Every
  protocol here tolerates a lost datagram, and the reactor must not stall.

**One socket per link** (`05-current-setup.md`). The camera answers from its port to whatever port we
sent from, so each link's traffic comes back to its own socket. Discovery broadcasts from that socket
too. Two links to the same camera use two sockets, two tokens and two camera-side sessions, as two SDK
sessions do today (R6). Whether the camera handles several sessions from one host this way is already
shown live: the HA worker and a probe run side by side today.

## The LAN-only rule (`core/src/runtime/egress.rs`, R5, R30)

Port of `global/egress.c`. Every `send_to` passes through `egress_allowed(dst)`:

- **IPv4 only**. Allowed: `255.255.255.255`, and any address inside a directly attached IPv4 subnet
  (`(dst & mask) == (if_addr & mask)`, so host and directed-broadcast addresses both pass). Anything
  else is refused, counted in a process-wide `AtomicU64` (`refused_sends`) and logged at WARNING,
  rate-limited to one a minute.
- **Interfaces** come from `getifaddrs` (the `if-addrs` crate). The list is cached and refreshed at
  most once a second, as `egress.c:25` does; it lives in a process-wide `RwLock`.
- **There is no name resolution anywhere**: the API takes no host names, so `refused_lookups` is
  always 0. The counter is kept so diagnostics keep their shape.
- The same interface list provides the **broadcast targets** for discovery (`20-iotc.md`): every up,
  non-loopback IPv4 interface with a non-zero netmask, ordered with the default-route interface first
  (from `/proc/net/route`, best effort; the order is a nicety, not a requirement).

## Errors and panics (R34)

- **Recoverable protocol problems** (malformed packets, unknown classes, bad tags) are counted and
  dropped; none is fatal.
- **Fatal link events** (`LinkError::Closed(..)`) close the link as `10-architecture.md` describes.
- **Panics**: the core is built with `panic = "unwind"`. The reactor's loop body runs inside
  `std::panic::catch_unwind`. If it panics, the link records `Closed(ThreadFailed)`, logs the panic
  message at ERROR, closes every queue, completes every waiter, and the thread exits. The process and
  every other link carry on. On the Python side, PyO3 turns a panic in a call into a
  `PanicException`, which the wrapper maps to `VtechError`.
- **What can still abort the process**: allocation failure (Rust aborts on OOM) and stack overflow.
  Both are kept out of reach by design: every allocation is bounded by a protocol limit (the table
  below), and nothing recurses. `#![forbid(unsafe_code)]` in the core removes memory corruption as a
  failure mode.
- **Never `unwrap()` on network input.** Parsers return `Option`/`Result`. A clippy lint set
  (`clippy::unwrap_used`, `expect_used`, `indexing_slicing` in parser modules) enforces it in CI.

## Resource limits

| Thing | Limit | Where |
|---|---|---|
| Links per process | 32 (configurable) | `Link::connect` → `Capacity` |
| Talk-back channels per link | 1 at a time (the integration needs 1) | `Link::talkback` → `Capacity` |
| Inbound datagram | 2048 bytes | IOTC |
| Partial frames per stream | 64; the oldest is given up beyond that | media |
| Fragments per frame | 2048 (2 MiB) | media |
| Parity slots | 4 × 27 KiB | FEC |
| Handshake reassembly | 8 KiB, 4 buffered messages | DTLS |
| Inbound control reassembly | 16 fragments, 16 KiB | control |
| Queues | as above | runtime |

Worst-case memory per link is about 6 MiB, almost all of it 60 queued 1080p frames.

## Logging (R32)

- The core logs with the `log` crate's macros. Levels: ERROR for thread failures; WARNING for refused
  egress, an unexpected `0x602` result, or a DTLS alert; INFO for link up, link down and the reason;
  DEBUG for retransmits, resets, gaps, and a periodic counters line every 30 s while streaming.
- The **Python wrapper** installs a `log::Log` implementation that pushes records into a process-wide
  bounded ring (1,024 records, drop oldest, counted). It never calls into Python.
- A Python daemon thread in `vtech_lan/link.py` blocks on `vtech_link.next_logs(timeout = 1.0)` (GIL
  released) and forwards each record to `logging.getLogger("custom_components.vtech_baby.link")` at
  the matching level. Rust threads never take the GIL (R37).
- The CLI uses `env_logger` instead.

## Packet trace (R33)

Per link, off by default; `LinkConfig.trace = Some(path)` turns it on. One text line per event, in
the same shape as `tutk-host`'s `tk_hex` (`common/tk_common.h`), so existing decoding scripts work:

```
vl-udp:  <s.ms> W|R <port> <len> <hex>       raw datagram (as sent / received, scrambled)
vl-iotc: <s.ms> W|R <ch> <len> <hex>         IOTC channel payload, unscrambled
vl-dtls: <s.ms> W|R <ch> <len> <hex>         DTLS plaintext (handshake bodies, application data)
vl-av:   <s.ms> W|R <class> <len> <hex>      AV packet
```

Secrets are never written. The AV login's password field is overwritten with `*` bytes before it is
traced. DTLS key material is never formatted. The `vl-dtls` handshake lines contain only public values.
A trace still contains decrypted control traffic, so it is for development, as `TK_TRACE=2` is today.

## Process-wide state

Kept to the minimum: the interface cache, the LAN-only counters, the log ring, and a registry of live
links (`Weak` handles, for diagnostics and the link cap). There is no global init call and no global
lock on any packet path.
