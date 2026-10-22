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

## vtech-link

- Build `vtech-link` (Rust core + PyO3 module) to `docs/vtech-link-design/`, starting with the
  phase 0 spikes in `90-migration.md`. Waits on the end of the `tutk-host` production soak only for
  phase 3 onwards.

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
