//! The camera's live-view sender: synthetic video and audio frames, encrypted under the session's
//! master, cut into class-0x0c fragments with the FEC group header, one Reed-Solomon parity packet
//! after each group of 20, and resends of what the client's ACK NAKs.

use std::collections::{BTreeMap, VecDeque};
use std::time::{Duration, Instant};

use vtech_link_core::av::packet::{self, CLASS_DATA, SUBHEADER};
use vtech_link_core::rng::{OsRng, Rng};
use vtech_link_core::service::frame_crypto;
use vtech_link_core::service::media_key::MasterKey;

/// Media fragments carry at most this much: frame bytes plus, on the last, the frame info.
const FRAGMENT: usize = 1024;
const INFO_LEN: usize = 16;
/// Data packets per FEC group.
const K: u16 = 20;
/// An FEC shard: sub-header and payload, zero-padded.
const BLOCK: usize = SUBHEADER + FRAGMENT;
/// The generator row for k = 20 (`tutk-host/av/fec.c:18`).
const ROW_20: [u8; 20] = [
    0x8f, 0xae, 0x5b, 0x70, 0xd0, 0xcd, 0x54, 0x43, 0x39, 0xa3, 0xc9, 0x58, 0x1b, 0xbb, 0xb3, 0x18, 0x1b,
    0x1c, 0x12, 0x14,
];
/// Packets kept for resending.
const RESEND_KEEP: usize = 4096;

pub const KIND_AUDIO: u8 = 3;
pub const KIND_VIDEO_KEY: u8 = 5;
pub const KIND_VIDEO: u8 = 7;
const VIDEO_TAG: &[u8; 4] = b"SIMV";
const AUDIO_TAG: &[u8; 4] = b"SIMA";
/// Tag, generation, index, length.
const PLAIN_HEADER: usize = 16;

/// What the camera streams after `START`.
#[derive(Debug, Clone)]
pub struct MediaSpec {
    pub video_fps: u32,
    /// Plaintext bytes per video frame (at least 16).
    pub video_bytes: usize,
    /// Frames from one keyframe to the next.
    pub gop: u32,
    /// 0 for no audio.
    pub audio_fps: u32,
    pub audio_bytes: usize,
    /// Send a parity packet after each group of 20.
    pub parity: bool,
    /// Resend what the client NAKs.
    pub resend: bool,
}

impl Default for MediaSpec {
    fn default() -> Self {
        Self {
            video_fps: 15,
            video_bytes: 3000,
            gop: 15,
            audio_fps: 25,
            audio_bytes: 160,
            parity: true,
            resend: true,
        }
    }
}

/// What a synthetic frame's plaintext says about itself.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FrameTag {
    pub video: bool,
    /// The media reset count when it was made.
    pub generation: u32,
    /// Its index in its stream since the camera started.
    pub index: u32,
}

/// The plaintext of synthetic frame `index`: a 16-byte tag, then a pattern.
pub fn synth_frame(video: bool, generation: u32, index: u32, len: usize) -> Vec<u8> {
    let len = len.max(PLAIN_HEADER);
    let mut f = Vec::with_capacity(len);
    f.extend_from_slice(if video { VIDEO_TAG } else { AUDIO_TAG });
    f.extend_from_slice(&generation.to_le_bytes());
    f.extend_from_slice(&index.to_le_bytes());
    f.extend_from_slice(&u32::try_from(len).unwrap_or(u32::MAX).to_le_bytes());
    f.extend((PLAIN_HEADER..len).map(|j| pattern(index, j)));
    f
}

fn pattern(index: u32, j: usize) -> u8 {
    (index as usize).wrapping_mul(7).wrapping_add(j).to_le_bytes()[0]
}

