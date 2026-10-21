# 40 — The AV client channel

Serves R2, R4, R7, R9–R11, R16, R31. Reference: `tutk-host/av/session.c` (login), `av/transport.c`
(dispatch, ACK, report, RTT), `av/ioctrl.c` (control frames). Wire authority:
`tutk-kit/docs/av/20-session.md` ("The login request"), `av/40-ioctrl.md` ("The IOCtrl frame on the
wire"), `av/90-fifos.md` ("The on-wire AV packet header").

The AV client runs over DTLS on IOTC channel 0. It logs in, then carries a reliable, ordered control
stream (IOCtrl both ways) and the media stream (camera to us, `45-media-receive.md`). It also keeps the
camera's sender happy by acknowledging everything.

## Module layout (`core/src/av/`)

| Module | Contents |
|---|---|
| `packet.rs` | `AvHeader` and the per-class body codecs: login, login reply, ACK, report, RTT, FEC envelope, control frame, media sub-header. Shared with the server side. |
| `client.rs` | `AvClient`: login, then the dispatcher and the transmit schedule. |
| `control.rs` | `ControlStream`: outbound queue, retransmit and ACK trimming; inbound ordering and reassembly. |
| `media_rx.rs`, `fec.rs` | `45-media-receive.md`. |

## The AV packet (`packet.rs`)

```
+0x00 class u8   0x00 login, 0x09 ACK, 0x0a report, 0x0b RTT, 0x0c FEC envelope (control, media, parity)
+0x01 flags u8   bit0 last fragment, bit2 end, bit3 (0x08) an 8-byte stats block follows
+0x02 version u16  0x000c
+0x04 counter u16  sender's running packet count; we stamp it on classes 0x09, 0x0a and 0x0c (av/session.c:37)
+0x06 u16          packet number (kit); we send 0, as tutk-host does
+0x08            body, or the stats block (8 bytes) then the body at +0x10 when flags & 0x08
```

`B` = `0x08`, or `0x10` with the stats block. The stats block's contents are not used
(`tutk-kit/docs/av/90-fifos.md`).

## Login (`client.rs`)

Inputs: account, password, and the camera's protocol version from IOTC (header `+0x02` of its `0x602`,
which is `0x1b`). `W = 0x101` because that version is ≥ 9 (`av/session.c:229`). `tutk-host` sends both
login packets (handshake-variant `hs = 1`, from `AvIn.resend = 1`) with credential mode 0, which puts
the password in clear inside DTLS (`sdk.py` `client_start`: `auth_type = 0`).

**Login packet `n`** (`n` = 0 or 1; `av/session.c` `build_login`), length `2W + 0x38 + 2n`
(570 and 572 bytes):

| Offset | Value |
|---|---|
| `+0x00` | `00` class, `+0x01` sub-type `0x00` (n = 0) or `0x20` (n = 1), `+0x02` u16 `0x000c` |
| `+0x10` | u16 body length `2W + 0x20 + 2n` |
| `+0x12` | `hs` = 1 |
| `+0x14` | u32 `nonce + n`; `nonce` is random, not 0 or `0xffffffff` |
| `+0x18` | account, NUL-padded to `W` (at most `W − 1` bytes) |
| `+0x18 + W` | password, NUL-padded to `W` |
| `+0x18 + 2W` | `hs` |
| `+0x1c + 2W` | the 20-byte opcode-support block `04 00 00 00 fb 07 1f 00` + 8 × `00` + `00 00 03 00` (`av/session.c` `av_opcode_support`) |
| `+0x30 + 2W` | credential mode `0` |
| `+0x34 + 2W` | `1` |

**Sequence**: send both packets, then resend both every 100 ms (`LOGIN_RESEND_TICKS`) until a
**login reply** arrives: class `0x00`, sub-type `0x21`, at least `0x1c` bytes, with the u32 at `+0x14`
equal to `nonce` or `nonce + 1`. The state byte at `+0x18`: `0` → logged in; any other value →
`AvAuthFailed(LoginRejected(state))` (`3` is "rejected"; today it maps to `AV_ER_WRONG_VIEWACCorPWD`,
`-20009`). The login shares the link's `av_timeout` deadline with the DTLS handshake. That replaces
today's version-derived deadline (`peer_version × 1000 / 50 + 2` ticks, about 27 s).

