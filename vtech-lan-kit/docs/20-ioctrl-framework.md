# 20 — The IOCtrl request/response model

All control and telemetry is carried by two SDK calls on the AV channel:

```
avSendIOCtrl(av, ioType, buf, len)                 -> send a message of type ioType with payload buf
avRecvIOCtrl(av, &ioType, buf, bufMax, timeout_ms) -> receive the next message; ioType is filled in
```

An IOCtrl message is a numeric **opcode** (`ioType`) plus a little-endian binary payload.

## Request/response pairing

- A **request** opcode `0xNNN` is answered by a **response** opcode `0xNNN+1` carrying the reply payload
  (e.g. GET `0x733` → reply `0x734`; SET `0x715` → ack `0x716`). The reference client relies on this
  pairing.
- Exceptions: the status-LED GET `0x709` is answered by `0x710` (not `0x70a`), and the daylight-saving
  GET `0x729` by `0x730` (not `0x72a`) (see `30-opcode-reference.md`). Both look like a decimal-style
  increment applied to hex. Implementations should list each reply opcode explicitly rather than
  computing request + 1.
- **Control/SET** opcodes either return a short ack (`req+1`) or are fire-and-forget (no reply).
- **GET** opcodes return a reply payload that decodes to named fields.
- **Pushes** are camera-initiated messages with no preceding request; they arrive interleaved with
  everything else and must be handled by reading `avRecvIOCtrl` in a loop and dispatching on `ioType`.

Because responses and pushes are interleaved on one channel, a caller that wants a specific reply should
read messages in a loop and match on the expected response opcode, dispatching or ignoring unrelated
messages that arrive first.

## Encoding conventions

- Integers are 32-bit little-endian unless stated. `word0` = payload bytes `@0..3`, `word1` = `@4..7`, …
- Many VTech GET requests take a 4-byte little-endian argument of `0` (a reserved/channel word).
- Standard TUTK stream/audio requests take an 8-byte payload whose `word0` is the channel (`0`).

## First IOCtrl on a new AV channel

**`IOTYPE_INNER_SND_DATA_DELAY`** (`0x0ff`) is conventionally the **first** IOCtrl on a new AV channel,
before any application command. The payload is 4 bytes, `int32` `0`, sent once
per channel, with no reply.

**The SDK refuses it from application code:** on SDK 4.3.8.1, `avSendIOCtrl(av, 0x0ff, …)` returns
`AV_ER_NOT_SUPPORT` (`-20033`), on both x86_64 and in Home Assistant's musl image. Every later IOCtrl, the
live-view key exchange and streaming work without it. It is an `IOTYPE_INNER_*` (SDK-internal) type,
most likely sent by the SDK itself. A client may send it once after
`avClientStartEx` and must treat `-20033` as harmless, or skip it.

## Required prep before the VTech GETs reply

The VTech vendor GET opcodes (the `0x7xx` / `0x74x` / `0x75x` / `0x73x` families) only return a reply
**after** a device-info request has been sent once on the session. Send `DEVINFO` (`0x330`, see
`70-device-info.md`) once after the AV channel is up, then issue the VTech GETs. Standard TUTK opcodes
(stream control, motion detect, audio format, device info itself) reply without this prep.

## The opcode object model (reference client)

The reference client represents each opcode as an object bundling: the numeric code, a name, an argument
encoder (arguments → request bytes), and, for opcodes with a reply, the response opcode + a decoder
(reply bytes → named fields). Two operations:

- `Op.send(t, av, *args)` — encode and fire (control/SET or fire-and-forget).
- `Op.request(t, av, *args)` — send, wait for the paired response, and return the decoded dict.

The full set is enumerated in `30-opcode-reference.md`.
