# AGENTS.md: vtech_baby

Notes for working on this repo. User-facing docs are in `README.md`; keep that short and put the detail here.

## What it is

A Home Assistant integration (domain `vtech_baby`) that controls and monitors the **VTech RM5754HD**
baby-monitor camera **LAN-direct, with no cloud**. The camera speaks only ThroughTek's Kalay P2P
protocol (TUTK), so the integration drives the vendor's Android SDK libraries, in a worker process of
its own so a crash in them can't take Home Assistant down. They are patched at setup to load on HA's
musl or glibc libc. Everything above the transport (opcodes, live-view
crypto) is implemented in Python. Not affiliated with VTech or ThroughTek.

## Layout

| Path | What |
|---|---|
| `custom_components/vtech_baby/` | **The package (what HACS installs).** `manifest.json`, `__init__.py`, platform modules, `vtech_lan/` (protocol library), `tooling/` (vendored SDK patcher), `native/<arch>/libtk.so` (prebuilt companion, built from `vtech-lan-kit/tooling/companion/`), `brand/`. |
| `vtech-lan-kit/` | The protocol kit: `docs/` (the spec), `client/` (reference client and QR provisioning), `tooling/` (SDK patcher and companion sources), `sdk/README.md`. |
| `vtech-lan-kit/sdk/<arch>/` | Local development copies of the SDK. **Gitignored; never commit them** (see Rules). |
| `tests/` | Offline tests (fake SDK, fake manager); `tests/live/` probes for a real camera. |
| `images/` | README screenshots (not part of the package). `device.png` is the device page, its cards laid out in three columns. |
| `brand-src/` | `icon.svg` (original, hand-written; don't replace it with VTech artwork) and `render.sh` (needs inkscape) for `brand/`. |
| `sync_vendored.sh` | Refreshes `tooling/` and `native/` from the kit. |

## Read first

| Doc | Why |
|---|---|
| "Design notes" below | Decisions, the entity map, open protocol questions and known issues. |
| `vtech-lan-kit/docs/README.md` → `00`–`95` | **Protocol authority**: opcodes, payloads, pushes, crypto, talk-back. |
| `vtech-lan-kit/docs/05-provisioning.md` | Credentials come from the pairing QR: UID + AV password decoded, authKey derived. |
| `vtech-lan-kit/docs/85-ha-deployment.md`, `90-ha-runtime.md` | How the native layer ships, patches, loads, threads, fails and reconnects inside HA. Follow these; don't re-derive them. |
| `vtech-lan-kit/client/vtech_camera_client.py` | Working reference implementation. Use it to check behaviour. |
| `vtech-lan-kit/tooling/README.md` | How the SDK patch and the companion `libtk.so` work. |

## Rules

### Proprietary material and secrets
- **The SDK libraries are user-supplied**: the user uploads ThroughTek's Kalay Mobile app (or any zip
  of the four libraries) in the config flow, and `tooling/extract_sdk.py` keeps the four libraries. Only SDK **4.3.8.1** is accepted (checked
  from the libraries' version string, not a hash). The package must never ship, download or commit them,
  patched or not (`vtech-lan-kit/sdk/README.md`, `docs/85-ha-deployment.md`). `.gitignore` excludes
  `vtech-lan-kit/sdk/*/` and every `.so` except our own `libtk.so`.
- The SDK license key and the QR/HKDF provisioning constants are app-wide values **embedded** in the
  package (`vtech_lan/sdk.py`, `vtech_lan/provision.py`), matching the kit's reference client (a test
  enforces it). They live in code only: the docs name and describe them but never print the values.
- Per-camera credentials (UID, AV password, derived authKey; account `admin`) come from the owner's
  **pairing QR** and live only in the config entry.
- **Never write a real camera's credentials (or a real pairing QR) into code, tests, fixtures, docs or
  commits.** Live tests take them from `VTECH_UID`, `VTECH_PWD`, `VTECH_AUTHKEY`, `VTECH_ACCOUNT`
  (`vtech-lan-kit/client/vtech_provision.py --env` prints them). Offline tests and the kit self-test use
  synthetic QRs (UID `TESTUID0000000000001`).

