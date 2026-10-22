//! `AvClient`: the AV client on channel 0, over DTLS. It logs in, then dispatches inbound packets to
//! the control stream and media receive, and keeps the camera's sender going with ACKs, reports and
//! RTT answers on deadlines instead of a 10 ms tick. Design: `docs/vtech-link-design/40-av-client.md`;
//! reference `tutk-host/av/session.c` (login), `av/transport.c` (dispatch and the transmit task).

use std::collections::VecDeque;
use std::time::{Duration, Instant};

use zeroize::Zeroizing;

use super::control::{ControlEvent, ControlStats, ControlStream, SendId};
use super::media_rx::{EncryptedFrame, MediaConfig, MediaRx, MediaStats};
use super::packet::{self, CTRL_INTERNAL, CTRL_USER};
use crate::error::{AvAuthStage, LinkError};

/// The internal control types of the media reset and its answer.
const OP_RESET: u32 = 0x24;
const OP_RESET_ACK: u32 = 0x25;
/// The handshake variant every login carries.
const LOGIN_HS: u16 = 1;
/// RTT samples above this are discarded (`av/transport.c:21`).
const RTT_LIMIT_MS: u16 = 5000;

#[derive(Debug, Clone)]
pub struct AvConfig {
    pub account: String,
    pub password: Zeroizing<String>,
    /// User control frames in flight before more queue behind them.
    pub ctrl_window: usize,
    pub ctrl_retransmit: Duration,
    pub login_resend: Duration,
    /// How long after an inbound packet its ACK may wait, to batch several.
    pub ack_coalesce: Duration,
    /// An ACK at least this often.
    pub ack_idle: Duration,
    pub report_interval: Duration,
    /// How long to wait for the camera to confirm a media reset.
    pub reset_wait: Duration,
    pub media: MediaConfig,
}

