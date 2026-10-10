# TODO

Development order and open protocol questions stay in `HA_INTEGRATION.md` (§6 "Status and next steps",
§5.2, §9). This file holds the rest, and what publishing needs.

## Before publishing

These need a decision from the owner, not just work:

- **Legal review of what the repo contains.** It embeds values taken from VTech's app: the ThroughTek
  SDK licence key (`vtech_lan/sdk.py`, kit client) and the pairing-QR AES key/IV and HKDF master key
  (`vtech_lan/provision.py`, kit). The kit docs describe the protocol and media encryption that were
  worked out by reverse engineering. The kit's position (`vtech-lan-kit/sdk/README.md`) is that the
  licence key is shareable under interoperability exceptions. Check that holds where it will be
  published, and decide whether the constants stay embedded or become user-supplied like the SDK.
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
- Trim `HA_INTEGRATION.md` and the kit docs for readers outside the original setup: they still mention
  the author's NAS, live-test directories and the unpublished `camera/` notes.
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

- Phase 6: a `media_player` for lullabies and talk-back (`HA_INTEGRATION.md`).
- Reading the pairing QR from a photo, not only pasted text (needs a QR decoder the HA image doesn't have).
