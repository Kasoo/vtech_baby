# 50 — Talk-back: the AV server channel

Serves R25–R28. Reference: `tutk-host/av/talkback.c`, `av/media_send.c`, `vtech_lan/talkback.py`.
Wire authority: `tutk-kit/docs/av/30-talkback.md` ("Server-side wire sequence"), `av/60-media-send.md`;
`docs/vtech-lan-kit/docs/40-media-streaming.md` ("Two-way audio").

For talk-back the roles reverse. We serve an AV channel on a free IOTC channel of the same session.
The camera connects to it as an AV client and receives our audio. The channel is **plain IOTC, not
DTLS**.

## Sequence

| Step | Who | What | Today |
|---|---|---|---|
| 1 | Python | `GETAUDIOOUTFORMAT 0x32a` → check A-law 8 kHz; `AUDIOSTOP 0x301` (half duplex) | `talkback.py` `_run` |
| 2 | Python → core | `link.talkback()`: the core runs the key exchange if needed (`60-application.md`), takes a free channel `ch` (1–31), opens an `AvServer` on it, and returns a `TalkBack` with `.channel` | `free_channel`, `serv_start` |
| 3 | Python | `SPEAKERSTART 0x350` with `ch` | `talkback.py` |
| 4 | Python → core | `talk.accept(timeout = 10 s)`: wait for the camera's login | `avServStart3` blocks |
| 5 | Python → core | `talk.send(alaw160)` per frame; blocks while the queue is full | `avSendAudioData` every 20 ms |
| 6 | Python → core | `talk.close()`: drains (`drain=True`) or drops what is queued, closes the server, frees the channel | `avServStop` + `Channel_OFF` + `avServExit` |
| 7 | Python | `SPEAKERSTOP 0x351` with `ch`; `AUDIOSTART 0x300` if the live view wants audio | `_teardown` |

Steps 1, 3 and 7 stay in Python because they are opcodes, and the opcode table lives in Python. The
half-duplex bookkeeping (`hold_audio_start`) stays in `talkback.py`.

## The server state machine (`core/src/av/server.rs`)

```
Listening ──DTLS record (datatype 1)──▶ reply "recv dtls data!" (stay)
    │
    ├──login (class 0, sub 0x00 or 0x20)──▶ reply 0x21 ──▶ Accepted ── send internal HELLO 0x40
    │                                                         │
    │ accept deadline → Failed(Timeout)                        ▼
    ▼                                                       Streaming ── close() ──▶ Closed
```

- **Listening** (`av/talkback.c:22`, `av/session.c:132` `av_read_task` server branch): the camera
  opens with a DTLS ClientHello sent as IOTC **datatype 1**. We answer each such datagram with the 15
  bytes `recv dtls data!` as **datatype 0**, and stay in Listening. Datagrams of any other kind before
  a login are dropped, since they may be left over from the channel's previous use
  (`av/session.c:150`).
- **Login** (`av/talkback.c:30` `av_serv_on_login`): class `0x00`, sub-type `0x00` (with `+0x12` = 0)
  or `0x20`, at least `2W + 0x38` bytes, with `W` from the camera's protocol version (`0x101`). The
  camera logs in with an empty account and password (`40-media-streaming.md`). We **accept any
  credentials** (state 0), as `sdk.py`'s auth callback does today. Reply, 60 bytes:

  | Offset | Value |
  |---|---|
  | `+0x01` | `0x21` |
  | `+0x02` | u16 `0x000c` |
  | `+0x10` | u16 `0x0024` |
  | `+0x14` | the login's 4-byte nonce, echoed |
  | `+0x18` | u32 state `0` |
  | `+0x1d`, `+0x1f`, `+0x20` | `1` |
  | `+0x24` | the 20-byte opcode-support block |

  Every login packet gets a reply; the first one moves the state to **Accepted**.
- **Accepted**: send internal control frame type `0x40` (HELLO), payload `00 01 00 00`
  (`av/talkback.c:72`). Start the same transmit schedule as the client channel: ACK, report, RTT and
  control retransmit (`40-av-client.md`), on the server's own deadlines. Process the camera's ACKs,
  reports and RTT packets the same way. `accept()` returns.
- **Streaming**: send audio (below). Inbound media is not expected; if any arrives it is counted and
  dropped.
- **close()**: drain or drop the queue, close the channel (`IotcSession::close_channel(ch)`), and
  free it. There are no wire messages of its own; `SPEAKERSTOP` (step 7) tells the camera.

## Sending a frame (`core/src/av/media_tx.rs`)

Port of `av/media_send.c` `send_frame`. A 20 ms A-law frame of 160 bytes becomes a 220-byte encrypted
frame (`60-application.md`), which fits in one fragment:

| Offset | Value |
|---|---|
| `+0x00` | class `0x0c`, flags `0x01`, u16 version, u16 counter |
| `+0x08` | group header: u16 `pn / 20`, u8 `pn % 20`, u8 `20`, u32 `1` |
| `+0x10` | media sub-header: kind `3`, sub-flags `1` (last), u16 `pn`, u16 count `1`, u16 info length `16`, u16 `plen = 220 + 16`, u16 checksum, u32 frame number, u32 audio frame count (from 1) |
| `+0x24` | the encrypted frame, then the 16-byte frame info |

- `pn` and the frame number count from 0 per channel (`av/media_send.c` `tx_pn`, `tx_frame`). No
  parity is sent.
- **Checksum** (`av/media_send.c:18`): a u16 sum of the 20 sub-header bytes, with the checksum field
  zero, plus every payload byte.
- **Frame info** (`talkback.py` `frame_info`): u16 codec `0x8a`, u8 flags `0x02`, 9 zero bytes, then a
  u32 millisecond timestamp at `+12`. The timestamp is the wall clock in ms, low 32 bits, as today.
  The core fills it at send time, which makes it more accurate than today's, filled before the pacing
  sleep.
- Frames larger than 1024 bytes are fragmented as in `send_frame`, though talk-back never produces
  one.

## Pacing and back-pressure (R26, R28)

- The caller's `send()` pushes **plaintext** into a bounded queue (`talk_queue`, default 10 frames =
  200 ms) and blocks while it is full, with the GIL released.
- The reactor has a 20 ms **send deadline**. On each tick it pops one frame, encrypts it, and sends
  it. The next deadline is `previous + 20 ms`, not `now + 20 ms`, so timing doesn't drift.
- If the queue is empty at a tick, nothing is sent and the clock **re-arms from the next frame's
  arrival**. That matches `talkback.py`'s `MAX_LAG_S` rule (restart the clock rather than burst after
  a stall).
- Encrypting on the reactor costs about 2 µs per frame.
- `close(drain=True)` (the default when media ends normally) sends what is queued first, at pace.
  `close(drain=False)` (a stop request) discards it.
- The camera ACKs our audio. We never resend audio; its ACKs only feed counters (`frames_sent`,
  `bytes_sent`, `frames_dropped_queue`). This matches today, where `avServResetBuffer` is a no-op
  (`av/talkback.c:149`) and nothing is resent.

## Errors

| Event | Result |
|---|---|
| No login by the accept deadline | `accept()` raises `Timeout`; the channel is freed |
| No free channel | `talkback()` raises `Capacity` |
| Link lost while talking | `send()`/`accept()` raise the link's `Closed(reason)` |
| Key exchange fails | `talkback()` raises `Protocol` |

## Size estimate

About 350 lines including tests.
