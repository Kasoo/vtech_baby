# 95 — Comparison with the VM901 bridge (`vtech-vm901-home-assistant`)

An independent project, [`Termina1/vtech-vm901-home-assistant`](https://github.com/Termina1/vtech-vm901-home-assistant),
implements a go2rtc bridge for the **VTech VM901** and advertises "VTech X25519/ChaCha20-Poly1305" media
encryption. This note compares its implementation (`bridge/vtech_native.c` and `docs/architecture.md`, as
of commit `383375e`) with the scheme specified in `50-media-decryption.md`.

**Conclusion: it is the same protocol.** Every step of the application-layer crypto matches this spec
byte for byte, so the VM901 and the RM5754HD share VTech's live-view media encryption.

## Where they match

| Step | This spec (`50-media-decryption.md`) | VM901 (`bridge/vtech_native.c`) |
|---|---|---|
| Client hello | `0x820`, `word0=1` + 32-byte X25519 public key (36 B) | `0x820`, `[counter=1 : u32 LE][my_pub : 32]` (36 B) |
| Camera reply | `0x821` ≥76 B: status `@0`, int `@4`, camera public key `@12`, salt `@44` | `[status][valid][?][cam_pub:32][cam_salt:32]`, same offsets |
| Shared secret | `S = X25519(n, p)` | `crypto_scalarmult(my_priv, cam_pub)` (keypair from libsodium `crypto_kx_keypair`) |
| Session key | `master = HMAC-SHA256(key = salt, msg = S)` | `PRK = HMAC-SHA256(key = cam_salt, data = shared)`, which they call "HKDF-Extract" |
| Frame layout | `[wrapped_key 32][nonce 12][tag 16][body]` | `enc_key @0`, `nonce @32`, `tag @44`, frames ≥60 B |
| Key unwrap | `wrapped_key XOR ChaCha20-IETF(master, nonce)` | `crypto_stream_chacha20_ietf_xor(enc_key, nonce, PRK)` |
| Decrypt | ChaCha20-Poly1305-IETF, no associated data | `crypto_aead_chacha20poly1305_ietf_decrypt(..., NULL, 0, nonce, k_frame)` |
| Audio | Same layout, same `master` | "Same PRK as video", reuses `decrypt_frame()` |

Their "PRK" is our `master` under a different name.

The stream bring-up matches too: they also use the `0x719` full-rate trigger (their "HD trigger": without
it the stream is ~1 fps keyframes only, as described in `40-media-streaming.md`), and send `0x7be`,
`0x300` and `0x1ff` in the same order.

## Differences

### Their docs describe a longer key derivation than their code

`docs/architecture.md` describes an "HMAC-SHA256/HKDF-style derivation" with a `VTech-video-key`
context. The code never uses that string and performs no HKDF-Expand step: it stops at the HMAC, exactly
as specified here. Their working code agrees with this spec.

### Coverage

- **Credentials.** The VM901 bridge gets the UID and AV password from VTech's cloud (after a one-time 2FA
  bootstrap) and needs the authKey supplied separately. This kit derives the whole set offline from the
  pairing QR: UID and AV password decoded from it, authKey an HKDF of the AV password
  (`05-provisioning.md`). Re-scanning the QR recovers from a rotation.
- **Talk-back.** This kit specifies and implements uplink encryption (random `frame_key`, sent wrapped;
  `50-media-decryption.md`, `40-media-streaming.md`). The VM901 bridge only receives.
- **Round 1.** This kit documents the optional empty round-1 `0x820` that is answered with `-1` (not
  ready).
- **`0x328` GETSUPPORTSTREAM.** On the RM5754HD it gets no reply (`30-opcode-reference.md`). The VM901
  bridge sends it twice.
- **Control and telemetry.** This kit specifies the full opcode set. The VM901 bridge sends `0x74b`,
  `0x7a4`, `0x7b0`, `0x733` and `0x7d6` as unnamed opcodes. The first four are specified in
  `30-opcode-reference.md`; `0x7d6` is not characterised here.

### Runtime

| | VM901 bridge | This kit |
|---|---|---|
| Native libraries | Android/bionic ARM64 SDK in a bionic chroot inside Docker | Patched SDK loaded on a host glibc or musl, x86_64 or aarch64 (`../tooling/`) |
| Crypto implementation | C, libsodium resolved via `dlsym`, driven from Go/cgo | Pure Python (`../client/vtech_camera_client.py`; the HA integration uses `cryptography`) |
| Credentials | UID and AV password from VTech's cloud API | Decoded offline from the pairing QR (`05-provisioning.md`) |
| authKey | Supplied separately | Derived from the AV password by HKDF (`05-provisioning.md`) |
| Output | ffmpeg → go2rtc (RTSP/WebRTC/HLS) | Reference client: MP4/HLS/RTP. HA integration: an in-process MPEG-TS feed for HA's go2rtc and stream component (`90-ha-runtime.md`) |

### Audio codec

The VM901 authors found the SDK's audio codec identifier inconsistent with the A-law/µ-law payloads
they saw across firmware versions, and made the codec configurable (`VTECH_AUDIO_CODEC`). On the
RM5754HD it is consistently `0x8a` (G.711 A-law). An implementation targeting other models should not
trust the codec ID blindly.

## Implications for this kit

- The VM901 bridge independently confirms, on a different model, that the crypto in
  `50-media-decryption.md` is correct and not specific to the RM5754HD.
- The scope note in `../README.md` ("untested against any other model") still holds for the kit as a
  whole, but the live-view crypto is known to work on the VM901 too.
