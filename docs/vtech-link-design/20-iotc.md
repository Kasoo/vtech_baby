# 20 — The IOTC layer

Serves R1, R3–R5, R7–R9, R40. Wire authority: `tutk-kit/docs/iotc/15-wire-format.md`,
`16-handshake.md`, `17-inbound.md`, `30-data.md`, `40-discovery.md`. Reference code: `tutk-host/iotc/`.

The IOTC layer turns a UID and authKey into an established session with a peer address, then carries
numbered channel payloads both ways, keeps the session alive and closes it. On the LAN-direct path it
is small: six outbound datagram types and four inbound ones.

## Module layout (`core/src/iotc/`)

| Module | Contents |
|---|---|
| `scramble.rs` | The keyed whitening, its inverse, the fixed key. Pure functions over `&mut [u8]`. |
| `codec.rs` | `Header` encode/parse; builders for the probe, record, connect ack, data, keepalive and close; the `0x602` parser. |
| `session.rs` | `IotcSession`, the sans-IO state machine. |

## Constants

| Name | Value | Source |
|---|---|---|
| `MAGIC` | `0x0204` (bytes `04 02`) | `iotc/wire.c:136` |
| `OUR_VERSION` | `0x1d` (header `+0x02`) | `iotc/iotc.h` `IOTC_VERSION` |
| `KEY_AUTH_MIN_VERSION` | `0x17` | `iotc/inbound.c:12` |
| `FLAG_PE`, `FLAG_SCRAMBLED`, `FLAG_SUBHEADER` | `0x01`, `0x02`, `0x08` | `iotc/iotc.h` |
| `PE_PREFIX` | `0x40` | `iotc/iotc.h` |
| `MAX_PAYLOAD` | `0x56c` (1388) channel payload bytes per datagram | `iotc/iotc.h` |
| `MAX_DATAGRAM` | 2048 received | `iotc/inbound.c:6` |
| Opcodes out | `0x0601` probe, `0x0402` connect ack, `0x0407` data, `0x0427` keepalive, `0x0417` close | `iotc/iotc.h` |
| Opcodes in | `0x0602` LAN reply, `0x0408` data, `0x0428` keepalive, `0x0418` close | `iotc/iotc.h` |
| `SDK_VERSION` | `0x040308c8` (4.3.8.200) | R40 |

## The scramble (`scramble.rs`)

Bit-exact port of `iotc/wire.c:33–121`. The transform, its permutation and the tail reorders are
specified in `tutk-kit/docs/iotc/15-wire-format.md` ("The scramble transform").

- `scramble(buf: &mut [u8], prefix: usize)` and `unscramble(buf, prefix)` work **in place** on the
  first `min(prefix, len)` bytes, in 16-byte blocks plus a tail.
- The inverse permutation is computed at **compile time** (`const fn` building the 128-entry map the
  way `build_inverse` does at `wire.c:50`), so there is no `pthread_once` equivalent and no runtime
  table to initialise.
- **The key** is the constant `"Charlie is the designer of P2P!!"[0..16]` with no mask. In
  `tutk-host`, `iotc_build_key` (`wire.c:124`) XORs in `GetLicenseKeyMask`. Our licence's JSON has no
  `keyMask` member, so the mask call fails and the bare constant is used (kit, "The scramble key"). We
  embed the constant. A one-off test pins it: it decodes the licence with the current `tutk-host`
  build and asserts there is no `keyMask` (`85-testing.md`, T-IOTC-3). If a future licence ever
  carried a mask, the key would become a config value; we don't build for that.
- **Performance**: the block step is a fixed bit permutation. A straightforward port does about
  40 shifts and masks per block, and a 1,100-byte media datagram under partial encryption touches only
  4 blocks. Nothing more is needed.

## The datagram header (`codec.rs`)

```rust
struct Header {               // 16 bytes
    version: u8,              // +0x02: 0x1d out; the camera sends 0x1b
    flags: u8,                // +0x03
    length: u16,              // +0x04: body bytes after the 16-byte header (sub-header included)
    seq: u16,                 // +0x06: per-channel send sequence on data, else 0
    opcode: u16,              // +0x08
    field_a: u16,             // +0x0a: 0x0021 on everything we send
    token16: u16,             // +0x0c: low 16 bits of our token on data, else 0
    channel: u8,              // +0x0e: data only
    field_f: u8,              // +0x0f: datatype (& 3) on data; 1 on the probe
}
```

Every outbound datagram gets `flags |= FLAG_SCRAMBLED` and is scrambled over its whole length, or over
`PE_PREFIX` bytes when `FLAG_PE` is set, just before it is handed to the socket
(`iotc/handshake.c:43` `iotc_send_to_peer`). The **connect record** is the one exception: it is never
scrambled.

**Inbound** (`iotc/inbound.c` `iotc_on_datagram`): drop datagrams under 16 or over 2048 bytes. Copy
the datagram, unscramble the first 16 bytes and drop it unless `+0x00` is `0x0204`. Unscramble the
rest of a fresh copy over `0x40` bytes if `flags & FLAG_PE`, else over the whole length. Then dispatch
on the opcode. Every other opcode is counted as ignored and dropped (today it is traced at
`inbound.c:150`).

