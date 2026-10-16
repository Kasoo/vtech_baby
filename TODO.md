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
