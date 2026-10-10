# AGENTS.md: vtech_baby

Notes for working on this repo. User-facing docs are in `README.md`; keep that short and put the detail here.

## What it is

A Home Assistant integration (domain `vtech_baby`) that controls and monitors the **VTech RM5754HD**
baby-monitor camera **LAN-direct, with no cloud**. The camera speaks only ThroughTek's Kalay P2P
protocol (TUTK), so the integration drives the vendor's Android SDK libraries in-process. They are
patched at setup to load on HA's musl or glibc libc. Everything above the transport (opcodes, live-view
crypto) is implemented in Python. Not affiliated with VTech or ThroughTek.

## Layout

| Path | What |
|---|---|
| `custom_components/vtech_baby/` | **The package (what HACS installs).** `manifest.json`, `__init__.py`, platform modules, `vtech_lan/` (protocol library), `tooling/` (vendored SDK patcher), `native/<arch>/libtk.so` (prebuilt companion, built from `vtech-lan-kit/tooling/companion/`), `brand/`. |
| `vtech-lan-kit/` | The protocol kit: `docs/` (the spec), `client/` (reference client and QR provisioning), `tooling/` (SDK patcher and companion sources), `sdk/README.md`. |
| `vtech-lan-kit/sdk/<arch>/` | Local development copies of the SDK. **Gitignored; never commit them** (see Rules). |
| `tests/` | Offline tests (fake SDK, fake manager); `tests/live/` probes for a real camera. |
| `HA_INTEGRATION.md` | The development plan and history: decisions, layout, phases, entity map, workflow, risks. |
| `images/` | README screenshots (not part of the package). `device.png` is the device page, its cards laid out in three columns. |
| `brand-src/` | `icon.svg` (original, hand-written; don't replace it with VTech artwork) and `render.sh` (needs inkscape) for `brand/`. |
| `sync_vendored.sh` | Refreshes `tooling/` and `native/` from the kit. |

## Read first

| Doc | Why |
|---|---|
| `HA_INTEGRATION.md` | **Design authority.** §6 "Status and next steps", §7 entity map, §9 risks. |
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
  package (`vtech_lan/sdk.py`, `vtech_lan/provision.py`), matching the kit (a test enforces it).
- Per-camera credentials (UID, AV password, derived authKey; account `admin`) come from the owner's
  **pairing QR** and live only in the config entry.
- **Never write a real camera's credentials (or a real pairing QR) into code, tests, fixtures, docs or
  commits.** Live tests take them from `VTECH_UID`, `VTECH_PWD`, `VTECH_AUTHKEY`, `VTECH_ACCOUNT`
  (`vtech-lan-kit/client/vtech_provision.py --env` prints them). Offline tests and the kit self-test use
  synthetic QRs (UID `TESTUID0000000000001`).

### Code
- `vtech_lan/` has **no `homeassistant` imports**, so it can be unit-tested and run standalone.
- **Runtime model** (`vtech-lan-kit/docs/90-ha-runtime.md`):
  - Load and initialise the SDK once per process, and never unload it.
  - Never make an SDK call on the event loop.
  - One reader thread owns `avRecvIOCtrl` for each session.
  - Long-lived loops run on dedicated threads, not the shared executor.
  - Pass buffers from `ctypes.create_string_buffer` with exact sizes.
- **Reply opcodes are listed explicitly per op**, never computed as request + 1: `0x709 → 0x710` and
  `0x729 → 0x730` break that rule (`HA_INTEGRATION.md` §5.1).
- **The kit is the spec.** If you find a protocol fact that is new or wrong, fix `vtech-lan-kit/docs/` in
  the same change.
- **Vendored copies match the kit.** `tooling/` and `native/` are refreshed together by
  `sync_vendored.sh`, which records the kit revision in `native/SOURCE` (`HA_INTEGRATION.md` §3.1). The
  revision there now is from before this repo existed; the next sync replaces it.
- **Provenance tags.** Label claims in the docs **[proven]**, **[design]** or **[verify]**.

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
  Run tests from the root: the package has modules named like stdlib modules (`select.py`), so never
  put the package directory itself first on `sys.path`.
- **Live tests:** `tests/live/run.sh` runs a probe inside the real HA image on a host on the camera's
  LAN, through a runner script you supply as `$VTECH_LIVE_RUNNER` (see the script header).
  `probe_phase1.py` is read-only and safe headless; check what any other probe does first.
- **hassfest:** `podman run --rm -v "$PWD:/github/workspace:ro,Z" ghcr.io/home-assistant/hassfest` from
  the root. CI (`.github/workflows/validate.yml`) runs it and the HACS check.
- **Try a change in HA:** copy `custom_components/vtech_baby` (only that) into a test instance's
  `custom_components/` and restart HA.

## Conventions

- Keep `README.md` short: what it does, what you need, how to install it and add a camera.
- Outstanding work goes in `TODO.md`; open protocol questions stay in `HA_INTEGRATION.md` §5.2 and §9.
- Releases are GitHub releases (HACS ignores bare tags). The tag matches `version` in `manifest.json`.
- Nothing about any particular network or deployment belongs here.
