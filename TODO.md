# TODO

Open protocol questions and known issues are in `AGENTS.md` ("Design notes"). This file holds the rest,
and what publishing needs.

## Before publishing

These need a decision from the owner, not just work:

- **The name.** "VTech" in the integration name is descriptive, but check it reads as unofficial
  (the README says it isn't affiliated).

HACS (hacs.xyz "Publish" and "Include" pages, and the checks in `hacs/action`):

- Choose a licence and add `LICENSE` (GitHub must recognise its SPDX ID). Also check the kit's
  own licensing notes still fit.
- Create the public GitHub repo and add it as a remote. Then fill in what needs its URL:
  - `manifest.json`: `documentation` (hassfest fails without it), `issue_tracker` and `codeowners`. HACS
    requires all three.
  - `README.md`: a My Home Assistant link that opens the repo in HACS
    (`https://my.home-assistant.io/redirect/hacs_repository/?owner=<owner>&repository=<repo>&category=integration`).
- On GitHub, set a description and topics (e.g. `home-assistant`, `hacs`, `vtech`, `baby-monitor`) and
  keep issues enabled.
- Publish a GitHub release (a tag alone isn't enough), with the tag matching `version` in `manifest.json`.
- Check the validation workflow passes on GitHub.
- To get into the HACS default list: a PR on `hacs/default` adding the repo to `integration`, from a
  personal account.
- `README.md` shows `images/device.png` by a relative path, which GitHub renders. Check whether HACS's
  README view does too; if not, switch it to the image's full `raw.githubusercontent.com` URL.
- Check that APKPure offers the x86_64 variant of Kalay Mobile 2.0.0 (only armeabi-v7a and arm64-v8a so
  far, 2026-10-10), as `README.md` tells users to download it. If it doesn't, find another public
  source or rethink the instructions.
- Find the oldest Home Assistant release that works and put it in `hacs.json` (2026.9.3 is the only one
  tested).

## Packaging

- Build and vendor the arm64 companion (`./sync_vendored.sh aarch64`, needs podman + qemu-user), so
  Raspberry Pi and other arm64 hosts work. Then update the README.
- A live-test runner in this repo, so `tests/live/` doesn't depend on a script kept elsewhere.

## Testing

- Soak and fault tests on a real HA host: 24 h+ idle session, camera power cycle, network bounce, the HA
  entry and the reference client connected at once, repeated reload/unload.
- Observer tests (owner at the camera): lullaby and talk-back, once built; a handset-made change
  arriving via pushes.
- Live view: the night-vision, resolution and reconnect cases; HLS open time; latency.
- Other VTech models that use the same protocol.

## Features

- A `media_player` for lullabies and talk-back: lullaby tracks as sources (`0x735`, volume `0x731`
  word2, state from `0x736`, `0x7ac` subtype 1 and `0x734 @4`), and `play_media` for TTS and media URLs
  as encrypted A-law talk-back (`40-media-streaming.md`). Sound alerts it causes must be tagged as
  self-triggered (the manager already does this for lullabies).
- Reading the pairing QR from a photo, not only pasted text (needs a QR decoder the HA image doesn't have).
