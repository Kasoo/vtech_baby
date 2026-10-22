# 90 — Migration plan

From `tutk-host` behind ctypes in a worker process, to `vtech-link` in-process. Each phase has a
deliverable, an exit criterion and a way back. Nothing in Home Assistant changes until phase 3. The
`tutk-host` soak that is running now (started 2026-10-20; `tutk-host/NOTES.md`) carries on alongside
phases 0–2, and the patcher path is retired only after it ends, as already planned.

## Phase 0 — Spikes (de-risk before building)

Small throwaway programs in `vtech-link/spikes/`, all read-only against the camera:

1. **Done 2026-10-21.** **DTLS with dimpl** (`30-dtls.md`, "The dimpl change and the spike"). Add `PSK_WITH_AES_128_GCM_SHA256`
   to a local dimpl checkout, and open the upstream issue/PR (Q13). Then the IOTC codec and connect
   (hard-coded, no state machine), and dimpl's PSK client over channel 0. Pass: the handshake completes
   and the AV login replies 0. Settles Q1. If dimpl's hello is refused, compare its extensions with
   OpenSSL's (in `30-dtls.md`). If dimpl is a dead end, run the same spike with our own client for
   `ccac`.
2. **Done 2026-10-21.** **Wrong AV password** with the same spike. Record which alert the camera sends, if any. Settles Q2.
3. **Done 2026-10-21.** **One socket per link, two links at once**, while HA's own session runs. Pass: three independent
   sessions answer. Settles Q3.
4. **libgcc_s** in the HA image: done, it's there (`80-build.md`).
5. **Done 2026-10-22.** **A PyO3 abi3 hello-world** built by `release.sh` for all four targets and imported by
   `ExtensionFileLoader` inside the HA image (amd64 for real, arm64 under qemu). Pass: it loads, and
   the import check passes. Result: all four sets build reproducibly and load in the HA image (amd64, and arm64
   under qemu) and in `python:3.13-slim`; `_panic()` raises `PanicException`; `tools/ha_import_check.sh` reruns it.

Exit: all five pass, and `95-open-questions.md` is updated with the answers. Way back: nothing has
changed anywhere.

## Phase 1 — The core, offline

Build `vtech-link-core` and `vtech-link-sim` to `20`–`70`: the modules in the order IOTC → DTLS → AV
client → media/FEC → services → talk-back → runtime. Each module lands with its T1 tests. Then the T2
scenarios, the T3 DTLS interop and the T5 fuzz targets.

Exit: T1, T2 (S1–S12, with loss), T3 and the T5 smoke runs all pass in CI; clippy is clean with the
parser lints; `cargo deny` passes.

## Phase 2 — CLI and live parity

Build `vtech-link-cli` and run T7 against the camera: `connect`, `watch` (10 minutes), `stream`
(10 minutes at 1080p), `restart` (25 cycles), and `stream --drop 50`. With the owner present,
`talk --silent`. Run the live differential comparison (T4).

Exit: 0 bad frames clean; with `--drop 50`, packets recovered and 0 bad frames; 25/25 restarts with a
keyframe first; trace cadence matching `tutk-host`'s; talk-back of 300 frames (as `probe_talk --silent`
does today). Way back: nothing has changed in HA.

## Phase 3 — The integration, behind a switch, in the worker

- Add `vtech-link-py` and `release.sh`/`sync_link.sh`; ship `_vtech_link.abi3.so` next to the C
  sets in `native/<arch>-<libc>/`.
- `vtech_lan`: `link.py`, `SessionBase`/`SdkSession`/`LinkSession`, and the `media.py`/`talkback.py`
  changes behind a backend argument; `errors.from_link`; `tests/fake_link.py`. The worker gets a
  `backend` parameter (`sdk` | `link`), set from a hidden option in the config entry
  (`options.backend`, default `sdk`).
- T6 passes on both backends.
- Live probes with `BACKEND=link` in the HA image: `probe_phase1` (2 × 70 s), `probe_stream`,
  `probe_restart`, `probe_control --allow-writes` (owner present), `probe_talk --silent` (owner
  present), `probe_stall`.
- Then switch the production HA to `backend=link`, still inside the worker. Any surprise can only take
  the worker down, never HA.

Exit: one week in production on `link` with no more reconnects than the `tutk-host` baseline, and
nothing in the log that the `tutk-host` period didn't show. Way back: set `options.backend` to `sdk`
(one click, no reinstall).

## Phase 4 — In-process

- `LocalClient` in the HA process, the media HTTP server moved into HA, and the worker no longer
  started for `backend=link`.
- Production on in-process `link` for **two weeks** (the soak, T8).

Exit: the soak criterion (`00-requirements.md`). Way back: `options.backend = sdk` brings back the
worker with `tutk-host`.

## Phase 5 — Retire

Delete, in one change per repo:
- `vtech_baby`: `sdk.py`, `worker.py`, `worker_client.py`, `ipc.py`, `_worker_main.py`, `SdkSession`,
  `crypto.py`, `native.py` and the patch path, `tooling/`, `native/<arch>/libtk.so`,
  `native/<arch>-<libc>/lib*APIs.so`, `sync_vendored.sh`, `sync_sdk.sh`, the SDK upload step in
  `config_flow.py`, `options.backend`, `fake_sdk.py` and the tests of removed code.
- `tutk-host/` and `tutk-sdk/`: archive under a tag (`tutk-host-final`) and remove from the tree, as
  `tutk-sdk` was archived at `tutk-sdk-dropin-v1`.
- Docs: rewrite `docs/vtech-lan-kit/docs/85-ha-deployment.md` and `90-ha-runtime.md` for the new
  library (no SDK, no patching, no worker), update `10-transport-session.md` (the call sequence becomes
  the link API), and update `README.md` and `AGENTS.md` (layout, the "Decisions" and "Known issues"
  sections, the rules about vendor SDK material, which no longer apply). Move this design's status to
  "implemented" and keep it as the library's design record.
- The camera repo: note in `camera/STATUS.md` that the integration no longer uses any TUTK binary.
  The TUTK kit stays as the reference for the wire.

Exit: `hassfest` and HACS validation pass; a fresh install on a clean HA (amd64, and aarch64 under qemu)
sets up from a pairing QR with no upload step.

## Estimate

| Phase | Effort (focused sessions) |
|---|---|
| 0 | 1–2 |
| 1 | 6–8 (about 4,500 lines of Rust including tests: IOTC 600, DTLS 900, AV client 800, media 700, talk-back 350, services 450, runtime 700) |
| 2 | 1–2 |
| 3 | 2–3 |
| 4 | 1, plus two weeks of soak |
| 5 | 1 |
