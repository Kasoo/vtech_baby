# Release TODO: vtech_baby on vtech-link

What a link-only release still needs, as of 2026-10-24. The detail behind most items is in `TODO.md`;
this file only orders them for the release.

## Resume here (2026-10-26)

Done this session: dimpl 0.9.0 (`vtech-0.9.0-psk.1`) with vtech-link's crypto crates on the same
majors, `spikes/` renamed `prototypes/`, aarch64 cross-compiled (`sync_link.sh` about 4 min), the
incomplete-frame wait (`86-live-losses.md` Open 1), the empty-NAL guard, and NAK / gap-skip debug logs.
The package's modules are from `140c15d`; the full offline matrix passed at `a6b6917`, and the workspace
and integration tests pass at HEAD.

1. **Deploy to the NAS and restart HA** (not done: the SSH key wasn't touched in time). Open the shared
   connection (`ssh -fN $HOST`), then `deploy-vtech-baby.sh --restart` (needs
   passwordless sudo on the NAS). The restart also starts the worker at debug level: the `logger:` block
   in the NAS HA's `configuration.yaml` (deployed 2026-10-26, backup `configuration.yaml.bak-20261026`)
   only reaches the worker when it starts.
2. **Watch the debug log** (`sudo journalctl -u homeassistant`, which keeps more than
   `home-assistant.log`'s two days):
   - no `gave up ... (no packet gap skipped)` line, which verifies Open 1;
   - `dropped an empty video frame`: if it appears, it was the go2rtc `RepairAVCC` crash's input;
   - `live view stopped: Invalid argument: '<none>' returned 22`, seen 3 times in the soak, each time in
     the same millisecond as a go2rtc EOF: find what returns EINVAL when a viewer leaves;
   - the `media: NAK [...]` and `gave up waiting for N packet(s) [...]` lines for Open 2.
3. **Re-sign the unsigned commits** (every one from `7f284ea`, including this session's `d22b8d8`,
   `b1e244f`, `4711c7e`, `36168b4`, `140c15d`, `5797cea`): see section 4.
4. **Then**: the 1080p live runs (check the parent handset first, restore the resolution after) and a
   forced-drop run for Open 2; the section 2 blockers (Phase 3 removals, retiring the worker).

## 1. Move dimpl to 0.9.0 (done 2026-10-26, except the rebuild)

The fork's branch `tls_ecdhe_psk_with_chacha20_poly1305_sha256` (upstream 0.9.0 plus the `ccac` suite
and the ServerKeyExchange group checks) is tagged `vtech-0.9.0-psk.1` (`11e3135`, signed, pushed to
`kasoo`), and every pin and lockfile points at it. `PSK_AES128_GCM` is on a separate fork branch, so
vtech-link dropped `Suite::PskAes128Gcm` (the link only ever offered `ccac`). Docs updated
(`30-dtls.md`, `80-build.md`, `README.md`, `95-open-questions.md`). dimpl's own tests pass on both
crypto backends; its 74 clippy warnings are all upstream's. vtech-link's own crypto crates moved to
dimpl's major versions (sha2/sha1 0.11, hmac 0.13, chacha20 0.10, chacha20poly1305 0.11, x25519-dalek
3, getrandom 0.4), so nothing but `syn` is duplicated; the core crypto vectors pass.

- [ ] Fail the build on duplicate crates: cargo-deny `[bans] multiple-versions = "deny"` (skip
      `syn`), or a `cargo tree -d -e normal` check in `release.sh` and CI.

