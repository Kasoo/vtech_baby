# 86 — Live media losses: vtech-link against tutk-host (2026-10-22)

The question: vtech-link's first long live runs lost media frames where `tutk-host` had reported none
(`85-testing.md`, "First live runs", the rows at `0edef3c`). Was that a defect in vtech-link? Short
answer: **no evidence of one.** Under identical conditions both stacks lose the same kinds of frames,
and `tutk-host`'s counters miss a whole class of loss. Two real issues remain, and both stacks share
them (below, "Open").

## Method

Everything ran from the NAS (on the camera's LAN segment, 192.0.2.10), read-only, at **720p**: the
camera reported quality 1 throughout, and the resolution wasn't changed. None of these runs is the
Phase 2 1080p run.

- vtech-link: the CLI (`vtech-link stream CAM SECONDS [--drop N]`, static musl build at `0e60255`) with
  `VTECH_LOG=debug`. The core's debug lines name every given-up frame (fragments held of total, kind,
  age), every fragment that arrives after its frame was released or given up, every gap skip, and
  every pause of 250 ms or more between media packets (`core/src/av/media_rx.rs`).
- `tutk-host`: `tests/live/probe_counters.py` in the HA image (`ghcr.io/home-assistant/home-assistant:stable`,
  host network). It streams through `SdkSession`'s media source for a fixed time, prints the SDK's
  receive counters, and logs any gap of 500 ms or more between video frames. The shipped release set
  (`native/x86_64-musl`, `tutk-host` `e280503`) ran B; the debug build (`tutk-host/build/x86_64-musl-debug`,
  2026-10-21, with `TK_DROP` and `TK_TRACE`) ran D, E and F.
- Each batch went to the NAS as one tarball over one SSH session (`tar | ssh nas 'mktemp; tar -x; sh run.sh'`),
  so it needed one key touch. The credentials travelled in a file the script deletes as soon as it has
  read it. The bundles and full logs were in the session's scratchpad and aren't kept.
- In runs B and D the probe printed "connected in 0 ms", a bug in the f-string's order (fixed in
  `tests/live/probe_counters.py` before E and F). It doesn't affect any count.

## Runs

| Run | Stack | Conditions | Video | Audio | Lost / bad | Other |
|---|---|---|---|---|---|---|
| earlier (`0edef3c`) | vtech-link | 600 s | 5,971 | 14,996 | 10 video given up | 259 late fragments, 1 NAK |
| A | vtech-link | 600 s, sequential | 5,973 | 15,001 | 1 video given up | 53 late fragments, 0 NAKs, **no packet pause ≥ 250 ms** |
| B | `tutk-host` | 600 s, sequential, right after A | 5,974 | 15,002 | 0 | 4 gaps of 509–533 ms between video frames |
| C | vtech-link | 120 s, `--drop 50`, sequential | 1,189 | 2,987 | 3 video, 14 audio given up | 166 dropped, 105 rebuilt, 208 NAKs, 15 gap skips |
| D | `tutk-host` (debug) | 120 s, `TK_DROP=50`, sequential | 1,218 | 2,962 | 3 video given up (3 bad) | 99 rebuilt, 212 NAKs, 13 gap skips (`TK_TRACE`) |
| E | both at once | 600 s, simultaneous | vl 5,981 / th 5,989 | vl 14,994 / th 15,002 | **0 / 0** | one 259 ms packet pause (vl); one 589 ms gap between video frames (th) |
| F | both at once | 120 s, drop 1 in 50 each, simultaneous | vl 1,174 / th 1,209 | vl **2,989** / th **2,955** | vl 3 video + 12 audio lost; th 4 video given up, audio 0 counted | vl 165 dropped, 105 rebuilt, 243 NAKs, **15** gap skips; th 114 rebuilt, 270 NAKs, **20** gap skips |

## Findings

**1. The plain-run losses are a threshold both stacks sit on, not a vtech-link defect.** Verified, run A:
keyframe 3405 (63 fragments) had 10 fragments 505 ms after its first arrived, with no gap in the
packet numbers (0 NAKs) and no pause in packet arrival (no 250 ms gap logged in the whole run). It was
given up at 505 ms; the other 53 fragments arrived **8 ms later**, together. So the camera sends a large
keyframe slowly, interleaved with audio, over about half a second. `tutk-host` gives up an incomplete
frame at the same 500 ms from its first fragment (`tutk-host/av/media_recv.c:21`, `:135`), and its
release runs on every consumer read, so its timing is effectively the same. Its 509–613 ms gaps between
video frames in B, D, E and F are the same slow keyframes finishing just in time. Whether a run loses
frames depends on keyframe size and timing, so the earlier 10 losses against `tutk-host`'s 0 were a
difference of conditions. With both streaming at once (E), both lost nothing.

