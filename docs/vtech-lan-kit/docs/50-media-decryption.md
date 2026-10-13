# 50 — Live-view key exchange and per-frame decryption

Video and audio frames are encrypted at the application layer (above the SDK's DTLS transport). Real
frames are served only to a client that completes the live-view key exchange; a client that skips it
receives an unencrypted placeholder image instead of the real stream.

## Live-view negotiation (`0x820` / `0x821`)

A two-round IOCtrl exchange on the AV channel establishes the per-session key material.

**Round 1 (optional):** send `0x820` with an empty payload. The camera replies `0x821` with
`int@0 = -1` (not-ready). This round can be skipped.

**Round 2:** send `0x820` with payload `word0 = 1` followed by the **client's 32-byte X25519 public
key** (36 bytes total). The camera replies `0x821` (≥76 bytes) with:

| Offset | Field |
|---|---|
| `@0` | `int` status — `0` means GO (key exchange accepted); other values mean not-ready/rejected |
| `@4` | `int` |
| `@12` | 32 bytes — the **camera's X25519 public key** `p` |
| `@44` | 32 bytes — the **key-derivation salt** |

If `int@0` is `0` and `@12`/`@44` are non-zero, the exchange succeeded.

## Session master key

```
S      = X25519(n, p)                       # n = client X25519 private scalar, p = camera pubkey @12
master = HMAC-SHA256(key = salt, msg = S)   # salt = the 32 bytes @44
```

`master` is constant for the session. All inputs are on-wire (`0x821 @12`/`@44`) plus the client's own
ephemeral key — no external secret is required to decrypt once the session is established.

## Per-frame decryption

Each encrypted frame (video or audio) has the wire layout:

```
[ wrapped_key : 32 ][ nonce : 12 ][ tag : 16 ][ body : N ]
```

Decrypt:

```
frame_key = wrapped_key XOR ChaCha20-IETF-keystream(key = master, nonce = nonce, counter = 0)
plaintext = ChaCha20-Poly1305-IETF-decrypt(key = frame_key, nonce = nonce, ciphertext = body, tag = tag)
```

- The keystream for the unwrap is ChaCha20 (IETF, 12-byte nonce, block counter 0) under `master`, taken
  over the first 32 bytes.
- The AEAD is ChaCha20-Poly1305-IETF: 12-byte nonce, 16-byte tag appended, no associated data. A correct
  `master` and `frame_key` verify the Poly1305 tag; a bad key fails the tag.
- `wrapped_key` and `nonce` are fresh per frame; `master` is per session.
- Audio frames use the **identical** layout and the same `master`.

After decryption, video `body` is Annex-B H.264 and audio `body` is G.711 A-law (see
`40-media-streaming.md`).

## Uplink (talk-back) encryption

Audio sent *to* the camera uses the same frame layout in the reverse direction, with the same session
`master`. The sender picks a random 32-byte `frame_key` and 12-byte `nonce` for each frame, encrypts
with ChaCha20-Poly1305-IETF (no AAD), and sends `frame_key XOR ChaCha20-IETF(master, nonce)` as
`wrapped_key`. The procedure is in `40-media-streaming.md`.
