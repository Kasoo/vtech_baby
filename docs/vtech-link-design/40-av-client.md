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

**The control frame** (both directions; `tutk-kit/docs/av/40-ioctrl.md` as corrected in kit commit
`4da044d`, from `_sendIOorIOInnerFrame`, the inbound admit `FUN_0011bed0` and the completion
`FUN_0011c920`). Offsets are datagram-absolute, without the stats block:

| Offset | Field |
|---|---|
| `+0x00` | class `0x0c`, flags 0, version, counter |
| `+0x08` | 8 zero bytes (group header; zero for control frames) |
| `+0x10` | u8 `00` (control) |
| `+0x11` | u8 control class: `0x70` user, `0x10` internal |
| `+0x12` | u16 **packet number**: a running counter, incremented once per fragment and never reset; the receiver uses it only as a duplicate gate |
| `+0x14` | u16 fragment count |
| `+0x16` | u16 fragment index, from 0 |
| `+0x18` | u16 this fragment's body length (type + payload on fragment 0, the raw chunk after) |
| `+0x1a` | u16 `0` |
| `+0x1c` | u32 **frame number**: one per message, from 0, the same on every fragment; shared by user and internal frames |
| `+0x20` | u32 `0` |
| `+0x24` | u32 IOCtrl type, **fragment 0 only**; later fragments carry payload from `+0x24` |
| `+0x28` | payload |

Fragmenting: `count = (len + 4 + 0x3ff) >> 10`. Fragment 0 carries the type and up to 1020 payload
bytes; each later fragment carries up to 1024. We send single-fragment frames only (R10: payload ≤ 1020),
so we write count 1, index 0, and a packet-number counter of our own.

`tutk-host` writes the frame number's low 16 bits at `+0x12` (`av/ioctrl.c:21`) and the u32 pairs
`1` and `4 + len` at `+0x14` and `+0x18`, which are the same bytes as count 1 / index 0 and
length / 0. That works because the camera keys on `+0x1c` and only needs `+0x12` to advance.

**Sending policy.** User frames go out through a window of **8 unacknowledged frames**
(`ctrl_window`, configurable), with a FIFO behind it. Internal frames (the reset) bypass the window.
The camera takes 8 back-to-back unacknowledged GETs and answers every one with no retransmission
(spike, 2026-10-21; Q5). Today concurrency is limited above us by `Session._send_lock`, because the SDK
refuses a second concurrent send (`-20021`). With the window in the stream, callers queue instead of
retrying on "busy" (R11).

**The camera's own control frames are stop and wait.** It sends one, and sends the next only after our
ACK covers it. With 8 requests outstanding, the replies came about 110 ms apart when we ACKed every
100 ms, about 55 ms apart at 50 ms, and about 33 ms apart at 10 ms (spike `--window 8 --ack-ms N`,
2026-10-21). So **the ACK after an inbound control frame must be prompt**. The 10 ms coalescing deadline
below is what lets a full state re-read (about 12 requests) finish in about 0.4 s rather than 1.3 s.
`tutk-host` behaves the same way through its 10 ms transmit tick.

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

**Inbound frames**, per stock's admit and completion: every inbound control fragment sets
`ack_due`. Fragments are stored keyed by **frame number (`+0x1c`) and fragment index (`+0x16`)**, and
duplicates are dropped. A message is delivered when its frame number is `ctrl_in + 1` and all `count`
fragments are present. The fragments are joined in index order (type from fragment 0), and `ctrl_in`,
the frame number our ACK reports at `+0x0c`, advances by one. `ctrl_in` counts messages, not fragments.
Frames ahead of `ctrl_in + 1` are held (at most 16 fragments, 16 KiB); stale ones are dropped. The camera
resends anything we haven't ACKed.

**A latent bug in `tutk-host`**, found from this: `av_ctrl_on_frame` (`av/ioctrl.c:100`) orders inbound
frames by `+0x12` (the packet number) and drops anything with a fragment count other than 1. While the
camera sends only single-fragment messages, its packet number equals its frame number and this works.
After one multi-fragment message, the two counters diverge. Every later frame then fails the
`ctrl_in + 1` check, the ACK stops advancing, and the control channel stalls until the link is
dropped. No multi-fragment inbound message has been seen yet. The fix is listed in `TODO.md`.
- User class `0x70`: emit `Event::Control { type: u32 at f+0x14, payload: f[0x18 .. 0x18 + body − 4] }`.
- Internal class `0x10`, type `0x25` (reset ack), with a u16 payload equal to our last reset sequence:
  complete the pending reset (`45-media-receive.md`).
- **Multi-fragment messages** (R16) are reassembled as above. None has been seen live, so the path
  is exercised in simulation, and it logs at DEBUG when it runs.

## Errors

| Event | Result |
|---|---|
| No login reply by the deadline | `Unreachable` (as today: the camera is up at the IOTC level, but the AV login timed out) |
| Login state ≠ 0 | `AvAuthFailed(LoginRejected(state))` |
| DTLS failure | per `30-dtls.md` |
| IOTC session closed | every pending send and request completes with that `Closed(reason)` |

## Status

**Built 2026-10-22** (`core/src/av/packet.rs`, `control.rs`, `client.rs`), with `core/tests/av.rs`.
Every outbound packet (both logins, ACK with and without NAKs, report, RTT, user and internal control
frames) is byte-identical to `tutk-host`'s, from the fixtures. The control stream covers the window of
8, retransmission, ACK trimming, the latency histogram, and inbound reassembly by frame number,
including a multi-fragment message followed by single ones (the `tutk-host` stall case). The client
covers the login resend and both failures, the ACK/report schedule (an idle ACK every 110 ms, a report
every 200 ms, a control frame ACKed 10 ms after it arrives), IOCtrl round trips, RTT answers, and the
media reset, both confirmed and timed out.

One change from the table above: a camera report is answered with an RTT packet at once, not on the
next tick.

## Size estimate

About 800 lines including tests: packet codecs 300, client 250, control stream 250.
