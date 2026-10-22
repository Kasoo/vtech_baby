//! One client's session with the camera: the DTLS server on channel 0, the camera's side of the AV
//! channel inside it (login, control stream, IOCtrl handling, media), and the talk-back client.

use std::net::SocketAddrV4;
use std::sync::Arc;
use std::time::{Duration, Instant};

use dimpl::{Config, Dtls, Output};
use vtech_link_core::av::control::{ControlEvent, ControlStream};
use vtech_link_core::av::packet::{
    self, CLASS_ACK, CLASS_DATA, CLASS_LOGIN, CLASS_REPORT, CTRL_INTERNAL, CTRL_USER,
};
use vtech_link_core::rng::{OsRng, Rng};
use vtech_link_core::service::frame_crypto;
use vtech_link_core::service::media_key::{KeyExchange, MasterKey, OP_KEY_HELLO, OP_KEY_REPLY};
use x25519_dalek::{PublicKey, StaticSecret};

use crate::media::MediaTx;
use crate::talk::{TalkClient, TalkOutput};
use crate::{CameraConfig, SimStats, WrongPassword};

pub const OP_START: u32 = 0x1ff;
pub const OP_STOP: u32 = 0x2ff;
pub const OP_SPEAKER_START: u32 = 0x350;
pub const OP_SPEAKER_STOP: u32 = 0x351;
/// The internal control types of the client's media reset and the camera's answer.
const OP_RESET: u32 = 0x24;
const OP_RESET_ACK: u32 = 0x25;
const CTRL_RETRANSMIT: Duration = Duration::from_millis(60);
const LOGIN_REPLY_LEN: usize = 0x3c;
const DATATYPE_DTLS: u8 = 1;
const CONTENT_CCS: u8 = 20;
/// A plaintext fatal `decrypt_error` alert, epoch 0, for the alert flavour of a wrong password.
const ALERT_DECRYPT_ERROR: [u8; 15] = [21, 0xfe, 0xfd, 0, 0, 0, 0, 0, 0, 0, 0x40, 0, 2, 2, 51];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Phase {
    Handshake,
    Connected,
    /// The client's Finished didn't verify: the wrong password. Nothing more is answered.
    Refused,
}

pub(crate) struct Session {
    pub addr: SocketAddrV4,
    pub token: [u8; 8],
    /// The client closed, or the camera did.
    pub ended: bool,
    seq: [u16; 32],
    dtls: Dtls,
    dtls_timeout: Option<Instant>,
    phase: Phase,
    buf: Vec<u8>,
    /// DTLS datagrams each way before the login, and which of them to drop.
    hs_in: u64,
    hs_out: u64,
    drop_in: Vec<u64>,
    drop_out: Vec<u64>,
    av: CameraAv,
    /// `(channel, datatype, payload)` for IOTC data datagrams.
    pub out: Vec<(u8, u8, Vec<u8>)>,
}

struct CameraAv {
    logged_in: bool,
    control: ControlStream,
    counter: u16,
    ack_seq: u16,
    epoch: Instant,
    master: Option<MasterKey>,
    media: MediaTx,
    talk: Option<TalkClient>,
}

impl Session {
    pub fn new(
        addr: SocketAddrV4,
        token: [u8; 8],
        dtls: Arc<Config>,
        config: &CameraConfig,
        now: Instant,
    ) -> Self {
        let mut dtls = Dtls::new_12_psk(dtls, now);
        // dimpl's server answers nothing until it has had a first timeout call.
        let _ = dtls.handle_timeout(now);
        let mut s = Self {
            addr,
            token,
            ended: false,
            seq: [0; 32],
            dtls,
            dtls_timeout: None,
            phase: Phase::Handshake,
            buf: vec![0; 4096],
            hs_in: 0,
            hs_out: 0,
            drop_in: config.dtls_drop_in.clone(),
            drop_out: config.dtls_drop_out.clone(),
            av: CameraAv {
                logged_in: false,
                control: ControlStream::new(1, CTRL_RETRANSMIT),
                counter: 0,
                ack_seq: 0,
                epoch: now,
                master: None,
                media: MediaTx::new(config.media.clone(), now),
                talk: None,
            },
            out: Vec::new(),
        };
        s.pump_dtls();
        s
    }