impl AvConfig {
    pub fn new(account: &str, password: &str) -> Self {
        Self {
            account: account.to_string(),
            password: Zeroizing::new(password.to_string()),
            ctrl_window: 8,
            ctrl_retransmit: Duration::from_millis(60),
            login_resend: Duration::from_millis(100),
            ack_coalesce: Duration::from_millis(10),
            ack_idle: Duration::from_millis(110),
            report_interval: Duration::from_millis(200),
            reset_wait: Duration::from_secs(1),
            media: MediaConfig::default(),
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum AvEvent {
    /// Logged in.
    Ready,
    /// A user IOCtrl from the camera.
    Control {
        typ: u32,
        payload: Vec<u8>,
    },
    /// The camera acknowledged this send.
    Acked(SendId),
    Frame(EncryptedFrame),
    /// A media reset finished: confirmed by the camera, or not within `reset_wait`.
    ResetDone {
        confirmed: bool,
    },
    /// The login failed or timed out; the client sends nothing more.
    Ended(LinkError),
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct AvStats {
    pub control: ControlStats,
    pub media: MediaStats,
    pub packets_ignored: u64,
    pub resets: u64,
    pub reset_timeouts: u64,
    pub rtt_ms: u16,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum State {
    Login { next_resend: Instant },
    Ready,
    Ended,
}

pub struct AvClient {
    config: AvConfig,
    state: State,
    nonce: u32,
    deadline: Instant,
    /// The millisecond clock's zero, for the 16-bit timestamps.
    epoch: Instant,
    control: ControlStream,
    media: MediaRx,
    counter: u16,
    ack_seq: u16,
    ack_urgent: bool,
    ack_at: Option<Instant>,
    last_ack: Instant,
    next_report: Instant,
    rtt_ms: u16,
    reset: Option<(u16, Instant)>,
    transmits: VecDeque<Vec<u8>>,
    events: VecDeque<AvEvent>,
    packets_ignored: u64,
    resets: u64,
    reset_timeouts: u64,
}

impl AvClient {
    /// Start the login, with a random `nonce` (not 0 or `u32::MAX`), failing with `Unreachable` if
    /// the camera hasn't answered by `deadline`.
    pub fn new(config: AvConfig, nonce: u32, now: Instant, deadline: Instant) -> Self {
        let control = ControlStream::new(config.ctrl_window, config.ctrl_retransmit);
        let media = MediaRx::new(config.media.clone());
        let mut client = Self {
            config,
            state: State::Login { next_resend: now },
            nonce: if nonce == 0 || nonce == u32::MAX { 1 } else { nonce },
            deadline,
            epoch: now,
            control,
            media,
            counter: 0,
            ack_seq: 0,
            ack_urgent: false,
            ack_at: None,
            last_ack: now,
            next_report: now,
            rtt_ms: 0,
            reset: None,
            transmits: VecDeque::new(),
            events: VecDeque::new(),
            packets_ignored: 0,
            resets: 0,
            reset_timeouts: 0,
        };
        client.handle_timeout(now);
        client
    }

    pub fn is_ready(&self) -> bool {
        self.state == State::Ready
    }

    fn clock(&self, now: Instant) -> u16 {
        #[allow(clippy::cast_possible_truncation)] // the wire carries the low 16 bits
        let ms = now.saturating_duration_since(self.epoch).as_millis() as u16;
        ms
    }

    /// Queue a packet for DTLS, stamping the counter where the class carries one.
    fn push(&mut self, mut pkt: Vec<u8>) {
        if packet::stamp(&mut pkt, self.counter) {
            self.counter = self.counter.wrapping_add(1);
        }
        self.transmits.push_back(pkt);
    }

    /// The next AV packet, as DTLS application data.
    pub fn poll_transmit(&mut self) -> Option<Vec<u8>> {
        self.transmits.pop_front()
    }

    pub fn poll_event(&mut self) -> Option<AvEvent> {
        self.events.pop_front()
    }

    /// Queue a user IOCtrl. Fails with `Protocol` before login.
    pub fn send_ioctrl(&mut self, now: Instant, typ: u32, payload: &[u8]) -> Result<SendId, LinkError> {
        if self.state != State::Ready {
            return Err(LinkError::Protocol("AV client not logged in"));
        }
        let id = self.control.send(now, typ, payload)?;
        self.flush_control();
        Ok(id)
    }

    /// Reset media receive: drop everything in progress, move the packet numbering on and tell the
    /// camera (`45-media-receive.md`, "The reset"). `ResetDone` follows.
    pub fn reset_media(&mut self, now: Instant) -> Result<(), LinkError> {
        if self.state != State::Ready {
            return Err(LinkError::Protocol("AV client not logged in"));
        }
        let (seq, payload) = self.media.reset();
        self.resets += 1;
        self.control.send_internal(now, OP_RESET, &payload);
        self.flush_control();
        self.reset = Some((seq, now + self.config.reset_wait));
        self.ack_urgent = true;
        self.send_ack(now);
        Ok(())
    }

    /// Every user send not yet acknowledged, for the runtime to fail when the link ends.
    pub fn drain_pending(&mut self) -> Vec<SendId> {
        self.control.drain_pending()
    }

    fn flush_control(&mut self) {
        while let Some(pkt) = self.control.poll_transmit() {
            self.push(pkt);
        }
        while let Some(ev) = self.control.poll_event() {
            match ev {
                ControlEvent::Acked(id) => self.events.push_back(AvEvent::Acked(id)),
                ControlEvent::Message {
                    class: CTRL_USER,
                    typ,
                    payload,
                } => {
                    self.events.push_back(AvEvent::Control { typ, payload });
                }
                ControlEvent::Message {
                    class: CTRL_INTERNAL,
                    typ: OP_RESET_ACK,
                    payload,
                } => {
                    let seq = packet::get16(&payload, 0);
                    if let Some((want, _)) = self.reset {
                        if seq == Some(want) {
                            self.reset = None;
                            self.events.push_back(AvEvent::ResetDone { confirmed: true });
                        }
                    }
                }
                ControlEvent::Message { .. } => self.packets_ignored += 1,
            }
        }
    }

    fn flush_media(&mut self) {
        while let Some(frame) = self.media.poll_frame() {
            self.events.push_back(AvEvent::Frame(frame));
        }
    }

    /// One AV packet from DTLS (`av/transport.c` `av_on_packet`).
    pub fn handle_packet(&mut self, now: Instant, pkt: &[u8]) {
        if let State::Login { .. } = self.state {
            match packet::login_reply(pkt, self.nonce) {
                Some(0) => {
                    self.state = State::Ready;
                    self.events.push_back(AvEvent::Ready);
                    // Start the schedule with an immediate report (`av/transport.c:164`).
                    self.next_report = now;
                    self.last_ack = now;
                    self.handle_timeout(now);
                }
                Some(state) => self.end(LinkError::AvAuthFailed(AvAuthStage::LoginRejected(u32::from(
                    state,
                )))),
                None => self.packets_ignored += 1,
            }
            return;
        }
        if self.state != State::Ready || pkt.len() < 8 {
            return;
        }
        match pkt.first().copied() {
            Some(packet::CLASS_ACK) => match packet::get16(pkt, 0x0c) {
                Some(acked) => {
                    self.control.on_ack(now, acked);
                    self.flush_control();
                }
                None => self.packets_ignored += 1,
            },
            Some(packet::CLASS_REPORT) => match packet::get16(pkt, 0x08) {
                Some(echo) => self.push(packet::rtt(echo, self.rtt_ms).to_vec()),
                None => self.packets_ignored += 1,
            },
            Some(packet::CLASS_RTT) => {
                if let Some(sent) = packet::get16(pkt, 0x08) {
                    let rtt = self.clock(now).wrapping_sub(sent);
                    if rtt < RTT_LIMIT_MS {
                        self.rtt_ms = rtt.max(1);
                    }
                }
            }
            Some(packet::CLASS_DATA) => self.on_data(now, pkt),
            _ => self.packets_ignored += 1,
        }
    }

    /// A class-0x0c packet: a control frame, a media fragment or parity (`av/transport.c:60`).
    fn on_data(&mut self, now: Instant, pkt: &[u8]) {
        let Some(env) = packet::envelope(pkt) else {
            return self.packets_ignored += 1;
        };
        if env.frame.first() == Some(&0) {
            match packet::ctrl_fragment(env.frame) {
                Some(frag) => self.control.on_fragment(frag),
                None => self.packets_ignored += 1,
            }
            if self.control.take_ack_due() {
                self.schedule_ack(now, self.config.ack_coalesce);
            }
            self.flush_control();
            return;
        }
        self.media.on_envelope(now, env.group, env.frame);
        if self.media.take_ack_due() {
            self.schedule_ack(now, self.config.ack_coalesce);
        }
        self.flush_media();
    }

    fn schedule_ack(&mut self, now: Instant, within: Duration) {
        let at = now + within;
        self.ack_at = Some(self.ack_at.map_or(at, |t| t.min(at)));
    }

    fn send_ack(&mut self, now: Instant) {
        let media = self.media.ack_fields(now);
        let pkt = packet::ack(&packet::Ack {
            pn_cum: media.pn_cum,
            pn_high: media.pn_high,
            ctrl_in: self.control.ctrl_in16(),
            urgent: std::mem::take(&mut self.ack_urgent),
            seq: self.ack_seq,
            clock: self.clock(now),
            naks: &media.naks,
        });
        self.ack_seq = self.ack_seq.wrapping_add(1);
        self.ack_at = None;
        self.last_ack = now;
        self.push(pkt);
    }

    pub fn poll_timeout(&self) -> Option<Instant> {
        match self.state {
            State::Login { next_resend } => Some(next_resend.min(self.deadline)),
            State::Ended => None,
            State::Ready => [
                self.ack_at,
                Some(self.last_ack + self.config.ack_idle),
                Some(self.next_report),
                self.control.poll_timeout(),
                self.media.poll_timeout(),
                self.reset.map(|(_, t)| t),
            ]
            .into_iter()
            .flatten()
            .min(),
        }
    }

    pub fn handle_timeout(&mut self, now: Instant) {
        match self.state {
            State::Login { next_resend } => {
                if now >= self.deadline {
                    return self.end(LinkError::Unreachable);
                }
                if now >= next_resend {
                    // Both login packets, every `login_resend` until the reply (`av/session.c:229`).
                    for n in 0..2 {
                        let pkt = packet::login(
                            n,
                            self.nonce,
                            &self.config.account,
                            &self.config.password,
                            LOGIN_HS,
                        );
                        self.push(pkt);
                    }
                    self.state = State::Login {
                        next_resend: now + self.config.login_resend,
                    };
                }
            }
            State::Ended => {}
            State::Ready => {
                if now >= self.next_report {
                    self.push(packet::report(self.clock(now), self.rtt_ms).to_vec());
                    self.next_report = now + self.config.report_interval;
                }
                self.control.handle_timeout(now);
                self.flush_control();
                self.media.handle_timeout(now);
                self.flush_media();
                if self.ack_at.is_some_and(|t| now >= t) || now >= self.last_ack + self.config.ack_idle {
                    self.send_ack(now);
                }
                if let Some((_, deadline)) = self.reset {
                    if now >= deadline {
                        self.reset = None;
                        self.reset_timeouts += 1;
                        self.events.push_back(AvEvent::ResetDone { confirmed: false });
                    }
                }
            }
        }
    }

    pub fn stats(&self) -> AvStats {
        AvStats {
            control: self.control.stats(),
            media: self.media.stats(),
            packets_ignored: self.packets_ignored,
            resets: self.resets,
            reset_timeouts: self.reset_timeouts,
            rtt_ms: self.rtt_ms,
        }
    }

    fn end(&mut self, error: LinkError) {
        self.state = State::Ended;
        self.events.push_back(AvEvent::Ended(error));
    }
}
