# 60 — Camera services on the AV channel

Serves R10–R18, R26. Reference: `vtech_lan/session.py` (waiters, dispatch, `live_view_master`),
`vtech_lan/crypto.py`. Wire authority: `docs/vtech-lan-kit/docs/20-ioctrl-framework.md`,
`50-media-decryption.md`.

This layer sits between the AV client and the API. It turns control messages into replies and pushes,
and frames into plaintext. It knows exactly two opcodes, `0x820` and `0x821`, both for the key
exchange. Every other opcode, including the talk-back announcements `0x350`/`0x351`, is just a number
to it.

## Request/reply matching (`core/src/service/requests.rs`)

State, on the reactor:

```rust
struct Requests {
    by_reply: HashMap<u32 /*reply type*/, VecDeque<Waiter>>,  // head = active, rest queued
}
struct Waiter { id: u64, completion: Arc<Completion>, multi: bool, deadline: Instant }
```

- `request(type, payload, reply_type, timeout)`: if no waiter is active for `reply_type`, the new one
  becomes active and its frame goes on the control stream. Otherwise it **waits its turn**: its frame
  is not sent until the waiter ahead of it finishes. That is how `session.py` behaves today, with one
  lock per reply opcode (`_reply_locks`). It keeps replies unambiguous, because the camera's replies
  carry no request id (R12).
- An inbound `Control { type, payload }`: if a waiter is active for `type`, it takes the payload.
  - Single reply: the waiter completes, and the next queued waiter for that type starts.
  - Multi-reply (`exchange`, R13): the payload is appended to the waiter's inbox. The waiter stays
    active until the caller calls `Exchange::close()` or its deadline passes.
  - Otherwise the payload goes to the **push queue** (R14).
- A deadline that passes completes the waiter with `Timeout`, and the next queued waiter starts. A
  reply that arrives later goes to the push queue, which `session.py` then logs at debug as an unhandled
  IOCtrl. That matches today.
- Losing the link completes every waiter with the link's error.

`send(type, payload)` (no reply) completes when the control stream reports the frame acknowledged
(R10). `request()` does not wait for the acknowledgement separately: the reply implies it.

## The push queue

Bounded (default 256). It holds `(type, payload, arrived)` in arrival order. When full, the **oldest**
push is dropped and counted (`pushes_dropped`). A consumer that has stopped reading should not block the
reactor. In practice the Python reader thread drains it continuously.

The keepalive GET (`0x733` every 60 s) stays in Python (`10-architecture.md`, "What moves where").

## The live-view key exchange (`core/src/service/media_key.rs`)

Port of `crypto.py` `KeyExchange` and `Session.live_view_master`, kit `50-media-decryption.md`:

1. Generate an x25519 secret from the `Rng`; `hello = u32 1 || our public key` (36 bytes).
2. Send it as a request: type `0x820`, reply `0x821`, timeout 5 s. This goes through the same
   request matcher, so a caller's own `0x821` request would queue behind it.
3. Reply: at least 76 bytes; i32 status at `+0` must be 0. The camera's public key is at `+12..+44`
   and the salt at `+44..+76`, and neither may be all zero. Otherwise fail with
   `Protocol("0x821 status N")`.
4. `master = HMAC-SHA256(key = salt, msg = X25519(secret, camera_public))`. It is stored in the link's
   `Zeroizing<[u8; 32]>`, never leaves the core, and is never logged.

It runs **once per link**, on first need: `MediaReceiver::start` or `Link::talkback`. Concurrent first
needs share one exchange (the second caller waits on the first's completion). The secret is dropped
once the master is computed.

## Frame crypto (`core/src/service/frame_crypto.rs`)

Port of `crypto.py`:

```
frame     = wrapped_key[32] || nonce[12] || tag[16] || body
frame_key = wrapped_key XOR ChaCha20(key = master, nonce, counter 0)[0..32]
plaintext = ChaCha20-Poly1305(frame_key).decrypt(nonce, body || tag, aad = none)
```

- **Decrypt** runs on the **consumer's** thread inside `MediaReceiver::next()`, not on the reactor
  (`10-architecture.md`). A frame under 60 bytes, or one that fails its tag, is counted
  (`decrypt_failed_*`) and skipped; `next()` carries on to the next frame. That matches `media.py`'s
  `bad` counter, and the loss marker of the following frame records it.
- **Encrypt** (talk-back) runs on the reactor at send time: a fresh random `frame_key` (32) and
  `nonce` (12) per frame; `ct || tag = ChaCha20-Poly1305(frame_key).encrypt(nonce, plaintext)`; the
  output is `(frame_key XOR keystream) || nonce || tag || ct`.
- The ChaCha20 keystream for the unwrap comes from the `chacha20` crate, with `ChaCha20::new(master,
  nonce)` and `apply_keystream` over 32 bytes. That replaces `crypto.py`'s Python XOR loop.
- The AEAD is `chacha20poly1305::ChaCha20Poly1305`.

## Consumer-facing frame

```rust
pub struct Frame {
    pub kind: Kind,               // Audio | Video
    pub key: bool,                // video kind 5/6
    pub number: u32,              // the shared frame number
    pub arrived: Instant,         // when the last fragment arrived (for PTS; avmux uses arrival time)
    pub lost_before: u32,         // frames given up or dropped in this stream since the previous one
    pub data: Vec<u8>,            // plaintext: an Annex-B access unit, or A-law
}
```

`key` is the media kind. `media.py` today finds keyframes by scanning for NAL type 5 (`is_keyframe`).
It can keep doing that. Whether kind 5 always means an IDR has not been checked
(`95-open-questions.md` Q9).

## Status

**Built 2026-10-22** (`core/src/service/`, `core/src/rng.rs`), with `core/tests/service.rs`. The key
exchange and frame crypto are checked against vectors generated from `vtech_lan/crypto.py` (fixed
secrets, salt, frame key and nonce): the same `hello`, a frame `crypto.py` encrypted decrypts, and our
encryption is byte-identical to its layout. The request matcher is sans-IO (`RequestEvent::Send`,
`Reply` and `Done`, for the runtime to turn into completions). The deadline counts from the call, so it
covers waiting for a turn as well. The `0x821` status goes to the log, since `LinkError::Protocol`
carries static text only.

## Size estimate

About 450 lines including tests.