    pub fn is_ready(&self) -> bool {
        !self.ended && self.av.logged_in
    }

    pub fn media_counters(&self) -> crate::media::MediaCounters {
        self.av.media.counters
    }

    /// The next IOTC sequence number on `channel`.
    pub fn next_seq(&mut self, channel: u8) -> u16 {
        let Some(seq) = self.seq.get_mut(usize::from(channel)) else {
            return 0;
        };
        let s = *seq;
        *seq = seq.wrapping_add(1);
        s
    }

    /// One IOTC data payload from the client.
    pub fn on_data(
        &mut self,
        now: Instant,
        channel: u8,
        datatype: u8,
        payload: &[u8],
        config: &CameraConfig,
        stats: &mut SimStats,
    ) {
        if channel == 0 {
            if datatype == DATATYPE_DTLS && config.answer_dtls {
                self.on_dtls(now, payload, config, stats);
            }
            return;
        }
        let Some(talk) = self.av.talk.as_mut().filter(|t| t.channel == channel) else {
            return;
        };
        let mut out = TalkOutput::default();
        talk.handle_datagram(now, datatype, payload, &mut out);
        self.on_talk_output(out, stats);
    }

    fn on_dtls(&mut self, now: Instant, datagram: &[u8], config: &CameraConfig, stats: &mut SimStats) {
        if self.phase == Phase::Refused {
            return;
        }
        if !self.av.logged_in {
            self.hs_in += 1;
            if self.drop_in.contains(&(self.hs_in - 1)) {
                return;
            }
        }
        let finished = has_ccs_and_finished(datagram);
        let result = self.dtls.handle_packet(datagram);
        let app = self.pump_dtls();
        if self.phase == Phase::Handshake && (finished || result.is_err()) {
            // The client's whole final flight arrived and the handshake still didn't complete.
            self.phase = Phase::Refused;
            stats.dtls_refused += 1;
            if config.wrong_password == WrongPassword::Alert {
                self.out.push((0, DATATYPE_DTLS, ALERT_DECRYPT_ERROR.to_vec()));
            }
            return;
        }
        for pkt in app {
            self.on_av_packet(now, &pkt, config, stats);
        }
        self.flush_av();
    }

    /// Move dimpl's output to the IOTC queue; returns the application data.
    fn pump_dtls(&mut self) -> Vec<Vec<u8>> {
        let mut app = Vec::new();
        loop {
            match self.dtls.poll_output(&mut self.buf) {
                Output::Packet(p) => {
                    let n = self.hs_out;
                    self.hs_out += u64::from(!self.av.logged_in);
                    if self.av.logged_in || !self.drop_out.contains(&n) {
                        self.out.push((0, DATATYPE_DTLS, p.to_vec()));
                    }
                }
                Output::BufferTooSmall { needed } => self.buf.resize(needed, 0),
                Output::Timeout(t) => {
                    self.dtls_timeout = Some(t);
                    return app;
                }
                Output::Connected => self.phase = Phase::Connected,
                Output::ApplicationData(d) => app.push(d.to_vec()),
                _ => {}
            }
        }
    }

    /// Send one AV packet through DTLS, stamping the counter where its class carries one.
    fn send_av(&mut self, mut pkt: Vec<u8>) {
        if packet::stamp(&mut pkt, self.av.counter) {
            self.av.counter = self.av.counter.wrapping_add(1);
        }
        if self.phase == Phase::Connected && self.dtls.send_application_data(&pkt).is_ok() {
            // One record per datagram, as each must fit one IOTC payload.
            self.pump_dtls();
        }
    }

