//! `MediaRx`: media receive. The packet window behind the ACK and NAK fields, frame assembly in frame
//! order, parity recovery, and the reset. Design: `docs/vtech-link-design/45-media-receive.md`;
//! reference `tutk-host/av/media_recv.c`, `av/ioctrl.c:156` (`av_media_reset`).

use std::collections::{BTreeMap, VecDeque};
use std::time::{Duration, Instant};

use super::control::seq_after16;
use super::fec::{Fec, GroupHeader};
use super::packet::{self, SUBHEADER};

const SUB_LAST: u8 = 0x01;
const MAX_FRAGS: u16 = 2048;
const MAX_INFO: usize = 16;
const MAX_FRAGMENT_PAYLOAD: usize = 1024;
/// Packet numbers tracked ahead of the cumulative ACK.
const WINDOW: u16 = 0x1000;
const PARITY_KIND: u8 = 0x0c;
/// How far a reset moves the packet numbering, so that anything already in flight looks stale.
const RESET_JUMP: u16 = 0x4000;
/// The most per-stream frames a jump in the `+0x10` count may skip and still be waited for and counted
/// lost. A larger jump, either way, is taken as the camera renumbering: release without waiting.
const MAX_FRAME_GAP: u32 = 64;

#[derive(Debug, Clone)]
pub struct MediaConfig {
    /// How long a gap in packet numbers may stay open before the ACK moves past it.
    pub gap_skip: Duration,
    /// How long an incomplete frame holds back the frames after it.
    pub partial_wait: Duration,
    /// The least time between two NAK lists.
    pub nak_repeat: Duration,
    /// Drop every Nth media or parity packet on arrival (0: none), to exercise recovery on a clean
    /// network, as `tutk-host`'s `TK_DROP` does. Tests and live probes only.
    pub drop_every: u32,
}