### Code
- `vtech_lan/` has **no `homeassistant` imports**, so it can be unit-tested and run standalone.
- **The SDK only ever runs in the worker process** (`vtech_lan/worker.py`, started by
  `vtech_lan/worker_client.py` through `_worker_main.py`). Home Assistant's process patches the libraries
  but never loads them. The worker imports no Home Assistant code; keep it that way (`tests/test_worker.py`
  checks).
- **Messages between HA and the worker** are pickles over a socket pair (`vtech_lan/ipc.py`). The
  session proxy forwards any public `Session` method by name, so a new `Session` method needs no IPC
  change. Live-view video never crosses the socket: the worker serves it over loopback HTTP.
- **Runtime model** (`vtech-lan-kit/docs/90-ha-runtime.md`), inside the worker:
  - Load and initialise the SDK once per worker, and never unload it; a new SDK means a new worker.
  - Never make an SDK call on the event loop.
  - One reader thread owns `avRecvIOCtrl` for each session.
  - Long-lived loops run on dedicated threads, not the shared executor.
  - Pass buffers from `ctypes.create_string_buffer` with exact sizes.
- **Reply opcodes are listed explicitly per op**, never computed as request + 1: `0x709 → 0x710` and
  `0x729 → 0x730` break that rule.
- **The kit is the spec.** If you find a protocol fact that is new or wrong, fix `vtech-lan-kit/docs/` in
  the same change.
- **Vendored copies match the kit.** `tooling/` and `native/` are refreshed together by
  `sync_vendored.sh`, which records the kit revision in `native/SOURCE`.
- **Facts, not derivations.** The kit and the code comments state what the protocol is and how the
  camera behaves, never how it was found: no sources, tools, captures, dates or references to private
  notes. Where something is unknown or unconfirmed, say so in words.

### The camera is a live baby monitor
- **Anything that makes light or sound at the camera needs the owner present.** That covers the
  night-light, the status LED, lullabies, talk-back and night-vision mode. Never run it headless;
  ask first.
- **Restore every setting you change**: motion and sound detection, temperature alerts, notification
  interval, resolution, night-vision mode, volume and timer. Read the value first, then write it back.
- The physical **parent handset** can change settings and caps video at 720p while it's on. Don't treat
  a value that changed on its own as a bug until you've checked the handset.

## Development

- **Camera reachability:** only from its own L2 segment. Connecting is by UID; LAN discovery is a subnet
  broadcast the camera answers on fixed UDP **32762**, so the client calls
  `IOTC_Set_LanSearchPort(32762)` (the SDK default 32761 is unanswered).
- **Offline tests:**
  `python3 -m venv .venv && .venv/bin/pip install -r requirements_test.txt`, then `.venv/bin/pytest`
  from the repo root. The tests that need the real SDK look in `vtech-lan-kit/sdk/<arch>/` (or
  `$VTECH_SDK_DIR`) and in `.build/<arch>/` for a patched set
  (`vtech-lan-kit/tooling/patch.sh vtech-lan-kit/sdk/x86_64 .build/x86_64`); they skip without them.
  Run tests from the root. Platform modules are named by Home Assistant (`select.py`, `sensor.py`, ...)
  and shadow other modules of the same name, so never put `custom_components/vtech_baby` (or
  `vtech_lan/`) on `sys.path` ahead of the standard library; the worker refuses to start if it is there
  at all.
- **Live tests:** `tests/live/run.sh` runs a probe inside the real HA image on a host on the camera's
  LAN, through a runner script you supply as `$VTECH_LIVE_RUNNER` (see the script header).
  `probe_phase1.py` is read-only and safe headless; check what any other probe does first.
- **hassfest:** `podman run --rm -v "$PWD:/github/workspace:ro,Z" ghcr.io/home-assistant/hassfest` from
  the root. CI (`.github/workflows/validate.yml`) runs it and the HACS check.
- **Try a change in HA:** copy `custom_components/vtech_baby` (only that) into a test instance's
  `custom_components/` and restart HA.

## Conventions

