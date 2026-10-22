# 80 — Mapping protocol capabilities to Home Assistant concepts

This describes **what the protocol makes possible** and which Home-Assistant entity concepts each
capability naturally fits. It is not an integration design — it is a menu of what could be exposed, so
the implementer can choose scope and structure.

## Controls (writable)

| Capability | Protocol | Natural HA concept | Notes |
|---|---|---|---|
| Night-light colour preset | SET `0x7a2` (0=off,1=yellow,2=orange,3=red); GET `0x7a0` | `light` (effect/preset list) or `select` | 4-state preset, not a dimmer. Control-only (no live-view needed). |
| Status/online LED | SET `0x711`, GET `0x709` | `switch` | On/off (note the wire flag is "LED off"). |
| Night-vision mode | SET `0x715` (Auto/On/Off), GET `0x713`, STATE push `0x785` | `select` (mode) + `binary_sensor` (night active) | Mode is the setting; `0x785` reports whether night/IR is currently active. |
| Video resolution | SET `0x320` (1080p/720p), GET `0x322` | `select` | Persists camera-side; capped to 720p while a parent handset is active. |
| Sound detection on/off | SET `0x7b2`, GET `0x7b0` | `switch` | |
| Motion detection sensitivity | SET `0x7a6`, GET `0x7a4` (off/low/med/high) | `select` | `0` = off. |
| Temperature alert thresholds | SET `0x74d`, GET `0x74b` (enable + min/max) | `switch` + `number`×2 | Min/max in °C. |
| Notification interval | SET `0x790`, GET `0x792` | `select` | Also governs event-push rate-limiting. |
| Lullaby / soothing sound | SET `0x735` `[0, track]` (`-1` stops; ack `0x736`); now-playing params `0x734 @4`; volume / auto-off timer via `0x731` `word2` / `word1` | `media_player`, or `select` (track) + `number` (volume) + `select` (timer) | Tracks 0–9 per `30-opcode-reference.md`. "Play All" (0) reports no per-track now-playing. Playback can trigger the camera's own sound detection. |
| Talk-back (two-way audio) | `0x350`/`0x351` + an AV server channel | assist / stream feature | Procedure in `40-media-streaming.md`; needs the session master (encrypted uplink). |

## Telemetry (read / push)

| Capability | Protocol | Natural HA concept | Notes |
|---|---|---|---|
| Temperature | params `0x733 @0` (°C) / `@8` (°F); push `0x738` | `sensor` (temperature) | Camera pushes updates on `0x738`. |
| Temperature state | push `0x738` `int@8` (0 in range, 1 too hot, 2 too cold) | `sensor` (enum) or two `binary_sensor`s | Against the `0x74d` thresholds; reported even while temperature alerts are disabled. |
| Motion / sound / temperature alerts | push `0x1fff` (event_code) | `binary_sensor` (motion, sound, too hot, too cold) / event entity | `0x01` = motion, `0x15` = sound (needs sound detection on), `0x16` / `0x17` = temperature too high / too low (needs temperature detection on; repeats ~every 100 s while out of range). Rate-limited by the notification interval. |
| Sound-detection sensitivity | params `0x734 @16` (1 low, 2 medium, 3 high) | `sensor` (read-only) | Not writable from the LAN; only the parent handset changes it. Re-read on `0x738` pushes. |
| Parent-unit active | GET `0x75a`, push `0x74f` | `binary_sensor` | True while the physical handset is on; explains 720p capping. |
| Recent-event history | `0x788`/`0x789` | attribute / event list | Motion events only; for sound and temperature history, record the `0x1fff` pushes. Clip playback is out of scope. |
| Device model / firmware | `0x330`/`0x331` | device registry info | For the HA device entry. |

See `85-ha-deployment.md` for how an integration ships and loads the native layer, and `90-ha-runtime.md`
for SDK lifecycle, threading and reconnection inside Home Assistant.

## Streaming

| Capability | Protocol | Natural HA concept |
|---|---|---|
| Live video | live-view handshake + `0x719` + `0x1ff`, decrypt H.264 (`50`) | `camera` entity (feed an H.264/HLS stream) |
| Live audio | `0x300` + decrypt G.711 A-law (`50`) | audio alongside the camera stream |

## Practical implementation notes

- **One session serves everything.** A single authenticated AV channel carries control, telemetry,
  pushes, and media — an integration can hold one session and multiplex, reading `avRecvIOCtrl` in a loop
  and dispatching pushes to the right entities.
- **Control without streaming.** Night-light and the settings GET/SET opcodes work on a control-only
  session; live-view/decryption is only needed for the `camera` feed.
- **Pushes drive state.** `0x1fff` (alerts), `0x738` (temperature), `0x785` (night state), `0x74f`
  (parent active), and `0x7ac` (LED/night-light) let entities update reactively rather than by polling.
- **Prerequisite prep.** Send `DEVINFO` (`0x330`) once per session before issuing the VTech GETs or
  starting the live view.
- **Mode vs state.** For night-vision, expose the *mode* (`0x715`/`0x713`, the setting) and the *state*
  (`0x785`, whether IR is currently on) as separate entities — they differ in Auto mode.
- **Applying night-vision mode briefly interrupts video** (ISP/IR-cut reconfigure); a `camera` feed
  should tolerate a few seconds of drop after such a change.