- [ ] Retest: `cargo test --workspace`, clippy, the simulator scenarios and T3 interop (in the
      workspace tests), the fuzz builds, `vtech-link/py/tests/run.sh`, and the DTLS test against the camera
      (`prototypes/dtls-camera ecdhe-psk-chacha`, from the camera's LAN).
- [ ] Rebuild with `sync_link.sh` (a few minutes now the aarch64 sets cross-compile) and redeploy to the NAS.
      The module in the package predates the backend switch: `native/LINK_SOURCE` names `0e60255`.
- [ ] The fork's uncommitted `TODO.md` still describes `psk-suites` and both suites.

## 2. Blockers for a link-only release

- [ ] **The Phase 3 removals** (`75-python-api.md`, "Changes in the integration"). The package still
      ships the `tutk-host` sets, `libtk.so`, `tooling/` and the SDK-upload config flow beside
      `_vtech_link`. Removing them:
  - settles the APKPure x86_64 question, since users no longer download Kalay Mobile;
  - shrinks the HACS download;
  - means updating `README.md`, `AGENTS.md`, `85-ha-deployment.md` and `90-ha-runtime.md`.
- [ ] **Retire the worker process with the SDK paths** (decided 2026-10-24). Once only Rust is left,
      the worker's main job, keeping C crashes out of HA, is gone. vtech-link builds with
      `panic = "unwind"`, and PyO3 turns a panic in a Python call into `PanicException`. Before
      removing it:
  - [ ] **Panics on Rust-owned threads.** A panic on the reactor thread never reaches Python. Check
        that a dead reactor ends every link it served with an error the session sees (then
        reconnect), not a silent hang, and that no `Mutex` poisoned by the panic wedges the module.
        Add a test like `_panic` for a panic on the reactor.
  - [ ] **Treat `PanicException` as fatal for that link:** drop it and reconnect, never reuse it.
  - [ ] **What still kills HA:** stack overflow, an abort (allocation failure, a panic during unwind)
        and bugs in dependencies' `unsafe` code (dimpl, RustCrypto SIMD). `vtech-link-core` is
        `forbid(unsafe_code)`. All are unlikely, and they're the remaining case for a worker. Keep
        `panic = "unwind"` in every release profile and check that no dependency sets `abort`.
  - [ ] **The GIL:** every blocking call in `py/src` has to release it. 4 calls use
        `allow_threads`/`detach` today; go through the rest.
  - [ ] **Work that moves into HA's process:** the live-view HTTP server (now served by the worker on
        loopback, socket bound by HA), MPEG-TS muxing (`avmux.py`) and the PyAV decoding for
        talk-back. Re-home the server (an HA view, or a thread on the same pre-bound socket) and
        measure HA's CPU and event-loop latency during a 1080p stream.
  - [ ] **Removals and docs:** remove `worker.py`, `worker_client.py`, `_worker_main.py`, `ipc.py`,
        the `worker_crashed` repair and `tests/test_worker.py`. Update `AGENTS.md` (the worker rules,
        "Decisions") and `90-ha-runtime.md` §6.
  - [ ] **A new module version needs an HA restart.** It always did for an upgrade, but switching
        modules without a restart, which the worker allowed, goes away.
- [ ] **Live-view stall and go2rtc panic** (`TODO.md`, "Live view stall and go2rtc panic"). There was
      "no keyframe within 10 s of START", then a zero-length NAL crashed go2rtc in `RepairAVCC`.
  - Make sure the integration never emits an empty NAL (done 2026-10-26, `media.has_nal`).
  - Find out whether the link build shows the same stall.
- [ ] **Media losses, items 1 and 2** (`86-live-losses.md`, "Open"):
  - give up an incomplete frame only after a gap below it has been skipped (built 2026-10-26, verify
    live);
  - find out why the camera ignores about 1 in 11 NAKs.

  Both stacks have them, but they visibly drop keyframes.
- [ ] **1080p live runs** of `stream`, `restart` and `stream --drop 50`. Everything so far ran at 720p.
      Check the parent handset first.
- [ ] **Soak verdict for the link build on the NAS** (live since 2026-10-23 19:59). A restart has
      undone its debug logging.
- [ ] **Fault tests on the link build:** camera power cycle, network bounce, repeated reload and unload.
- [ ] **Talk-back over the link** (owner present): build the `talk` CLI command, then test the speaker
      entity in HA on the link backend.

## 3. Distribution: PyPI wheels, GitLab primary, GitHub mirror for HACS