- Keep `README.md` short: what it does, what you need, how to install it and add a camera.
- Outstanding work goes in `TODO.md`; open protocol questions and known issues go in "Design notes" below.
- Releases are GitHub releases (HACS ignores bare tags). The tag matches `version` in `manifest.json`.
- Nothing about any particular network or deployment belongs here.

## Design notes

### Decisions
- **The SDK runs in a worker process** (`90-ha-runtime.md` §6). One worker per HA process serves every
  camera. It is started with `subprocess` and `python -I`, and imports the package's `vtech_lan` without
  running the integration's `__init__` (`_worker_main.py` registers the parent packages as empty
  modules), so it holds no Home Assistant code and `vtech_lan` classes cross the IPC under their own
  names. If it dies, every session is lost with `WorkerDied`, a Repairs issue (`worker_crashed`, not
  persistent) records why, and the next reconnect starts a new worker. A new SDK upload restarts it too,
  so switching SDKs no longer needs an HA restart.
- **The live view is served by the worker** (MPEG-TS over HTTP on 127.0.0.1). HA binds the listening
  socket once and passes it to every worker, so the URL (port and token) HA's go2rtc has registered
  survives worker restarts and entry reloads.
- **`vtech_lan/` is a rewrite, not a wrapper, of the reference client.** The client reads replies inline
  and initialises the SDK per session; HA needs one reader thread per channel and one init per process.
- **Pushes drive state** (`local_push`). GETs run on connect, after a reconnect, on the keepalive, and to
  confirm a write.
- **LAN-only, always.** The companion's egress policy keeps the SDK off the internet, the client sets
  `IOTC_Set_LanSearchPort(32762)`, and `Session.connect()` refuses any session whose mode isn't LAN
  (`NotLanDirect`). Without that, the SDK falls back to ThroughTek's relay.
- **Several cameras share one SDK** per process (`vtech_lan.sdk.load()`, 16 AV channels); loading it again
  with a different library dir or licence is refused.

### Entity map

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
| Live view | `camera` (`STREAM`): loopback MPEG-TS (H.264 + AAC); snapshots from keyframes | — | media path |
| Device | device registry | — | `0x330 → 0x331` model / vendor / firmware |

Deliberately not exposed: daylight saving (`0x727`/`0x729`), timezone (`0x3a0`), `0x7ba`. Planned: a
`media_player` for lullabies and talk-back (`TODO.md`).

### Open protocol questions
- No ack is known for `0x790` (notification interval SET): it's fire-and-forget, confirmed by re-reading
  `0x792`.
- What `0x321` carries when the camera refuses `quality = 2` while the parent unit is on is unknown;
  every resolution write is confirmed by re-reading `0x322`.
- Ack encodings differ per opcode (`0x7a3` = 1 byte `01`; `0x712` = `int@0 = 0`; `0x716` = `int@0 = 1`;
  `0x728` = `word1 = 0`; `0x732` = `int@0 = 1`), so each op in `ops.py` has its own success check.
- The real length of notification interval index 0 ("default"), and whether index 6 ("never") also stops
  `0x1fff` pushes to LAN clients.

### Known issues
- **Stale credentials look like an offline camera.** After a Wi-Fi change or a re-pair the AV password
  and authKey rotate (the UID doesn't), and a stale authKey makes the connect time out (`-13`) rather
  than fail with `-68`. The manager counts consecutive unreachable connects while the camera's MAC is on
  the LAN (`STALE_CREDS_FAILURES`) and then starts reauth, which asks for a fresh pairing QR.
- **Post-connect IOCtrl stall.** Occasionally, a `0x731` write within about a minute of (re)connecting is
  followed by ~35–40 s with no replies. The session isn't lost and recovers by itself; the cause is
  unknown and a standalone repro (`tests/live/probe_stall.py`) hasn't triggered it. Re-reads keep their
  previous values and stop after two timeouts, and a write during the stall fails cleanly.
- **A native crash in the SDK or the companion kills the worker**, and with it every camera's session
  until the reconnect (10 s backoff). Home Assistant keeps running. The worker's stderr (the
  container log) has the faulthandler dump of its Python threads.
