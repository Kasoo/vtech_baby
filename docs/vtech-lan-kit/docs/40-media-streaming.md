# 40 — Media streaming

## Bring-up sequence

After the AV channel is up and (for video) the live-view key exchange has completed (see
`50-media-decryption.md`), the minimal sequence to get a full-rate encrypted A/V stream is:

```
(optional) SETSTREAMCTRL 0x320  quality=2|1   # choose resolution; persists, so optional per-session
(optional) AUDIOSTART    0x300                 # only if audio is wanted
VIDEO_FULLRATE           0x719  word0=0        # select the full-rate stream
START                    0x1ff                 # begin live video
```

Notes:
- **Clean the SDK's receive buffers before every `START`** (`avClientCleanVideoBuf(av)`, and
  `avClientCleanAudioBuf(av)` when audio is wanted).
  The first frame after `START` is then a keyframe carrying SPS/PPS. Without it, a restart on the same
  session can begin with stale P-frames from the previous run.
- `STOP` (`0x2ff`) then `START` again on the same session works repeatedly; no new key exchange is needed
  (the session master holds).
- **`0x719` is required for full frame rate.** Without it the camera sends a ~1 fps keyframe-only
  preview; with it, ~10 fps with P-frames.
- `SETSTREAMCTRL` persists camera-side, so resolution need not be set every session.
- The camera caps resolution to 720p while a physical **parent handset** is active (poll `0x75a` /
  observe push `0x74f`); with the parent off it serves whatever resolution is persisted.
  The cap is applied live and lifted live: turning the handset on drops a running 1080p stream to 720p,
  turning it off restores 1080p within seconds with no write. The resolution isn't
  pushed, so re-read `0x322` after each `0x74f`. **Don't send `SETSTREAMCTRL quality=2` during the cap:**
  it is accepted, the re-read shows 720p, and after the handset goes off the camera stays at 720p — the
  capped value seems to have replaced the stored preference (inferred from behaviour).

## Receiving frames

Video frames are read with the SDK frame-receive call (`avRecvFrameData2`), which returns the frame
payload plus frame-info metadata. Audio frames are read with `avRecvAudioData`. Both payloads are
**application-layer encrypted**; decrypt each with the scheme in `50-media-decryption.md` before use.

- **Video codec:** H.264 (Annex-B NAL units after decryption), one access unit per frame. Resolution
  1280×720 or 1920×1080 per the `SETSTREAMCTRL` quality byte. A keyframe (IDR, with SPS/PPS) every ~2 s;
  ~11–14 fps at 1080p over the LAN.
- **Audio codec:** `codec_id 0x8a` = **G.711 A-law, 8 kHz, mono** (in the SDK's codec enum, `0x89` is
  µ-law, `0x8a` A-law and `0x8c` PCM). Decrypted silence is `0xD5`, the A-law zero code. Frames are
  ~40 ms / 320 bytes at ~25 fps.

## Two-way audio (talk-back)

Talk-back reverses the roles: the **client** runs an AV **server** on a second IOTC channel of the same
session, and the camera connects to it as an AV client to receive audio. Uplink frames use the **same
application-layer encryption as the downlink** (`50-media-decryption.md`), keyed by the same session master.

1. **Session master.** Complete the `0x820`/`0x821` key handshake (`50-media-decryption.md`) on the control
   channel, if it hasn't been done in this session. Live-view `START` is not required.
2. **Uplink format:** `GETAUDIOOUTFORMAT` `0x32a` (8 bytes, `int@0` = 0) → `0x32b`, 12 bytes:

   | Offset | Field | RM5754HD value |
   |---|---|---|
   | `int@4` | codec id | `0x8a` (G.711 A-law) |
   | `byte@8` | sample-rate index (`0` = 8 kHz) | `0` |
   | `byte@9` | data bits | `0` |
   | `byte@10` | channels | `0` (mono) |
   | `byte@11` | server channel (`0` = use a new channel) | `0` |

3. **Pick a channel:** `IOTC_Session_Get_Free_Channel(sid)` → `ch`.
4. **Announce it:** `SPEAKERSTART` `0x350`, 8 bytes, `int@0` = `ch`. There is no reply.
5. **Serve:** `avServStart3(sid, authFn, timeout_s, 0, ch, &resend)` blocks until the camera connects,
   then returns the uplink AV index.
   - `authFn` is `int (*)(const char *account, char *password)`, returning 1 to accept. The camera logs
     in with an empty account and password.
   - `avServStartEx` also works.
6. **Send audio:** `avSendAudioData(idx, frame, len, frameinfo, 16)` every 20 ms.
   - The plaintext is 20 ms of G.711 A-law: 160 bytes at 8 kHz.
   - `frame` wraps it exactly like a downlink frame:
     `[wrapped_key : 32][nonce : 12][tag : 16][ciphertext : 160]` = 220 bytes. Use a fresh random
     `frame_key` and `nonce` for each frame; `ciphertext‖tag` = ChaCha20-Poly1305-IETF(`frame_key`, `nonce`,
     plaintext, no AAD); `wrapped_key` = `frame_key` XOR ChaCha20-IETF keystream(`master`, `nonce`).
   - Unencrypted frames are accepted by the SDK but the camera plays near-silence.
   - `frameinfo` (16 bytes): `u16@0` = codec id, `u8@2` = flags `0x02` (mono, 8 kHz, 16-bit
     capture), `u32@12` = timestamp in ms, all other bytes 0.
   - If a send returns `AV_ER_EXCEED_MAX_SIZE` (`-20006`), call `avServResetBuffer(idx, 1, 30)`.
7. **Stop:**
   - `avServStop(idx)`
   - `IOTC_Session_Channel_OFF(sid, ch)`
   - `avServExit(sid, ch)`
   - `SPEAKERSTOP` `0x351`, 8 bytes, `int@0` = `ch`

Treat talk-back as half-duplex: send `AUDIOSTOP` `0x301` before starting, and `AUDIOSTART` `0x300` after
stopping if camera audio was being received. Played audio can trigger the camera's own sound detection (a `0x1fff`
`0x15` event).

## Frame-rate / timing

Decoded video is variable-rate; derive presentation timestamps from arrival if muxing. The reference
client muxes A/V to MP4/HLS and can emit RTP/HLS for viewing; see its `--mp4` / `--hls` / `--rtp` /
`--play` options.