## Outbound datagrams, byte by byte

All built in a fixed `[u8; N]` on the stack, then scrambled.

**Discovery probe**, connect form, 88 bytes (`iotc/handshake.c:14`, `iotc/wire.c:145`):

| Offset | Value |
|---|---|
| `+0x00` | header: magic, version `0x1d`, flags `0` (→ `0x02` when scrambled), length `0x48`, opcode `0x0601`, field_a `0x0021`, `+0x0f` = `0x01` |
| `+0x10` | UID, 20 bytes, uppercased, NUL-padded |
| `+0x34` | u32 `SDK_VERSION` |
| `+0x38` | our token, 8 bytes |
| `+0x40` | u32 `1` |
| `+0x4a` | authKey, 8 bytes raw |

**Connect record**, 52 bytes, **not scrambled** (`iotc/handshake.c:24`):

| Offset | Value |
|---|---|
| `+0x00` | u16 `0xcc51` |
| `+0x04` | u8 `2` (version), `+0x05` u8 `0x10` (LAN command), `+0x06` u16 `0x0028` |
| `+0x10` | our token, 8 bytes |
| `+0x18` | u32 `SDK_VERSION`, `+0x1c` u8 `0x1d` |
| `+0x20` | HMAC-SHA1 (20 bytes), key = UID padded to 20 bytes with NUL, then the 8 authKey bytes (28 bytes); data = bytes `+0x00..+0x20` |

**Connect ack** `0x0402`, 52 bytes (`iotc/handshake.c:54`): header with length `0x24`, opcode
`0x0402`, field_a `0x0033`; UID at `+0x10`; token at `+0x24`; u32 `SDK_VERSION` at `+0x30`. Every other byte
is zero; `tutk-host` leaves `+0x2c..0x2f` uninitialised (`TODO.md`), and the fixture test masks them.

**Data** `0x0407`, 28 + n bytes (`iotc/data.c` `IOTC_Session_Write_Data_And_Datatype`): header with
flags `0x08 | (pe ? 0x01 : 0)`, length `12 + n`, seq = the channel's send counter (post-increment,
wraps at 16 bits), opcode `0x0407`, field_a `0x0021`, token16 = token bytes 0–1, channel, field_f =
datatype (0 data, 1 DTLS record). Then the 12-byte sub-header: u32 `12`, then the 8-byte token. Then
the payload, at most `MAX_PAYLOAD`.

**Keepalive** `0x0427` and **close** `0x0417`, 24 bytes each (`iotc/handshake.c:67`): header with flags
`0x08`, length `8`, field_a `0x0021`; body = the token.

## The `0x602` reply (`codec.rs` `parse_lan_reply`)

From `iotc/inbound.c:67` (`handle_lan_reply`); kit `17-inbound.md`.

- **Long form**: header `length ≥ 0xb8` and datagram `≥ 0xc5` bytes. The token is at `+0xbc` (8
  bytes), the result at `+0xc4`, the camera's IOTC version (u32) at `+0x30`, its protocol version at
  header `+0x02`. `pe_capable = flags & FLAG_SCRAMBLED`.
- **Short form**: `0x24 ≤ length < 0xb8`, datagram `≥ 0x24` bytes. The camera's UID is at `+0x10`.
  This is the no-auth "device found" reply.

```rust
enum LanReply<'a> {
    Long { token: [u8; 8], result: u8, proto_version: u8, iotc_version: u32, pe_capable: bool },
    Short { uid: &'a [u8; 20], proto_version: u8 },
}
```

## The session state machine (`session.rs`)

```
            connect()                     long 0x602, token matches, result 1|7
  Idle ───────────────▶ Searching ──────────────────────────────────────────▶ Established
                          │  every 100 ms: broadcast record + probe            │
                          │  deadline: → Failed(Unreachable)                   │ 0x418 → Closed(RemoteClose)
                          │  long 0x602 result 3|4|5|6 → Failed(Rejected)      │ 61 silent ticks → Closed(RemoteTimeout)
                          │  short 0x602, our UID → Failed(NoKeyAuth)          │ close() → send 0x417 ×3 → Closed(Local)
                          ▼                                                    ▼
                        Failed                                               Closed
```

**State held**: UID (uppercased), authKey, token (8 random bytes per connect,
`iotc/session.c:150`), the peer address once known, the camera's versions, `pe_capable`/`pe_on`,
per-channel send counters `[u16; 32]`, `channel_open: u32` bitmask (bit 0 always set), the silence
and keepalive tick counters, and the next search and alive deadlines.

**Searching.**
- On entry, and every `search_interval` (100 ms), queue the record and then the scrambled probe to
  each **directed broadcast address** at `search_port` (`iotc/discovery.c:63`, `40`
  `Broadcast_Message_By_Netmask`). The driver supplies the list of on-link IPv4 interfaces
  (`70-runtime.md`). Skip loopback and interfaces with a zero address or netmask. **Change from
  today:** Home Assistant's container hosts carry container bridges (seen in the trace: `10.89.x.1/24`),
  and today we broadcast into those too. The driver orders interfaces with the default-route
  interface first, and the config may name the interface. All on-link interfaces stay the default,
  because the camera can be on any of them.