    fn on_av_packet(&mut self, now: Instant, pkt: &[u8], config: &CameraConfig, stats: &mut SimStats) {
        let class = pkt.first().copied();
        if class == Some(CLASS_LOGIN) {
            return self.on_login(pkt, config, stats);
        }
        if !self.av.logged_in || pkt.len() < 8 {
            return;
        }
        match class {
            Some(CLASS_ACK) => {
                if let Some(acked) = packet::get16(pkt, 0x0c) {
                    self.av.control.on_ack(now, acked);
                }
                let pn_cum = packet::get16(pkt, 0x08).unwrap_or(0xffff);
                let n = usize::from(packet::get16(pkt, 0x0e).unwrap_or(0));
                let naks: Vec<u16> = (0..n.min(packet::MAX_NAKS))
                    .filter_map(|i| packet::get16(pkt, 0x16 + 2 * i))
                    .collect();
                let mut resend = Vec::new();
                self.av.media.on_ack(pn_cum, &naks, &mut resend);
                for p in resend {
                    self.send_av(p);
                }
            }
            Some(CLASS_REPORT) => {
                if let Some(echo) = packet::get16(pkt, 0x08) {
                    self.send_av(packet::rtt(echo, 1).to_vec());
                }
            }
            Some(CLASS_DATA) => {
                if let Some(frag) = packet::envelope(pkt).and_then(|e| packet::ctrl_fragment(e.frame)) {
                    self.av.control.on_fragment(frag);
                    self.handle_control(now, config, stats);
                }
                // The camera ACKs every control fragment at once, duplicates included.
                if self.av.control.take_ack_due() {
                    self.send_ack(now);
                }
            }
            _ => {}
        }
    }

    /// Answer every login packet; the first one logs the client in.
    fn on_login(&mut self, pkt: &[u8], config: &CameraConfig, stats: &mut SimStats) {
        let sub = pkt.get(1).copied();
        if pkt.len() < 0x18 || !matches!(sub, Some(0 | 0x20)) {
            return;
        }
        let mut reply = [0u8; LOGIN_REPLY_LEN];
        reply[1] = packet::LOGIN_REPLY;
        reply[2..4].copy_from_slice(&packet::VERSION.to_le_bytes());
        reply[0x10..0x12].copy_from_slice(&0x24u16.to_le_bytes());
        reply[0x14..0x18].copy_from_slice(&pkt[0x14..0x18]);
        reply[0x18] = config.login_state;
        reply[0x24..0x38].copy_from_slice(&packet::OPCODE_SUPPORT);
        self.send_av(reply.to_vec());
        if !self.av.logged_in && config.login_state == 0 {
            self.av.logged_in = true;
            stats.logins += 1;
        }
    }

    fn send_ack(&mut self, now: Instant) {
        #[allow(clippy::cast_possible_truncation)] // the wire carries the low 16 bits
        let clock = now.saturating_duration_since(self.av.epoch).as_millis() as u16;
        let pkt = packet::ack(&packet::Ack {
            pn_cum: 0xffff,
            pn_high: 0xffff,
            ctrl_in: self.av.control.ctrl_in16(),
            urgent: false,
            seq: self.av.ack_seq,
            clock,
            naks: &[],
        });
        self.av.ack_seq = self.av.ack_seq.wrapping_add(1);
        self.send_av(pkt);
    }

    /// Act on the control messages delivered so far.
    fn handle_control(&mut self, now: Instant, config: &CameraConfig, stats: &mut SimStats) {
        while let Some(ev) = self.av.control.poll_event() {
            let ControlEvent::Message { class, typ, payload } = ev else {
                continue;
            };
            match class {
                CTRL_INTERNAL if typ == OP_RESET => {
                    let (Some(seq), Some(cum)) = (packet::get16(&payload, 0), packet::get16(&payload, 2))
                    else {
                        continue;
                    };
                    self.av.media.reset(cum);
                    stats.resets += 1;
                    let mut ack = [0u8; 4];
                    ack[..2].copy_from_slice(&seq.to_le_bytes());
                    self.av.control.send_internal(now, OP_RESET_ACK, &ack);
                }
                CTRL_USER => self.on_ioctrl(now, typ, &payload, config, stats),
                _ => {}
            }
        }
    }

    fn on_ioctrl(
        &mut self,
        now: Instant,
        typ: u32,
        payload: &[u8],
        config: &CameraConfig,
        stats: &mut SimStats,
    ) {
        stats.ioctrls.push((typ, payload.to_vec()));
        match typ {
            OP_KEY_HELLO => {
                if let Some((reply, master)) = key_exchange(payload) {
                    self.av.master = Some(master);
                    let _ = self.av.control.send(now, OP_KEY_REPLY, &reply);
                }
            }
            OP_START => self.av.media.start(now),
            OP_STOP => self.av.media.on = false,
            OP_SPEAKER_START => {
                if let Some(channel) = packet::get32(payload, 0).and_then(|c| u8::try_from(c).ok()) {
                    self.av.talk = Some(TalkClient::new(channel, now));
                }
            }
            OP_SPEAKER_STOP => self.av.talk = None,
            _ => {}
        }
        for (reply, body) in (config.responder)(typ, payload) {
            let _ = self.av.control.send(now, reply, &body);
        }
    }

