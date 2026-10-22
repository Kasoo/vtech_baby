# 30 — Opcode reference

All payloads are little-endian. `wordN` = the Nth 32-bit LE integer (`word0` = bytes `@0..3`). A request
`0xNNN` is answered by `0xNNN+1` unless noted. Send `DEVINFO` (`0x330`) once after connecting before
issuing the VTech (`0x7xx`/`0x74x`/`0x75x`/`0x73x`) GETs or starting the live view (see
`20-ioctrl-framework.md`).

## Streaming / standard TUTK

| Opcode | Name | Request payload | Reply | Semantics |
|---|---|---|---|---|
| `0x0ff` | `IOTYPE_INNER_SND_DATA_DELAY` | 4B, `int32` `0` | — | Conventionally the first IOCtrl on a new AV channel. **SDK 4.3.8.1 refuses it from application code with `AV_ER_NOT_SUPPORT` (`-20033`)**; nothing depends on it (`20-ioctrl-framework.md`). The value is always `0`. No reply. |
| `0x1ff` | `IOTYPE_USER_IPCAM_START` | 8B, `word0`=channel(0) | — | Start live video. |
| `0x2ff` | `IOTYPE_USER_IPCAM_STOP` | 8B, channel | — | Stop live video. |
| `0x300` | `IOTYPE_USER_IPCAM_AUDIOSTART` | 8B, channel | — | Camera begins sending audio frames. |
| `0x301` | `IOTYPE_USER_IPCAM_AUDIOSTOP` | 8B, channel | — | Stop audio. |
| `0x320` | `IOTYPE_USER_IPCAM_SETSTREAMCTRL_REQ` | 8B: `word0`=channel, byte`@4`=quality | `0x321` | **Resolution selector.** quality `2` → 1920×1080, `1` → 1280×720. Persists camera-side across sessions. |
| `0x322` | `IOTYPE_USER_IPCAM_GETSTREAMCTRL_REQ` | 8B, channel | `0x323` | Reply: `word0`=channel, byte`@4`=current quality. |
| `0x324` | `IOTYPE_USER_IPCAM_SETMOTIONDETECT_REQ` | (standard TUTK) | `0x325` | Standard TUTK motion-detect set. |
| `0x326` | `IOTYPE_USER_IPCAM_GETMOTIONDETECT_REQ` | 8B, channel | `0x327` | Reply: `word0`=channel, `word1`=sensitivity. |
| `0x328` | `IOTYPE_USER_IPCAM_GETSUPPORTSTREAM_REQ` | 4B reserved | `0x329` | **Not serviced on this camera** — no reply is returned. Resolutions are not enumerable this way; use the fixed `SETSTREAMCTRL` quality byte. |
| `0x32a` | `IOTYPE_USER_IPCAM_GETAUDIOOUTFORMAT_REQ` | 8B, channel | `0x32b` | Talk-back uplink format. Reply: `word0`=channel, `word1`=codec id (`0x8a`), `byte@8` sample-rate index, `byte@9` data bits, `byte@10` channels, `byte@11` server channel (`40-media-streaming.md`). |
| `0x330` | `IOTYPE_USER_IPCAM_DEVINFO_REQ` | 20B (see `70-device-info.md`) | `0x331` | Device info; also the required prep for VTech GETs. |
| `0x350` | `IOTYPE_USER_IPCAM_SPEAKERSTART` | 8B, `int@0` = the client's server-side IOTC channel | — | Talk-back start: the camera connects to that channel as an AV client; uplink frames are encrypted (`40-media-streaming.md`). |
| `0x351` | `IOTYPE_USER_IPCAM_SPEAKERSTOP` | 8B, same channel | — | Talk-back stop. |
| `0x3a0` | `IOTYPE_USER_IPCAM_GET_TIMEZONE_REQ` | 268B zero | `0x3a1` | Timezone. Reply 16B: `word0` = struct size (16), `word1` = timezone supported (1), `word2` = **effective GMT offset in minutes** (includes the daylight-saving hour when that setting is on), `word3` = timezone index into the vendor's timezone-name table (e.g. `72` = United Kingdom (London)). |
| `0x719` | (video full-rate selector) | 8B, `word0`=0 | — | Selects the full-rate video stream; send just before `START`. Without it the stream is a ~1 fps keyframe-only preview. |
| `0x7be` | (client announce) | 8B, `word0`=int | — | First opcode in the connect burst; no reply. Optional. |

