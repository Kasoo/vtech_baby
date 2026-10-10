# VTech RM5754HD — LAN-local protocol kit

A clean-room description of the **LAN-local control and streaming protocol** of the **VTech RM5754HD**
baby-monitor camera, plus a reference client and the tooling needed to talk to it from a normal Linux
host. Everything here is stated as protocol fact; it is intended as the implementation reference for a
Home-Assistant integration.

> **Scope note.** All of this is characterised against the **VTech RM5754HD** specifically. It is
> untested against any other model; other VTech/Kalay cameras may differ in opcodes, credentials, or the
> media crypto.

The camera speaks **only** ThroughTek's Kalay P2P protocol (TUTK) — there is no RTSP/ONVIF. All control
and telemetry is `avSendIOCtrl` / `avRecvIOCtrl` on an authenticated TUTK AV session; video and audio
arrive on the AV frame channels and are application-layer encrypted.

## Layout

| Path | What it is |
|---|---|
| `docs/` | The protocol specification (start at `docs/README.md`). |
| `client/vtech_camera_client.py` | Reference client: connects, runs the video handshake, decrypts video+audio, plays audio through the camera speaker (talk-back), and exposes an opcode framework for control/telemetry. |
| `client/vtech_provision.py` | Offline credential provisioning: pairing QR (text or photo) → UID + AV password + derived authKey (`docs/05-provisioning.md`). |
| `sdk/` | Local-development copies of the ThroughTek/TUTK Kalay SDK libraries (x86_64 and aarch64), plus the tested build's version and digests. **We do not redistribute these binaries** — see `sdk/README.md` for a public source. |
| `tooling/` | The load-time ELF patcher (`elfpatch.py`, `patch_sdk.py`), companion source and build step that let the SDK load on a host libc. |

## Quick start

1. **Provide the inputs.** The one input the user supplies is the **SDK libraries** for their
   architecture — the kit doesn't host the binaries but points to a public source (`docs/00-prerequisites.md`,
   `sdk/README.md`); `python3 tooling/extract_sdk.py <app.xapk> <SDK_DIR>` takes them out of the download. The SDK **license key** is embedded in the kit (a small static fact), so no
   `LICENSE_KEY.txt` is needed (drop one in `VTECH_LIBDIR` only to override it). The **per-camera
   credentials** are derived offline from the owner's pairing QR (`docs/05-provisioning.md`):
   `python3 client/vtech_provision.py --image qr.jpg --env` prints the `VTECH_*` exports.
2. **Build the library set:** `cd tooling && ./patch.sh [SDK_DIR]` produces `patched/` with the patched
   SDK libraries and the companion, for the architecture of `SDK_DIR`. (No `LICENSE_KEY.txt` needed — the
   license is embedded; add one here only to override it.)
3. **Run the client** on a host on the camera's LAN segment:
   ```sh
   export VTECH_LIBDIR=$PWD/tooling/patched
   export VTECH_UID=... VTECH_PWD=... VTECH_AUTHKEY=... VTECH_ACCOUNT=admin
   python3 client/vtech_camera_client.py --mp4 out.mp4 --seconds 10
   python3 client/vtech_camera_client.py --talk message.wav      # talk-back: play a WAV on the camera
   ```

## What the protocol supports (and where it maps in HA)

Control: night-light (colour preset), status LED, day/night (night-vision) mode, video resolution.
Telemetry: temperature, motion/sound/temperature detection settings, notification interval, parent-unit
active state, device info. Streaming: encrypted H.264 video + G.711 audio, and talk-back (encrypted
G.711 audio to the camera speaker). Events: asynchronous motion/sound/temperature alert pushes and the
stored motion-event list. See `docs/80-ha-feature-mapping.md` for how each maps to Home-Assistant entity
concepts.

## Hosts

64-bit **x86_64** and **aarch64** Linux hosts, with **glibc or musl**. aarch64 needs a 4K- or 16K-page
kernel. 32-bit hosts (armv7, i386) are not supported. For deploying inside Home Assistant (no compiler on
the host, SDK supplied by the user), see `docs/85-ha-deployment.md` and `docs/90-ha-runtime.md`.

## Networking requirement

The client connects by device UID over the Kalay P2P transport, and the camera and the client host must
share the same L2 segment (the camera's LAN). The camera answers the SDK's LAN-search broadcast on fixed
UDP port **32762**, not on the SDK's default port (32761), so LAN discovery needs
`IOTC_Set_LanSearchPort(32762)`; without it, only ThroughTek's master finds the camera (and with no cloud
the connect fails). Setting the port makes the data path LAN-direct but does **not** stop the cloud path:
with internet the SDK still does master DNS and STUN in parallel. For no cloud contact, also deny the
client internet routing, or use the companion `libtk.so`, which refuses the SDK's off-link sends and DNS
lookups. If the LAN path is unavailable and the client has internet, the SDK falls back to a ThroughTek
relay. See `docs/00-prerequisites.md` ("Host / networking").