/// Read a synthetic frame's tag, checking its length and pattern; `None` if it isn't one.
pub fn parse_frame(data: &[u8]) -> Option<FrameTag> {
    let video = match data.get(..4)? {
        t if t == VIDEO_TAG => true,
        t if t == AUDIO_TAG => false,
        _ => return None,
    };
    let word = |at: usize| Some(u32::from_le_bytes(data.get(at..at + 4)?.try_into().ok()?));
    let (generation, index, len) = (word(4)?, word(8)?, word(12)?);
    if usize::try_from(len).ok()? != data.len() {
        return None;
    }
    let body_ok = data
        .iter()
        .enumerate()
        .skip(PLAIN_HEADER)
        .all(|(j, &b)| b == pattern(index, j));
    body_ok.then_some(FrameTag {
        video,
        generation,
        index,
    })
}

/// Multiplication in GF(2^8) with the polynomial 0x11d, as the parity uses.
fn gf_mul(mut a: u8, mut b: u8) -> u8 {
    let mut out = 0u8;
    while b != 0 {
        if b & 1 != 0 {
            out ^= a;
        }
        let carry = a & 0x80 != 0;
        a <<= 1;
        if carry {
            a ^= 0x1d;
        }
        b >>= 1;
    }
    out
}

#[derive(Debug, Default, Clone, Copy)]
pub(crate) struct MediaCounters {
    pub video_frames: u64,
    pub audio_frames: u64,
    pub packets: u64,
    pub parity: u64,
    pub resent: u64,
}

pub(crate) struct MediaTx {
    spec: MediaSpec,
    pub on: bool,
    next_video: Instant,
    next_audio: Instant,
    /// The next packet number.
    pn: u16,
    /// Shared by both streams, as the frame number at sub-header `+0x0c`.
    frame_number: u32,
    video_index: u32,
    audio_index: u32,
    /// Video frames since the last keyframe; the next is a keyframe when this is 0.
    gop_pos: u32,
    generation: u32,
    /// The shards of the current FEC group, by index.
    group: Vec<Option<Vec<u8>>>,
    sent: BTreeMap<u16, Vec<u8>>,
    sent_order: VecDeque<u16>,
    rng: OsRng,
    pub counters: MediaCounters,
}

fn interval(fps: u32) -> Duration {
    Duration::from_secs(1) / fps.max(1)
}

/// The next due time after one sent at `due`: one interval on, but never more than one behind `now`, so
/// a stalled loop doesn't burst.
fn catch_up(due: Instant, now: Instant, step: Duration) -> Instant {
    due.max(now.checked_sub(step).unwrap_or(now)) + step
}

impl MediaTx {
    pub fn new(spec: MediaSpec, now: Instant) -> Self {
        Self {
            spec,
            on: false,
            next_video: now,
            next_audio: now,
            pn: 0,
            frame_number: 0,
            video_index: 0,
            audio_index: 0,
            gop_pos: 0,
            generation: 0,
            group: vec![None; usize::from(K)],
            sent: BTreeMap::new(),
            sent_order: VecDeque::new(),
            rng: OsRng,
            counters: MediaCounters::default(),
        }
    }

    /// `START`: stream from a keyframe.
    pub fn start(&mut self, now: Instant) {
        self.on = true;
        self.gop_pos = 0;
        self.next_video = now;
        self.next_audio = now;
    }

    /// The client's reset: number the next packet after its new `pn_cum`, forget what was sent, and
    /// restart from a keyframe.
    pub fn reset(&mut self, pn_cum: u16) {
        self.pn = pn_cum.wrapping_add(1);
        self.sent.clear();
        self.sent_order.clear();
        self.group.iter_mut().for_each(|s| *s = None);
        self.gop_pos = 0;
        self.generation += 1;
    }

