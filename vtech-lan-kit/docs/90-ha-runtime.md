# 90 — Running the SDK inside Home Assistant

How to host the SDK in-process in Home Assistant once the library set has been prepared
(`85-ha-deployment.md`): process-wide initialisation, sessions, threading, errors and reconnection,
teardown, fault isolation and streaming. Call signatures and struct layouts are in
`10-transport-session.md`; the IOCtrl model is in `20-ioctrl-framework.md`.

## 1. One loaded SDK per process

- Load the library set **once per process**, and **never unload it**. `ctypes` never calls `dlclose`,
  and the SDK starts internal threads that live inside the libraries' code. Hold the loaded handles in a
  module-level singleton that every config entry shares, created under a lock.
- The SDK's global state is also per process:

  | Call | First call | Repeat call |
  |---|---|---|
  | `TUTK_SDK_Set_License_Key(key)` | `0` | — |
  | `IOTC_Initialize2(0)` | `0` | `IOTC_ER_ALREADY_INITIALIZED` (`-3`) |
  | `avInitialize(max_channels)` | the channel capacity (e.g. `16`) | `AV_ER_ALREADY_INITIALIZED` (`-20031`) |

  Initialise once in the singleton, and treat the two `ALREADY_INITIALIZED` codes as success if a code
  path can reach them twice. Size `max_channels` for every camera the process may connect to.
- `avDeInitialize()` followed by `IOTC_DeInitialize()` is supported, and initialising again afterwards in
  the same process works. There is no benefit to it in HA: stay initialised for the process lifetime and
  never de-initialise on config-entry unload.

## 2. Sessions

- **One session per camera:** `IOTC_Get_SessionID` → `IOTC_Connect_ByUIDEx` → `avClientStartEx`, then
  the per-session prep (`DEVINFO 0x330`; `20-ioctrl-framework.md`). If you also send
  `IOTYPE_INNER_SND_DATA_DELAY` `0x0ff` first, treat its `AV_ER_NOT_SUPPORT` (`-20033`) as harmless.
- Sessions are independent. Several can be open at once in one process, **including more than one to the
  same camera**, and each carries its own IOCtrl traffic.
- `IOTC_Connect_ByUIDEx` returns in well under a second when the camera is reachable. When it isn't, the
  call blocks for up to the connect timeout in the input struct (`@0x94`, seconds).
- **Session IDs and AV channel indexes are reused** after a session is closed. Never cache them across a
  reconnect.

## 3. Threading

Every SDK call blocks. **`ctypes` releases the GIL for the duration of a foreign call**, so a thread
blocked in the SDK doesn't stall other Python threads. SDK calls must still never run on HA's event loop.

- **One reader thread per session owns `avRecvIOCtrl`.** Loop on it with a short timeout (e.g. 1000 ms);
  `AV_ER_TIMEOUT` (`-20011`) and `AV_ER_DATA_NOREADY` (`-20012`) just mean "nothing yet".
  - Route each message by opcode: completed replies go to the waiting request, and pushes (`0x1fff`,
    `0x738`, `0x785`, `0x74f`, `0x7ac`) go to entity updates.
  - Hand results to HA with `hass.loop.call_soon_threadsafe(...)` or the dispatcher helpers.
  - Keep `avRecvIOCtrl` to that one thread per channel.
- **Sends may come from any thread.** `avSendIOCtrl` on a channel while the reader thread is blocked in
  `avRecvIOCtrl` on the same channel is supported: the reply is delivered to the blocked reader. So a
  request is "send from the caller's executor job, then await a future that the reader completes when the
  matching response opcode arrives" (reply opcode = request + 1 unless `30-opcode-reference.md` says
  otherwise).
- **But serialise the sends themselves: one `avSendIOCtrl` per channel at a time.**
  `avSendIOCtrl` blocks until the camera acknowledges the IOCtrl. A second send on the same channel
  while one is in progress fails with `-20021` (`AV_ER_SENDIOCTRL_ALREADY_CALLED`). Seen live when a
  keepalive GET collided with an entity re-read while the camera was slow to ack. Hold a per-session
  lock around the send (not around the wait for the reply).
- **Media gets its own thread, and only while a stream is being consumed.** `avRecvFrameData2` and
  `avRecvAudioData` are polled and return `AV_ER_DATA_NOREADY` (`-20012`) when empty (back off briefly).
  They return `AV_ER_INCOMPLETE_FRAME` (`-20013`) or `AV_ER_LOSED_THIS_FRAME` (`-20014`) for a damaged
  frame (skip it). With a dedicated IOCtrl reader running, the media thread must not also read IOCtrls.