## Controls

### Night-light (illumination LED)

| Opcode | Direction | Payload | Semantics |
|---|---|---|---|
| `0x7a2` | SET | byte`@0` = value | Colour preset: **0 = off, 1 = yellow, 2 = orange, 3 = red**. Values ≥4 are a no-op (the LED holds its last valid value). No live-view needed. Ack `0x7a3`: 1B, `01` = success; the camera also pushes `0x7ac`. |
| `0x7a0` → `0x7a1` | GET | req 4B `0` | Reply byte`@0` = current preset (0–3). |
| `0x7ac` | push | 24B | Night-light / params state-change notify. `int@0` = subtype, `int@4` = new value: `0` night-light level, `1` lullaby track (`-1` = stopped), `2` temperature-detection enable, `8` status LED, `0x14` speaker volume, `0x15` lullaby timer (s). Sound-sensitivity changes (parent-side only) have no `0x7ac` push; instead the camera sends a `0x738` push ~1–2 s before `0x734` changes — treat any `0x738` as a cue to re-GET `0x733`. |

### Status / online LED (physical indicator)

Distinct from the night-light. This is the camera's online-status indicator light.

| Opcode | Direction | Payload | Semantics |
|---|---|---|---|
| `0x711` → `0x712` | SET | 8B, `int@4` = flag | `int@4` = **1 turns the LED off, 0 turns it on** (the flag is "LED disabled"). Ack `0x712`: `int@0`=0. |
| `0x709` → `0x710` | GET | 8B, channel | **The reply opcode is `0x710`, not `0x70a`.** Reply `int@4`: 0 = LED on, 1 = LED off. |
| `0x7ac` subtype 8 | push | 24B | State-change notify (`int@0`=8, `int@4`=new value). |

While the LED is enabled it is **solid green when idle and flashes green while live video is being
streamed** (`START` … `STOP`, including a brief stream for a snapshot); while disabled it stays off in
both cases. So with the LED enabled,
anyone in the room can see when the camera is being watched.

### Day/night (night-vision) mode

| Opcode | Direction | Payload | Semantics |
|---|---|---|---|
| `0x715` → `0x716` | SET | 8B, `int@4` = mode | **0 = Auto, 1 = On (forces night/IR even in a lit room), 2 = Off.** Ack `0x716`: `int@0`=1 (success). Applying a mode briefly drops video while the ISP/IR-cut reconfigures (set, wait ~6–8 s, then stream). |
| `0x713` → `0x714` | GET | 4B `0` | Reply `int@4` = current mode (same enum). |
| `0x785` | push | 4B | **Current day/night STATE**: `int@0` = 1 (night/IR active) or 0 (day). Distinct from the mode setting: mode Auto resolves to a state based on ambient light. |

### Video resolution

`SETSTREAMCTRL` (`0x320`, above): quality byte `2` = 1080p, `1` = 720p. Persists across sessions. See
`40-media-streaming.md` for the caveat that a physical parent handset caps this to 720p while active (`0x75a`/`0x74f`).

## Telemetry / settings

### Consolidated parameters