impl Default for MediaConfig {
    fn default() -> Self {
        Self {
            gap_skip: Duration::from_millis(300),
            partial_wait: Duration::from_millis(500),
            nak_repeat: Duration::from_millis(50),
            drop_every: 0,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StreamKind {
    Video,
    Audio,
}

impl StreamKind {
    /// The kit's fold: 3 and 4 are audio, 5 to 8 video; anything else isn't media
    /// (`tutk-kit/docs/av/90-fifos.md`).
    fn of(kind: u8) -> Option<Self> {
        match kind {
            3 | 4 => Some(Self::Audio),
            5..=8 => Some(Self::Video),
            _ => None,
        }
    }
}

/// A complete frame, still encrypted under the live-view key.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EncryptedFrame {
    pub stream: StreamKind,
    pub kind: u8,
    pub number: u32,
    /// The per-stream frame count (`+0x10`).
    pub seq: u32,
    /// When its first fragment arrived.
    pub arrived: Instant,
    pub data: Vec<u8>,
    /// The frame info from the last fragment (at most 16 bytes).
    pub info: Vec<u8>,
    /// A NAK went out while it waited.
    pub late: bool,
    /// Frames of this stream given up since the previous one delivered.
    pub lost_before: u32,
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct StreamCounters {
    pub frames: u64,
    pub bytes: u64,
    pub given_up: u64,
    pub late: u64,
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct MediaStats {
    pub video: StreamCounters,
    pub audio: StreamCounters,
    pub packets_requested: u64,
    pub packets_recovered: u64,
    pub packets_duplicate: u64,
    pub packets_stale: u64,
    pub fragments_dropped: u64,
    pub gaps_skipped: u64,
    /// Fragments of a frame already released or given up, dropped to keep the order.
    pub fragments_behind: u64,
    /// Packets dropped on purpose (`MediaConfig::drop_every`).
    pub packets_dropped_test: u64,
}

/// The fields our ACK reports for media.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AckFields {
    pub pn_cum: u16,
    pub pn_high: u16,
    pub naks: Vec<u16>,
}

/// Received packet numbers ahead of `cum` (`media_recv.c:24–107`).
struct PacketWindow {
    cum: u16,
    high: u16,
    seen: [u64; (WINDOW / 64) as usize],
    gap_since: Option<Instant>,
}

impl PacketWindow {
    fn new(start: u16) -> Self {
        Self {
            cum: start,
            high: start,
            seen: [0; (WINDOW / 64) as usize],
            gap_since: None,
        }
    }

    fn slot(pn: u16) -> (usize, u64) {
        let i = pn % WINDOW;
        (usize::from(i / 64), 1u64 << (i % 64))
    }

    fn get(&self, pn: u16) -> bool {
        let (w, b) = Self::slot(pn);
        self.seen.get(w).is_some_and(|x| x & b != 0)
    }

    fn set(&mut self, pn: u16, v: bool) {
        let (w, b) = Self::slot(pn);
        if let Some(x) = self.seen.get_mut(w) {
            if v { *x |= b } else { *x &= !b }
        }
    }

    fn advance_contiguous(&mut self) {
        while self.get(self.cum.wrapping_add(1)) {
            self.cum = self.cum.wrapping_add(1);
            self.set(self.cum, false);
        }
    }

    /// Mark `pn` received. `Err(true)` for a duplicate, `Err(false)` for one at or behind `cum` or
    /// too far ahead. `Ok(true)` when a gap was skipped.
    fn track(&mut self, now: Instant, pn: u16, gap_skip: Duration) -> Result<bool, bool> {
        if !seq_after16(pn, self.cum) || pn.wrapping_sub(self.cum) >= WINDOW {
            return Err(false);
        }
        if self.get(pn) {
            return Err(true);
        }
        self.set(pn, true);
        if seq_after16(pn, self.high) {
            self.high = pn;
        }
        self.advance_contiguous();
        let mut skipped = false;
        match self.gap_since {
            _ if self.cum == self.high => self.gap_since = None,
            None => self.gap_since = Some(now),
            Some(since) if now.saturating_duration_since(since) >= gap_skip => {
                // Move past the missing packets to the next received one; the camera stops resending
                // what the cumulative ACK has passed.
                loop {
                    self.cum = self.cum.wrapping_add(1);
                    if self.cum == self.high || self.get(self.cum.wrapping_add(1)) {
                        break;
                    }
                }
                // Landing on `high` passes a received packet; clear its bit, or it would read as a
                // duplicate once the window wraps.
                self.set(self.cum, false);
                self.advance_contiguous();
                self.gap_since = (self.cum != self.high).then_some(now);
                skipped = true;
            }
            Some(_) => {}
        }
        Ok(skipped)
    }

    /// Unseen packets between `cum` and `high`, as offsets from `cum`; at most `max`.
    fn naks(&self, max: usize) -> Vec<u16> {
        let mut out = Vec::new();
        let mut pn = self.cum.wrapping_add(1);
        while seq_after16(self.high, pn) && out.len() < max {
            if !self.get(pn) {
                out.push(pn.wrapping_sub(self.cum));
            }
            pn = pn.wrapping_add(1);
        }
        out
    }
}

struct Partial {
    first_at: Instant,
    kind: u8,
    seq: u32,
    count: u16,
    have: u16,
    frags: Vec<Option<Vec<u8>>>,
    info: Option<Vec<u8>>,
}

struct Assembler {
    /// Keyed by frame number; ordered by serial distance from `base`.
    partial: BTreeMap<u32, Partial>,
    lost: u32,
    /// The per-stream count of the last frame released or given up; `None` until the first, and after
    /// a reset or a renumbering.
    last: Option<u32>,
}

/// How per-stream count `seq` stands against the last one released.
enum Place {
    /// The next one, or the first after a reset.
    Next,
    /// After `missing` frames that haven't come.
    After { missing: u32 },
    /// At or before the last one released.
    Behind,
    /// Too far either way: the camera has renumbered.
    Renumbered,
}

impl Assembler {
    fn new() -> Self {
        Self {
            partial: BTreeMap::new(),
            lost: 0,
            last: None,
        }
    }

    fn place(&self, seq: u32) -> Place {
        let Some(last) = self.last else {
            return Place::Next;
        };
        let ahead = seq.wrapping_sub(last);
        let behind = last.wrapping_sub(seq);
        match ahead {
            1 => Place::Next,
            2..=MAX_FRAME_GAP => Place::After { missing: ahead - 1 },
            _ if behind < MAX_FRAME_GAP => Place::Behind,
            _ => Place::Renumbered,
        }
    }

    /// The oldest partial frame in 32-bit serial order.
    fn oldest(&self) -> Option<u32> {
        let mut keys = self.partial.keys().copied();
        let first = keys.next()?;
        Some(keys.fold(first, |a, b| if b.wrapping_sub(a) >= 0x8000_0000 { b } else { a }))
    }
}

pub struct MediaRx {
    config: MediaConfig,
    window: PacketWindow,
    video: Assembler,
    audio: Assembler,
    fec: Fec,
    ready: VecDeque<EncryptedFrame>,
    nak_at: Option<Instant>,
    ack_due: bool,
    reset_seq: u16,
    /// Media and parity packets that have arrived, for `drop_every`.
    arrivals: u64,
    stats: MediaStats,
}

impl MediaRx {
    pub fn new(config: MediaConfig) -> Self {
        Self {
            config,
            window: PacketWindow::new(0xffff),
            video: Assembler::new(),
            audio: Assembler::new(),
            fec: Fec::new(),
            ready: VecDeque::new(),
            nak_at: None,
            ack_due: false,
            reset_seq: 0,
            arrivals: 0,
            stats: MediaStats::default(),
        }
    }

    /// One class-0x0c media or parity frame (`frame` starts at the media sub-header) with its FEC
    /// group header.
    pub fn on_envelope(&mut self, now: Instant, group: &[u8], frame: &[u8]) {
        if self.config.drop_every > 0 {
            self.arrivals += 1;
            if self.arrivals % u64::from(self.config.drop_every) == 0 {
                return self.stats.packets_dropped_test += 1;
            }
        }
        let is_parity = frame.first() == Some(&PARITY_KIND);
        if !is_parity {
            self.on_fragment(now, frame);
        }
        let Some(header) = GroupHeader::parse(group) else {
            return;
        };
        if let Some(rebuilt) = self.fec.on_packet(header, frame) {
            self.stats.packets_recovered += 1;
            self.on_fragment(now, &rebuilt);
        }
    }

    /// One media fragment (`media_recv.c` `av_media_on_packet`).
    pub fn on_fragment(&mut self, now: Instant, f: &[u8]) {
        let Some(frag) = Fragment::parse(f) else {
            return self.stats.fragments_dropped += 1;
        };
        self.ack_due = true;
        match self.window.track(now, frag.pn, self.config.gap_skip) {
            Err(true) => return self.stats.packets_duplicate += 1,
            Err(false) => return self.stats.packets_stale += 1,
            Ok(skipped) => self.stats.gaps_skipped += u64::from(skipped),
        }
        let asm = match frag.stream {
            StreamKind::Video => &mut self.video,
            StreamKind::Audio => &mut self.audio,
        };
        // A late resend of a frame already released or given up would come out after its successors.
        if matches!(asm.place(frag.seq), Place::Behind) {
            return self.stats.fragments_behind += 1;
        }
        let p = asm.partial.entry(frag.number).or_insert_with(|| Partial {
            first_at: now,
            kind: frag.kind,
            seq: frag.seq,
            count: frag.count,
            have: 0,
            frags: vec![None; usize::from(frag.count)],
            info: None,
        });
        if p.count != frag.count {
            return self.stats.fragments_dropped += 1;
        }
        let Some(slot) = p.frags.get_mut(usize::from(frag.index)) else {
            return;
        };
        if slot.is_some() {
            return self.stats.packets_duplicate += 1;
        }
        *slot = Some(frag.payload.to_vec());
        p.have += 1;
        if frag.info.is_some() {
            p.info = frag.info.map(<[u8]>::to_vec);
        }
        self.release(now, frag.stream);
    }

    /// Move frames out in frame order: the oldest once complete, or given up once it has waited
    /// `partial_wait` (`media_recv.c` `release`). A frame that follows a gap in the per-stream count
    /// waits as long for the missing frames, whose packets may all have been lost and be on their way
    /// again; `tutk-host` releases it at once, and the resent frame then comes out after it.
    fn release(&mut self, now: Instant, stream: StreamKind) {
        let asm = match stream {
            StreamKind::Video => &mut self.video,
            StreamKind::Audio => &mut self.audio,
        };
        let counters = match stream {
            StreamKind::Video => &mut self.stats.video,
            StreamKind::Audio => &mut self.stats.audio,
        };
        while let Some(number) = asm.oldest() {
            let Some(p) = asm.partial.get(&number) else { return };
            let waited = now.saturating_duration_since(p.first_at) >= self.config.partial_wait;
            if let Place::After { missing } = asm.place(p.seq) {
                if !waited {
                    return;
                }
                asm.lost += missing;
                counters.given_up += u64::from(missing);
            }
            if p.have < p.count || p.info.is_none() {
                if !waited {
                    // The frames before it are given up; it keeps its own wait.
                    asm.last = Some(p.seq.wrapping_sub(1));
                    return;
                }
                asm.last = Some(p.seq);
                asm.partial.remove(&number);
                asm.lost += 1;
                counters.given_up += 1;
                continue;
            }
            asm.last = Some(p.seq);
            let Some(p) = asm.partial.remove(&number) else {
                return;
            };
            let late = self.nak_at.is_some_and(|t| t >= p.first_at);
            let data: Vec<u8> = p.frags.into_iter().flatten().flatten().collect();
            counters.frames += 1;
            counters.bytes += data.len() as u64;
            counters.late += u64::from(late);
            self.ready.push_back(EncryptedFrame {
                stream,
                kind: p.kind,
                number,
                seq: p.seq,
                arrived: p.first_at,
                data,
                info: p.info.unwrap_or_default(),
                late,
                lost_before: std::mem::take(&mut asm.lost),
            });
        }
    }

    /// When an incomplete frame will be given up.
    pub fn poll_timeout(&self) -> Option<Instant> {
        [&self.video, &self.audio]
            .into_iter()
            .filter_map(|a| {
                a.partial
                    .get(&a.oldest()?)
                    .map(|p| p.first_at + self.config.partial_wait)
            })
            .min()
    }

    pub fn handle_timeout(&mut self, now: Instant) {
        self.release(now, StreamKind::Video);
        self.release(now, StreamKind::Audio);
    }

    pub fn poll_frame(&mut self) -> Option<EncryptedFrame> {
        self.ready.pop_front()
    }

    /// Whether a media packet has arrived since the last call.
    pub fn take_ack_due(&mut self) -> bool {
        std::mem::take(&mut self.ack_due)
    }

    /// The media fields for an ACK going out now. The NAK list is included only when `nak_repeat` has
    /// passed since the last one (`av/transport.c:114`).
    pub fn ack_fields(&mut self, now: Instant) -> AckFields {
        let mut naks = Vec::new();
        if self
            .nak_at
            .is_none_or(|t| now.saturating_duration_since(t) >= self.config.nak_repeat)
        {
            naks = self.window.naks(packet::MAX_NAKS);
            if !naks.is_empty() {
                self.nak_at = Some(now);
                self.stats.packets_requested += naks.len() as u64;
            }
        }
        AckFields {
            pn_cum: self.window.cum,
            pn_high: self.window.high,
            naks,
        }
    }

    /// Drop everything received and in progress, and move the packet numbering on, so that what the
    /// camera already has in flight is ignored. Returns the payload of the internal `0x24` frame that
    /// tells the camera (`av/ioctrl.c:156`): u16 reset sequence, u16 the new `pn_cum`, 4 zero bytes.
    pub fn reset(&mut self) -> (u16, [u8; 8]) {
        self.video.partial.clear();
        self.audio.partial.clear();
        self.video.lost = 0;
        self.audio.lost = 0;
        self.video.last = None;
        self.audio.last = None;
        self.ready.clear();
        self.fec.reset();
        self.reset_seq = self.reset_seq.wrapping_add(1);
        let cum = self.window.cum.wrapping_add(RESET_JUMP);
        self.window = PacketWindow::new(cum);
        self.nak_at = None;
        let mut payload = [0u8; 8];
        payload[..2].copy_from_slice(&self.reset_seq.to_le_bytes());
        payload[2..4].copy_from_slice(&cum.to_le_bytes());
        (self.reset_seq, payload)
    }

    pub fn stats(&self) -> MediaStats {
        self.stats
    }
}

/// A parsed media sub-header (`media_recv.c:174`).
struct Fragment<'a> {
    kind: u8,
    stream: StreamKind,
    pn: u16,
    count: u16,
    index: u16,
    number: u32,
    seq: u32,
    payload: &'a [u8],
    info: Option<&'a [u8]>,
}

impl<'a> Fragment<'a> {
    fn parse(f: &'a [u8]) -> Option<Self> {
        let kind = *f.first()?;
        let stream = StreamKind::of(kind)?;
        let last = f.get(1)? & SUB_LAST != 0;
        let count = packet::get16(f, 0x04)?;
        let w6 = packet::get16(f, 0x06)?;
        let plen = usize::from(packet::get16(f, 0x08)?);
        // On the last fragment `+0x06` is the frame-info length, not the index.
        let (index, info_len) = if last {
            (count.checked_sub(1)?, usize::from(w6))
        } else {
            (w6, 0)
        };
        if count == 0 || count > MAX_FRAGS || index >= count || info_len > plen || info_len > MAX_INFO {
            return None;
        }
        let body = f.get(SUBHEADER..SUBHEADER + plen)?;
        let (payload, info) = body.split_at(plen - info_len);
        if payload.len() > MAX_FRAGMENT_PAYLOAD {
            return None;
        }
        Some(Self {
            kind,
            stream,
            pn: packet::get16(f, 0x02)?,
            count,
            index,
            number: packet::get32(f, 0x0c)?,
            seq: packet::get32(f, 0x10)?,
            payload,
            info: last.then_some(info),
        })
    }
}