- **Optional unicast hint** (new): if the config has the camera's last known IPv4 address (the
  integration knows its MAC and can read the neighbour table; `manager.py` `mac_present`), also send
  the record and probe there each round. The kit says the connect-path probe works unicast too
  (`15-wire-format.md`). This makes discovery work if broadcasts are filtered, and costs nothing.
- **On a long `0x602` whose token matches ours:**
  - result `1` or `7`: if the camera's version byte `< 0x17` (no key auth), fail `NoKeyAuth`.
    Otherwise record the peer's address and port **from the datagram's source**, the versions and
    `pe_capable`. Send the connect probe and the `0x0402` ack **unicast** to the peer, in that order
    (`iotc/handshake.c:54`). Enter `Established`.
  - result `3` → `MaxSessions`, `4` → `Port`, `5` → `AuthKey`, `6` → `NoKeyAuth`, any other →
    `Other(result)` (`iotc/inbound.c:50`; today an unlisted result is ignored and the connect times
    out, which hides it).
- **On a short `0x602`** whose UID matches ours: fail `NoKeyAuth` (`inbound.c:94`), since we always
  have an authKey.
- Datagrams from any address are accepted in this state (we don't know the camera's address yet).
  The token or UID match is the filter.
- At the connect deadline: `Failed(Unreachable)`.

**Established.**
- Only datagrams **from the peer's address and port** are processed; others are counted and dropped.
  This is new: today the shared socket matches by token alone. The token check stays as well.
- **`0x0408` data** (`iotc/inbound.c:107` `handle_data`): if `flags & FLAG_SUBHEADER`, skip the
  12-byte sub-header, and its token (at `+0x14`) must equal ours. Otherwise `+0x0c` must equal our
  token's first 2 bytes. The payload length is `length − 12` (or `length`), and must fit in the
  datagram. Reset the silence counter. Emit `Data { channel: +0x0e, datatype: +0x0f & 3, seq: +0x06,
  payload }`. Drop data for a channel that is not open (today it would queue it unread).
- **`0x0428`** with our token: reset the silence counter.
- **`0x0418`** with our token: `Closed(RemoteClose)`.
- **Keepalive timer**, every `alive_interval` (1 s) (`iotc/init.c:89`): increment the silence counter.
  If it exceeds 60, close with `RemoteTimeout`. Otherwise send `0x0427` if the counter is ≥ 5, or
  on every other tick. That gives about 2 s while the camera is talking and 1 s once it has been
  quiet for 5 s.
- **`send(channel, datatype, payload)`**: build and queue a data datagram as above.
- **`set_partial_encryption(on)`**: allowed only if `pe_capable`. Today `IOTC_sCHL_new` turns it on
  for the whole session when the DTLS channel starts (`iotc/dtls.c:233`), so every later datagram on
  every channel scrambles only its first `0x40` bytes. We keep that: `AvClient` turns PE on when it
  starts DTLS on channel 0. Whether the camera accepts fully scrambled data as well is untested and
  doesn't matter (`95-open-questions.md` Q7).
- **`close()`**: queue three `0x0417` datagrams back to back (`iotc/handshake.c:78`), then
  `Closed(Local)`. The driver flushes them before it exits.

**Channels.** `open_channel(ch)` sets bit `ch`. `free_channel()` returns the lowest clear bit in
1–31 and sets it (`iotc/channels.c`). `close_channel(ch)` clears the bit and its send counter
(`iotc/channels.c` `IOTC_Session_Channel_OFF`). Channel 0 is the AV client channel. Talk-back takes
`free_channel()`.

**Not carried over**: the per-channel receive queues, the `rx_index`/`rx_count`/`chan_rx` lost-packet
bookkeeping (`iotc/data.c`; the AV layer never reads the "lost" flag), the read-abort flag, and the
128-slot table.

## Errors

| Event | `LinkError` |
|---|---|
| Search deadline | `Unreachable` |
| Long `0x602`, result 3/4/5/6/other | `IotcRejected(MaxSessions/Port/AuthKey/NoKeyAuth/Other)` |
| Short `0x602` for our UID, or camera version `< 0x17` | `IotcRejected(NoKeyAuth)` |
| `0x0418` | `Closed(RemoteClose)` |
| 61 silent ticks | `Closed(RemoteTimeout)` |
| Socket send error other than `WouldBlock`, or a refused off-link send to the peer | logged and counted; not fatal by itself (the silence timer decides) |

## Status

**Built 2026-10-22** (`core/src/iotc/`), with the tests T-IOTC-1 to 5 (`core/tests/iotc.rs`, and the
unit tests in `scramble.rs`). Every outbound datagram is byte-identical to `tutk-host`'s, from the
fixtures in `core/tests/fixtures/tutk_host.txt` (`tools/fixtures/gen.sh`), apart from the ack bytes
above.

## Size estimate

About 600 lines including tests: scramble 150, codec 250, session 200.
