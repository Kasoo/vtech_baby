# VTech RM5754HD — LAN-local protocol specification

This directory specifies the LAN-local protocol of the VTech RM5754HD as implementation-ready facts.
Read in order:

| Doc | Contents |
|---|---|
| [`00-prerequisites.md`](00-prerequisites.md) | Key material and native libraries required before any connection. |
| [`05-provisioning.md`](05-provisioning.md) | Deriving the per-camera credential set (UID, AV password, authKey) offline from the pairing QR. |
| [`10-transport-session.md`](10-transport-session.md) | Transport model, session establishment, and the connect/auth call sequence. |
| [`20-ioctrl-framework.md`](20-ioctrl-framework.md) | The IOCtrl request/response model used by all control and telemetry. |
| [`30-opcode-reference.md`](30-opcode-reference.md) | Every opcode: arguments, response layouts, and semantics. |
| [`40-media-streaming.md`](40-media-streaming.md) | Starting video/audio, the stream bring-up sequence, codecs, and talk-back (audio to the camera). |
| [`50-media-decryption.md`](50-media-decryption.md) | The live-view key exchange and the per-frame encryption scheme, in both directions. |
| [`60-event-channel.md`](60-event-channel.md) | Asynchronous alert pushes and the stored-event list. |
| [`70-device-info.md`](70-device-info.md) | Device information response layout. |
| [`80-ha-feature-mapping.md`](80-ha-feature-mapping.md) | How protocol capabilities map to Home-Assistant entity concepts. |
| [`85-ha-deployment.md`](85-ha-deployment.md) | Shipping the native layer in a Home-Assistant integration: what ships, what the user supplies, patching the SDK at runtime with a prebuilt companion. |
| [`90-ha-runtime.md`](90-ha-runtime.md) | Running the SDK inside Home Assistant: process-wide init, sessions, threading, errors and reconnection, teardown, fault isolation, streaming. |
| [`95-vm901-comparison.md`](95-vm901-comparison.md) | Comparison with the independent VM901 bridge: same live-view crypto, differences in coverage and runtime. |

## Conventions

- **Byte offsets** are written `@N` (0-based). Multi-byte integers are **little-endian** unless stated.
  `word0`/`word1`/… mean the 1st/2nd/… 32-bit little-endian integer of a payload.
- **Opcode pairing:** a request opcode `0xNNN` is answered by response opcode `0xNNN+1` unless stated
  otherwise. Asynchronous camera-initiated messages ("pushes") arrive on their own opcodes with no
  request.
- **Opcode names:** opcodes that carry a symbolic ThroughTek/VTech SDK constant are named with that
  constant (`IOTYPE_...`). Opcodes with no published constant are given a descriptive name in this spec.
- **Confidence:** where a field's meaning is not characterised, the text says so (e.g. "meaning not
  characterised", or a byte marked `-`). Treat those fields as opaque: don't rely on their values.
- The reference client (`../client/vtech_camera_client.py`) implements the connect, live-view,
  stream and talk-back paths plus a representative subset of the control/telemetry opcodes; the full
  opcode set is specified in `30-opcode-reference.md`.
