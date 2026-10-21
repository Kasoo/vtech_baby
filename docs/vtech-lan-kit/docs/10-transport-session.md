# 10 — Transport and session establishment

## Layers

The stack has three layers, all provided by the SDK libraries:

1. **IOTC** (`libIOTCAPIs.so`) — P2P session establishment to the device by UID.
2. **AV** (`libAVAPIs.so`) — an authenticated, DTLS-secured AV channel over the IOTC session; carries
   IOCtrl messages and the video/audio frame streams.
3. **Global/init** (`libTUTKGlobalAPIs.so`, `libNebulaAPIs.so`) — SDK license and init.

Everything above the AV channel (IOCtrl semantics, the live-view key exchange, per-frame decryption) is
application-level and is specified in the other docs; it does not require the SDK beyond `avSendIOCtrl` /
`avRecvIOCtrl` / the frame-receive calls.

## Call sequence

```
TUTK_SDK_Set_License_Key(license_key)          -> 0
IOTC_Initialize2(0)                            -> 0
IOTC_Set_LanSearchPort(32762)                  -> 0   (process-wide; before or after Initialize2)
avInitialize(16)                               -> >0 (max channels)
sid = IOTC_Get_SessionID()                     -> >=0
ses = IOTC_Connect_ByUIDEx(uid, sid, &connect_input)   -> >=0 (session id) | -68 on wrong/absent authKey
av  = avClientStartEx(&av_in, &av_out)         -> >=0 (AV channel index)
```

`IOTC_Set_LanSearchPort(32762)` is required for LAN discovery: the camera answers the LAN-search
broadcast on UDP 32762 only, and the SDK's default is 32761 (`00-prerequisites.md`, "Host / networking").

After `avClientStartEx` returns a non-negative AV channel index, the session is ready for `avSendIOCtrl`
/ `avRecvIOCtrl` (see `20-ioctrl-framework.md`) and for starting media (see `40-media-streaming.md`).

## `IOTC_Connect_ByUIDEx` input struct

160 bytes (`0xA0`), little-endian:

| Offset | Type | Value | Meaning |
|---|---|---|---|
| `@0x00` | uint32 | `0xA0` (160) | struct size (validated by the SDK) |
| `@0x04` | uint32 | `0` | auth type (must be 0) |
| `@0x08` | char[] | authKey, NUL-padded | the per-camera IOTC authKey (derived from the AV password — `05-provisioning.md`) |
| `@0x94` | uint32 | e.g. `10`–`15` | connect timeout (seconds) |
| `@0x98` | uint32 | `0` | trailing enum field |

The remaining bytes are zero. The camera is in enhanced-security mode, so a connect with no/incorrect
authKey returns `-68`.

## `avClientStartEx` input struct (`av_in`)

64 bytes (`0x40`), little-endian:

| Offset | Field | Value |
|---|---|---|
| `@0x00` | size | `0x40` (64) |
| `@0x04` | iotc session id | `ses` from `IOTC_Connect_ByUIDEx` |
| `@0x08` | channel (byte) | `0` |
| `@0x0c` | timeout (seconds) | e.g. `30` |
| `@0x10` | account pointer | → `admin` (or configured account) |
| `@0x18` | password pointer | → AV password |
| `@0x20` | resend | `1` |
| `@0x24` | security_mode | `1` (DTLS) |
| `@0x28` | auth_type | `0` (password) |
| `@0x2c` | sync_recv_data | `0` |
| `@0x30` | dtls_cipher_suites pointer | `NULL` |
| `@0x38` | uint32 | `0` |
| `@0x3c` | uint32 | `0` |

The output struct returns, among other fields, the negotiated server type and security mode; a
`security_mode` of `1` in the output confirms the DTLS channel.

## The DTLS channel

The AV channel's DTLS runs over IOTC channel 0 as IOTC data of datatype 1. The camera is the DTLS 1.2
server:

- **Cipher suite**: `TLS_ECDHE_PSK_WITH_CHACHA20_POLY1305_SHA256` (`0xCCAC`), with the x25519 group.
- **PSK**: identity `AUTHPWD_<account>` (e.g. `AUTHPWD_admin`); key `SHA-256(AV password)`, 32 bytes.
  The ServerKeyExchange carries an empty identity hint.
- **Other suites** the camera accepts when offered alone: `TLS_PSK_WITH_AES_128_GCM_SHA256`,
  `TLS_PSK_WITH_CHACHA20_POLY1305_SHA256` and `TLS_ECDHE_PSK_WITH_AES_128_CBC_SHA256`. It does not
  accept the PSK AES-CCM or AES-CCM_8 suites. It answers a ClientHello it can't serve with a
  `handshake_failure` alert or not at all, depending on the hello.
- **A wrong AV password** (a PSK mismatch): the camera sends its ServerHello flight, then never answers
  the client's Finished. There is no alert.
- **No HelloVerifyRequest**: the camera answers the first ClientHello directly.
- The camera agrees to the **extended master secret** when offered, and sends a NewSessionTicket
  when the client offers the session-ticket extension.
- Each side's flight arrives as one IOTC datagram holding several records.

## Session lifetime

- One AV channel (`av`) is used for all IOCtrl and media.
- The AV channel is authenticated once at `avClientStartEx`; individual IOCtrls are not separately
  authenticated.
- Closing is by the SDK's stop/close calls when finished.