**On login**: start the transmit schedule (below) with an immediate report (`av/transport.c:164`), and
emit `Event::AvReady`.

## The transmit schedule (`client.rs`)

Today a dedicated thread wakes every 10 ms (`av/transport.c` `av_tx_task`). Here the same decisions
hang off deadlines:

| What | When | Builder |
|---|---|---|
| ACK | after any inbound control frame or media packet (`ack_due`), at most once per 10 ms; and at least every `ack_idle` (110 ms) regardless | `send_ack`, `av/transport.c:114` |
| Report | every `report_interval` (200 ms), and once at start | `send_report`, `av/transport.c:141` |
| RTT answer | on the next tick after an inbound report | `send_rtt`, `av/transport.c:152` |
| Control retransmit | per frame, `ctrl_retransmit` (60 ms) after its last send | `av/ioctrl.c:55` |

The 10 ms batching of ACKs is a **coalescing deadline**: an inbound packet sets
`ack_at = min(ack_at, now + 10 ms)` rather than sending at once. This keeps the ACK rate the camera
has seen (≤ 100/s while streaming, about 9/s idle) without a fixed 10 ms wake-up when idle (R39).

**ACK** (class `0x09`, flags 0), 24 bytes, or `0x16 + 2n` with NAKs:

| Offset | Value |
|---|---|
| `+0x08` | u16 `pn_cum`: every media packet up to here received (`0xffff` before the first) |
| `+0x0a` | u16 `pn_high`: the highest received |
| `+0x0c` | u16 `ctrl_in`: the camera's highest contiguous control frame (`0xffff` before the first) |
| `+0x0e` | u16 `n` NAK entries |
| `+0x10` | u16 urgent: 1 on the first ACK after a reset (`45-media-receive.md`) |
| `+0x12` | u16 ACK sequence, from 0 |
| `+0x14` | u16 low 16 bits of a millisecond clock |
| `+0x16` | `n` × u16 `missing − pn_cum`, at most 32, only if `nak_repeat` (50 ms) has passed since the last NAK list (`av/transport.c:114`) |

**Report** (class `0x0a`, flags `0x08`), 16 bytes: `+0x08` u16 ms clock, `+0x0a` u16 our RTT estimate
(`av/transport.c:141`). The kit says the camera's field at `+0x0a` is an unknown metric, not RTT. Ours
is accepted as it is, so we keep sending the RTT.

**RTT** (class `0x0b`, flags 0), 20 bytes: `+0x08` the report timestamp we are echoing, `+0x0a` and
`+0x0c` our RTT estimate (`av/transport.c:152`). Not counter-stamped.

**Inbound dispatch** (`av/transport.c:80`): class `0x09` → `ControlStream::on_ack(u16 at +0x0c)`;
`0x0a` → remember `+0x08` and schedule an RTT answer; `0x0b` → RTT sample `now16 − u16(+0x08)`, kept if
under 5 s (`av/transport.c:21`); `0x0c` → the FEC envelope (below); class `0x00` sub-type `0x21`
after login → ignored. Anything else is counted and dropped.

**FEC envelope** (class `0x0c`, `av/transport.c:60` `on_data`): the group header is at `B`
(u16 group, u8 index, u8 k, u32 1), and the frame at `f = B + 8` with `flen = len − B − 8`, which
must be ≥ `0x14`.
- `f[0] == 0x00` → a control frame → `ControlStream::on_frame`.
- `f[0] == 0x0c` → parity → `Fec::on_packet` only.
- otherwise → a media fragment → `MediaRx::on_packet`, then `Fec::on_packet`.

## The control stream (`control.rs`)

**Outbound frame** (`av/ioctrl.c:21` `av_ctrl_send`), `0x28 + len` bytes:

| Offset | Value |
|---|---|
| `+0x00` | class `0x0c`, flags 0, version, counter |
| `+0x08` | 8 zero bytes (group header; `tutk-host` leaves it zero for control frames) |
| `+0x10` | `00` (control), `+0x11` control class `0x70` (user) or `0x10` (internal) |
| `+0x12` | u16 frame number, low 16 bits |
| `+0x14` | u32 `1` (fragment count) |
| `+0x18` | u32 `4 + len` (type + payload) |
| `+0x1c` | u32 frame number |
| `+0x20` | u32 `0` |
| `+0x24` | u32 IOCtrl type |
| `+0x28` | payload, `len ≤ 0x3fc` (1020) bytes |

