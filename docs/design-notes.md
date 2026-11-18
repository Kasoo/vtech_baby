# Design notes

Decisions behind the integration, the map from entities to protocol opcodes, the open protocol questions
and the known issues.

## Decisions
- **The module runs in Home Assistant's process** (decided 2026-10-24, done 2026-10-29). The worker
  process existed to keep the vendor's C libraries' crashes out of HA; with only Rust left, a panic
  unwinds into an exception instead. What could still take HA down is a stack overflow, an abort (an
  allocation failure, a panic during unwind) or a bug in a dependency's `unsafe` code (dimpl,
  RustCrypto SIMD); `vtech-link-core` itself is `forbid(unsafe_code)`. Tag `worker-process-v1` is
  the last state with the worker.
- **One hub per HA process** serves every camera. A config entry's manager gets its live view and
  talk-back from the hub; they run on whichever session the manager last connected with `media=True`.
- **The live view is served by the hub** (MPEG-TS over HTTP on 127.0.0.1, its own threads, off the
  event loop). The server lives as long as the hub, so the URL (port and token) HA's go2rtc has
  registered survives entry reloads.
- **`vtech_lan/` is a rewrite, not a wrapper, of the reference client.** The client reads replies inline
  and initialises the SDK per session; HA needs one reader thread per channel and one init per process.
- **Pushes drive state** (`local_push`). GETs run on connect, after a reconnect, on the keepalive, and to
  confirm a write.
- **LAN-only, always.** vtech-link implements only LAN-direct sessions: it sends only to directly
  attached IPv4 subnets (host or directed broadcast) or 255.255.255.255, never resolves names, and has
  no P2P or relay path. `lan_only_stats()` counts refused sends (`vtech-link-design/00-requirements.md`, R5).
- **Several cameras share one module** per process (`vtech_lan.link.load()`).
- **`vtech-link` comes from PyPI** (decided 2026-11-06): HACS installs one zip on every host and pip picks
  the wheel for the arch and libc, so no binaries live in git. In a container, Home Assistant installs
  requirements into the container, so it fetches the wheel again each time the container is recreated.
- **The security mode comes from the pairing QR** (2026-11). A version-5 credential of type 5 is an
  enhanced camera (authKey + DTLS), type 4 and every version-1 record a plain one (no authKey, no
  DTLS); the entry stores it as `plain` (absent = enhanced, so older entries need no migration). A
  camera that answers "no key authentication" (`-69`) when given a key is connected again at once in
  plain mode (`LinkSession._open`), and the manager rewrites its entry as plain. The opposite (a plain
  entry whose camera wants a key) is a `-68` and goes to reauth like rotated credentials.
- **One config entry per camera, also for a QR with several** (2026-11). The config flow lists the QR's
  cameras that aren't configured yet and offers a picker when more than one is left; the same QR adds
  the next. Cameras are matched by UID, never by the QR's camera index, and reauth takes the record with
  the entry's UID.
- **Version 2 config entries** (2026-10): version 1 ran on the vendor's SDK and stored its directory;
  `async_migrate_entry` drops it and deletes the extracted and patched libraries under the HA config
  directory.

## Entity map

| Entity | Platform | Write | Read / state |
|---|---|---|---|
| Night-light | `light`, colour temperature only, 500–2900 K: red 500, orange 1700, yellow 2900 (band edges 1100 / 2300). A request snaps to the nearest preset; brightness is fixed at 255; a plain `turn_on` restores the last colour. An unchanged result is re-sent as a forced state write so the dialog's sliders snap back. | `0x7a2` (ack `0x7a3`) | `0x7a0`; push `0x7ac` sub 0 |
| Status LED | `switch` (wire flag inverted: 1 = off) | `0x711` (ack `0x712`) | `0x709 → 0x710`; push `0x7ac` sub 8 |
| Night vision | `select` Auto / On / Off | `0x715` (ack `0x716`) | `0x713` |
| Night vision active | `binary_sensor` | — | push `0x785` |
| Resolution | `select` 1080p / 720p; 1080p refused while the parent unit is on (it would replace the stored preference) | `0x320` | `0x322`; re-read after a write and 3 s / 10 s after each `0x74f` |
| Parent unit active | `binary_sensor` | — | `0x75a`; push `0x74f` |
| Temperature | `sensor` °C | — | `0x733 @0`; push `0x738` |
| Temperature state | `sensor` enum ok / too_hot / too_cold | — | push `0x738 @8` |
| Temperature alerts | `switch` | `0x74d` (ack `0x74e`) | `0x74b`; push `0x7ac` sub 2 |
| Temp alert above / below | 2 × `number` °C, 0.1 step (×10 on the wire) | `0x74d` | `0x74b` |
| Motion sensitivity | `select` Off / Low / Medium / High | `0x7a6` (keeps the record flag) | `0x7a4` |
| Sound detection | `switch` | `0x7b2` (ack `0x7b3`) | `0x7b0` |
| Sound sensitivity | `sensor`, read-only (set on the parent handset) | — | `0x734 @16`; re-read on `0x738` |
| Notification interval | `select` Default / 1 / 3 / 5 / 10 / 30 min / Never | `0x790` | `0x792 → 0x793` |
| Lullaby timer | `select` 15 / 30 / 60 min / Non-stop | `0x731` word1 | `0x734 @12`; push `0x7ac` sub 0x15 |
| Alerts | `event` motion / sound / too_hot / too_cold | — | push `0x1fff` |
| Motion, Sound | 2 × `binary_sensor`, auto-off after the notification interval + 15 s (min 30 s) | — | push `0x1fff` |
| Speaker | `media_player`. Sources = the 10 lullabies; stop; volume 0–5 (5 = 1.0; the range is unconfirmed). Play / announce media (TTS, media sources, http(s) URLs) as talk-back, decoded as it plays (PyAV); `play_media` returns when the media has played, new media replaces old. Media stops a lullaby; an announcement restarts it afterwards. Sound alerts from either are tagged self-triggered. | Lullaby `0x735` (ack `0x736`); volume `0x731` word2; talk-back `0x32a`, `0x820`, `0x301`, `0x350`, AV server channel, stop `0x351` (`0x300` if the live view runs) | `0x734 @4`, `@20`; push `0x7ac` sub 1, `0x14` |
| Talk card | `custom:vtech-talk-card` (`frontend/`, loaded on every dashboard by `talk.py`): hold to talk. Websocket subscription `vtech_baby/talk` (`entity_id`, `sample_rate`); event `ready` gives a binary handler id, then binary messages of 16-bit mono PCM, an empty one to end; last event `end` or `error`. The audio plays with `play_media` (announce), content id `vtech_baby_talk:<id>`: the speaker takes the talk's audio from memory (`vtech_lan.audio.LiveAudio`) and decodes it as it arrives, with no HTTP, so a reverse proxy in front of HA can't block it. Ends on release, unsubscribe, disconnect, 3 s without audio or 120 s. Any user with control of the speaker; one talk at a time per speaker (`busy`), since replacing an announcement loses the lullaby it would resume. | as the speaker | — |
| Live view | `camera` (`STREAM`): loopback MPEG-TS (H.264 + AAC); snapshots from keyframes | — | media path |
| Device | device registry | — | `0x330 → 0x331` model / vendor / firmware |

