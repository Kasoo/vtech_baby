# 45 — Media receive

Serves R18–R24. Reference: `tutk-host/av/media_recv.c` (window, assembly, release, counters),
`av/fec.c` (parity), `av/ioctrl.c:156` (`av_media_reset`). Wire authority: `tutk-kit/docs/av/90-fifos.md`
("Media fragment body", "FEC envelope", "NAK/retransmit semantics"), `av/80-fec.md`.

All of this is live-proven in `tutk-host` (`tutk-host/NOTES.md` "Status": 0 bad frames clean; 35
packets rebuilt and 0 bad under 1-in-50 induced loss). The port keeps the behaviour and changes the
data structures.

## The media fragment (sub-header at `f`, 0x14 bytes)

| Offset | Field |
|---|---|
| `+0x00` | kind: 3/4 audio, 5/6 video key, 7/8 video inter. Any other value is dropped. |
| `+0x01` | sub-flags: bit0 = last fragment |
| `+0x02` | u16 packet number `pn`: the ACK/NAK number space; a resend keeps it |
| `+0x04` | u16 fragment count |
| `+0x06` | u16: fragment index, or on the last fragment the frame-info length (16) |
| `+0x08` | u16 `plen`: payload bytes in this fragment, frame info included on the last |
| `+0x0a` | u16 checksum (not checked on receive; `tutk-host` doesn't either) |
| `+0x0c` | u32 frame number, shared by audio and video |
| `+0x10` | u32 per-stream frame count |
| `+0x14` | payload |

Validation (`av/media_recv.c:174`): `plen ≤ flen − 0x14`; `1 ≤ count ≤ 2048`; index < count;
`info_len ≤ plen`; `info_len ≤ 16`; `plen − info_len ≤ 1024`. A fragment that fails any check is
dropped and counted.

## Structures (`core/src/av/media_rx.rs`)

```rust
struct MediaRx {
    window: PacketWindow,             // ACK/NAK bookkeeping over pn
    video: StreamAsm, audio: StreamAsm,
    fec: Fec,                         // fec.rs
    counters: RecvCounters,
    nak_at: Option<Instant>, naks_sent: u32,
}
struct PacketWindow {                 // av/media_recv.c:24–107
    cum: u16, high: u16,              // 0xffff at start and after a reset
    seen: BitArr<[u64; 64]>,          // 4096 bits: received pn ahead of cum, indexed pn % 4096
    gap_since: Option<Instant>,
}
struct StreamAsm {
    partial: BTreeMap<u32 /*frame number, serial order*/, Partial>,
    ready: VecDeque<EncryptedFrame>,  // handed to the consumer queue by the driver
}
struct Partial {
    first_at: Instant, kind: u8, count: u16, have: u16,
    frags: Vec<Option<Bytes>>,        // one slot per fragment, each its own plen
    info: Option<([u8; 16], u8)>, naked_while_waiting: bool,
}
pub struct EncryptedFrame { kind: u8, number: u32, arrived: Instant, data: Vec<u8>, info: [u8; 16], info_len: u8, late: bool }
```

**Changes from `tutk-host`:**
- Fragments are kept as separate `Bytes` slices and joined once when the frame completes. Today each
  frame gets `count × 1024` bytes up front and closes the gaps with `memmove` (`media_recv.c:150`).
  The join is a single copy into the frame's `Vec`.
- `BTreeMap` keyed by frame number, in serial order relative to the oldest partial frame. Today it is a
  linked list scanned for the oldest frame on every release (`media_recv.c:124`).
- Frame numbers compare in 32-bit serial arithmetic (`(a − b) as i32`), as today.

## The packet window (ACK and NAK)

Port of `track_pn` (`av/media_recv.c:75`) and `av_media_naks` (`:109`):

- Accept `pn` only if it is after `cum` and less than 4096 ahead (16-bit serial order); otherwise it is
  a duplicate or stale, and the whole fragment is dropped (counted). A `pn` already marked seen is a
  duplicate: drop it.
- Mark it seen; `high = max(high, pn)`; advance `cum` across contiguous seen packets, clearing their
  bits.
- **Gap skip**: if `cum < high`, start `gap_since`. When a gap has stayed open for `gap_skip`
  (300 ms), advance `cum` past the missing packets up to the next seen one, then carry on contiguously.
  The camera stops resending what the cumulative ACK has passed.
- **NAK list**: on an ACK when `nak_repeat` (50 ms) has passed since the last list, name every unseen
  `pn` in `(cum, high)`, at most 32, as `pn − cum`. Each name counts in `packets_requested`, and the
  time is recorded in `nak_at`.

## Frame assembly and release

Port of `av_media_on_packet` and `release` (`av/media_recv.c:124–200`):

- A fragment goes to the video or audio `StreamAsm` by kind (3/4 audio, else video). Its partial frame
  is found or created by frame number. A fragment whose `count` disagrees with the frame's, or whose
  slot is already filled, is dropped. The payload is stored without the frame info; the last fragment
  also stores the info.
- **Release** runs after each fragment and on every deadline. While the oldest partial frame is
  complete, move it to `ready`. If the oldest is incomplete and has waited `partial_wait` (500 ms), drop
  it and count it `given_up`. It holds back every frame after it until then, which keeps delivery
  ordered.
- A frame released while a NAK was outstanding (`nak_at ≥ first_at`) is counted `late`
  (`media_recv.c:146`, commit `a0e6452`).
- `ready` drains into the link's video or audio **output queue** (`70-runtime.md`). When the queue is
  full (60 video, 200 audio), the oldest queued frame is dropped and counted `overflow`, as today
  (`media_recv.c:160`).
- A dropped frame (given up or overflowed) also records a **loss marker** in its stream, so that the
  consumer learns of the gap in order (R23). Today the next read returns `AV_ER_LOSED_THIS_FRAME`, which
  `media.py` counts as `bad`. Here the next delivered frame carries `lost_before: u32`.

## Parity recovery (`core/src/av/fec.rs`)

Port of `av/fec.c` (kit `av/80-fec.md` "Recovered parity generator"):

- GF(2⁸), polynomial `0x11d`, generator 2. The `exp`/`log` tables are built at compile time
  (`const fn`), rather than with `pthread_once` (`fec.c:45`).
- The generator rows for `k = 20` and `k = 25` are copied verbatim from `fec.c:18–25`. Any other `k`
  is ignored.
- Four group slots; a new group takes the oldest slot (`fec.c:66`). Each slot holds `k + 1` blocks of
  1044 bytes, a bitmask of what is present, and a `done` flag. That is 27 KiB per slot, allocated once
  per link (108 KiB) instead of `calloc` on first use.
- **Data shard** = the sub-header plus `plen` bytes, zero-padded to 1044. A data packet larger than
  1044 bytes is skipped (`fec.c:124`). The **parity block** is the 1044 bytes after the parity
  packet's own 0x14 sub-header. A parity packet must have `index == k`, and a data packet
  `index < k` (`fec.c:116`).
- When all `k` data shards are present, the group is done. When the parity and exactly `k − 1` data
  shards are present, rebuild the missing one:
  `d_m = (parity ⊕ Σ_{i≠m} g_i·d_i) · g_m⁻¹`. Count `packets_recovered`, mark the group done, and feed
  the rebuilt `0x14 + plen` bytes to `MediaRx::on_packet` as though they had arrived. The window
  ignores a duplicate if the original also arrives.
- **All kinds** are recovered, including kind 7. The stock SDK leaves kind 7 to NAK only
  (`tutk-kit/docs/av/80-fec.md` "Receive side").
- Performance: a rebuild costs `k × 1044` table multiply-accumulates (about 26,000 operations) and
  happens at most once per group. A 256 × 256 multiplication table (64 KiB, compile time) would make
  each step one lookup, but is not needed.

## The reset (R19)

Port of `clean` (`av/media_recv.c:303`) and `av_media_reset` (`av/ioctrl.c:156`), as one operation,
`MediaRx::reset`, driven by `MediaReceiver::reset()`:

1. Drop every partial and ready frame in both streams, and flush both output queues. Clear the window
   bits and the parity slots.
2. `reset_seq += 1`; `cum = high = old_cum + 0x4000`. The jump makes everything the camera already
   has in flight look stale.
3. Send an internal control frame (class `0x10`) of type `0x24`, with an 8-byte payload: u16
   `reset_seq`, u16 the new `cum`, then 4 zero bytes. Mark the next ACK urgent, and send it at once.
4. Wait for an internal `0x25` whose u16 payload equals `reset_seq`, for up to 1 s
   (`RESET_WAIT_MS`). A timeout is not an error, as today (the wait simply ends). It is counted, and
   the link continues.

Today the Python layer cleans video and audio separately, and only the video clean runs the
handshake. The new API has a single reset (`75-python-api.md`).

## Counters (R23)

The eight that exist (`sdk.py` `RECV_STATS`): `packets_requested`, `video_given_up`, `video_late`,
`video_overflow`, `audio_given_up`, `audio_late`, `audio_overflow`, `packets_recovered`. New ones:
`fragments_dropped` (bad header), `packets_duplicate`, `packets_stale`, `gaps_skipped`,
`resets`/`reset_timeouts`, `decrypt_failed_video`/`audio` (counted by the consumer;
`60-application.md`), and `frames_video`/`audio` and `bytes_video`/`audio` delivered.

## Status

**Built 2026-10-22** (`core/src/av/media_rx.rs`, `fec.rs`), with `core/tests/av.rs` and
`core/tests/fec.rs`. The parity rebuild matches `tutk-host`'s `fec.c` byte for byte (fixtures `av.fec.*`).
Overflow of the output queues belongs to the runtime (`70-runtime.md`), so `MediaRx` emits every frame
and counts `given_up` and `late`.

Two deliberate differences from `tutk-host` (`TODO.md`, "tutk-host"):
- kind 4 is audio, following the kit;
- a gap skip that lands on `pn_high` clears that packet's seen bit.

## Size estimate

About 700 lines including tests: window 120, assembly 250, FEC 200, reset and counters 130.