Our frame numbers count from 0 and are shared by user and internal frames. **`+0x12` carries the
frame number in both directions, as `tutk-host` does** (`av/ioctrl.c:21` writes it, `:100` reads it),
and that is proven live. The kit calls `+0x12` the "fragment index" (`tutk-kit/docs/av/40-ioctrl.md`).
The two agree for frame 0 and for every single-fragment frame the camera has acknowledged so far.
`95-open-questions.md` Q4 tracks reconciling them.

**Sending policy.** User frames go through a FIFO with **one user frame in flight** (stop and wait).
Internal frames (the reset) go out at once alongside it. This is new. Today concurrency is limited
above us by `Session._send_lock` because the SDK refuses a second concurrent send (`-20021`). Moving
the limit into the stream keeps the wire pattern the camera has seen (never two unacknowledged user
frames), and callers queue instead of retrying on "busy" (R11). The window could later grow to `N`
behind a config value (Q5).

- Every unacknowledged frame is resent `ctrl_retransmit` (60 ms) after its last send, re-stamped with a
  fresh counter (`av/ioctrl.c:55`).
- **`on_ack(acked)`**, from the camera's ACK `+0x0c`: ignore `0xffff`. Advance `ctrl_acked` if `acked`
  is after it in 16-bit serial order, then drop every in-flight frame at or before `ctrl_acked`
  (`av/ioctrl.c:77`). An ACK that doesn't advance still trims, which covers a frame queued after an
  earlier ACK of the same number. Each dropped user frame completes its sender with `Ok`, and the
  next queued frame goes out.
- **Ack latency** (R31): for each frame, the time from first send to ACK goes into a histogram
  (buckets at 10/50/100/500 ms, 1/5/10/30 s), with a retransmit count. This is what will show whether
  the post-connect stall is a stalled ACK (`AGENTS.md` "Known issues").
- **No AV-level dead-link timeout by default.** If a frame is never ACKed, the IOTC silence timer still
  catches a dead camera. The known 35–40 s stall recovers by itself, so a short AV timeout would turn it
  into a reconnect. A caller's own timeout fails its request with `Timeout`, and the frame then stays
  queued until it is ACKed or the link closes (the camera may still act on it). Q6 revisits this once
  the histogram has data.

**Inbound frames** (`av/ioctrl.c:100` `av_ctrl_on_frame`): every inbound control frame sets `ack_due`.
A frame is accepted when its number (u16 at `f+2`) is `ctrl_in + 1`, the u32 at `f+8` (body length) is
≥ 4 and fits the packet, and the u32 at `f+4` (fragment count) is 1. Then `ctrl_in` advances.
Duplicates and frames out of order are dropped; the camera resends anything we haven't ACKed.
- User class `0x70`: emit `Event::Control { type: u32 at f+0x14, payload: f[0x18 .. 0x18 + body − 4] }`.
- Internal class `0x10`, type `0x25` (reset ack), with a u16 payload equal to our last reset sequence:
  complete the pending reset (`45-media-receive.md`).
- **Multi-fragment frames** (count > 1, R16): new. Fragments sharing a frame number are collected by
  index until all `count` have arrived (at most 16 fragments, 16 KiB), and then delivered as one
  message. Today these are dropped (`av/ioctrl.c:107`, logged as "dropped a %u-fragment control
  frame"). The fragment-index field is ambiguous (Q4), so until a multi-fragment frame has been seen
  live this path is only exercised in simulation, and it logs at WARNING when it runs.

## Errors

| Event | Result |
|---|---|
| No login reply by the deadline | `Unreachable` (as today: the camera is up at the IOTC level, but the AV login timed out) |
| Login state ≠ 0 | `AvAuthFailed(LoginRejected(state))` |
| DTLS failure | per `30-dtls.md` |
| IOTC session closed | every pending send and request completes with that `Closed(reason)` |

## Size estimate

About 800 lines including tests: packet codecs 300, client 250, control stream 250.
