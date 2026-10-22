//! `AvServer`: the talk-back channel. We serve a plain (not DTLS) AV channel on a free IOTC channel; the
//! camera logs in as a client and receives our audio, paced one frame per 20 ms. Design:
//! `docs/vtech-link-design/50-talkback.md`; reference `tutk-host/av/talkback.c`, `av/media_send.c`,
//! `vtech_lan/talkback.py`.

use std::collections::VecDeque;
use std::time::{Duration, Instant};

use super::control::ControlStream;
use super::packet::{self, CLASS_DATA, LOGIN_WIDTH, SUBHEADER};
use crate::error::LinkError;

/// IOTC datatypes on the talk-back channel.
pub const DATATYPE_PLAIN: u8 = 0;
pub const DATATYPE_DTLS: u8 = 1;
/// Our answer to the camera's DTLS ClientHello, which makes it log in plainly (`av/talkback.c:22`).
pub const REFUSE_DTLS: &[u8] = b"recv dtls data!";

const OP_HELLO: u32 = 0x40;
const HELLO: [u8; 4] = [0x00, 0x01, 0x00, 0x00];
const LOGIN_REPLY_LEN: usize = 0x3c;
const KIND_AUDIO: u8 = 3;
const MEDIA_FLAGS: u8 = 0x01;
/// Packets per error-correction group in the group header; no parity is sent.
const FEC_K: u16 = 20;
const FRAGMENT: usize = 1024;
/// The A-law codec id in the frame info.
const CODEC_ALAW: u16 = 0x8a;

/// Seals a talk-back frame and stamps it: supplied by the link, which holds the master key, the
/// randomness and the wall clock.
pub trait Sealer {
    fn seal(&mut self, plain: &[u8]) -> Vec<u8>;
    /// Milliseconds of wall-clock time, low 32 bits, for the frame info.
    fn wall_ms(&mut self) -> u32;
}