    /// Queue an unsolicited IOCtrl.
    pub fn push(&mut self, now: Instant, typ: u32, payload: &[u8]) {
        if self.is_ready() {
            let _ = self.av.control.send(now, typ, payload);
            self.flush_av();
        }
    }

    /// Send what the control stream has queued.
    fn flush_av(&mut self) {
        while let Some(pkt) = self.av.control.poll_transmit() {
            self.send_av(pkt);
        }
    }

    /// Timers: DTLS retransmission, control resends, media, talk-back.
    pub fn tick(&mut self, now: Instant, stats: &mut SimStats) {
        if self.ended || self.phase == Phase::Refused {
            return;
        }
        if self.dtls_timeout.is_none_or(|t| now >= t) {
            let _ = self.dtls.handle_timeout(now);
            self.pump_dtls();
        }
        if !self.av.logged_in {
            return;
        }
        self.av.control.handle_timeout(now);
        self.flush_av();
        let mut media = Vec::new();
        self.av.media.tick(now, self.av.master.as_ref(), &mut media);
        for p in media {
            self.send_av(p);
        }
        if let Some(talk) = &mut self.av.talk {
            let mut out = TalkOutput::default();
            talk.handle_timeout(now, &mut out);
            self.on_talk_output(out, stats);
        }
    }

    fn on_talk_output(&mut self, out: TalkOutput, stats: &mut SimStats) {
        let Some(channel) = self.av.talk.as_ref().map(|t| t.channel) else {
            return;
        };
        stats.talk_logins += u64::from(out.logged_in);
        for (datatype, payload) in out.transmits {
            self.out.push((channel, datatype, payload));
        }
        for sealed in out.frames {
            match self
                .av
                .master
                .as_ref()
                .and_then(|m| frame_crypto::decrypt(m, &sealed))
            {
                Some(plain) => stats.talk_frames.push(plain),
                None => stats.talk_undecryptable += 1,
            }
        }
    }
}

/// The camera's half of the live-view key exchange: the `0x821` reply to a `0x820` hello, and the
/// master both sides derive.
fn key_exchange(hello: &[u8]) -> Option<(Vec<u8>, MasterKey)> {
    let client_public: [u8; 32] = hello.get(4..36)?.try_into().ok()?;
    let secret: [u8; 32] = OsRng.array();
    let salt: [u8; 32] = OsRng.array();
    let public = PublicKey::from(&StaticSecret::from(secret));
    let mut reply = vec![0u8; 76];
    reply[12..44].copy_from_slice(public.as_bytes());
    reply[44..76].copy_from_slice(&salt);
    // X25519 is symmetric: the core's derivation, run with our secret on a reply carrying the
    // client's key, yields the client's master.
    let mut mirrored = vec![0u8; 76];
    mirrored[12..44].copy_from_slice(&client_public);
    mirrored[44..76].copy_from_slice(&salt);
    let master = KeyExchange::new(secret).master(&mirrored).ok()?;
    Some((reply, master))
}

/// Whether a DTLS datagram carries a ChangeCipherSpec and an epoch-1 record after it: the client's
/// whole final flight.
fn has_ccs_and_finished(datagram: &[u8]) -> bool {
    let mut rest = datagram;
    let mut ccs = false;
    while let (Some(&content), Some(epoch), Some(len)) = (
        rest.first(),
        rest.get(3..5).map(|e| u16::from_be_bytes([e[0], e[1]])),
        rest.get(11..13)
            .map(|l| usize::from(u16::from_be_bytes([l[0], l[1]]))),
    ) {
        if ccs && epoch >= 1 {
            return true;
        }
        ccs |= content == CONTENT_CCS;
        let Some(next) = rest.get(13 + len..) else {
            return false;
        };
        rest = next;
    }
    false
}
