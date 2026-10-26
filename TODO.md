# TODO

Open protocol questions and known issues are in `AGENTS.md` ("Design notes"). This file holds the rest,
and what publishing needs.

## Before publishing

HACS (hacs.xyz "Publish" and "Include" pages, and the checks in `hacs/action`):

- Publish a GitHub release (a tag alone isn't enough), with the tag matching `version` in
  `manifest.json`. Waiting on the APKPure check below.
- To get into the HACS default list: a PR on `hacs/default` adding the repo to `integration`, from a
  personal account.
- Check that APKPure offers the x86_64 variant of Kalay Mobile 2.0.0 (only armeabi-v7a and arm64-v8a so
  far, 2026-10-10), as `README.md` tells users to download it. If it doesn't, find another public
  source or rethink the instructions.

## tutk-host

- Inbound control frames are ordered by `+0x12` (the camera's per-fragment packet number), and
  multi-fragment ones are dropped (`tutk-host/av/ioctrl.c` `av_ctrl_on_frame`). After the first
  multi-fragment message from the camera, every later frame would be dropped and the control channel
  would stall until reconnect. Fix: key on the frame number at `+0x1c` with the fragment index at
  `+0x16` and count at `+0x14`, reassemble, and advance `ctrl_in` per message
  (`docs/vtech-link-design/40-av-client.md`, "Inbound frames"). Not seen live. Fix after the production
  soak, or sooner if a control stall appears.
- The `0x0402` connect ack is built in an uninitialised stack buffer (`tutk-host/iotc/handshake.c:57`
  `iotc_send_connect_request`), so `+0x2c..0x2f`, which the kit has as zero
  (`tutk-kit/docs/iotc/16-handshake.md`, `_zero2c`), carry 4 bytes of stack to the camera. The camera
  doesn't check them. Fix: zero `ack` first. Found by the vtech-link fixtures (2026-10-22). With the fix
  above, after the soak.
- Its receive counters can't see a frame none of whose packets arrived: `*_given_up` counts partial
  frames only. Every audio frame is one packet, so under packet loss it reports 0 audio lost while losing
  audio. This also qualifies the `TK_DROP=50` "0 bad frames" in `tutk-host/NOTES.md`
  (`docs/vtech-link-design/86-live-losses.md`, finding 3). Fix if `tutk-host` stays in production.
- Three more differences found while porting to vtech-link (2026-10-22), none seen live. Note only:
  - Media kind 4 goes to the video stream (`tutk-host/av/media_recv.c` `KIND_AUDIO` is 3 only). The
    kit folds `{3,4}` to audio (`tutk-kit/docs/av/90-fifos.md`). Kind 4 hasn't been seen from this
    camera.
  - After a gap skip that lands on `pn_high` (`media_recv.c` `track_pn`), that packet's seen bit stays
    set. Once the 4096-packet window wraps, a fresh packet with the same slot reads as a duplicate and
    is dropped, so it has to be resent.
  - The control frame number is a `u16` (`av/ioctrl.c` `c->frame`) written into the u32 at `+0x1c`, so
    it wraps to 0 after 65,536 sends where the stock SDK's u32 carries on. The camera ACKs the low 16
    bits, so this is probably harmless.

## vtech-link

Status 2026-10-22: every core module is built and tested (`docs/vtech-link-design/*` "Status"
sections): IOTC, DTLS, the AV client, media receive with FEC, services, talk-back, the runtime and a
read-only CLI (`connect`, `watch`, `stream [--drop N]`, `restart`), the PyO3 module
(`75-python-api.md`, "Status"), and `vtech_lan`'s `LinkSession` beside the SDK path (same doc,
"Changes in `vtech_lan`"). The simulator runs 25 end-to-end scenarios and the T3 OpenSSL interop passes,
fuzzing covers 10 targets. Live: `watch` 10 min passed; streaming lost frames, and the investigation
(`docs/vtech-link-design/86-live-losses.md`) found the same losses in `tutk-host`, with two causes
shared by both stacks. Production switching waits on the end of the `tutk-host` soak.

**Resume here (2026-10-23):** "Media losses: next steps" item 1 (gap-aware give-up) and item 2 (NAKs
the camera ignores), then the 1080p live runs; then Phase 3's integration side. Next:

- **Phase 3, the integration side** (`75-python-api.md`, "Changes in the integration"). The first step
  is built (2026-10-23): `const.BACKEND` chooses the transport at build time (decided 2026-10-22: we swap
  only until vtech-link is finished, then it replaces the other two). A "link" build loads the module
  `sync_link.sh` installs (`native.bundled_link`/`prepare_link`), skips the SDK step in setup, opens
  every session on the link, and puts the link's `info()`/`stats()` in diagnostics. **Live on the NAS HA since
  2026-10-23 19:59** (`1d37bd3`): connected over LAN with the SDK not loaded, and a live-view snapshot
  decoded. Debug logging for `custom_components.vtech_baby` was set with `logger.set_level`, which a
  restart undoes. Next: watch it against the `tutk-host` burn-in. Afterwards, the removals in that table (SDK upload, `tooling/`,
  `libtk.so`, the `tutk-host` sets, the worker).
- **Discuss: the reactor's wait** (`70-runtime.md`, "Status"). It blocks in `recv_from` with
  `SO_RCVTIMEO`, then drains in non-blocking mode, which costs an `ioctl` pair on every wake-up that
  receives data (143 in S4; idle wake-ups no longer make any). Swapping to `poll` plus `recvfrom` with
  `MSG_DONTWAIT` would remove them and the per-wake `setsockopt`. Neither has a safe API in the current
  dependencies (`socket2` needs an uninitialised buffer, and reading it back needs `unsafe`), so it
  means adding a crate such as `rustix`, a reviewed decision (`80-build.md`).
- **No clock or socket injection**: T2 runs on real loopback sockets and the wall clock, not the
  virtual clock and network the design describes. The protocol timers can be shortened through
  `LinkConfig.timing` (`70-runtime.md`, "Status"), which brought S6 down from 61 s to 1 s.
- **Simulator gaps**: the 5-minute 1080p run (S4), fault injection with several links (S9), and
  duplicated or jittered datagrams (only drops are simulated). The simulator never splits the 16-byte
  frame info across fragments; whether the camera does is unverified.
- **Media losses: next steps** (`docs/vtech-link-design/86-live-losses.md`, "Open"). The investigation
  found no vtech-link defect: both stacks lose the same frames under the same conditions.
  1. Give up an incomplete frame only after a packet gap below it has been skipped, not after 500 ms
     alone. The camera trickles large keyframes over about 0.5 s, so both stacks lose an occasional
     keyframe. Built with offline tests (2026-10-26, `incomplete_wait`); verify with a long live run:
     no "gave up ... (no packet gap skipped)" line should appear.
  2. Find out why the camera ignores about 1 in 11 NAKs under forced drops (both stacks): log the NAK
     lists and skipped packet numbers and look for a pattern; try a 600 ms gap skip; compare with the
     vendor SDK.
  3. The Phase 2 runs at 1080p are still to do (the camera was at 720p; check the parent handset first).
- **Live, rest of T7** (`85-testing.md`):
  - `watch` 10 min passed.
  - `stream` and `restart` ran only at 720p. `restart` was 24/25 (one keyframe given up, finding 1 in
    `86-live-losses.md`), and `stream --drop 50` lost frames to ignored NAKs (finding 2). Redo at 1080p
    once those are fixed.
  - `talk --silent` needs a `talk` CLI command (not built) and the owner present.
  - DEVINFO takes 70–170 ms: compare with `tutk-host` on the same request (T4).
  - How to run it all in one SSH session: `86-live-losses.md`, "Method".
- **Not built yet**: the packet trace (R33); allocation tracking
  in the fuzz targets; the runtime's `Partial frames per stream: 64` cap (`70-runtime.md` limits table).
- **Unsigned commits**: every vtech_baby commit from `7f284ea` on (2026-10-22) went in unsigned,
  because the security key wasn't touched in time. Re-sign them with the key at hand:
  `git rebase --exec 'git commit --amend --no-edit -S' 5287f7e`.
- **The dimpl fork**: upstream work is listed in the checkout's uncommitted `TODO.md`. Clippy is now
  available (rustup 1.99.1), so its first item can be done.
- **Trim dimpl for PSK-only use** (measured 2026-10-22 on the unstripped x86_64 module, 1.55 MB of
  code). Two features in the fork, both on by default so upstream users see no change:
  - `dtls13`: leave out `src/dtls13/`, the `Client13`/`Server13` arms of `Inner`, the 1.2/1.3 auto
    mode (`auto.rs`) and the 1.3 suite list. About 212 KB. The boundaries are clean, so this is mostly
    mechanical. Do it first.
  - `certificates`: leave out `der`, `x509-cert`, `spki`/`pkcs8`/`sec1`, `ecdsa`/`signature` and
    `p256`/`p384`: about 220 KB, plus the certificate suites in dimpl's 1.2 code. More invasive: it runs
    through the crypto provider (`sign.rs`, `provider.rs`, `kx_group.rs`, where P-256/P-384 are also
    key-exchange groups), the public API (`DtlsCertificate`, `certificate::`, `Output::PeerCert`), and
    the 1.2 handshake. Ask upstream first (the fork's `TODO.md` has this item).
  - Together about 430 KB: the stripped module from about 1.9 MB to 1.4–1.5 MB, and the HACS download
    from about 7 MB to about 5.5 MB.
- **Decide the release profile** (`80-build.md`, "Size levers"): fat LTO and `opt-level = "s"` take
  the module from 1,945 KB to 1,411 KB (−27%) on stable, at 3.3× slower crypto with headroom to spare.
  `-Z build-std` adds only about 50 KB more and needs nightly. Not worth it. Measure the arm64 builds and a
  live 1080p stream on `s` before switching.

## Live view stall and go2rtc panic (2026-10-23)

Seen in the live HA log (`/config/HomeAssistantConfig/home-assistant.log` on the NAS) during the new-SDK
burn-in, the first since HA restarted at 07:38. Neither is explained yet.

- 18:20:02 `vtech_baby.vtech_lan.media`: `111A: live view stopped: no keyframe within 10 s of START`, then
  `stream_worker` logged `Error opening stream (I/O error, http://127.0.0.1:46429/.../<UID>.ts)`
  on `camera.vtech_5754_xxxx_live_view`. Find why no keyframe arrived (camera didn't send one, or the
  receive side dropped it) and whether START should retry.
- 18:25:15 `homeassistant.components.go2rtc.server`: `panic: runtime error: index out of range [4] with
  length 0` in `h264.RepairAVCC` (`pkg/h264/avcc.go:20`, go2rtc 1.9.14), so go2rtc stopped. Five minutes
  after the stall above. `RepairAVCC` indexes the NAL header, so a zero-length NAL reached go2rtc.
  Hypothesis, not checked: the integration passed an empty or truncated NAL after the stalled START. Check
  what the live-view path emits when a frame is empty.
- The integration logs only at WARNING and above, so the healthy hours of the burn-in leave no trace.
  Consider debug logging for `custom_components.vtech_baby` while it runs.

## Packaging

- A live-test runner in this repo, so `tests/live/` doesn't depend on a script kept elsewhere.

## Testing

- Soak and fault tests on a real HA host: 24 h+ idle session, camera power cycle, network bounce, the HA
  entry and the reference client connected at once, repeated reload/unload.
- Observer tests (owner at the camera): talk-back (`tests/live/probe_talk.py`, then the speaker entity
  in HA), lullabies and volume on the speaker; a handset-made change arriving via pushes. Open
  questions: the volume range (0–5 is assumed: move the handset's volume end to end and watch the
  `0x7ac` `0x14` pushes, no sound needed); whether volume applies to talk-back; what talk-back does to a
  playing lullaby (the speaker stops it first, which may be unnecessary); whether the live view's audio
  track survives the gap; whether TTS needs gain.
- Live view: the night-vision, resolution and reconnect cases; HLS open time; latency.
- Other VTech models that use the same protocol.
- arm64 on real hardware. It has only been tested under qemu; a native run adds the LL/SC atomics path,
  16K pages and enforced PAC/BTI. A Pi 4 covers LL/SC, a Pi 5 on Raspberry Pi OS covers 16K pages.

## Features

- Reading the pairing QR from a photo, not only pasted text (needs a QR decoder the HA image doesn't have).
