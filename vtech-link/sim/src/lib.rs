//! A simulated VTech RM5754HD camera for end-to-end tests of the `vtech-link` client: the camera's side
//! of IOTC (LAN search, keepalive, data, close), the DTLS 1.2 PSK server, the AV login and control
//! stream, scripted IOCtrl replies and pushes, encrypted synthetic media with parity and NAK resends, and
//! the talk-back client. It runs on its own thread, on a loopback UDP socket, and is built on the core's
//! codecs and state machines wherever the camera's side mirrors the client's.
//!
//! Design: `docs/vtech-link-design/85-testing.md` (T2). Unlike the design's in-memory network with a
//! virtual clock, this one runs on real sockets and the wall clock, so it exercises the link's reactor
//! as it is.

#![forbid(unsafe_code)]
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::indexing_slicing)]

mod iotc;
pub mod media;
mod session;
mod talk;

use std::collections::HashMap;
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4, UdpSocket};
use std::sync::mpsc::{self, Receiver, Sender, TryRecvError};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use dimpl::PskResolver;
use dimpl::crypto::Dtls12CipherSuite;
use sha2::{Digest, Sha256};
use vtech_link_core::iotc::codec;
use vtech_link_core::iotc::session::wire_uid;

pub use media::{FrameTag, MediaSpec, parse_frame};
pub use session::{OP_SPEAKER_START, OP_SPEAKER_STOP, OP_START, OP_STOP};

use iotc::FromClient;
use session::Session;

/// The IOCtrls the default responder answers.
pub const OP_DEVINFO: u32 = 0x330;
pub const OP_DEVINFO_REPLY: u32 = 0x331;
pub const OP_GET_PARAMS: u32 = 0x733;
pub const OP_GET_PARAMS_REPLY: u32 = 0x734;
pub const MODEL: &str = "RM5754";
pub const VENDOR: &str = "vtech";
/// The firmware version in `DEVINFO`, as its bytes read most significant first (`ops.py`).
pub const FIRMWARE: [u8; 4] = [1, 2, 3, 4];

/// How long the camera's loop waits for a datagram before running its timers.
const TICK: Duration = Duration::from_millis(1);

/// Replies to a user IOCtrl `(type, payload)`: zero or more `(reply type, payload)`, sent in order.
pub type Responder = Arc<dyn Fn(u32, &[u8]) -> Vec<(u32, Vec<u8>)> + Send + Sync>;

/// Whether to drop datagram `n` (counted from 0, per direction).
pub type Loss = Arc<dyn Fn(u64) -> bool + Send + Sync>;

/// Drop every `n`th datagram.
pub fn drop_every(n: u64) -> Loss {
    Arc::new(move |i| n > 0 && (i + 1) % n == 0)
}

/// What the camera does when the client's DTLS Finished doesn't verify (the wrong AV password).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WrongPassword {
    /// Say nothing more, as the real camera does.
    Silent,
    /// Send a fatal `decrypt_error` (51) alert.
    Alert,
}

/// The `0x331` DEVINFO body as `ops.py` `dec_devinfo` reads it: model at 0, vendor at 16, firmware
/// bytes at 32.
pub fn devinfo() -> Vec<u8> {
    let mut r = vec![0u8; 36];
    r[..MODEL.len()].copy_from_slice(MODEL.as_bytes());
    r[16..16 + VENDOR.len()].copy_from_slice(VENDOR.as_bytes());
    r[32..36].copy_from_slice(&FIRMWARE);
    r
}

/// The default script: `DEVINFO` and `GET_PARAMS`; anything else gets no reply.
pub fn default_replies(typ: u32, _payload: &[u8]) -> Vec<(u32, Vec<u8>)> {
    match typ {
        OP_DEVINFO => vec![(OP_DEVINFO_REPLY, devinfo())],
        OP_GET_PARAMS => vec![(OP_GET_PARAMS_REPLY, (0..64).collect())],
        _ => Vec::new(),
    }
}

