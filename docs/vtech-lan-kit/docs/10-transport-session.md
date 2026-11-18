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

## Security modes

A camera is in one of two security modes; it is a per-camera state, not fixed by the model, and it governs
both the IOTC connect and the AV login:

- **Enhanced** — the IOTC connect requires the per-camera authKey, and the AV login is DTLS-PSK
  (`security_mode = 1`). Credentials come from the pairing QR / cloud (`05-provisioning.md`,
  `06-cloud-credentials.md`).
- **Plain** — the camera uses **no key authentication** and **no DTLS**. The IOTC connect carries no authKey,
  and the AV login runs in plain security mode (`security_mode = 0`). The only per-camera secret is the AV
  password.

**Selecting the mode — from the QR.** The pairing QR encodes each camera's mode (`05-provisioning.md`): a
**version-1 QR is plain** (connect with no authKey, plain login, the QR token as the AV password); a
**version-5 QR with a type-5 credential is enhanced** (authKey + DTLS), and a **type-4 credential is plain**. Pick
the connect path from the QR; no probing is needed.

**The IOTC connect result code** (in the camera's long connect reply): **1 or 7 = proceed** (identical for both
modes — it does not identify the mode); **5 = wrong or absent key** for an enhanced camera (surfaces as `-68`);
**6 = `IOTC_ER_DEVICE_NOT_USE_KEY_AUTHENTICATION` (`-69`)** — the client presented an authKey but the camera does
**not** use key authentication. Result 6 is **persistent** (it is provoked by sending a key, not by the camera
still booting), so a client that keeps resending the key loops until timeout. The correct response to result 6 is
to **drop the authKey and reconnect without one** — which is the plain path. (A client that already knows from the
QR that the camera is plain connects keyless from the start and sees result 7 directly.)

## Call sequence

```
TUTK_SDK_Set_License_Key(license_key)          -> 0
IOTC_Initialize2(0)                            -> 0
IOTC_Set_LanSearchPort(32762)                  -> 0   (process-wide; before or after Initialize2)
avInitialize(16)                               -> >0 (max channels)
sid = IOTC_Get_SessionID()                     -> >=0

# enhanced camera:
ses = IOTC_Connect_ByUIDEx(uid, sid, &connect_input)   -> >=0 (session id) | -68 on wrong/absent authKey
av  = avClientStartEx(&av_in, &av_out)                 -> >=0  (av_in.security_mode = 1, DTLS)

# plain camera:
ses = IOTC_Connect_ByUID(uid, sid)                     -> >=0 (session id), no authKey supplied
av  = avClientStartEx(&av_in, &av_out)                 -> >=0  (av_in.security_mode = 0, plain)
```

A keyless connect against an **enhanced** camera fails. After `avClientStartEx`, the IOCtrl framework,
device-info prep and opcodes work identically in both modes (confirmed in plain mode by device-info and
parameter reads). Whether **media streaming, talk-back and the live-view key exchange** behave identically in
plain mode is **unconfirmed**: the key exchange is application-level and independent of the AV-channel security,
so it is expected to carry over, but it has not been exercised in plain mode.

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

The remaining bytes are zero. This struct carries the authKey and is used for an **enhanced** camera; a
connect with no/incorrect authKey against an enhanced camera returns `-68`.

For a **plain** camera, no authKey is supplied: connect with `IOTC_Connect_ByUID(uid, sid)` (no input struct).
The keyless connect probe carries an all-zero authKey field and no key-authentication record; the camera
establishes on result 1/7 (see "Selecting the mode" above). Whether presenting an authKey to a plain camera is
rejected outright, or merely ignored, is unconfirmed.

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
| `@0x24` | security_mode | `1` (DTLS, enhanced) · `0` (plain) |
| `@0x28` | auth_type | `0` (password) |
| `@0x2c` | sync_recv_data | `0` |
| `@0x30` | dtls_cipher_suites pointer | `NULL` |
| `@0x38` | uint32 | `0` |
| `@0x3c` | uint32 | `0` |

`security_mode` selects the AV-channel security: `1` for a DTLS (enhanced) login, `0` for a plain login. The
output struct returns the negotiated server type and security mode; the output `security_mode` echoes the one
in force (`1` DTLS, `0` plain). The account (`admin`) and AV password are supplied the same way in both modes.

## The DTLS channel

This section applies to an **enhanced** camera. A **plain** camera does not support DTLS: an AV login with
`security_mode = 1` against it is refused (the camera reports it does not support DTLS), and the plain login
(`security_mode = 0`) must be used instead. In plain mode the AV channel carries IOCtrl and media without the
DTLS record layer below; the login is authenticated by the account and AV password alone.

**Plain-mode login on the wire** (for a reimplementation that does not use the SDK): after the IOTC session is
established, the login is sent as ordinary IOTC data (datatype 0 on channel 0), not inside a DTLS record. The
login datagram carries the account (`admin`) and the **AV password in plaintext** — there is no `SHA-256` of the
password and no PSK — so in plain mode the password is protected only by the reversible IOTC scramble. The
camera returns the login reply as a class-0 data message (sub-type `0x21`) with a status byte — `0` = accepted.
A wrong password is an **explicit** `AV_ER_WRONG_VIEWACCorPWD` (`-20009`) (a non-zero status in that reply), not
a silent handshake failure; the exact non-zero reject value in plain mode is not yet characterised. A client
that sends a plain (status-byte) login this way receives the `0x21` reply **directly** — there is no
"recv dtls data!" data preamble (that preamble is seen only with the DTLS-style login variant, not the plain
login). Partial encryption on the client's outbound data frames is **optional** (either the whole datagram is
scrambled, or only its first `0x40` bytes when the partial-encryption flag is set); both are accepted.

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