HACS can't install binaries per architecture: it installs one zip (the repo tree, or a release asset
with `zip_release`/`filename` in `hacs.json`) on every host. The usual way is a separate PyPI package
for the native code, pinned in `manifest.json` `requirements`. HA installs it with pip/uv at start-up,
and pip picks the wheel for the host. Once the SDK paths are gone, `_vtech_link` is the only native
file, so the package becomes pure Python.

- [ ] **Publish vtech-link as a PyPI package** (check the name first). Build abi3 wheels with the floor
      at HA's minimum Python, and no usable sdist (HA images have no Rust toolchain):
  - `musllinux_1_2_x86_64` and `musllinux_1_2_aarch64` for HA OS, Container and Supervised (Alpine);
  - `manylinux_2_28_x86_64` and `manylinux_2_28_aarch64` (or older glibc tags) for HA Core in a venv.
  - No 32-bit: HA dropped armv7, armhf and i386 (2025.12, from memory; check).
- [ ] **Licence notices in the wheel:** ship the third-party notices in its `.dist-info`, through
      maturin's `license-files`.
- [ ] **Pin it in `manifest.json`:** `"requirements": ["vtech-link==X.Y.Z"]`. Then remove
      `native/<arch>-<libc>/_vtech_link.abi3.so`, `native/LINK_SOURCE`, `sync_link.sh` and
      `native.bundled_link`/`prepare_link`, and import the installed module. Update the `AGENTS.md`
      rule ("only `sync_link.sh` installs it"): the exact pin and the CI build from a tag replace it.
- [ ] **GitLab is the primary host; GitLab CI builds the wheels.** `release.sh` already
      cross-compiles aarch64 in the rust-cross images (2026-10-26); CI can run it as is, plus
      `tools/ha_import_check.sh` under qemu. This replaces the `sync_link.sh` item.
- [ ] **PyPI trusted publishing from GitLab CI** (PyPI supports GitLab as an OIDC publisher), on tags
      only, with attestations.
- [ ] **CI checks on GitLab:** `cargo test --workspace`, clippy, the Python module tests, `pytest`, and
      hassfest (it runs as a container).
- [ ] **Push-mirror to GitHub for HACS.** A mirror copies branches and tags, **not GitHub releases**,
      and HACS needs releases. Have a GitLab CI job create the GitHub release through the GitHub API
      (or `gh release create`) once the tag reaches the mirror, with a fine-grained token scoped to
      that one repo.
- [ ] **The HACS action needs GitHub Actions:** keep `.github/workflows/validate.yml` running on the
      mirror for the HACS check, or run the `hacs/action` checks from GitLab CI.
- [ ] **Release order:** wheel to PyPI first, then the integration release with the new pin. HA
      installs the pinned version at start-up, so users need internet the first time.
- [ ] **Point `manifest.json` at the GitHub mirror:** `documentation` and `issue_tracker` have to be the
      mirror's URLs, or say where issues go.

## 4. Release mechanics

- [ ] **Re-sign the unsigned commits** before anything is pushed publicly: every vtech_baby commit from
      `7f284ea` on, including the latest.
      `git rebase --exec 'git commit --amend --no-edit -S' 5287f7e`.
- [ ] **Third-party licence notices** for the Rust crates compiled into `_vtech_link.abi3.so` (dimpl,
      RustCrypto, PyO3, ...). Only the project's own `LICENSE` exists. Generate them with something
      like `cargo about`; they ship in the wheel (section 3).
- [ ] **GitHub release on the mirror** (created from GitLab CI, section 3), tagged to match `version`
      in `manifest.json` (now `0.1.0`).
- [ ] **PR on `hacs/default`** adding the repo to `integration`, from a personal account.

## 5. Optional before release

- [ ] Decide the release profile: fat LTO with `opt-level = "s"` is −27% (`80-build.md`, "Size levers").
- [ ] Trim dimpl for PSK-only use: the `dtls13` and `certificates` features, about 430 KB.
- [ ] arm64 on real hardware (so far only under qemu).
- [ ] Upstream the dimpl suites. A release doesn't need it, as long as the fork's tags are never moved.