    /// Send the frames that are due, as AV packets into `out`.
    pub fn tick(&mut self, now: Instant, master: Option<&MasterKey>, out: &mut Vec<Vec<u8>>) {
        let Some(master) = master.filter(|_| self.on) else {
            return;
        };
        if self.spec.video_fps > 0 && now >= self.next_video {
            self.next_video = catch_up(self.next_video, now, interval(self.spec.video_fps));
            let key = self.gop_pos == 0;
            self.gop_pos = (self.gop_pos + 1) % self.spec.gop.max(1);
            let plain = synth_frame(true, self.generation, self.video_index, self.spec.video_bytes);
            self.video_index += 1;
            self.counters.video_frames += 1;
            let kind = if key { KIND_VIDEO_KEY } else { KIND_VIDEO };
            self.send_frame(master, kind, self.video_index, &plain, out);
        }
        if self.spec.audio_fps > 0 && now >= self.next_audio {
            self.next_audio = catch_up(self.next_audio, now, interval(self.spec.audio_fps));
            let plain = synth_frame(false, self.generation, self.audio_index, self.spec.audio_bytes);
            self.audio_index += 1;
            self.counters.audio_frames += 1;
            self.send_frame(master, KIND_AUDIO, self.audio_index, &plain, out);
        }
    }

    /// Encrypt one frame and send it as fragments of at most 1024 bytes, the frame info at the end of
    /// the last. The info never straddles two fragments: when it doesn't fit, it goes alone.
    fn send_frame(
        &mut self,
        master: &MasterKey,
        kind: u8,
        stream_count: u32,
        plain: &[u8],
        out: &mut Vec<Vec<u8>>,
    ) {
        let key: [u8; 32] = self.rng.array();
        let nonce: [u8; 12] = self.rng.array();
        let sealed = frame_crypto::encrypt(master, key, nonce, plain);
        let mut chunks: Vec<&[u8]> = sealed.chunks(FRAGMENT).collect();
        if chunks.last().is_none_or(|c| c.len() + INFO_LEN > FRAGMENT) {
            chunks.push(&[]);
        }
        let count = u16::try_from(chunks.len()).unwrap_or(u16::MAX);
        let number = self.frame_number;
        self.frame_number = self.frame_number.wrapping_add(1);
        let mut info = [0u8; INFO_LEN];
        info[12..].copy_from_slice(&number.to_le_bytes());
        for (i, chunk) in chunks.iter().enumerate() {
            let last = i + 1 == chunks.len();
            let mut payload = chunk.to_vec();
            if last {
                payload.extend_from_slice(&info);
            }
            let w6 = if last { INFO_LEN } else { i };
            let mut sub = [0u8; SUBHEADER];
            sub[0] = kind;
            sub[1] = u8::from(last);
            sub[2..4].copy_from_slice(&self.pn.to_le_bytes());
            sub[4..6].copy_from_slice(&count.to_le_bytes());
            sub[6..8].copy_from_slice(&u16::try_from(w6).unwrap_or(0).to_le_bytes());
            sub[8..10].copy_from_slice(&u16::try_from(payload.len()).unwrap_or(0).to_le_bytes());
            sub[0x0c..0x10].copy_from_slice(&number.to_le_bytes());
            sub[0x10..0x14].copy_from_slice(&stream_count.to_le_bytes());
            let mut frame = sub.to_vec();
            frame.extend_from_slice(&payload);
            self.send_data(frame, out);
        }
    }

    /// One data packet: the envelope, a copy kept for resends, and the parity once its group is whole.
    fn send_data(&mut self, frame: Vec<u8>, out: &mut Vec<Vec<u8>>) {
        let pn = self.pn;
        self.pn = self.pn.wrapping_add(1);
        let (group, index) = (pn / K, pn % K);
        let pkt = envelope(group, index, &frame);
        self.counters.packets += 1;
        if self.spec.resend {
            self.sent.insert(pn, pkt.clone());
            self.sent_order.push_back(pn);
            while self.sent_order.len() > RESEND_KEEP {
                if let Some(old) = self.sent_order.pop_front() {
                    self.sent.remove(&old);
                }
            }
        }
        out.push(pkt);
        if !self.spec.parity {
            return;
        }
        if index == 0 {
            self.group.iter_mut().for_each(|s| *s = None);
        }
        self.group[usize::from(index)] = Some(frame);
        if index == K - 1 {
            if let Some(parity) = self.parity() {
                out.push(envelope(group, K, &parity));
                self.counters.parity += 1;
            }
        }
    }