- **Use dedicated threads for these long-lived loops**, not the shared default executor, which they would
  otherwise hold indefinitely. Short one-off calls such as connect, a send or teardown can use
  `hass.async_add_executor_job`.
- **Don't register SDK callbacks**, with one exception. Everything except talk-back is plain
  call/return; native-to-Python callbacks on SDK-internal threads add risk. Talk-back's `avServStart3`
  needs an auth callback (section 8): keep the `CFUNCTYPE` object referenced until the server is stopped,
  and keep it trivial (return 1) so it cannot raise.

## 4. Errors and reconnection

| Code(s) | Meaning | What an integration should do |
|---|---|---|
| `IOTC_ER_DEVICE_OFFLINE` (`-90`), `IOTC_ER_TIMEOUT` (`-13`), `IOTC_ER_CAN_NOT_FIND_DEVICE` (`-19`), `IOTC_ER_NETWORK_UNREACHABLE` (`-41`), `IOTC_ER_DEVICE_NOT_LISTENING` (`-24`) | Camera not reachable (powered off, off the network, not on the same L2 segment) | Mark entities unavailable; retry with backoff (e.g. 10 s growing to 5 min). During setup, raise `ConfigEntryNotReady`. |
| `IOTC_ER_DEVICE_REJECT_BY_WRONG_AUTH_KEY` (`-68`), `AV_ER_WRONG_VIEWACCorPWD` (`-20009`) | Credentials wrong, or rotated on the camera side (a Wi-Fi change or "update security key" rotates the AV password, and the authKey co-rotates) | **Don't retry in a loop.** Start re-authentication (`ConfigEntryAuthFailed`): ask for a fresh pairing QR and re-provision (`05-provisioning.md`) rather than for typed credentials — the new AV password and authKey fall out of it. |
| `IOTC_ER_UNLICENSE` (`-10`) | License key missing or invalid | Setup error, surfaced to the user. |
| `IOTC_ER_EXCEED_MAX_SESSION` (`-18`), `IOTC_ER_DEVICE_EXCEED_MAX_SESSION` (`-48`), `AV_ER_EXCEED_MAX_CHANNEL` (`-20002`) | Session or channel capacity exhausted (local or camera side) | Close stale sessions; raise `max_channels`; retry later. |
| `AV_ER_SESSION_CLOSE_BY_REMOTE` (`-20015`), `AV_ER_REMOTE_TIMEOUT_DISCONNECT` (`-20016`), `IOTC_ER_SESSION_CLOSE_BY_REMOTE` (`-22`), `IOTC_ER_REMOTE_TIMEOUT_DISCONNECT` (`-23`), `AV_ER_INVALID_SID` (`-20010`), `IOTC_ER_INVALID_SID` (`-14`) | An established session is gone | Tear down (section 5), mark entities unavailable, and reconnect with backoff. |
| `AV_ER_TIMEOUT` (`-20011`), `AV_ER_DATA_NOREADY` (`-20012`) | No data within the timeout | Normal; keep looping. |
| `AV_ER_INVALID_ARG` (`-20000`) on a receive | The channel index is no longer valid (the channel was stopped) | Treat as session gone; reconnect. |

Other points:
- **Liveness:** an idle session stays up, and the camera sends unsolicited `0x738` temperature pushes on
  it. Because a dropped camera may simply fall silent, also send a periodic cheap GET (e.g. `0x733` every
  60 s). A request that gets no reply after a few attempts is a dead session: tear down and reconnect.
- **After every reconnect:** redo the per-session prep (`DEVINFO`), re-read all state (entities
  may have changed while the session was down), and re-run the live-view handshake if a stream is active
  (`50-media-decryption.md`; the key is per session).
- A camera that re-joins the network after being offline reconnects with a plain new
  `IOTC_Connect_ByUIDEx`; nothing needs resetting on the host.

## 5. Teardown

- Close a session with **`avClientStop(av)` followed by `IOTC_Session_Close(sid)`**. Stop the session's
  reader and media threads first, or treat `AV_ER_INVALID_ARG` (`-20000`) in them as the signal to exit.
  A receive on a stopped channel index returns that code.
