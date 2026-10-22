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
read-only CLI, and the PyO3 module (`75-python-api.md`, "Status"). The simulator runs 22 end-to-end scenarios, fuzzing covers 10 targets, and first live
runs from the NAS were clean (`85-testing.md`, "First live runs"). Phase 3 onwards still waits on the
end of the `tutk-host` production soak. Next:

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
- **Live**: DEVINFO takes 70–170 ms. Compare with `tutk-host` on the same request. Then the rest of
  T7: `stream` 10 minutes at 1080p, `restart` ×25, `stream --drop 50`, and `talk --silent` with the
  owner present (the CLI has no `restart`, `--drop` or `talk` yet). `watch` 10 minutes passed
  2026-10-22 (`85-testing.md`).
- **Not built yet**: the packet trace (R33); allocation tracking
  in the fuzz targets; the runtime's `Partial frames per stream: 64` cap (`70-runtime.md` limits table).
- **The dimpl fork**: upstream work is listed in the checkout's uncommitted `TODO.md`. Clippy is now
  available (rustup 1.99.1), so its first item can be done.

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
