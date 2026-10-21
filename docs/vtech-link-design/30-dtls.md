# 30 — DTLS: a client for one cipher suite

Serves R2, R4, R7, R34, R36. Reference: `tutk-host/iotc/dtls.c` (OpenSSL over a custom BIO),
`tutk-host/av/init.c:112–150` (`av_chan_open`, `psk_client_cb`); kit `tutk-kit/docs/iotc/50-dtls.md`,
`tutk-kit/docs/av/10-init.md`. Standards: RFC 6347 (DTLS 1.2), RFC 5246 (TLS 1.2), RFC 5489
(ECDHE_PSK), RFC 7905 (ChaCha20-Poly1305 suites), RFC 7627 (extended master secret), RFC 8422 (X25519 in
TLS 1.2).

The AV client channel (IOTC channel 0) is DTLS 1.2 with a pre-shared key: identity
`"AUTHPWD_" + account`, key `SHA-256(password)` (`av/init.c:124`). DTLS records travel as IOTC data
with **datatype 1**. Today OpenSSL runs that handshake, offering its full default suite list. This
design replaces OpenSSL with a small sans-IO client that offers only the suite the camera picks.

## What the camera negotiates (live, 2026-10-21)

Captured with the `tutk-host` debug build, whose `TK_TRACE=2` tap logs IOTC channel payloads in clear:

```sh
cd vtech_baby/tests/live && . $CAMERA_DIR/.camera.env && TK_TRACE=2 \
  VTECH_LIVE_RUNNER=$CAMERA_DIR/shim/tests/live_run.sh ARCH=amd64 \
  PATCHED=$REPO/tutk-host/build/x86_64-musl-debug ./run.sh probe_phase1.py 5 1
```

The `tk-iotc:` lines for channel 0, decoded:

