---
name: camera-compatibility
description: Work out whether a VTech baby-monitor model other than the tested RM5754HD can work with this integration — decode and classify its pairing QR, obtain the credential set where possible, probe the camera's connect/auth over the LAN, and read the results. Use when someone asks "will my VTech model X work?", reports a camera that won't pair or add, or pastes a pairing QR / connect error. Works from the documented protocol and the user's own camera only.
---

# Check a VTech model's compatibility

The integration is written and tested for the **RM5754HD**. This skill helps determine how far another
model gets. Two independent questions, in order: can we get its **credentials**, and does it speak the
**protocol**. The full user-facing walkthrough is `COMPATIBILITY.md`; this is the operating procedure.

**Ground rules**
- Work only from the documented protocol (`docs/vtech-lan-kit/docs/`) and the user's **own camera**.
  Never reason from, or ask the user to inspect, the VTech app.
- A pairing QR and the credentials it yields (UID, AV password, authKey, version-1 token) are the
  user's camera secrets. **Never print, log, commit, or ask the user to paste them.** Work with the
  `--inspect` verdict line and the probe's interpretation line, which carry no secrets.
- The probe connects and authenticates only — it never starts video/audio or changes a setting, so it is
  safe against a live monitor. Don't add steps that would make the camera emit light or sound.

## Step 1 — classify the pairing QR

Get the QR's text (any QR reader) or a photo, then from `docs/vtech-lan-kit/client`:

```
python3 vtech_provision.py --inspect --qr "<text>"      # or --image photo.jpg (needs pyzbar/opencv)
```

Read the version byte / verdict:

- **`v5-ok`** (version `0x05`) — the documented format; the QR carries UID + AV password for each camera
  (several cameras per QR are possible). The report shows each camera's mode: `enhanced` (authKey) or
  `plain` (no authKey). Go to Step 2.
- **`v1-plain`** (version `0x01`) — the QR's 8-char field is the AV password of a plain-mode camera, so the
  QR provisions it directly, with no authKey. The integration supports it from version 0.4.0.
  Whether a version-1 camera can be enhanced is unconfirmed; if a key is rejected on one, report it. Go to
  Step 2.
- **`unknown-version`** — an uncharacterised format. Capture the report line for Step 4; do not guess at
  its structure beyond what `inspect_qr` reports. Format facts live in
  `docs/vtech-lan-kit/docs/05-provisioning.md` ("Other QR formats"); add a new version there only once
  one is actually characterised.

## Step 2 — provision

```
python3 vtech_provision.py --qr "<text>" --env [--uid UID]
```

Yields the `VTECH_*` set: `VTECH_AUTHKEY` for an enhanced camera, `VTECH_PLAIN=1` for a plain one. A QR
with several cameras needs `--uid` to pick one (without `--env` it lists them all). Format and derivation:
`docs/vtech-lan-kit/docs/05-provisioning.md`.

## Step 3 — probe the camera

From a host on the camera's **LAN segment**, with `VTECH_LIBDIR` pointing at the patched SDK libs
(`docs/vtech-lan-kit/docs/00-prerequisites.md`):

```
python3 vtech_probe.py --qr "<text>" [--uid UID]   # any provisionable QR, or:
VTECH_UID=... VTECH_PWD=... VTECH_AUTHKEY=... VTECH_ACCOUNT=admin python3 vtech_probe.py
# plain camera: VTECH_PLAIN=1 (or --plain) instead of VTECH_AUTHKEY
```

Interpret (the probe prints these in words; return codes in `docs/vtech-lan-kit/docs/00-prerequisites.md`
and `10-transport-session.md`):

- **connect ok + AV ok** — transport and both auth layers work; strong candidate. Go to Step 4.
- **connect `-68`** — reachable, UID resolved, authKey rejected. **Transport compatible, credentials
  wrong**: stale key (re-provision from a fresh QR).
- **connect `-69`** — the camera does not use key authentication: it is plain-mode. Connect without an
  authKey (`--plain` / `VTECH_PLAIN=1`).
- **connect `-13`** — timeout: reachability, not credentials. Check the host shares the camera's subnet
  and the camera is up.
- **connect ok, AV refused** — authKey accepted, AV account/password rejected; an untested model may use
  an AV account other than `admin`.

## Step 4 — map the protocol / report

For a camera that connects, the remaining work is which opcodes it answers
(`docs/vtech-lan-kit/docs/30-opcode-reference.md`): add it in Home Assistant, see which entities respond,
which are missing or wrong. Help the user write a GitHub report with the model number, the `--inspect`
verdict line, the probe interpretation line, and the per-entity results — **never** the QR or any
credential. If the investigation establishes a new protocol fact, record it in the kit docs
(`docs/vtech-lan-kit/docs/`) in the kit's facts-only style, and update `COMPATIBILITY.md` if the
user-facing steps change.
