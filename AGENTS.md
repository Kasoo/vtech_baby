# AGENTS.md: vtech_baby

Notes for working on this repo. User-facing docs are in `README.md`; keep that short and put the detail here.

## What it is

A Home Assistant integration (domain `vtech_baby`) that controls and monitors the **VTech RM5754HD**
baby-monitor camera **LAN-direct, with no cloud**. The camera speaks only ThroughTek's Kalay P2P
protocol (TUTK). The integration has its own implementation of the transport, vtech-link
(`vtech-link/`, Rust), a Python extension module published to PyPI, loaded into Home Assistant's
process. The opcode layer above the transport is Python. Not affiliated with VTech or ThroughTek.

## Layout

| Path | What |
|---|---|
| `custom_components/vtech_baby/` | **The package (what HACS installs).** `manifest.json`, `__init__.py`, platform modules, `vtech_lan/` (opcode layer, sessions, the hub), `brand/`. Pure Python: `manifest.json` pins `vtech-link`, which Home Assistant installs from PyPI. |
| `vtech-link/` | The transport: protocol core, CLI, simulator, Python module (`_vtech_link`). Status and next steps: `TODO.md` ("vtech-link"). |
| `tutk-host/` | Not shipped. Our earlier implementation of the three TUTK SDK libraries, which the package used before vtech-link (tag `dual-backend-v1`); kept for A/B runs against vtech-link. See `tutk-host/NOTES.md`. |
| `tutk-sdk/` | Archived (tag `tutk-sdk-dropin-v1`): the drop-in build of the same libraries for the patched path. |
| `docs/vtech-link-design/` | Its design, with a "Status" section per document for what was built and how it differs. Start at its `README.md`. |
| `docs/vtech-lan-kit/` | The protocol kit: `docs/` (the spec), `client/` (reference client on the vendor's SDK, and QR provisioning), `tooling/` (SDK patcher and companion sources), `sdk/README.md`. The package uses none of the kit's code. |
| `docs/vtech-lan-kit/sdk/<arch>/` | Local development copies of the vendor's SDK, for the reference client. **Gitignored; never commit them** (see Rules). |
| `tests/` | Offline tests (`fake_link.py` stands in for `_vtech_link`, `fake_camera.py` for the camera); `tests/live/` probes for a real camera. |
| `images/` | README screenshots (not part of the package). `device.png` is the device page, its cards laid out in three columns; `social-preview.png` is the GitHub social preview, rendered from `brand-src/social.svg` and uploaded by hand (Settings → General → Social preview). |
| `brand-src/` | `icon.svg` (original, hand-written; don't replace it with VTech artwork), `social.svg` (the social preview, built from the icon and `images/device.png`) and `render.sh` (needs inkscape and Open Sans) for `brand/` and `images/social-preview.png`. |
| `tools/vtech_cloud_credentials.py` | Standalone helper (stdlib only, not part of the package): logs in to the user's VTech cloud account and prints each camera's UID and AV password, for the typed-credentials option. User guide: `CREDENTIALS.md`; protocol: kit `06-cloud-credentials.md`; tests: `tests/test_cloud_credentials.py`. The integration never uses the cloud. |
| `publish.sh` | Takes a GitLab release to PyPI and GitHub (see "Releases" below). |

## Read first

| Doc | Why |
|---|---|
| `docs/design-notes.md` | Decisions, the entity map, open protocol questions and known issues. |
| `docs/vtech-lan-kit/docs/README.md` → `00`–`95` | **Protocol authority**: opcodes, payloads, pushes, crypto, talk-back. |
| `docs/vtech-lan-kit/docs/05-provisioning.md` | Credentials come from the pairing QR: UID + AV password decoded, authKey derived. |
| `docs/vtech-lan-kit/docs/90-ha-runtime.md` | How a session threads, fails and reconnects inside HA. Its SDK specifics (ctypes, buffers, return codes) are background now: vtech-link handles them. `85-ha-deployment.md` is the SDK-era packaging, kept as background. |
| `docs/vtech-link-design/` | vtech-link's design; `75-python-api.md` is the module's surface the package uses. |
| `docs/vtech-lan-kit/client/vtech_camera_client.py` | Working reference implementation. Use it to check behaviour. |
| `COMPATIBILITY.md` | User guide for trying a model other than the RM5754HD: classify its pairing QR, provision/probe, report. Written from the documented protocol + the user's own camera only — never the VTech app. |

## Other VTech models (compatibility)

`COMPATIBILITY.md` is the user-facing walkthrough; the `camera-compatibility` project skill
(`.claude/skills/`, discovered when Claude Code's cwd is this repo) is the operating procedure for it.
Supporting tools in `docs/vtech-lan-kit/client/`:

- `vtech_provision.py --inspect --qr <text>` (or `--image`) classifies any pairing QR by version byte,
  not just the documented version 5: it reports whether the QR can be provisioned offline and lists each
  camera with its security mode (a version-1 QR's 8-char field is a plain camera's AV password). The
  known formats are specified in `docs/vtech-lan-kit/docs/05-provisioning.md` ("Other QR formats").
- `vtech_probe.py` — connect-only probe (no media, no setting changes; safe against a live monitor) that
  runs the documented connect/auth sequence against the user's camera and interprets the return code
  (`-68` = reachable but wrong credentials; `-13` = not reachable; AV-layer refusal = account/password).

Keep all of this in pure protocol + own-camera terms: never add guidance that reasons from, or asks a
user to inspect, the VTech app. (The cloud helper, `CREDENTIALS.md`, is the one place that involves the
user's VTech account, by their choice; keep it out of the compatibility guide and the integration.) Add a
new QR version to the kit spec only once it is actually characterised from a real example.

## Rules

### Proprietary material and secrets
- **The vendor's SDK libraries** are used only by the kit's reference client and `tutk-host` A/B runs,
  from local copies. Never ship, download or commit them, patched or not (`docs/vtech-lan-kit/sdk/README.md`).
  `.gitignore` excludes `docs/vtech-lan-kit/sdk/*/` and every `.so`.
- The QR/HKDF provisioning constants are app-wide values **embedded** in the package
  (`vtech_lan/provision.py`), matching the kit's reference client (a test enforces it). They live in code
  only: the docs name and describe them but never print the values.
- Per-camera credentials (UID, AV password, derived authKey; account `admin`) come from the owner's
  **pairing QR**, or typed by the owner, and live only in the config entry. The cloud helper's app-wide
  constants (the IRM header, the RSA key) live in `tools/vtech_cloud_credentials.py` only, like the QR ones.
- **Never write a real camera's credentials (or a real pairing QR) into code, tests, fixtures, docs or
  commits.** Live tests take them from `VTECH_UID`, `VTECH_PWD`, `VTECH_AUTHKEY`, `VTECH_ACCOUNT`
  (`docs/vtech-lan-kit/client/vtech_provision.py --env` prints them). Offline tests and the kit self-test use
  synthetic QRs (UID `TESTUID0000000000001`).

### Code
- `vtech_lan/` has **no `homeassistant` imports**, so it can be unit-tested and run standalone.
- **The hub** (`vtech_lan/hub.py`, one per HA process) loads `_vtech_link` once (`vtech_lan/link.py` imports it), hands out sessions, and owns each camera's live view, talk-back and the
  loopback HTTP server.
- **The only native code is Rust**, built with `panic = "unwind"`: keep that in every release profile.
  PyO3 raises a panic in a module call as `PanicException`, which derives from `BaseException`, so
  `except Exception` misses it. Every module call goes through `LinkSession._call` (or `_open`), which
  turns a panic into a lost session (`SessionGone`); the link is never used again. A panic on the
  link's own thread ends that link with `closed (thread_failed)`.
- **Sessions:** `SessionBase` holds everything above the transport (the opcode helpers, push decoding,
  the keepalive); `LinkSession` drives `_vtech_link` (`vtech_lan/link.py`), which matches replies,
  decrypts and paces media itself. It hands out the live view's media source (`media.py`) and the
  talk-back uplink (`talkback.py`).
- **Runtime model** (`docs/vtech-lan-kit/docs/90-ha-runtime.md`):
  - Load the module once per process, and never unload it; a new module takes a Home Assistant
    restart (the `restart_required` Repairs issue says so).
  - Every blocking module call releases the GIL (vtech-link's `py/src` uses `detached`); keep it so.
  - Never make a blocking module call on the event loop.
  - One reader thread per session takes the link's pushes.
  - Long-lived loops run on dedicated threads, not the shared executor.
- **Reply opcodes are listed explicitly per op**, never computed as request + 1: `0x709 → 0x710` and
  `0x729 → 0x730` break that rule.
- **The kit is the spec.** If you find a protocol fact that is new or wrong, fix `docs/vtech-lan-kit/docs/` in
  the same change.
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
  broadcast the camera answers on fixed UDP **32762** (vtech-link's default; the SDK's default 32761
  is unanswered, so the reference client calls `IOTC_Set_LanSearchPort(32762)`).
- **Offline tests:**
  `python3 -m venv .venv && .venv/bin/pip install -r requirements_test.txt`, then `.venv/bin/pytest`
  from the repo root. The test that loads the real module needs a `vtech-link` wheel installed in the venv
  (`vtech-link/release.sh`, then `pip install vtech-link/dist/<set>/*.whl`); it skips without one. Run tests from the root. Platform modules are named by Home Assistant (`select.py`, `sensor.py`, ...)
  and shadow other modules of the same name, so never put `custom_components/vtech_baby` (or
  `vtech_lan/`) on `sys.path` ahead of the standard library.
- **Live tests:** `tests/live/run.sh` runs a probe inside the real HA image on a host on the camera's
  LAN, through a runner script you supply as `$VTECH_LIVE_RUNNER` (see the script header).
  `probe_phase1.py` is read-only and safe headless; check what any other probe does first.
- **vtech-link builds:** put rustup's cargo first, `PATH=$HOME/.cargo/bin:$PATH`. Fedora's `/usr/bin/cargo`
  comes first by default, ignores `rust-toolchain.toml` (1.99.1) and mixes compilers in one `target/`
  (stale builds, or "compiled by an incompatible version of rustc" from clippy). Tests: `cargo test
  --workspace`; the Python module's tests: `vtech-link/py/tests/run.sh`. Live probes with the CLI from
  a host on the camera's LAN, and A/B runs against `tutk-host`: `docs/vtech-link-design/86-live-losses.md`, "Method" (one
  tarball over one SSH session, so the key is touched once).
- **hassfest:** `podman run --rm -v "$PWD:/github/workspace:ro,Z" ghcr.io/home-assistant/hassfest` from
  the root. The GitHub mirror's CI (`.github/workflows/validate.yml`) runs it and the HACS check.
- **GitLab CI** (`.gitlab-ci.yml`, hosted runners): runs on tags and from the Run pipeline button, not on
  pushes, to save compute minutes, so run it by hand before tagging. The Rust and Python tests always
  run; `release.sh` per arch runs on tags and is manual otherwise, aarch64 natively on the arm64 runner.
  `module-release` then runs the module tests against the release x86_64-glibc build.
- **Try a change in HA:** copy `custom_components/vtech_baby` (only that) into a test instance's
  `custom_components/` and restart HA.
- **Try an unpublished `vtech-link` in HA:** with HA running, `python3 -m uv pip install` the wheel inside the
  container (the image sets `UV_SYSTEM_PYTHON`, so it lands in `/usr/local`, where HA's own installs go), then
  restart HA from inside (`homeassistant.restart`). It is lost when the container is recreated; HA then
  installs the pinned version from PyPI.

## Conventions

- Keep `README.md` short: what it does, what you need, how to install it and add a camera.
- Outstanding work goes in `TODO.md` and `RELEASE_TODO.md`, local files that are gitignored and never
  committed; open protocol questions and known issues go in `docs/design-notes.md`.
- **GitLab is the primary repo** (remote `gitlab`). It push-mirrors to GitHub (remote `github`), which
  exists only for HACS. Push to GitLab only; a direct push to GitHub diverges from the mirror.
- `vtech-link/py/LICENSE` is a copy of the root `LICENSE`, because `release.sh` builds from the `vtech-link/`
  subtree alone. Change both together.
- Nothing about any particular network or deployment belongs here.

## Releases

The integration and `vtech-link` share one version, and a tag `X.Y.Z` releases both. `vtech-link` stays in this
repo and isn't published to crates.io: its only consumer is this integration, and it depends on the `dimpl`
fork through `[patch.crates-io]`. HACS reads only GitHub releases and ignores bare tags.

The first release is `0.3.0`, counting the two earlier designs: `0.1.0` was the vendor's SDK, `0.2.0` the
drop-in SDK. Don't tag those numbers: any `X.Y.Z` tag starts a release pipeline.

1. `./set_version.py X.Y.Z`, commit, then push the commit and the tag to GitLab. Its pipeline checks the versions, builds and tests the wheels, and `gitlab-release` creates the GitLab
   release: the four wheels, `SHA256SUMS` and `REVISION`, stored in the package registry because job
   artifacts expire. CI holds no PyPI or GitHub credentials.
2. `./publish.sh --dry-run X.Y.Z` downloads and verifies the release: checksums, revision against the tag, one
   wheel per target, the manifest's version and pin, `twine check`, and any wheel already on PyPI.
3. `./publish.sh X.Y.Z` uploads to PyPI, then creates the GitHub release. PyPI comes first so the version HACS
   offers can always be installed. It asks before each step, and a rerun skips what is done.

Credentials are local: twine finds the PyPI token in the keyring (service
`https://upload.pypi.org/legacy/`, username `__token__`) or in `TWINE_PASSWORD`, and the GitHub token is
`GITHUB_TOKEN` or else `gh`'s.

## Design notes

`docs/design-notes.md` holds the decisions (in-process module, one hub per HA process, pushes drive state,
LAN-only, `vtech-link` from PyPI, version 2 config entries), the entity map (entity, platform, write and
read opcodes), the open protocol questions and the known issues. Read it before changing an entity or the
session model, and update it with the change.