#[derive(Debug, Clone)]
pub struct ServerConfig {
    pub frame_interval: Duration,
    /// Plaintext frames queued before `push` refuses more.
    pub queue: usize,
    pub ctrl_retransmit: Duration,
    pub ack_idle: Duration,
    pub report_interval: Duration,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            frame_interval: Duration::from_millis(20),
            queue: 10,
            ctrl_retransmit: Duration::from_millis(60),
            ack_idle: Duration::from_millis(110),
            report_interval: Duration::from_millis(200),
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum ServerEvent {
    /// The camera logged in; audio may flow.
    Accepted,
    /// The channel is finished: closed (after draining), or no login by the deadline.
    Ended(LinkError),
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct ServerStats {
    pub frames_sent: u64,
    pub bytes_sent: u64,
    pub frames_dropped_queue: u64,
    pub dtls_refused: u64,
    pub logins: u64,
    pub packets_ignored: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum State {
    Listening {
        deadline: Instant,
    },
    Streaming,
    /// Closing once the queue has gone out.
    Draining,
    Ended,
}

pub struct AvServer {
    config: ServerConfig,
    state: State,
    epoch: Instant,
    control: ControlStream,
    counter: u16,
    ack_seq: u16,
    last_ack: Instant,
    next_report: Instant,
    rtt_ms: u16,
    queue: VecDeque<Vec<u8>>,
    next_send: Option<Instant>,
    tx_pn: u16,
    tx_frame: u32,
    tx_audio: u32,
    transmits: VecDeque<(u8, Vec<u8>)>,
    events: VecDeque<ServerEvent>,
    stats: ServerStats,
}

/// The media sub-header checksum: a u16 sum of the sub-header (checksum field zero) and the payload
/// (`av/media_send.c:18`).
fn checksum(sub: &[u8], payload: &[u8]) -> u16 {
    sub.iter()
        .chain(payload)
        .fold(0u16, |s, &b| s.wrapping_add(u16::from(b)))
}

/// The frame info `talkback.py` sends: u16 codec, u8 flags 2, 9 zero bytes, u32 ms timestamp.
pub fn frame_info(wall_ms: u32) -> [u8; 16] {
    let mut info = [0u8; 16];
    info[..2].copy_from_slice(&CODEC_ALAW.to_le_bytes());
    info[2] = 0x02;
    info[12..].copy_from_slice(&wall_ms.to_le_bytes());
    info
}

impl AvServer {
    /// Listen for the camera's login until `deadline`.
    pub fn new(config: ServerConfig, now: Instant, deadline: Instant) -> Self {
        let control = ControlStream::new(1, config.ctrl_retransmit);
        Self {
            config,
            state: State::Listening { deadline },
            epoch: now,
            control,
            counter: 0,
            ack_seq: 0,
            last_ack: now,
            next_report: now,
            rtt_ms: 0,
            queue: VecDeque::new(),
            next_send: None,
            tx_pn: 0,
            tx_frame: 0,
            tx_audio: 0,
            transmits: VecDeque::new(),
            events: VecDeque::new(),
            stats: ServerStats::default(),
        }
    }

    pub fn is_accepted(&self) -> bool {
        matches!(self.state, State::Streaming | State::Draining)
    }

    pub fn is_ended(&self) -> bool {
        self.state == State::Ended
    }

    fn clock(&self, now: Instant) -> u16 {
        #[allow(clippy::cast_possible_truncation)] // the wire carries the low 16 bits
        let ms = now.saturating_duration_since(self.epoch).as_millis() as u16;
        ms
    }

    fn push_packet(&mut self, mut pkt: Vec<u8>) {
        if packet::stamp(&mut pkt, self.counter) {
            self.counter = self.counter.wrapping_add(1);
        }
        self.transmits.push_back((DATATYPE_PLAIN, pkt));
    }

    /// The next `(datatype, payload)` for the channel.
    pub fn poll_transmit(&mut self) -> Option<(u8, Vec<u8>)> {
        self.transmits.pop_front()
    }

    pub fn poll_event(&mut self) -> Option<ServerEvent> {
        self.events.pop_front()
    }

    /// One inbound IOTC payload on the channel.
    pub fn handle_datagram(&mut self, now: Instant, datatype: u8, pkt: &[u8]) {
        match self.state {
            State::Ended => {}
            State::Listening { .. } if datatype == DATATYPE_DTLS => {
                self.stats.dtls_refused += 1;
                self.transmits.push_back((DATATYPE_PLAIN, REFUSE_DTLS.to_vec()));
            }
            _ if datatype != DATATYPE_PLAIN => self.stats.packets_ignored += 1,
            _ if pkt.first() == Some(&packet::CLASS_LOGIN) => self.on_login(now, pkt),
            // Anything else before a login may be left over from the channel's previous use.
            State::Listening { .. } => self.stats.packets_ignored += 1,
            State::Streaming | State::Draining => self.on_packet(now, pkt),
        }
    }

    /// The camera's login (`av/talkback.c:30`). Any credentials are accepted; every login packet is
    /// answered, and the first moves us on.
    fn on_login(&mut self, now: Instant, pkt: &[u8]) {
        let sub = pkt.get(1).copied();
        if pkt.len() < 2 * LOGIN_WIDTH + 0x38 || (sub == Some(0) && pkt.get(0x12) != Some(&0)) {
            return self.stats.packets_ignored += 1;
        }
        let mut reply = [0u8; LOGIN_REPLY_LEN];
        reply[1] = packet::LOGIN_REPLY;
        reply[2..4].copy_from_slice(&packet::VERSION.to_le_bytes());
        reply[0x10..0x12].copy_from_slice(&0x24u16.to_le_bytes());
        if let Some(nonce) = pkt.get(0x14..0x18) {
            reply[0x14..0x18].copy_from_slice(nonce);
        }
        reply[0x1d] = 1;
        reply[0x1f] = 1;
        reply[0x20] = 1;
        reply[0x24..0x38].copy_from_slice(&packet::OPCODE_SUPPORT);
        self.push_packet(reply.to_vec());
        self.stats.logins += 1;
        if let State::Listening { .. } = self.state {
            self.state = State::Streaming;
            self.control.send_internal(now, OP_HELLO, &HELLO);
            self.flush_control();
            self.next_report = now;
            self.last_ack = now;
            self.events.push_back(ServerEvent::Accepted);
        }
    }

    fn on_packet(&mut self, now: Instant, pkt: &[u8]) {
        match pkt.first().copied() {
            Some(packet::CLASS_ACK) => {
                if let Some(acked) = packet::get16(pkt, 0x0c) {
                    self.control.on_ack(now, acked);
                    self.flush_control();
                }
            }
            Some(packet::CLASS_REPORT) => {
                if let Some(echo) = packet::get16(pkt, 0x08) {
                    self.push_packet(packet::rtt(echo, self.rtt_ms).to_vec());
                }
            }
            Some(packet::CLASS_RTT) => {
                if let Some(sent) = packet::get16(pkt, 0x08) {
                    let rtt = self.clock(now).wrapping_sub(sent);
                    if rtt < 5000 {
                        self.rtt_ms = rtt.max(1);
                    }
                }
            }
            Some(CLASS_DATA) => {
                // Control frames from the camera make an ACK due; it sends no media here.
                if let Some(frag) = packet::envelope(pkt).and_then(|e| packet::ctrl_fragment(e.frame)) {
                    self.control.on_fragment(frag);
                    self.flush_control();
                } else {
                    self.stats.packets_ignored += 1;
                }
            }
            _ => self.stats.packets_ignored += 1,
        }
    }

    fn flush_control(&mut self) {
        while let Some(pkt) = self.control.poll_transmit() {
            self.push_packet(pkt);
        }
        // User messages and ACK completions don't occur on this channel.
        while self.control.poll_event().is_some() {}
    }

    /// Queue one plaintext A-law frame. `Err(frame)` when the queue is full: the caller waits.
    pub fn push(&mut self, now: Instant, plain: Vec<u8>) -> Result<(), Vec<u8>> {
        if self.state != State::Streaming || self.queue.len() >= self.config.queue {
            return Err(plain);
        }
        self.queue.push_back(plain);
        // After a pause the clock restarts from this frame rather than bursting to catch up.
        if self.next_send.is_none() {
            self.next_send = Some(now);
        }
        Ok(())
    }

    /// Room in the queue.
    pub fn space(&self) -> usize {
        self.config.queue.saturating_sub(self.queue.len())
    }

    /// Close the channel: after sending what is queued (`drain`), or at once.
    pub fn close(&mut self, drain: bool) {
        match self.state {
            State::Ended => {}
            State::Streaming if drain && !self.queue.is_empty() => self.state = State::Draining,
            _ => {
                self.stats.frames_dropped_queue += self.queue.len() as u64;
                self.queue.clear();
                self.end(LinkError::Closed(crate::error::CloseReason::Local));
            }
        }
    }

    pub fn poll_timeout(&self) -> Option<Instant> {
        match self.state {
            State::Listening { deadline } => Some(deadline),
            State::Ended => None,
            State::Streaming | State::Draining => [
                Some(self.last_ack + self.config.ack_idle),
                Some(self.next_report),
                self.control.poll_timeout(),
                self.next_send,
            ]
            .into_iter()
            .flatten()
            .min(),
        }
    }

    pub fn handle_timeout(&mut self, now: Instant, sealer: &mut dyn Sealer) {
        match self.state {
            State::Listening { deadline } => {
                if now >= deadline {
                    self.end(LinkError::Timeout);
                }
            }
            State::Ended => {}
            State::Streaming | State::Draining => {
                if self.next_send.is_some_and(|t| now >= t) {
                    self.send_next(sealer);
                }
                if now >= self.next_report {
                    self.push_packet(packet::report(self.clock(now), self.rtt_ms).to_vec());
                    self.next_report = now + self.config.report_interval;
                }
                self.control.handle_timeout(now);
                self.flush_control();
                if now >= self.last_ack + self.config.ack_idle || self.control.take_ack_due() {
                    self.send_ack(now);
                }
                if self.state == State::Draining && self.queue.is_empty() {
                    self.end(LinkError::Closed(crate::error::CloseReason::Local));
                }
            }
        }
    }

    /// Pop, seal and send one frame; the next is due one interval after this one was.
    fn send_next(&mut self, sealer: &mut dyn Sealer) {
        let Some(due) = self.next_send else { return };
        let Some(plain) = self.queue.pop_front() else {
            self.next_send = None;
            return;
        };
        let sealed = sealer.seal(&plain);
        let info = frame_info(sealer.wall_ms());
        self.send_frame(&sealed, &info);
        self.next_send = if self.queue.is_empty() && self.state == State::Draining {
            None
        } else {
            Some(due + self.config.frame_interval)
        };
    }

    /// One frame as fragments of 1024 bytes with the frame info at the end of the last, each with the
    /// error-correction group header (`av/media_send.c` `send_frame`).
    fn send_frame(&mut self, data: &[u8], info: &[u8; 16]) {
        let chunks: Vec<&[u8]> = if data.is_empty() {
            vec![&[]]
        } else {
            data.chunks(FRAGMENT).collect()
        };
        let count = u16::try_from(chunks.len()).unwrap_or(u16::MAX);
        let number = self.tx_frame;
        self.tx_frame = self.tx_frame.wrapping_add(1);
        self.tx_audio = self.tx_audio.wrapping_add(1);
        for (i, chunk) in chunks.iter().enumerate() {
            let pn = self.tx_pn;
            self.tx_pn = self.tx_pn.wrapping_add(1);
            let last = i + 1 == chunks.len();
            let mut payload = chunk.to_vec();
            if last {
                payload.extend_from_slice(info);
            }
            let mut sub = [0u8; SUBHEADER];
            sub[0] = KIND_AUDIO;
            sub[1] = u8::from(last);
            sub[2..4].copy_from_slice(&pn.to_le_bytes());
            sub[4..6].copy_from_slice(&count.to_le_bytes());
            let w6 = if last { info.len() } else { i };
            sub[6..8].copy_from_slice(&u16::try_from(w6).unwrap_or(0).to_le_bytes());
            sub[8..10].copy_from_slice(&u16::try_from(payload.len()).unwrap_or(0).to_le_bytes());
            sub[0x0c..0x10].copy_from_slice(&number.to_le_bytes());
            sub[0x10..0x14].copy_from_slice(&self.tx_audio.to_le_bytes());
            let sum = checksum(&sub, &payload);
            sub[0x0a..0x0c].copy_from_slice(&sum.to_le_bytes());

            let mut pkt = Vec::with_capacity(16 + SUBHEADER + payload.len());
            pkt.extend_from_slice(&[CLASS_DATA, MEDIA_FLAGS]);
            pkt.extend_from_slice(&packet::VERSION.to_le_bytes());
            pkt.extend_from_slice(&[0, 0, 0, 0]);
            pkt.extend_from_slice(&(pn / FEC_K).to_le_bytes());
            #[allow(clippy::cast_possible_truncation)] // pn % 20 < 20
            pkt.extend_from_slice(&[(pn % FEC_K) as u8, 20]);
            pkt.extend_from_slice(&1u32.to_le_bytes());
            pkt.extend_from_slice(&sub);
            pkt.extend_from_slice(&payload);
            self.stats.bytes_sent += pkt.len() as u64;
            self.push_packet(pkt);
        }
        self.stats.frames_sent += 1;
    }

    fn send_ack(&mut self, now: Instant) {
        let pkt = packet::ack(&packet::Ack {
            pn_cum: 0xffff,
            pn_high: 0xffff,
            ctrl_in: self.control.ctrl_in16(),
            urgent: false,
            seq: self.ack_seq,
            clock: self.clock(now),
            naks: &[],
        });
        self.ack_seq = self.ack_seq.wrapping_add(1);
        self.last_ack = now;
        self.push_packet(pkt);
    }

    pub fn stats(&self) -> ServerStats {
        self.stats
    }

    fn end(&mut self, error: LinkError) {
        self.state = State::Ended;
        self.next_send = None;
        self.events.push_back(ServerEvent::Ended(error));
    }
}
