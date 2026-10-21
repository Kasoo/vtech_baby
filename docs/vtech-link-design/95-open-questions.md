# 95 — Open questions and risks

Each item says what is unknown, why it matters, how it gets settled, and the default if it can't be.

## Open questions

| ID | Question | Matters for | Settled by | Default |
|---|---|---|---|---|
| Q1 | Does the camera accept our reduced ClientHello (one suite, four extensions)? | `30-dtls.md` | Phase 0 spike 1 | Send OpenSSL's ClientHello byte for byte and still only complete `0xCCAC` |
| Q2 | Which alert, if any, does the camera send for a wrong AV password (a PSK mismatch)? | R4: telling a wrong password from an unreachable camera | Phase 0 spike 2 (read-only) | Treat any fatal alert after our Finished, or a silent handshake timeout after our Finished, as `AvAuthFailed` |
| Q3 | Does the camera handle several sessions from one host on separate source ports? | `70-runtime.md` socket per link | Phase 0 spike 3 | Share one socket per process and demultiplex by token, as `tutk-host` does |
| Q4 | What does control-frame `+0x12` mean: frame number low bits (as `tutk-host` writes and reads it), or fragment index (kit)? | Multi-fragment inbound frames (R16) | Read `_sendIOorIOInnerFrame` and the inbound admit again in the binary (TUTK kit agent), or catch a multi-fragment frame live | Keep `tutk-host`'s reading; reassemble multi-fragment frames only by `+0x1c` frame number and treat `+0x12` as opaque |
| Q5 | Does the camera tolerate more than one unacknowledged user control frame? | Throughput of bursts of requests (a full state re-read is about 12 requests) | A live test in phase 2 with a window of 2 and 4 (read-only GETs) | Stop-and-wait (window 1) |
| Q6 | During the post-connect stall (`AGENTS.md` "Known issues"), are our control frames ACKed but not answered, or not ACKed at all? | Whether an AV-level dead-link timeout is safe | The ack-latency histogram (R31) during the soak | No AV-level timeout |
| Q7 | Does the camera accept fully scrambled data once partial encryption is negotiated, and does PE matter at all? | Simplicity only | Not needed; tested in phase 2 if it ever matters | Turn PE on at DTLS start, as today |
| Q8 | Is `libgcc_s.so.1` in HA's image? | Unwinding (R34) | **Settled 2026-10-21: yes**, from Alpine `libgcc` (`80-build.md`) | — |
| Q9 | Is media kind 5 always an IDR access unit? | Could replace `media.py`'s NAL scan with `Frame.key` | Compare `Frame.key` with the NAL scan over a long stream in phase 2 | Keep the NAL scan |
| Q10 | What is the report metric at `+0x0a` (`tutk-kit/docs/av/90-fifos.md`)? | Nothing the camera has objected to | Not pursued | Send our RTT estimate, as today |
| Q11 | Is the camera's `0x0428` keepalive rate enough to keep our 60 s silence timer from firing on an idle link with no media? | R7 false positives | Already shown by `tutk-host` (same timer, `probe_phase1` 2 × 70 s idle, HA soak) | Keep 60 s |
| Q12 | Does advertising `4.3.8.200` change anything camera-side compared with `4.3.8.100`? | R40 | Phase 2 live connect | Keep `4.3.8.100` if anything differs |

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
