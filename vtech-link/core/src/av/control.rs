//! `ControlStream`: the reliable, ordered control channel inside the AV stream. Outbound user frames go
//! through a window of unacknowledged frames and are resent until the camera's ACK covers them; inbound
//! fragments are reassembled by frame number and delivered in order. Design:
//! `docs/vtech-link-design/40-av-client.md` ("The control stream"); reference `tutk-host/av/ioctrl.c`,
//! with inbound keyed as the kit has it (`tutk-kit/docs/av/40-ioctrl.md`), not as `tutk-host` does.

use std::collections::{BTreeMap, VecDeque};
use std::time::{Duration, Instant};

use super::packet::{self, CTRL_INTERNAL, CTRL_MAX_PAYLOAD, CtrlFragment};
use crate::error::LinkError;

/// Identifies one user send, for its completion.
pub type SendId = u64;

/// Fragments held for messages that can't be delivered yet, and their total size.
const HELD_FRAGMENTS: usize = 16;
const HELD_BYTES: usize = 16 * 1024;
/// How far ahead of the next expected message a fragment may be and still be held.
const AHEAD_LIMIT: u32 = 64;
/// The most fragments one message may have: 64 KiB, far above any IOCtrl.
const MAX_FRAGMENTS: u16 = 64;

/// Upper bounds of the ACK latency buckets, in milliseconds; the last bucket is everything above.
pub const LATENCY_BUCKETS_MS: [u64; 8] = [10, 50, 100, 500, 1_000, 5_000, 10_000, 30_000];

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ControlEvent {
    /// The camera acknowledged this user send.
    Acked(SendId),
    /// A complete inbound message.
    Message { class: u8, typ: u32, payload: Vec<u8> },
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct ControlStats {
    pub frames_sent: u64,
    pub retransmits: u64,
    /// First send to ACK, per user frame, in the `LATENCY_BUCKETS_MS` buckets plus one above.
    pub ack_latency: [u64; 9],
    pub messages_in: u64,
    pub multi_fragment_in: u64,
    pub fragments_dropped: u64,
    pub fragments_duplicate: u64,
    pub fragments_stale: u64,
}

struct Outbound {
    id: Option<SendId>,
    class: u8,
    typ: u32,
    payload: Vec<u8>,
}

struct InFlight {
    id: Option<SendId>,
    frame: u32,
    pkt: Vec<u8>,
    first_sent: Instant,
    last_sent: Instant,
}

/// Fragments of one inbound message.
struct Assembly {
    class: u8,
    count: u16,
    parts: BTreeMap<u16, Vec<u8>>,
}

/// `a` comes after `b` in 16-bit serial order.
pub fn seq_after16(a: u16, b: u16) -> bool {
    a != b && a.wrapping_sub(b) < 0x8000
}

pub struct ControlStream {
    window: usize,
    retransmit: Duration,
    next_id: SendId,
    next_frame: u32,
    next_packet: u16,
    queued: VecDeque<Outbound>,
    inflight: VecDeque<InFlight>,
    /// The camera's cumulative ACK of our frames (low 16 bits of the frame number).
    acked: Option<u16>,
    /// The last message delivered to us, `u32::MAX` before the first.
    ctrl_in: u32,
    held: BTreeMap<u32, Assembly>,
    held_fragments: usize,
    held_bytes: usize,
    ack_due: bool,
    transmits: VecDeque<Vec<u8>>,
    events: VecDeque<ControlEvent>,
    stats: ControlStats,
}

impl ControlStream {
    pub fn new(window: usize, retransmit: Duration) -> Self {
        Self {
            window: window.max(1),
            retransmit,
            next_id: 1,
            next_frame: 0,
            next_packet: 0,
            queued: VecDeque::new(),
            inflight: VecDeque::new(),
            acked: None,
            ctrl_in: u32::MAX,
            held: BTreeMap::new(),
            held_fragments: 0,
            held_bytes: 0,
            ack_due: false,
            transmits: VecDeque::new(),
            events: VecDeque::new(),
            stats: ControlStats::default(),
        }
    }

    /// Queue a user IOCtrl; it goes out when the window has room. Its `Acked` event follows the
    /// camera's ACK.
    pub fn send(&mut self, now: Instant, typ: u32, payload: &[u8]) -> Result<SendId, LinkError> {
        if payload.len() > CTRL_MAX_PAYLOAD {
            return Err(LinkError::InvalidArgument("IOCtrl payload over 1020 bytes"));
        }
        let id = self.next_id;
        self.next_id += 1;
        self.queued.push_back(Outbound {
            id: Some(id),
            class: packet::CTRL_USER,
            typ,
            payload: payload.to_vec(),
        });
        self.fill_window(now);
        Ok(id)
    }

    /// Send an internal control frame (the media reset) at once, outside the window.
    pub fn send_internal(&mut self, now: Instant, typ: u32, payload: &[u8]) {
        self.transmit(
            now,
            Outbound {
                id: None,
                class: CTRL_INTERNAL,
                typ,
                payload: payload.to_vec(),
            },
        );
    }

    fn user_in_flight(&self) -> usize {
        self.inflight.iter().filter(|f| f.id.is_some()).count()
    }

    fn fill_window(&mut self, now: Instant) {
        while self.user_in_flight() < self.window {
            let Some(out) = self.queued.pop_front() else {
                return;
            };
            self.transmit(now, out);
        }
    }

    fn transmit(&mut self, now: Instant, out: Outbound) {
        let frame = self.next_frame;
        self.next_frame = self.next_frame.wrapping_add(1);
        let pkt = packet::ctrl_frame(out.class, self.next_packet, frame, out.typ, &out.payload);
        self.next_packet = self.next_packet.wrapping_add(1);
        self.transmits.push_back(pkt.clone());
        self.stats.frames_sent += 1;
        self.inflight.push_back(InFlight {
            id: out.id,
            frame,
            pkt,
            first_sent: now,
            last_sent: now,
        });
    }

    /// The camera's ACK `+0x0c`: it has every frame of ours up to `acked` (`av/ioctrl.c:77`).
    pub fn on_ack(&mut self, now: Instant, acked: u16) {
        if acked == 0xffff {
            return;
        }
        if self.acked.is_none_or(|prev| seq_after16(acked, prev)) {
            self.acked = Some(acked);
        }
        let Some(acked) = self.acked else { return };
        // An ACK that doesn't advance still trims: a frame queued after an earlier ACK of the same
        // number is covered by it.
        while let Some(front) = self.inflight.front() {
            #[allow(clippy::cast_possible_truncation)] // the camera ACKs the low 16 bits
            let frame16 = front.frame as u16;
            if seq_after16(frame16, acked) {
                break;
            }
            if let Some(done) = self.inflight.pop_front() {
                if let Some(id) = done.id {
                    self.record_latency(now.saturating_duration_since(done.first_sent));
                    self.events.push_back(ControlEvent::Acked(id));
                }
            }
        }
        self.fill_window(now);
    }

    fn record_latency(&mut self, d: Duration) {
        let ms = u64::try_from(d.as_millis()).unwrap_or(u64::MAX);
        let bucket = LATENCY_BUCKETS_MS
            .iter()
            .position(|&b| ms <= b)
            .unwrap_or(LATENCY_BUCKETS_MS.len());
        if let Some(n) = self.stats.ack_latency.get_mut(bucket) {
            *n += 1;
        }
    }

    /// One inbound control fragment. Every fragment makes an ACK due, even a duplicate, so the camera
    /// learns where we are.
    pub fn on_fragment(&mut self, frag: CtrlFragment<'_>) {
        self.ack_due = true;
        let expected = self.ctrl_in.wrapping_add(1);
        let ahead = frag.frame_number.wrapping_sub(expected);
        if ahead >= 0x8000_0000 {
            return self.stats.fragments_stale += 1;
        }
        if ahead >= AHEAD_LIMIT {
            return self.stats.fragments_dropped += 1;
        }
        if frag.count > MAX_FRAGMENTS {
            return self.stats.fragments_dropped += 1;
        }
        // Room is always kept for the next message; the camera resends what we don't ACK.
        let full = self.held_fragments >= HELD_FRAGMENTS || self.held_bytes + frag.body.len() > HELD_BYTES;
        if full && ahead != 0 {
            return self.stats.fragments_dropped += 1;
        }
        let entry = self.held.entry(frag.frame_number).or_insert_with(|| Assembly {
            class: frag.class,
            count: frag.count,
            parts: BTreeMap::new(),
        });
        if entry.count != frag.count {
            return self.stats.fragments_dropped += 1;
        }
        if entry.parts.contains_key(&frag.index) {
            return self.stats.fragments_duplicate += 1;
        }
        if frag.index == 0 {
            entry.class = frag.class;
        }
        entry.parts.insert(frag.index, frag.body.to_vec());
        self.held_fragments += 1;
        self.held_bytes += frag.body.len();
        self.deliver();
    }

    /// Deliver every complete message at the head, in order.
    fn deliver(&mut self) {
        loop {
            let expected = self.ctrl_in.wrapping_add(1);
            let complete = self
                .held
                .get(&expected)
                .is_some_and(|a| a.parts.len() == usize::from(a.count));
            if !complete {
                return;
            }
            let Some(a) = self.held.remove(&expected) else {
                return;
            };
            self.held_fragments = self.held_fragments.saturating_sub(a.parts.len());
            let joined: Vec<u8> = a.parts.into_values().flatten().collect();
            self.held_bytes = self.held_bytes.saturating_sub(joined.len());
            self.ctrl_in = expected;
            self.stats.messages_in += 1;
            if a.count > 1 {
                self.stats.multi_fragment_in += 1;
                log::debug!(
                    "av: control message {expected} reassembled from {} fragments",
                    a.count
                );
            }
            let (Some(typ), Some(payload)) = (packet::get32(&joined, 0), joined.get(4..)) else {
                continue;
            };
            self.events.push_back(ControlEvent::Message {
                class: a.class,
                typ,
                payload: payload.to_vec(),
            });
        }
    }

    /// The low 16 bits of the last delivered message, for our ACK's `+0x0c` (`0xffff` before the
    /// first).
    pub fn ctrl_in16(&self) -> u16 {
        #[allow(clippy::cast_possible_truncation)]
        let v = self.ctrl_in as u16;
        v
    }

    /// Whether a control fragment has arrived since the last call.
    pub fn take_ack_due(&mut self) -> bool {
        std::mem::take(&mut self.ack_due)
    }

    pub fn poll_timeout(&self) -> Option<Instant> {
        self.inflight.iter().map(|f| f.last_sent + self.retransmit).min()
    }

    /// Resend every frame whose last send is `retransmit` old (`av/ioctrl.c:55`).
    pub fn handle_timeout(&mut self, now: Instant) {
        for f in self.inflight.iter_mut() {
            if now >= f.last_sent + self.retransmit {
                f.last_sent = now;
                self.transmits.push_back(f.pkt.clone());
                self.stats.retransmits += 1;
            }
        }
    }

    /// The next AV packet to send; the caller stamps its counter.
    pub fn poll_transmit(&mut self) -> Option<Vec<u8>> {
        self.transmits.pop_front()
    }

    pub fn poll_event(&mut self) -> Option<ControlEvent> {
        self.events.pop_front()
    }

    /// Every user send not yet acknowledged, in order: in flight, then queued. Used to fail them all
    /// when the link ends.
    pub fn drain_pending(&mut self) -> Vec<SendId> {
        let ids = self
            .inflight
            .drain(..)
            .filter_map(|f| f.id)
            .chain(self.queued.drain(..).filter_map(|q| q.id));
        ids.collect()
    }

    pub fn stats(&self) -> ControlStats {
        self.stats
    }
}