**2. Under forced drops both stacks lose frames because the camera doesn't answer some NAKs.** Verified,
runs C, D and F: about 1 in 11 of the dropped packets that parity couldn't rebuild was never resent
within the 300 ms gap skip (vl 15, th 13 and 20). vtech-link logged no late fragment in F, so those
resends didn't come late, they didn't come at all. vtech-link's losses are those frames. Most are
single-packet audio frames whose only packet was dropped; video losses are keyframes 63/64 complete.
Why the camera ignores these NAKs is unknown (below).

**3. `tutk-host`'s counters can't see a whole lost frame.** Verified by code and runs D and F.
`video_given_up`/`audio_given_up` count only *partial* frames given up (`media_recv.c` `release`). A
frame none of whose packets arrives leaves no partial, so it isn't counted, and the read path returns no
`AV_ER_LOSED_THIS_FRAME` for it. Every audio frame is one packet, so `tutk-host` reports 0 audio lost
under drops while delivering fewer audio frames than vtech-link in the same 120 s (F: 2,955 against
2,989, with vtech-link counting 12 lost). vtech-link sees these losses through the per-stream frame count
(`+0x10`, `45-media-receive.md`). **This corrects `tutk-host/NOTES.md`**: its `TK_DROP=50` result
("35 packets rebuilt, 0 bad frames") counted only partial frames, so 0 bad doesn't mean nothing was lost.

**4. Things that look different but aren't.** These are verified:
- `late` audio is high on vtech-link (163–167 against 1–3). vtech-link holds audio frames behind a
  missing one, which is the ordering fix, and a NAK goes out during the hold, so they count as late.
  `tutk-host` releases them at once, out of order.
- ACKs, reports, RTT packets and NAK lists are byte-identical between the stacks (fixtures,
  `40-av-client.md`), and the timing constants match: 110 ms idle ACK, 50 ms NAK repeat, 32 NAKs, 300 ms
  gap skip, 500 ms partial wait.
- The earlier 259 late fragments were first sends arriving late, not resends: `packets_duplicate` was 0,
  and they had new packet numbers.

## Open

1. **Don't give up a frame that is still arriving (both stacks).** Built in vtech-link (2026-10-26):
   an incomplete frame waits 500 ms only after a packet gap has been skipped, otherwise 2 s
   (`incomplete_wait`, `45-media-receive.md`), with offline tests for a keyframe trickled over 505 ms
   and for a gap skip. Each give-up's debug line says which rule applied. Still to verify live, as
   below. A frame missing fragments while the
   packet numbering has no gap below them hasn't lost anything; the camera just hasn't finished sending
   it. Proposed rule for `media_rx.rs`: give up an incomplete frame only once a gap at or below its
   packets has been skipped (or after a much longer backstop, e.g. 2 s). The offline tests need a case
   for a frame trickled over more than 500 ms. This is inference from finding 1, so verify it live: a
   long run with the new rule should show no given-up frame without a gap skip. Consider the same fix in
   `tutk-host` if it stays in production.
2. **Why the camera ignores some NAKs.** Unknown. Leads to check, cheapest first:
   - log each NAK list and each skipped packet number (vtech-link debug, built 2026-10-26: `media: NAK
     [...]` and `media: gave up waiting for N packet(s) [...]`) and see whether the ignored
     ones share something: the same parity group, a group whose parity was also dropped, the first
     packet after a parity packet, or audio;
   - try a longer gap skip (600 ms) to rule out slow resends;
   - compare with the stock SDK (the patched vendor libraries) to see whether it also loses these.
3. **Count whole-frame losses in `tutk-host`** (or retire it): its `RECV_STATS` understate loss under
   packet drops. Recorded in `TODO.md` ("tutk-host").
4. **The Phase 2 runs at 1080p** haven't been done: the camera was at 720p. Check the parent handset (it
   caps the stream at 720p while on) before setting anything, and restore the setting afterwards.
