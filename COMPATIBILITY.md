# Will my VTech model work?

This integration is written and tested for the **RM5754HD**. VTech sells many baby monitors, and most
are likely built on the same platform, but another model may use different credentials, a different
pairing-QR format, or control opcodes this integration doesn't send. This page helps you find out how
far your model gets — using only your own camera and its pairing QR. You don't need a VTech account for
the checks here, and nothing here inspects the VTech app.

> **Status — version-1 QRs (e.g. RM5764HD / RM7766).** Some models emit a version-1 pairing QR. Its
> 8-character field is the AV password of a **plain-mode** camera, which connects with no authKey, so the
> QR provisions it directly. The integration supports this from version 0.4.0. Whether a
> camera with a version-1 QR can instead be in enhanced mode is unconfirmed. Step 1 below tells you which
> format your model uses.

There are two independent questions:

1. **Credentials** — can the integration get the UID, AV password and (for an enhanced camera) authKey your camera needs?
2. **Protocol** — once connected, does the camera speak the opcodes the integration uses?

Work through them in order; the credential question is the one that stops most other models today.

## The credential set

To open a session a camera needs four things (`docs/vtech-lan-kit/docs/00-prerequisites.md`):

| Item | Where it comes from on the RM5754HD |
|---|---|
| **UID** | the pairing QR |
| **AV password** | the pairing QR |
| **authKey** | derived from the AV password by a fixed construction; a **plain**-mode camera uses none |
| **AV account** | fixed string `admin` |

So on the RM5754HD the pairing QR alone yields everything. Each camera is in one of two security modes:
**enhanced** (authKey and DTLS) or **plain** (no authKey, no DTLS), and the QR says which. A QR can hold
several cameras. The mode is described in `docs/vtech-lan-kit/docs/10-transport-session.md`.

## Step 1 — identify your pairing QR format

Read the QR the parent unit shows for "Connect Mobile Phone" / "Camera Pairing QR Code" with any QR
reader to get its **text** (a base64 string), then classify it:

```
cd docs/vtech-lan-kit/client
python3 vtech_provision.py --inspect --qr "<the QR text>"
# or, from a photo (needs pyzbar or opencv-python):
python3 vtech_provision.py --inspect --image photo.jpg
```

The known formats:

| Version byte | Verdict | What it means |
|---|---|---|
| **`0x05`** | `v5-ok` | The documented format. The QR carries each camera's UID **and** AV password, so the full credential set is provisioned offline from it. The report lists each camera with its mode: `enhanced` (password under TLV type 5, authKey derived) or `plain` (type 4, no authKey). This is the RM5754HD (enhanced). |
| **`0x01`** | `v1-plain` | The QR carries each camera's UID and an 8-character token, which is the AV password of a **plain**-mode camera. It is provisioned offline with **no authKey**. (Observed on the RM5764HD / RM7766.) |
| anything else | `unknown-version` | Not a format this tool knows. The report gives the facts worth sharing (see Step 4). |

If you get **`v5-ok`** or **`v1-plain`**, go to Step 2. If you get **`unknown-version`**, skip to Step 4.

## Step 2 — turn a QR into credentials

```
python3 vtech_provision.py --qr "<the QR text>"                 # lists every camera in the QR
python3 vtech_provision.py --qr "<the QR text>" --env [--uid UID]   # --uid picks one camera
```

This prints `VTECH_UID`, `VTECH_PWD` and `VTECH_ACCOUNT`, plus `VTECH_AUTHKEY` for an enhanced camera or
`VTECH_PLAIN=1` for a plain one. When the QR holds several cameras, choose one with `--uid`.
`--selftest` checks the tool against built-in vectors; the format and derivation are specified in
`docs/vtech-lan-kit/docs/05-provisioning.md`.

## Step 3 — probe your camera

Run the connect-only probe from a host **on the camera's LAN segment** (the connect is by UID; a client
on a different subnet can't reach the camera). The probe needs the patched SDK library set — see
`docs/vtech-lan-kit/docs/00-prerequisites.md` — pointed at by `VTECH_LIBDIR`. It only connects and
authenticates; it does **not** start video or audio or change any setting, so it is safe against a live
monitor.

```
cd docs/vtech-lan-kit/client
export VTECH_LIBDIR=/path/to/patched/libs
# from a QR directly (add --uid UID to choose a camera when the QR holds several):
python3 vtech_probe.py --qr "<the QR text>"
# or from credentials you already have:
export VTECH_UID=... VTECH_PWD=... VTECH_AUTHKEY=... VTECH_ACCOUNT=admin
python3 vtech_probe.py
# a plain-mode camera has no authKey: set VTECH_PLAIN=1 (or pass --plain) instead of VTECH_AUTHKEY
```

What the result tells you:

| Result | Meaning |
|---|---|
| **connect ok + AV ok** | The transport and both auth layers work. Your model is a strong candidate; add it in Home Assistant and see which features respond (Step 4). |
| **connect `-68`** (wrong/absent authKey) | The camera is reachable and resolved the UID, but rejected the authKey. The **transport is compatible**; the **credentials are the problem** — a stale key (re-provision from a fresh QR). |
| **connect `-69`** | The camera does not use key authentication: it is a **plain**-mode camera. Connect without an authKey (`--plain`, or provision from the QR, which sets `VTECH_PLAIN=1` for it). |
| **connect `-13`** (timeout) | No answer in time. Usually the camera isn't on this host's LAN segment, is still booting, or the LAN search didn't reach it. This is a **reachability** result, not a credentials one. |
| **connect ok, AV refused** | The IOTC session came up but the AV layer rejected the account/password. The authKey was accepted but the AV account or password differs — for an untested model the AV account may not be `admin`. |

The connect sequence and these return codes are documented in
`docs/vtech-lan-kit/docs/10-transport-session.md` and `00-prerequisites.md`.

## Step 4 — what to report

When you add a camera, the integration detects the model and warns if it's untested; please report how
far it got either way (GitHub issues). Useful details, none of which require the VTech app:

- the model number, and whether it pairs one or two cameras to a parent unit;
- the `--inspect` **verdict line** and each camera's mode (and, for `unknown-version`, the whole report
  line) — but **never paste the QR text itself or the decoded UID/token/password**: those are your camera's secrets;
- the probe result (the interpretation line, not your credentials);
- if you got a session: which Home Assistant entities worked, which were missing or wrong. The opcodes
  the integration uses are in `docs/vtech-lan-kit/docs/30-opcode-reference.md`; a model that connects but
  ignores some of them is using a different or extended opcode set.

With a connecting camera and notes on which opcodes answer, adding a model is mostly a protocol-mapping
job. A report on your model still helps, including whether a camera with a version-1 QR ever turns out to
be in enhanced mode (unconfirmed so far).
