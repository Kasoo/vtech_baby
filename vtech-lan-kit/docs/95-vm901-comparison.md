# 95 — Comparison with the VM901 bridge (`vtech-vm901-home-assistant`)

An independent project, [`Termina1/vtech-vm901-home-assistant`](https://github.com/Termina1/vtech-vm901-home-assistant)
(reviewed at commit `383375e`, 2026-08-26), implements a go2rtc bridge for the **VTech VM901** and
advertises "VTech X25519/ChaCha20-Poly1305" media encryption. This note compares its implementation
against the scheme specified in `50-media-decryption.md`.

**Conclusion: it is the same protocol.** Every step of the application-layer crypto matches this spec
byte for byte. The VM901 and the RM5754HD share VTech's live-view media encryption.

Sources on the VM901 side are its `bridge/vtech_native.c` (handshake at lines ~738–814, frame decrypt
at ~204–237, post-handshake burst at ~815–880) and `docs/architecture.md`. Sources on our side are
`50-media-decryption.md`, `40-media-streaming.md` and `30-opcode-reference.md`.

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

The stream bring-up also matches: they independently found the `0x719` full-rate trigger (their
"HD trigger": without it the stream is ~1 fps keyframes only, as described in `40-media-streaming.md`),
and send `0x7be`, `0x300` and `0x1ff` in the same order as our connect burst.

## Differences

### Their docs overstate the key derivation

`docs/architecture.md` describes an "HMAC-SHA256/HKDF-style derivation" and "a VTech-specific media key
schedule using the `VTech-video-key` context". The code never uses that string and performs no
HKDF-Expand step: it stops at the HMAC, exactly as specified here. Either the context string was lifted
from app strings and is unused, or it is a leftover hypothesis. Their working code agrees with this
spec, not with their prose.

### Our coverage is wider

- **Fully-offline provisioning.** The VM901 bridge bootstraps credentials from VTech's cloud (2FA, then
  a devices API call) and captures the authKey from the app's own connect. We derive the entire
  credential set offline from the pairing QR — UID + AV password decode out of the QR and the authKey is
  an HKDF of the AV password (`05-provisioning.md`) — with no cloud, no account and no capture, and it
  self-heals on rotation by re-scanning the QR.
- **Talk-back.** We specify and implement uplink encryption (random `frame_key`, sent wrapped; see
  `50-media-decryption.md` and `40-media-streaming.md`). The VM901 bridge only receives.
- **Round 1.** We document the optional empty round-1 `0x820` that is answered with `-1` (not ready).
- **`0x328` GETSUPPORTSTREAM.** On the RM5754HD it gets no reply (`30-opcode-reference.md`). The VM901
  bridge still sends it twice, copying the official app.
- **Control and telemetry.** We specify the full opcode set. The VM901 bridge sends `0x74b`, `0x7a4`,
  `0x7b0`, `0x733` and `0x7d6` as unnamed "unknown" opcodes because the app sends them.

### Runtime differences

| | VM901 bridge | This kit |
|---|---|---|
| Native libraries | Android/bionic ARM64 SDK in a bionic chroot inside Docker | Patched SDK loaded on a host glibc or musl, x86_64 or aarch64 (`../tooling/`) |
| Crypto implementation | C, libsodium resolved via `dlsym`, driven from Go/cgo | Pure Python in `../client/vtech_camera_client.py` |
| Credentials | UID and AV password fetched from VTech's cloud API after a one-time 2FA bootstrap | UID and AV password decoded offline from the pairing QR — no cloud, no 2FA (`05-provisioning.md`) |
| authKey | Needed; recovered by capturing the app's `IOTC_Connect_ByUIDEx` call | Needed; **derived** on the client by HKDF from the AV password — never captured (`05-provisioning.md`) |
| Output | ffmpeg → go2rtc (RTSP/WebRTC/HLS) | MP4/raw stream from the reference client; HA integration planned |

### Audio codec

The VM901 authors found the SDK's audio codec identifier inconsistent with the A-law/µ-law payloads
they observed across firmware, and made the codec configurable (`VTECH_AUDIO_CODEC`). On the RM5754HD
we see a consistent `0x8a` (G.711 A-law). An implementation targeting other models should not trust the
codec ID blindly.

## Implications for this kit

- The VM901 bridge is independent confirmation, from a different model, that the crypto in
  `50-media-decryption.md` is correct and not specific to the RM5754HD.
- The scope note in `../README.md` ("untested against any other model") remains true of the kit as a
  whole, but the live-view crypto specifically has now been independently implemented for the VM901.