| Opcode | Direction | Payload | Semantics |
|---|---|---|---|
| `0x733` → `0x734` | GET | 4B `0` | Reply = 9×int32: `@0` temp °C, `@4` lullaby track index (`-1`=off), `@8` temp °F, `@12` lullaby auto-off timer (seconds: 900/1800/3600, `0` = non-stop), `@16` **sound-detect sensitivity** (1 low, 2 medium, 3 high), `@20` speaker volume (lullaby playback; 5 by default, 2 is audibly quieter; a 0–5 range is unconfirmed), `@24` motion-tracking sensitivity (0/25/50/75), `@28` opaque (meaning not characterised; echo it back unchanged in `0x731` `word5`), `@32` flag. |
| `0x7ba` → `0x7bb` | GET | 4B `0` | Extended params. Reply 64B: bytes `@0..@35` are identical to the `0x734` reply; byte `@36` = °C tenths digit, byte `@37` = °F tenths digit, byte `@38` = relative humidity (%); rest zero. **On the RM5754HD `@36..@38` are always 0** (no tenths, no humidity sensor), so `0x733` is sufficient there. |
| `0x731` → `0x732` | SET | 24B: `word0` = 0, `word1` = `@12` auto-off timer, `word2` = `@20` volume, `word3` = `@16` (**ignored** — sound sensitivity is not writable through this opcode), `word4` = `@24` motion-tracking sensitivity, `word5` = `@28` (opaque: send the value just read from `0x734`) (field names refer to the `0x734` offsets). `@0`, `@4` and `@8` are not writable here; send the current values of the fields you are not changing. | Ack `0x732`: `int@0` = **1 success, 0 failure**. |

### Temperature detection (alert thresholds)

| Opcode | Direction | Payload | Semantics |
|---|---|---|---|
| `0x74b` → `0x74c` | GET | 4B `0` | Reply 12B: `word0` = enable (0/1), `word1` = max threshold (°C ×10), `word2` = min threshold (°C ×10). |
| `0x74d` | SET | 12B: `word0` enable, `word1` max (°C ×10), `word2` min (°C ×10) | Sets temperature detection. Ack `0x74e`: 4B, `1` = success; the camera also pushes `0x7ac` subtype 2. While enabled and out of range, the camera pushes `0x1fff` `0x16` (too hot) or `0x17` (too cold) (`60-event-channel.md`). **The camera quantises the thresholds:** it keeps whole °F and reports °C ×10 truncated, so a write reads back changed: 265 → 266, 152 → 150, while 238 and 161 (= 75 °F / 61 °F) round-trip unchanged. Re-read `0x74b` after a write to show what was applied. |
| `0x738` | push | 12B | Temperature notify: `int@0` °C, `int@4` °F, `int@8` **temperature state** against the `0x74d` thresholds: `0` within range, `1` above max (too hot), `2` below min (too cold). The state is evaluated whether or not temperature detection is enabled. |

### Motion detection / tracking

| Opcode | Direction | Payload | Semantics |
|---|---|---|---|
| `0x7a4` → `0x7a5` | GET | 4B `0` | Reply 12B, same layout as the `0x7a6` body: `word0` = sensitivity enum **0 = off, 25 = low, 50 = med, 75 = high**, `word1` = video-record flag, `word2` = 0. |
| `0x7a6` → `0x7a7` | SET | 12B: `word0` = sensitivity, `word1` = video-record flag, `word2` = 0 | Sets sensitivity (and whether motion records a clip). Ack `0x7a7`: 1B, `01` = success. |

### Sound detection

| Opcode | Direction | Payload | Semantics |
|---|---|---|---|
| `0x7b0` → `0x7b1` | GET | 4B `0` | Reply 16B: `word0` = sound-detect on/off. Only `word0` is used. Sound **sensitivity** is not here — it's params `0x734 @16`. |
| `0x7b2` → `0x7b3` | SET | 16B, `word0` = on/off | Sets sound detection. Ack `0x7b3`: `word0`=1 (success). `word0` is stored as a raw int (writing `0x101` reads back 257); `word1..3` are acked but not stored. |

### Clock