#[derive(Clone)]
pub struct CameraConfig {
    pub uid: String,
    pub authkey: String,
    pub password: String,
    pub account: String,
    /// The `0x602` result for a matching authKey: 1 accepts; 3, 4 and 6 refuse.
    pub iotc_result: u8,
    /// Header `+0x02` of the camera's datagrams.
    pub proto_version: u8,
    pub iotc_version: u32,
    /// Sessions accepted before the result is 3 (no free session).
    pub max_sessions: usize,
    /// Answer the LAN search at all.
    pub answer_search: bool,
    /// Answer DTLS on channel 0.
    pub answer_dtls: bool,
    pub wrong_password: WrongPassword,
    /// The state in the AV login reply; non-zero refuses.
    pub login_state: u8,
    /// Loss on what arrives from clients, and on what the camera sends.
    pub loss_in: Option<Loss>,
    pub loss_out: Option<Loss>,
    /// Per session, which DTLS datagrams before the AV login to drop, counted from 0 in each
    /// direction: in 0 is the ClientHello, out 0 the server's first flight.
    pub dtls_drop_in: Vec<u64>,
    pub dtls_drop_out: Vec<u64>,
    pub responder: Responder,
    pub media: MediaSpec,
}

impl CameraConfig {
    pub fn new(uid: &str, authkey: &str, password: &str) -> Self {
        Self {
            uid: uid.to_string(),
            authkey: authkey.to_string(),
            password: password.to_string(),
            account: "admin".to_string(),
            iotc_result: 1,
            proto_version: 0x1b,
            iotc_version: 0x0403_08c8,
            max_sessions: 8,
            answer_search: true,
            answer_dtls: true,
            wrong_password: WrongPassword::Silent,
            login_state: 0,
            loss_in: None,
            loss_out: None,
            dtls_drop_in: Vec::new(),
            dtls_drop_out: Vec::new(),
            responder: Arc::new(default_replies),
            media: MediaSpec::default(),
        }
    }
}

/// What the camera has seen and done, over all sessions.
#[derive(Debug, Default, Clone)]
pub struct SimStats {
    pub datagrams_in: u64,
    pub datagrams_out: u64,
    pub dropped_in: u64,
    pub dropped_out: u64,
    pub probes: u64,
    pub sessions: u64,
    pub logins: u64,
    /// Handshakes refused for the wrong password.
    pub dtls_refused: u64,
    pub keepalives: u64,
    /// `0x417` datagrams from clients.
    pub close_datagrams: u64,
    /// Every user IOCtrl received, in order.
    pub ioctrls: Vec<(u32, Vec<u8>)>,
    pub resets: u64,
    pub video_frames_sent: u64,
    pub audio_frames_sent: u64,
    pub media_packets_sent: u64,
    pub parity_sent: u64,
    pub resent: u64,
    pub talk_logins: u64,
    /// Talk-back frames received, decrypted.
    pub talk_frames: Vec<Vec<u8>>,
    pub talk_undecryptable: u64,
}

enum Command {
    Push { typ: u32, payload: Vec<u8> },
    Close,
    Kill,
    Stop,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(PoisonError::into_inner)
}

/// A running simulated camera. Dropping it stops the thread.
pub struct Camera {
    addr: SocketAddrV4,
    commands: Sender<Command>,
    stats: Arc<Mutex<SimStats>>,
    thread: Option<JoinHandle<()>>,
}

impl Camera {
    /// Bind 127.0.0.1 on a free port and start answering.
    pub fn start(config: CameraConfig) -> Self {
        let socket = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        socket.set_read_timeout(Some(TICK)).unwrap();
        let SocketAddr::V4(addr) = socket.local_addr().unwrap() else {
            unreachable!("bound to an IPv4 address")
        };
        let (tx, rx) = mpsc::channel();
        let stats = Arc::new(Mutex::new(SimStats::default()));
        let sim = Sim::new(config, socket, rx, Arc::clone(&stats));
        let thread = std::thread::Builder::new()
            .name(format!("vtech-sim-{}", addr.port()))
            .spawn(move || sim.run())
            .unwrap();
        Self {
            addr,
            commands: tx,
            stats,
            thread: Some(thread),
        }
    }

    pub fn port(&self) -> u16 {
        self.addr.port()
    }

    pub fn addr(&self) -> SocketAddrV4 {
        self.addr
    }

    /// Send an unsolicited IOCtrl to every logged-in client.
    pub fn push(&self, typ: u32, payload: &[u8]) {
        let _ = self.commands.send(Command::Push {
            typ,
            payload: payload.to_vec(),
        });
    }

    /// Close every session from the camera's side (`0x418`).
    pub fn close_sessions(&self) {
        let _ = self.commands.send(Command::Close);
    }