- On config-entry unload, close that entry's sessions only. Keep the libraries loaded and the SDK
  initialised (section 1).
- After a close, a new connect in the same process works normally, and may be handed the same session ID
  and channel index.

## 6. Fault isolation

The SDK and companion run as native code **inside the Home Assistant process**, so a crash in native code
(a segfault, or an `abort` from a stack or FORTIFY check) terminates Home Assistant itself. Keep the
blast radius small:
- Pass exact buffer sizes (`bufMax`) matching the buffers you allocate, and never hand the SDK a Python
  `bytes` object it may write into; use `ctypes.create_string_buffer`.
- Serialise use of a given channel as described in section 3.
- Gate loading on `patch_sdk()`'s verification and on `preflight()` (`85-ha-deployment.md`).

**Out-of-process alternative:** host the SDK in a child Python process, e.g. started with
`multiprocessing` in `spawn` mode or as a subprocess, and talk to it over a pipe. A native crash then
kills only the child, which the integration restarts like a reconnect. The cost is IPC for every IOCtrl
and for the media stream. The in-process design is simpler; the out-of-process one is safer.

## 7. Feeding a `camera` entity

- Decrypted video is H.264 and audio is G.711 A-law, 8 kHz mono (`40-media-streaming.md`,
  `50-media-decryption.md`). HA's stream pipeline wants a URL: `stream_source()` is registered with HA's
  go2rtc (a separate process, for WebRTC; it adds its own Opus transcode) and opened by the stream
  component (HLS). The design that works: mux in-process with PyAV, which ships with HA — H.264 as
  packets with arrival timestamps, A-law re-encoded to **AAC** (go2rtc's MPEG-TS reader and HLS both take
  AAC; neither takes PCMA) — into MPEG-TS served on a **loopback-only** HTTP endpoint. Avoid an `ffmpeg`
  subprocess fed raw streams on pipes: raw elementary streams carry no timestamps, and a single blocking
  writer deadlocks while ffmpeg probes one input. With PyAV, set the muxer's `max_interleave_delta` low
  (its 10 s default withholds all output from a live stream). New readers should start at a keyframe.
- **Run the media path on demand only.** Start it when a stream or snapshot is requested and stop it
  when the last viewer goes away; the control and telemetry session stays up independently. Streaming
  costs real CPU (decryption plus remux); control does not need it.
- For snapshots (`async_camera_image`), decode the first keyframe after the stream starts, and cache it
  briefly: dashboard thumbnails ask every few seconds and would otherwise keep restarting the stream.
- Clean the SDK buffers before each `START` (`40-media-streaming.md`), and stop only after a short grace
  period: go2rtc closes and reopens its connection, so an immediate stop/start would churn the camera.
- Resolution is camera-side state (`0x320`), and the camera caps it to 720p while a parent handset is
  active (`0x74f` / `0x75a`). Applying a night-vision mode change briefly interrupts video
  (`80-ha-feature-mapping.md`).

## 8. Talk-back

The procedure is in `40-media-streaming.md`; the reference client's `talkback()` implements it.
- **Source audio:** anything that can be decoded to 8 kHz mono 16-bit PCM, e.g. the output of a Home
  Assistant TTS engine or a media file, converted with the `ffmpeg` binary that ships with HA. Encoding to
  A-law and encrypting each 20 ms frame is cheap enough to do in Python in real time.
- **Session master:** talk-back needs the master from the `0x820`/`0x821` handshake. It does not need
  video to be running. Reuse the master if a stream is active in the same session; otherwise run the
  handshake first. The master is per session, so redo it after every reconnect (section 4).
- **Threading:** `avServStart3` blocks until the camera connects (well under a second when reachable),
  and sending is paced in real time. Run a whole talk-back on a dedicated thread, never on the event
  loop. It uses a separate IOTC channel and AV index, so it doesn't disturb the IOCtrl reader thread.
- **One at a time per camera.** Serialise talk-back requests (e.g. a lock per camera) and always run the
  stop sequence, including on error, so the channel is released.
- **Half-duplex:** camera audio is stopped (`AUDIOSTOP`) while talking. If a stream with audio is
  active, restart it (`AUDIOSTART`) afterwards; `talkback(..., resume_audio=True)` does this.
- **Side effect:** audio played through the speaker can trigger the camera's own sound detection (a
  `0x1fff` `0x15` event). Suppress or tag sound events that arrive during and shortly after a talk-back.