| Dir | Records in the IOTC datagram |
|---|---|
| → | `ClientHello` (record version `feff`, epoch 0 seq 0): client_version `fefd`, no session id, **no cookie**, 53 suites (OpenSSL's default list), extensions renegotiation_info, ec_point_formats, supported_groups (x25519, secp256r1, x448, secp521r1, secp384r1), session_ticket, encrypt_then_mac, extended_master_secret, signature_algorithms |
| ← | `ServerHello` (epoch 0 seq 0, msg_seq 0): version `fefd`, **suite `0xCCAC` = `TLS_ECDHE_PSK_WITH_CHACHA20_POLY1305_SHA256`**, extensions renegotiation_info, session_ticket, **extended_master_secret**. `ServerKeyExchange` (msg_seq 1): `00 00` (empty identity hint), `03` named_curve, `00 1d` **x25519**, `20` + 32-byte public key. `ServerHelloDone` (msg_seq 2). |
| → | `ClientKeyExchange` (msg_seq 1): `00 0d "AUTHPWD_admin"`, `20` + 32-byte x25519 public key. `ChangeCipherSpec`. `Finished` (epoch 1 seq 0, 40 bytes on the wire = 12-byte header + 12 verify bytes + 16-byte tag). |
| ← | `NewSessionTicket` (msg_seq 3). `ChangeCipherSpec`. `Finished` (epoch 1 seq 0). |
| ↔ | application data, record type 23, epoch 1. |

Observed: **no HelloVerifyRequest** (the camera does not use cookies); each side's flight arrives in one
IOTC datagram; the handshake takes about 80 ms. Recorded in the kit
(`docs/vtech-lan-kit/docs/10-transport-session.md`) and passed on to the TUTK kit.

## Scope

**Implemented**: the client side of one full handshake for suite `0xCCAC`, with x25519 and the extended
master secret; HelloVerifyRequest (the camera doesn't send one, but it costs 30 lines and a
standard-conformant server may); handshake retransmission; inbound handshake fragment reassembly;
application data in both directions; replay protection; inbound alerts.

**Not implemented**: other suites, other groups, certificates, signatures, session resumption and
tickets, renegotiation, encrypt-then-MAC (meaningless for an AEAD), compression, and outbound
fragmentation (every message we send is small), and sending `close_notify`. Today's client sets
quiet shutdown (`iotc/dtls.c` `SSL_set_quiet_shutdown(ssl, 1)`) and sends no alert on close; we
match that.

## Our ClientHello

```
record:     16 fe ff | 00 00 | 00 00 00 00 00 00 | len
handshake:  01 | len24 | msg_seq 00 00 | frag_off 00 00 00 | frag_len = len24
body:       fe fd                          client_version DTLS 1.2
            random[32]                     from the Rng
            00                             session_id: empty
            00 | cookie                    cookie: empty, or the HelloVerifyRequest's
            00 02 cc ac                    cipher_suites: ECDHE_PSK_WITH_CHACHA20_POLY1305_SHA256 only
            01 00                          compression: null
            extensions:
              ff 01 00 01 00               renegotiation_info, empty
              00 0a 00 04 00 02 00 1d      supported_groups: x25519
              00 0b 00 02 01 00            ec_point_formats: uncompressed
              00 17 00 00                  extended_master_secret
```

That is 4 extensions against OpenSSL's 7, and 1 suite against 53. Whether the camera accepts this
reduced hello is the first thing Phase 0 checks (`90-migration.md`, `95-open-questions.md` Q1). If it
does not, the fallback is to send OpenSSL's hello byte for byte (above) and still only *complete* the
handshake if the server picks `0xCCAC`.

## Handshake state machine (`core/src/dtls/client.rs`)

```
Start ──send CH──▶ WaitServerHello ──HVR──▶ (resend CH with cookie, reset transcript) ──┐
                         │ SH, SKE, SHD                                                  │
                         ▼                                                               │
                 send CKE, CCS, Finished ──▶ WaitServerFinished ──CCS, Finished ok──▶ Connected
                         │                        │ alert / bad Finished / timeout
                         └─────────── Failed ◀────┘
```

**Inbound handshake processing.** Records of type 22 (handshake) in epoch 0 are parsed into messages
(12-byte header: type, length u24, message_seq u16, fragment_offset u24, fragment_length u24).
Messages are reassembled by `message_seq` when fragmented, with at most 8 KiB buffered. They are
processed strictly in `next_receive_seq` order. A message with a lower seq is a retransmission: if it
belongs to the server's previous flight, we retransmit our last flight (RFC 6347 §4.2.4). A higher seq
is buffered (at most 4 messages).

- **ServerHello**: version must be `fefd`; suite must be `0xCCAC`; compression 0. Record
  `server_random`. If the extensions include `extended_master_secret`, `ems = true`. An unexpected
  extension is ignored, except `session_ticket` (we didn't offer it).
- **ServerKeyExchange**: `psk_identity_hint` (u16 length, ignored), then `03 00 1d`, then a 1-byte
  length that must be 32, then the server's x25519 public key.
- **ServerHelloDone**: empty. Now build our flight.
- **HelloVerifyRequest** (type 3, only before ServerHello): `server_version`, then the cookie (u8
  length, ≤ 255). Resend the ClientHello with the cookie and `message_seq = 1`, with the **same
  random**. RFC 6347 §4.2.1: the first ClientHello and the HelloVerifyRequest are left out of the
  transcript.
- **NewSessionTicket** (if a server sends one anyway): add it to the transcript and ignore it.
- **Finished** (epoch 1): `verify_data` must equal `PRF(master, "server finished", transcript_hash)[0..12]`,
  where the transcript includes our Finished.

**Our second flight** (one IOTC datagram, three records):

```
ClientKeyExchange  (epoch 0, msg_seq 1, or 2 after a HelloVerifyRequest)
    00 0d "AUTHPWD_admin"      psk_identity: u16 length + bytes ("AUTHPWD_" + account, at most 0x77 bytes)
    20 <our x25519 public>     ECPoint: u8 length + 32 bytes
ChangeCipherSpec   (epoch 0): 01
Finished           (epoch 1, seq 0, msg_seq + 1): PRF(master, "client finished", transcript_hash)[0..12]
```

## Key schedule

```
psk            = SHA-256(password)                                        32 bytes (av/init.c:124)
Z              = X25519(our_secret, server_public)                        reject all-zero (RFC 8422 §5.11)
premaster      = u16(32) || Z || u16(32) || psk                           RFC 5489 §2
session_hash   = SHA-256(transcript through ClientKeyExchange)            RFC 7627, when ems
master         = ems ? PRF(premaster, "extended master secret", session_hash)[0..48]
                     : PRF(premaster, "master secret", client_random || server_random)[0..48]
key_block      = PRF(master, "key expansion", server_random || client_random)[0..88]
client_write_key = key_block[0..32]     server_write_key = key_block[32..64]
client_write_iv  = key_block[64..76]    server_write_iv  = key_block[76..88]
PRF            = TLS 1.2 P_SHA256 (RFC 5246 §5)
```

**Transcript**: the SHA-256 of every handshake message in order, each as its 12-byte DTLS header (with
`fragment_offset = 0` and `fragment_length = length`) followed by its body (RFC 6347 §4.2.6). We
keep a running `Sha256` and clone it at each point where a hash is needed.

## Record layer (`core/src/dtls/record.rs`)

```
type u8 | version u16 (fefd; feff on the first ClientHello) | epoch u16 | seq u48 | length u16 | fragment
```

- **Encrypted records** (epoch 1, RFC 7905 §2): `nonce = write_iv XOR (0u32 || epoch u16 || seq u48)`,
  `aad = epoch || seq || type || version || plaintext_length`, ciphertext = ChaCha20-Poly1305
  (key = write_key), fragment = ciphertext || tag. There is no explicit nonce.
- **Outbound sequence numbers**: per epoch, from 0. The epoch-1 Finished is seq 0, so application data
  starts at seq 1.
- **Inbound**: parse every record in the datagram (the camera packs a whole flight into one). Drop a
  record whose epoch is not the current one, or that fails decryption (counted, RFC 6347 §4.1.2.7),
  or that falls outside or repeats within a **64-entry sliding replay window** (§4.1.2.6).
- **Size limit**: one IOTC payload is at most 1388 bytes, so the largest application plaintext is
  1388 − 13 − 16 = **1359** bytes. The AV layer's largest packet is 1080 bytes (a full media fragment
  with group and media headers and a 16-byte frame info), well within that.
- One AV packet = one record = one IOTC datagram on the way out.

## Timers

- **Retransmission** (RFC 6347 §4.2.4): the last flight is resent if the next server flight doesn't
  arrive within the timer. It starts at 500 ms and doubles each time, the policy `tutk-host` gives
  OpenSSL (`iotc/dtls.c:97` `dtls_timer_cb`), with no upper limit before the handshake deadline.
- **Handshake deadline**: the link's `av_timeout` (default 30 s), which today's code passes to
  `IOTC_sCHL_set_dtls_connect_timeout` (`av/init.c:152`, `av/session.c:268`). On expiry the link
  fails with `Unreachable`.

## Alerts and failures

| Event | Result |
|---|---|
| Fatal alert before our Finished (e.g. `handshake_failure` 40, `illegal_parameter` 47) | `LinkError::Protocol("dtls handshake refused")` with the alert code in the log |
| Fatal alert after our Finished: `bad_record_mac` 20, `decrypt_error` 51, or `unknown_psk_identity` 115 | `AvAuthFailed(DtlsAlert(code))`: the camera rejected our PSK, so the AV password is wrong (R4) |
| Server `Finished` mismatch | `AvAuthFailed(DtlsAlert(0))` (a PSK mismatch shows up here too) |
| `close_notify` (0) or any fatal alert once connected | `Closed(DtlsAlert(code))` |
| Suite, group or version not what we offered | `Protocol("dtls: unexpected server parameters")` |

The wrong-password mapping is a hypothesis to confirm in Phase 0, with a deliberately wrong AV
password against the real camera (`95-open-questions.md` Q2). It is read-only and safe. Today the
same case surfaces as `AV_ER_TIMEOUT` → `Unreachable` (`av/session.c:217`).

## Crypto dependencies

RustCrypto, pure Rust: `x25519-dalek` (static secret from `Rng`), `sha2`, `hmac`, `chacha20poly1305`.
The PRF is 20 lines over `hmac::Hmac<Sha256>`. Secrets (`psk`, `premaster`, `master`, write keys)
live in `zeroize::Zeroizing` buffers and are never formatted or traced (R33).

## Testing

`85-testing.md` covers known-answer tests for the PRF and key schedule, and handshakes against
`openssl s_server -dtls1_2 -psk … -cipher ECDHE-PSK-CHACHA20-POLY1305` over a loopback UDP adapter.
It also covers a replay of the captured camera flight, structure only (the randoms differ), fuzzing of
the record and handshake parsers, and loss and reorder of every flight in the simulator.

## Size estimate

About 900 lines including tests: record 200, handshake codec 250, client state machine 300,
PRF/keys 100.