    /// The parity frame for the current group: a sub-header whose first byte is 0x0c, then the sum of
    /// each shard times its generator coefficient. `None` if a shard is missing (after a reset).
    fn parity(&self) -> Option<Vec<u8>> {
        let mut block = vec![0u8; BLOCK];
        for (shard, &g) in self.group.iter().zip(&ROW_20) {
            for (o, &d) in block.iter_mut().zip(shard.as_ref()?) {
                *o ^= gf_mul(g, d);
            }
        }
        let mut frame = vec![0u8; SUBHEADER];
        frame[0] = CLASS_DATA;
        frame.extend_from_slice(&block);
        Some(frame)
    }

    /// The client's ACK: forget what `pn_cum` covers and resend each NAKed packet still held.
    pub fn on_ack(&mut self, pn_cum: u16, naks: &[u16], out: &mut Vec<Vec<u8>>) {
        if pn_cum != 0xffff {
            while let Some(&front) = self.sent_order.front() {
                if vtech_link_core::av::control::seq_after16(front, pn_cum) {
                    break;
                }
                self.sent_order.pop_front();
                self.sent.remove(&front);
            }
        }
        for &off in naks {
            if let Some(pkt) = self.sent.get(&pn_cum.wrapping_add(off)) {
                out.push(pkt.clone());
                self.counters.resent += 1;
            }
        }
    }
}

/// A class-0x0c packet: header, FEC group header, then the frame. The counter is stamped on sending.
fn envelope(group: u16, index: u16, frame: &[u8]) -> Vec<u8> {
    let mut pkt = Vec::with_capacity(16 + frame.len());
    pkt.extend_from_slice(&[CLASS_DATA, 0x01]);
    pkt.extend_from_slice(&packet::VERSION.to_le_bytes());
    pkt.extend_from_slice(&[0; 4]);
    pkt.extend_from_slice(&group.to_le_bytes());
    pkt.extend_from_slice(&[u8::try_from(index).unwrap_or(0), u8::try_from(K).unwrap_or(0)]);
    pkt.extend_from_slice(&1u32.to_le_bytes());
    pkt.extend_from_slice(frame);
    pkt
}

#[cfg(test)]
mod tests {
    use super::*;
    use vtech_link_core::av::fec::{Fec, GroupHeader};

    #[test]
    fn synthetic_frames_round_trip() {
        let f = synth_frame(true, 2, 9, 3000);
        assert_eq!(
            parse_frame(&f),
            Some(FrameTag {
                video: true,
                generation: 2,
                index: 9
            })
        );
        let mut bad = f.clone();
        bad[100] ^= 1;
        assert_eq!(parse_frame(&bad), None);
    }

    #[test]
    fn parity_rebuilds_with_the_core() {
        // Twenty shards through our parity, one dropped, rebuilt by the client's FEC.
        let mut tx = MediaTx::new(
            MediaSpec {
                resend: false,
                ..MediaSpec::default()
            },
            Instant::now(),
        );
        let frames: Vec<Vec<u8>> = (0..20u16)
            .map(|i| {
                let mut f = vec![KIND_VIDEO; SUBHEADER + 100 + usize::from(i) * 40];
                let plen = u16::try_from(f.len() - SUBHEADER).unwrap();
                f[8..10].copy_from_slice(&plen.to_le_bytes());
                f
            })
            .collect();
        let mut out = Vec::new();
        for f in &frames {
            tx.send_data(f.clone(), &mut out);
        }
        assert_eq!(out.len(), 21);
        let mut fec = Fec::new();
        let mut rebuilt = None;
        for (i, pkt) in out.iter().enumerate() {
            if i == 7 {
                continue;
            }
            let env = packet::envelope(pkt).unwrap();
            let header = GroupHeader::parse(env.group).unwrap();
            if let Some(r) = fec.on_packet(header, env.frame) {
                rebuilt = Some(r);
            }
        }
        assert_eq!(rebuilt.as_deref(), Some(&frames[7][..]));
    }
}