    /// Go silent: read and drop everything, send nothing.
    pub fn kill(&self) {
        let _ = self.commands.send(Command::Kill);
    }

    pub fn stats(&self) -> SimStats {
        lock(&self.stats).clone()
    }

    /// Wait until `done` holds for the stats, or `timeout`; returns whether it held.
    pub fn wait_for(&self, timeout: Duration, done: impl Fn(&SimStats) -> bool) -> bool {
        let deadline = Instant::now() + timeout;
        loop {
            if done(&lock(&self.stats)) {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    }
}

impl Drop for Camera {
    fn drop(&mut self) {
        let _ = self.commands.send(Command::Stop);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

/// The camera's PSK lookup: only `AUTHPWD_<account>` has a key.
struct Psk {
    identity: Vec<u8>,
    key: Vec<u8>,
}

impl PskResolver for Psk {
    fn resolve(&self, identity: &[u8]) -> Option<Vec<u8>> {
        (identity == self.identity).then(|| self.key.clone())
    }
}

/// The camera's thread state.
struct Sim {
    config: CameraConfig,
    uid: [u8; 20],
    dtls: Arc<dimpl::Config>,
    socket: UdpSocket,
    commands: Receiver<Command>,
    shared: Arc<Mutex<SimStats>>,
    stats: SimStats,
    sessions: HashMap<[u8; 8], Session>,
    killed: bool,
    /// Datagrams to send once the stats are published, so that a test that sees a reply also sees
    /// what led to it.
    outbox: Vec<(SocketAddrV4, Vec<u8>)>,
    sent: u64,
    received: u64,
}

impl Sim {
    fn new(
        config: CameraConfig,
        socket: UdpSocket,
        commands: Receiver<Command>,
        shared: Arc<Mutex<SimStats>>,
    ) -> Self {
        let psk = Psk {
            identity: format!("AUTHPWD_{}", config.account).into_bytes(),
            key: Sha256::digest(config.password.as_bytes()).to_vec(),
        };
        let dtls = dimpl::Config::builder()
            .with_psk_server(None, Arc::new(psk))
            .dtls12_cipher_suites(&[Dtls12CipherSuite::ECDHE_PSK_CHACHA20_POLY1305_SHA256])
            .dtls13_cipher_suites(&[])
            .use_server_cookie(false)
            .mtu(codec::MAX_PAYLOAD)
            .build()
            .unwrap();
        // A UID the client couldn't send matches nothing.
        let uid = wire_uid(&config.uid).unwrap_or([0xff; 20]);
        Self {
            config,
            uid,
            dtls: Arc::new(dtls),
            socket,
            commands,
            shared,
            stats: SimStats::default(),
            sessions: HashMap::new(),
            killed: false,
            outbox: Vec::new(),
            sent: 0,
            received: 0,
        }
    }

    fn run(mut self) {
        let mut buf = vec![0u8; codec::MAX_DATAGRAM + 1];
        loop {
            loop {
                match self.commands.try_recv() {
                    Ok(Command::Stop) | Err(TryRecvError::Disconnected) => return,
                    Ok(cmd) => self.on_command(cmd),
                    Err(TryRecvError::Empty) => break,
                }
            }
            if let Ok((n, SocketAddr::V4(from))) = self.socket.recv_from(&mut buf) {
                self.on_datagram(from, &mut buf[..n]);
            }
            let now = Instant::now();
            for s in self.sessions.values_mut() {
                s.tick(now, &mut self.stats);
            }
            self.flush();
            self.publish();
            for (to, pkt) in std::mem::take(&mut self.outbox) {
                self.transmit(to, &pkt);
            }
        }
    }

    fn on_command(&mut self, cmd: Command) {
        let now = Instant::now();
        match cmd {
            Command::Push { typ, payload } => {
                for s in self.sessions.values_mut() {
                    s.push(now, typ, &payload);
                }
            }
            Command::Close => {
                let live: Vec<_> = self
                    .sessions
                    .values_mut()
                    .filter(|s| !s.ended)
                    .map(|s| {
                        s.ended = true;
                        (s.addr, s.token)
                    })
                    .collect();
                for (addr, token) in live {
                    let pkt = iotc::token_datagram(&token, codec::OP_CLOSE_IN, self.config.proto_version);
                    self.send(addr, &pkt);
                }
            }
            Command::Kill => self.killed = true,
            Command::Stop => {}
        }
    }

    fn on_datagram(&mut self, from: SocketAddrV4, buf: &mut [u8]) {
        let n = self.received;
        self.received += 1;
        self.stats.datagrams_in += 1;
        if self.killed || self.config.loss_in.as_ref().is_some_and(|f| f(n)) {
            self.stats.dropped_in += 1;
            return;
        }
        let now = Instant::now();
        let version = self.config.proto_version;
        match iotc::parse(buf) {
            Some(FromClient::Probe { uid, token, authkey }) => self.on_probe(now, from, uid, token, authkey),
            Some(FromClient::Data {
                token,
                channel,
                datatype,
                payload,
            }) => {
                if let Some(s) = self.sessions.get_mut(&token).filter(|s| !s.ended) {
                    s.on_data(now, channel, datatype, &payload, &self.config, &mut self.stats);
                }
            }
            Some(FromClient::Alive { token }) => {
                if self.sessions.get(&token).is_some_and(|s| !s.ended) {
                    self.stats.keepalives += 1;
                    let pkt = iotc::token_datagram(&token, codec::OP_ALIVE_IN, version);
                    self.send(from, &pkt);
                }
            }
            Some(FromClient::Close { token }) => {
                if let Some(s) = self.sessions.get_mut(&token) {
                    self.stats.close_datagrams += 1;
                    s.ended = true;
                }
            }
            Some(FromClient::Record | FromClient::ConnectAck { .. }) | None => {}
        }
    }

    /// Answer a LAN search for our UID, opening a session when the result accepts.
    fn on_probe(
        &mut self,
        now: Instant,
        from: SocketAddrV4,
        uid: [u8; 20],
        token: [u8; 8],
        authkey: [u8; 8],
    ) {
        self.stats.probes += 1;
        if !self.config.answer_search || uid != self.uid {
            return;
        }
        let known = self.sessions.contains_key(&token);
        let live = self.sessions.values().filter(|s| !s.ended).count();
        let result = if authkey[..] != *self.config.authkey.as_bytes() {
            5
        } else if !known && live >= self.config.max_sessions {
            3
        } else {
            self.config.iotc_result
        };
        if matches!(result, 1 | 7) && !known {
            self.sessions.insert(
                token,
                Session::new(from, token, Arc::clone(&self.dtls), &self.config, now),
            );
            self.stats.sessions += 1;
        }
        let pkt = iotc::lan_reply(
            &self.uid,
            &token,
            result,
            self.config.proto_version,
            self.config.iotc_version,
        );
        self.send(from, &pkt);
    }

    /// Turn every session's queued payloads into data datagrams.
    fn flush(&mut self) {
        let version = self.config.proto_version;
        let mut out = Vec::new();
        for s in self.sessions.values_mut() {
            for (channel, datatype, payload) in std::mem::take(&mut s.out) {
                if s.ended {
                    continue;
                }
                let seq = s.next_seq(channel);
                out.push((
                    s.addr,
                    iotc::data(&s.token, channel, datatype, seq, &payload, version),
                ));
            }
        }
        for (to, pkt) in out {
            self.send(to, &pkt);
        }
    }

    fn send(&mut self, to: SocketAddrV4, pkt: &[u8]) {
        self.outbox.push((to, pkt.to_vec()));
    }

    fn transmit(&mut self, to: SocketAddrV4, pkt: &[u8]) {
        let n = self.sent;
        self.sent += 1;
        if self.killed || self.config.loss_out.as_ref().is_some_and(|f| f(n)) {
            self.stats.dropped_out += 1;
            return;
        }
        if self.socket.send_to(pkt, to).is_ok() {
            self.stats.datagrams_out += 1;
        }
    }

    /// Copy the stats out for the handle.
    fn publish(&mut self) {
        let mut media = media::MediaCounters::default();
        for s in self.sessions.values() {
            let c = s.media_counters();
            media.video_frames += c.video_frames;
            media.audio_frames += c.audio_frames;
            media.packets += c.packets;
            media.parity += c.parity;
            media.resent += c.resent;
        }
        self.stats.video_frames_sent = media.video_frames;
        self.stats.audio_frames_sent = media.audio_frames;
        self.stats.media_packets_sent = media.packets;
        self.stats.parity_sent = media.parity;
        self.stats.resent = media.resent;
        *lock(&self.shared) = self.stats.clone();
    }
}