Deliberately not exposed: daylight saving (`0x727`/`0x729`), timezone (`0x3a0`), `0x7ba`.

## Open protocol questions
- No ack is known for `0x790` (notification interval SET): it's fire-and-forget, confirmed by re-reading
  `0x792`.
- What `0x321` carries when the camera refuses `quality = 2` while the parent unit is on is unknown;
  every resolution write is confirmed by re-reading `0x322`.
- Ack encodings differ per opcode (`0x7a3` = 1 byte `01`; `0x712` = `int@0 = 0`; `0x716` = `int@0 = 1`;
  `0x728` = `word1 = 0`; `0x732` = `int@0 = 1`), so each op in `ops.py` has its own success check.
- The real length of notification interval index 0 ("default"), and whether index 6 ("never") also stops
  `0x1fff` pushes to LAN clients.
- Plain mode is confirmed live for the connect, login, device info and parameter reads; its live view
  (key exchange, media) and talk-back run in the simulator only. Whether a version-1 QR can belong to
  an enhanced camera is unconfirmed (`vtech-lan-kit/docs/05-provisioning.md`); such a camera would
  reject the keyless connect with `-68` and land in reauth.
- A plain link ignores a short LAN reply and waits for the long one; the owner's plain camera sends the
  long one. A plain camera that answered only with a short reply would look unreachable.
- Whether a genuinely rotated (old) authKey gets the same explicit `-68` reject as an arbitrary wrong one.
  The manager assumes it does: only `AuthFailed` starts reauth, and an unreachable camera (no search
  reply) is retried forever. Stale credentials that went silent instead would show as a camera that
  never comes back.

## Known issues
- **Post-connect IOCtrl stall.** Occasionally, a `0x731` write within about a minute of (re)connecting is
  followed by ~35–40 s with no replies. The session isn't lost and recovers by itself; the cause is
  unknown and a standalone repro (`tests/live/probe_stall.py`) hasn't triggered it. It happened in HA
  with a speaker volume write (`0x731` word2) about 25 s after the connect: for the next ~30 s,
  `0x32b` (talk-back's audio format) and `0x736` (lullaby) got no reply. Re-reads keep their
  previous values and stop after two timeouts, and a write during the stall fails cleanly. What a full
  read timed out on or skipped is read again 30, 60 and 120 s later (`UNREAD_REREAD_S`); a read at
  reconnect keeps those fields' values from before the session was lost until then.
- **An unanswered password check is ambiguous.** The camera answers a wrong AV password with silence in
  the DTLS handshake (`av_auth_failed`/`dtls_silent`, `AuthUnanswered`), and a lost reply looks the
  same. The manager starts reauth only after three in a row (`AUTH_UNANSWERED_TRIES`): at setup they
  are tried back to back, on reconnect with the usual backoff. An explicit rejection (a DTLS alert, a
  refused login, a wrong authKey) starts reauth at once. The config flow's probe isn't retried.
- **A live view that stops bringing video is ended** after 15 s (`NO_VIDEO_S`), like one that brings no
  first keyframe within 10 s: subscribers get end of stream, and the next one sends STOP and START
  again. The loopback HTTP handler doesn't watch its client; it relies on the stream ending.
- **A native crash in the module kills Home Assistant** (see "Decisions" for what can still cause
  one). A Rust panic doesn't: it loses that camera's session, which reconnects.