| Opcode | Direction | Payload | Semantics |
|---|---|---|---|
| `0x729` → `0x730` | GET | 4B `0` | Daylight-saving setting. **The reply opcode is `0x730`, not `0x72a`.** Reply 12B: `word0` = 0, `word1` = 1 (constant; supported flag), `word2` = daylight saving on (1) / off (0). Timezone itself is `0x3a0`. |
| `0x727` → `0x728` | SET | 8B: `word0` = 0, `word1` = on (1) / off (0) | Daylight-saving setting. Adds one hour to the offset reported in `0x3a1 word2`. Ack `0x728`: 8B, `word1` = **0 on success**. |

### Notification interval

| Opcode | Direction | Payload | Semantics |
|---|---|---|---|
| `0x792` → `0x793` | GET | 4B `0` | Reply 8B: `word0`=0, `word1` = interval index. |
| `0x790` | SET | 8B, `word1` = interval index | Index enum: **0 = default, 1 = 1 min, 2 = 3 min, 3 = 5 min, 4 = 10 min, 5 = 30 min, 6 = don't notify.** This also rate-limits event pushes (see `60`). |

### Parent-unit active state

| Opcode | Direction | Payload | Semantics |
|---|---|---|---|
| `0x75a` → `0x75b` | GET | 4B `0` | Reply `int@0` = **1 if the physical parent handset is active, 0 if not.** While active, the camera caps video to 720p and rejects quality=2. |
| `0x74f` | push | 4B | Async notify of the same state on change (`int@0`: parent on → 1, off → 0). Available as a push in addition to the poll. |

### Lullaby / soothing sounds

| Opcode | Direction | Payload | Semantics |
|---|---|---|---|
| `0x735` | SET | 8B `[0, track]` (int32 LE) | Start a melody / soothing sound on the camera speaker; `track = -1` stops. Ack **`0x736`** = 8B `[1, track]`. The camera also pushes `0x7ac` subtype 1 and params `0x734 @4` follows. Playback stops after the `@12` timer. Starting a new track while one plays just switches. |

Track index (parent-handset menu order):

| Index | Track | Index | Track |
|---|---|---|---|
| 0 | Play All \* | 5 | Brahms' Lullaby |
| 1 | My Bonnie | 6 | White Noise |
| 2 | Itsy Bitsy Spider | 7 | Crickets |
| 3 | Hot Cross Buns | 8 | Stream |
| 4 | Hush Little Baby | 9 | Birds |

\* **Play All (0)** cycles all tracks internally; `@4` stays `0` and the camera does *not* report which track is currently sounding. A "now playing" index is only available for a single-track selection (1–9).

Timer and volume are set through `0x731` (`word1` / `word2`). Melody playback is loud enough to trigger the camera's own sound detection (a `0x1fff` code `0x15` event).

## Events

Full formats in `60-event-channel.md`.

| Opcode | Direction | Payload | Semantics |
|---|---|---|---|
| `0x1fff` | push | 24B | `IOTYPE_USER_IPCAM_EVENT_REPORT`: live alert. `event_code@16`: `0x01` motion, `0x15` sound, `0x16` temperature too high, `0x17` temperature too low. Rate-limited by the notification interval. |
| `0x788` → `0x789` | GET | 24B: channel, start/end `STimeDay` (UTC), event filter, status filter | `IOTYPE_VTECH_IPCAM_LISTEVENT_REQ/RESP`: stored-event list, returned as ≤252-byte pages of 12-byte records. Only motion events (`event` = 4) are stored. |

## Notes

- `IOTYPE_VTECH_...` names are ThroughTek/VTech SDK constants where published; opcodes shown without an
  `IOTYPE_` name have no published constant and are described by function.
- The low standard-TUTK opcodes (`0x2ff`, `0x301`, `0x322`, `0x326`, `0x32a`) that appear in the
  connect/settings command sequences are the same standard commands listed above.
