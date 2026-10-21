# 95 — Open questions and risks

Each item says what is unknown, why it matters, how it gets settled, and the default if it can't be.

## Open questions

| ID | Question | Matters for | Settled by | Default |
|---|---|---|---|---|
| Q1 | Does the camera accept dimpl's ClientHello? | `30-dtls.md` | **Settled 2026-10-21: yes**, for `ccac` and `00a8` (the dimpl spike) | — |
| Q13 | Will dimpl upstream take `PSK_WITH_AES_128_GCM_SHA256` (and later `ECDHE_PSK_WITH_CHACHA20_POLY1305_SHA256`)? | The DTLS choice | An issue/PR on `algesten/dimpl` | Carry the suite as a patch on a fork; else our own client |
| Q2 | What does the camera do for a wrong AV password (a PSK mismatch)? | R4 | **Settled 2026-10-21**: it sends the ServerHello, then nothing after our Finished (no alert). `AvAuthFailed` after 3 s of silence following our Finished | — |
| Q3 | Does the camera handle several sessions from one host on separate source ports? | `70-runtime.md` socket per link | **Settled 2026-10-21: yes.** Two spike links on separate sockets (one per DTLS suite), each held for 20 s and answering, alongside HA's session and the parent unit streaming; HA logged nothing | — |
| Q4 | What does control-frame `+0x12` mean? | Multi-fragment inbound frames (R16) | **Settled 2026-10-21** (TUTK kit agent, from the binary; kit commit `4da044d`): the per-fragment packet number, a duplicate gate only. Count `+0x14`, index `+0x16`, frame `+0x1c`; reassembly keys on frame + index; ACK `+0x0c` counts messages (`40-av-client.md`) | — |
| Q5 | Does the camera tolerate more than one unacknowledged user control frame? | Throughput of bursts of requests | **Settled 2026-10-21: yes, at least 8**, with no retransmits. Its own replies are stop and wait on our ACK (`40-av-client.md`) | Window 8 |
| Q6 | During the post-connect stall (`AGENTS.md` "Known issues"), are our control frames ACKed but not answered, or not ACKed at all? | Whether an AV-level dead-link timeout is safe | The ack-latency histogram (R31) during the soak | No AV-level timeout |
| Q7 | Does the camera accept fully scrambled data once partial encryption is negotiated, and does PE matter at all? | Simplicity only | Not needed; tested in phase 2 if it ever matters | Turn PE on at DTLS start, as today |
| Q8 | Is `libgcc_s.so.1` in HA's image? | Unwinding (R34) | **Settled 2026-10-21: yes**, from Alpine `libgcc` (`80-build.md`) | — |
| Q9 | Is media kind 5 always an IDR access unit? | Could replace `media.py`'s NAL scan with `Frame.key` | Compare `Frame.key` with the NAL scan over a long stream in phase 2 | Keep the NAL scan |
| Q10 | What is the report metric at `+0x0a` (`tutk-kit/docs/av/90-fifos.md`)? | Nothing the camera has objected to | Not pursued | Send our RTT estimate, as today |
| Q11 | Is the camera's `0x0428` keepalive rate enough to keep our 60 s silence timer from firing on an idle link with no media? | R7 false positives | Already shown by `tutk-host` (same timer, `probe_phase1` 2 × 70 s idle, HA soak) | Keep 60 s |
| Q12 | Does advertising `4.3.8.200` change anything camera-side compared with `4.3.8.100`? | R40 | **Settled 2026-10-21: no**; the same connect, login and replies | — |

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Our DTLS client has an interop bug only the camera exposes (e.g. a transcript detail) | Medium | Blocks phase 2 | Phase 0 spike first; OpenSSL interop tests; the 2026-10-21 trace to compare record by record |
| Timing differences (ACK coalescing instead of a fixed 10 ms tick) change the camera's sending | Low | Stalls or bursts | T4 trace comparison; config values default to today's; the ACK deadline can drop back to a fixed 10 ms tick |
| A security bug in our own DTLS | Low | The link carries the camera's password and its control traffic | Narrow scope (one suite, one peer, LAN-only); RFC-checked key schedule; fuzzing; secrets zeroized. The DTLS here protects against passive LAN observers, as OpenSSL did. It doesn't widen any exposure we have today |
| Rust toolchain or crate supply chain | Low | Build integrity | `--locked`, `cargo deny`, pinned toolchain, reproducible builds, and a short reviewed crate list (`80-build.md`) |
| aarch64 untested on real hardware | Medium | Raspberry Pi users | Live under qemu in the HA image (as today's `ARM64_PLAN.md` did); a community tester before a release |
| In-process removes the crash wall for PyAV too | Low | HA crash from a malformed media file in talk-back | HA's own stream component already runs PyAV in-process; talk-back decodes only http(s) media from HA's own media sources and TTS |
| Losing the working C stack too early | Low | Regressions with no fallback | `options.backend` switch through phase 4; `tutk-host` archived under a tag, not deleted from history |

## Resolved while writing this design

- **The negotiated DTLS suite and group**: `TLS_ECDHE_PSK_WITH_CHACHA20_POLY1305_SHA256` with x25519
  and the extended master secret; no HelloVerifyRequest; the server sends a NewSessionTicket. From a
  live `TK_TRACE=2` capture on 2026-10-21 (`30-dtls.md`). This is what makes a dependency-free DTLS
  client a small job rather than a general TLS stack.
- **libgcc_s in HA's image**: present (Q8).
- **dimpl works against the camera** once the two suites are added; a wrong AV password shows as
  silence after our Finished (Q1, Q2; `30-dtls.md`).
- **Which suites the camera accepts**: plain PSK with AES-128-GCM or ChaCha20, and ECDHE_PSK with
  ChaCha20 or AES-128-CBC; not CCM or CCM_8. It ignores an unacceptable hello rather than alerting
  (`30-dtls.md`).
