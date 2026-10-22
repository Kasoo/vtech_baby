//! The link's reactor thread. It owns the socket and every protocol machine, and runs them on one loop:
//! wait for a datagram, a command or the earliest deadline; feed it in; move what the machines produce
//! between layers; send. Design: `docs/vtech-link-design/70-runtime.md`.
//!
//! The wait is a blocking `recv_from` with `SO_RCVTIMEO` set to the next deadline, and callers wake it
//! with a one-byte datagram over loopback to the same socket. That keeps the libc surface to the socket
//! calls the SDK already made (no epoll or eventfd).

use std::collections::HashMap;
use std::io::ErrorKind;
use std::net::{SocketAddr, UdpSocket};
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::mpsc::Receiver;
use std::sync::{Arc, PoisonError};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use super::egress;
use super::link::{LinkConfig, LinkInfo, LinkStats, Shared};
use super::sync::{Completion, Queue};
use crate::av::server::{AvServer, Sealer, ServerConfig, ServerEvent};
use crate::av::{AvClient, AvConfig, AvEvent, SendId};
use crate::dtls::{DATATYPE_DTLS, DtlsClient, DtlsConfig, DtlsEvent};
use crate::error::{CloseReason, LinkError};
use crate::iotc::{IotcConfig, IotcEvent, IotcSession};
use crate::rng::{OsRng, Rng};
use crate::service::frame_crypto;
use crate::service::media_key::{KeyExchange, MasterKey, OP_KEY_HELLO, OP_KEY_REPLY};
use crate::service::requests::{RequestEvent, RequestId, Requests};

const KEY_TIMEOUT: Duration = Duration::from_secs(5);
/// How long an opened talk-back channel waits for the camera, at most.
const TALK_LISTEN: Duration = Duration::from_secs(60);
/// The longest the reactor sleeps with nothing scheduled.
const IDLE_WAIT: Duration = Duration::from_secs(1);
/// The shortest socket timeout; a deadline already due is checked after it.
const MIN_WAIT: Duration = Duration::from_micros(200);

pub(crate) enum Command {
    Send {
        typ: u32,
        payload: Vec<u8>,
        done: Arc<Completion<()>>,
    },
    Request {
        typ: u32,
        payload: Vec<u8>,
        reply: u32,
        deadline: Instant,
        done: Arc<Completion<Vec<u8>>>,
    },
    Exchange {
        typ: u32,
        payload: Vec<u8>,
        reply: u32,
        deadline: Instant,
        started: Arc<Completion<RequestId>>,
        inbox: Arc<Queue<Vec<u8>>>,
    },
    EndExchange {
        id: RequestId,
    },
    MediaStart {
        done: Arc<Completion<()>>,
    },
    MediaReset {
        done: Arc<Completion<()>>,
    },
    MediaStop,
    TalkOpen {
        done: Arc<Completion<u8>>,
    },
    TalkAccept {
        deadline: Instant,
        done: Arc<Completion<()>>,
    },
    TalkClose {
        drain: bool,
        done: Arc<Completion<()>>,
    },
    Stats {
        done: Arc<Completion<LinkStats>>,
    },
    Close {
        done: Option<Arc<Completion<()>>>,
    },
}

enum Waiter {
    Single(Arc<Completion<Vec<u8>>>),
    Exchange(Arc<Queue<Vec<u8>>>),
    /// The live-view key exchange's `0x820`.
    Key,
}

/// Who is waiting for the live-view key.
enum KeyWaiter {
    Media(Arc<Completion<()>>),
    Talk(Arc<Completion<u8>>),
}

enum Key {
    None,
    Exchanging(KeyExchange),
    Ready(Arc<MasterKey>),
}

struct Talk {
    channel: u8,
    server: AvServer,
    accept: Vec<(Instant, Arc<Completion<()>>)>,
    close: Vec<Arc<Completion<()>>>,
}

struct LinkSealer<'a> {
    master: &'a MasterKey,
    rng: &'a mut OsRng,
}

impl Sealer for LinkSealer<'_> {
    fn seal(&mut self, plain: &[u8]) -> Vec<u8> {
        let mut key = [0u8; 32];
        let mut nonce = [0u8; 12];
        self.rng.fill(&mut key);
        self.rng.fill(&mut nonce);
        frame_crypto::encrypt(self.master, key, nonce, plain)
    }

    fn wall_ms(&mut self) -> u32 {
        let ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |d| d.as_millis());
        #[allow(clippy::cast_possible_truncation)] // the frame info carries the low 32 bits
        let low = ms as u32;
        low
    }
}

pub(crate) struct Reactor {
    config: LinkConfig,
    shared: Arc<Shared>,
    socket: UdpSocket,
    commands: Receiver<Command>,
    waker: SocketAddr,
    rng: OsRng,
    iotc: IotcSession,
    dtls: Option<DtlsClient>,
    av: Option<AvClient>,
    requests: Requests,
    connect_done: Option<Arc<Completion<LinkInfo>>>,
    sends: HashMap<SendId, Arc<Completion<()>>>,
    waiters: HashMap<RequestId, Waiter>,
    key: Key,
    key_waiters: Vec<KeyWaiter>,
    media_on: bool,
    reset_waiters: Vec<Arc<Completion<()>>>,
    talk: Option<Talk>,
    close_waiters: Vec<Arc<Completion<()>>>,
    finished: bool,
    datagrams_in: u64,
    datagrams_out: u64,
    send_errors: u64,
    refused_sends: u64,
}

impl Reactor {
    pub(crate) fn new(
        config: LinkConfig,
        shared: Arc<Shared>,
        socket: UdpSocket,
        commands: Receiver<Command>,
        waker: SocketAddr,
        connect_done: Arc<Completion<LinkInfo>>,
    ) -> Result<Self, LinkError> {
        let mut rng = OsRng;
        let mut token = [0u8; 8];
        rng.fill(&mut token);
        let iotc_config = IotcConfig {
            search_port: config.search_port,
            sdk_version: config.sdk_version,
            ..IotcConfig::default()
        };
        let iotc = IotcSession::new(iotc_config, &config.uid, config.authkey.as_bytes(), token)?;
        Ok(Self {
            requests: Requests::new(usize::MAX),
            config,
            shared,
            socket,
            commands,
            waker,
            rng,
            iotc,
            dtls: None,
            av: None,
            connect_done: Some(connect_done),
            sends: HashMap::new(),
            waiters: HashMap::new(),
            key: Key::None,
            key_waiters: Vec::new(),
            media_on: false,
            reset_waiters: Vec::new(),
            talk: None,
            close_waiters: Vec::new(),
            finished: false,
            datagrams_in: 0,
            datagrams_out: 0,
            send_errors: 0,
            refused_sends: 0,
        })
    }

    /// The thread's body. A panic anywhere in a step ends this link with `ThreadFailed`, and nothing
    /// else.
    pub(crate) fn run(mut self) {
        let now = Instant::now();
        let mut targets = egress::broadcast_targets(self.config.interface.as_deref());
        targets.extend(self.config.peer_hint);
        self.iotc
            .connect(now, now + self.config.connect_timeout, &targets);
        let mut buf = vec![0u8; crate::iotc::codec::MAX_DATAGRAM + 1];
        while !self.finished {
            if let Err(panic) = catch_unwind(AssertUnwindSafe(|| self.step(&mut buf))) {
                let msg = panic
                    .downcast_ref::<&str>()
                    .map(|s| (*s).to_string())
                    .or_else(|| panic.downcast_ref::<String>().cloned())
                    .unwrap_or_default();
                log::error!("link thread failed: {msg}");
                let _ = catch_unwind(AssertUnwindSafe(|| {
                    self.fail(LinkError::Closed(CloseReason::ThreadFailed))
                }));
                break;
            }
        }
        self.shared.link_ended();
    }

    fn step(&mut self, buf: &mut [u8]) {
        let now = Instant::now();
        let wait = self
            .deadline()
            .map_or(IDLE_WAIT, |d| d.saturating_duration_since(now))
            .clamp(MIN_WAIT, IDLE_WAIT);
        let _ = self.socket.set_nonblocking(false);
        let _ = self.socket.set_read_timeout(Some(wait));
        let first = self.socket.recv_from(buf);
        self.on_recv(first, buf);
        // Then whatever else has arrived, without waiting.
        if self.socket.set_nonblocking(true).is_ok() {
            for _ in 0..512 {
                let next = self.socket.recv_from(buf);
                if next.is_err() {
                    break;
                }
                self.on_recv(next, buf);
            }
        }
        let now = Instant::now();
        while let Ok(cmd) = self.commands.try_recv() {
            self.on_command(now, cmd);
        }
        self.on_timeout(now);
        self.pump(now);
    }

    fn on_recv(&mut self, got: std::io::Result<(usize, SocketAddr)>, buf: &mut [u8]) {
        let (n, from) = match got {
            Ok(x) => x,
            Err(e)
                if matches!(
                    e.kind(),
                    ErrorKind::WouldBlock | ErrorKind::TimedOut | ErrorKind::Interrupted
                ) =>
            {
                return;
            }
            Err(e) => {
                log::debug!("recv: {e}");
                return;
            }
        };
        if from == self.waker {
            return;
        }
        let SocketAddr::V4(from) = from else { return };
        let Some(datagram) = buf.get_mut(..n) else { return };
        self.datagrams_in += 1;
        let now = Instant::now();
        self.iotc.handle_datagram(now, from, datagram);
        self.pump(now);
    }

    fn deadline(&self) -> Option<Instant> {
        [
            self.iotc.poll_timeout(),
            self.dtls.as_ref().and_then(DtlsClient::poll_timeout),
            self.av.as_ref().and_then(AvClient::poll_timeout),
            self.requests.poll_timeout(),
            self.talk.as_ref().and_then(|t| t.server.poll_timeout()),
            self.talk
                .as_ref()
                .and_then(|t| t.accept.iter().map(|(d, _)| *d).min()),
        ]
        .into_iter()
        .flatten()
        .min()
    }

    fn on_timeout(&mut self, now: Instant) {
        self.iotc.handle_timeout(now);
        if let Some(d) = &mut self.dtls {
            d.handle_timeout(now);
        }
        if let Some(av) = &mut self.av {
            av.handle_timeout(now);
        }
        self.requests.handle_timeout(now);
        if let Some(talk) = &mut self.talk {
            if let Key::Ready(master) = &self.key {
                let mut sealer = LinkSealer {
                    master,
                    rng: &mut self.rng,
                };
                talk.server.handle_timeout(now, &mut sealer);
            }
            let (late, waiting): (Vec<_>, Vec<_>) = talk.accept.drain(..).partition(|(d, _)| now >= *d);
            talk.accept = waiting;
            for (_, done) in late {
                done.complete(Err(LinkError::Timeout));
            }
        }
    }

    // ---- moving data between the layers ----

    fn pump(&mut self, now: Instant) {
        for _ in 0..64 {
            let mut moved = false;
            while let Some(ev) = self.iotc.poll_event() {
                moved = true;
                self.on_iotc(now, ev);
            }
            while let Some(ev) = self.dtls.as_mut().and_then(DtlsClient::poll_event) {
                moved = true;
                self.on_dtls(now, ev);
            }
            while let Some(ev) = self.av.as_mut().and_then(AvClient::poll_event) {
                moved = true;
                self.on_av(now, ev);
            }
            while let Some(ev) = self.requests.poll_event() {
                moved = true;
                self.on_request(now, ev);
            }
            while let Some(ev) = self.talk.as_mut().and_then(|t| t.server.poll_event()) {
                moved = true;
                self.on_server(ev);
            }
            moved |= self.feed_talk(now);
            moved |= self.move_transmits(now);
            if !moved || self.finished {
                break;
            }
        }
        self.flush_socket();
    }

    /// AV packets into DTLS, DTLS and talk-back payloads into IOTC.
    fn move_transmits(&mut self, now: Instant) -> bool {
        let mut moved = false;
        while let Some(pkt) = self.av.as_mut().and_then(AvClient::poll_transmit) {
            moved = true;
            if let Some(d) = &mut self.dtls {
                let _ = d.send(now, &pkt);
            }
        }
        while let Some(rec) = self.dtls.as_mut().and_then(DtlsClient::poll_transmit) {
            moved = true;
            let _ = self.iotc.send(0, DATATYPE_DTLS, &rec);
        }
        while let Some((channel, (datatype, data))) = self
            .talk
            .as_mut()
            .and_then(|t| t.server.poll_transmit().map(|x| (t.channel, x)))
        {
            moved = true;
            let _ = self.iotc.send(channel, datatype, &data);
        }
        moved
    }

    fn flush_socket(&mut self) {
        while let Some(tx) = self.iotc.poll_transmit() {
            if !egress::check(*tx.to.ip()) {
                self.refused_sends += 1;
                continue;
            }
            match self.socket.send_to(&tx.data, tx.to) {
                Ok(_) => self.datagrams_out += 1,
                Err(e) => {
                    self.send_errors += 1;
                    log::debug!("send to {}: {e}", tx.to);
                }
            }
        }
    }

    /// Move queued talk-back audio into the server while it has room.
    fn feed_talk(&mut self, now: Instant) -> bool {
        let Some(talk) = &mut self.talk else { return false };
        let mut moved = false;
        while talk.server.is_accepted() && talk.server.space() > 0 {
            let Some(frame) = self.shared.talk_in.try_pop() else {
                break;
            };
            moved = true;
            let _ = talk.server.push(now, frame);
        }
        moved
    }

    fn on_iotc(&mut self, now: Instant, ev: IotcEvent) {
        match ev {
            IotcEvent::Connected(peer) => {
                log::info!(
                    "iotc: connected to {} (version 0x{:x})",
                    peer.addr,
                    peer.proto_version
                );
                if peer.pe_capable {
                    let _ = self.iotc.set_partial_encryption(true);
                }
                let config = DtlsConfig {
                    suite: self.config.suite,
                    handshake_timeout: self.config.av_timeout,
                    ..DtlsConfig::default()
                };
                match DtlsClient::new(config, &self.config.account, &self.config.password, now) {
                    Ok(d) => self.dtls = Some(d),
                    Err(e) => self.fail(e),
                }
            }
            IotcEvent::Data {
                channel: 0,
                datatype: DATATYPE_DTLS,
                payload,
                ..
            } => {
                if let Some(d) = &mut self.dtls {
                    d.handle_datagram(now, &payload);
                }
            }
            IotcEvent::Data {
                channel,
                datatype,
                payload,
                ..
            } => {
                if let Some(talk) = self.talk.as_mut().filter(|t| t.channel == channel) {
                    talk.server.handle_datagram(now, datatype, &payload);
                }
            }
            IotcEvent::Ended(e) => self.fail(e),
        }
    }

    fn on_dtls(&mut self, now: Instant, ev: DtlsEvent) {
        match ev {
            DtlsEvent::Connected => {
                let config = AvConfig {
                    ctrl_window: self.config.ctrl_window,
                    ..AvConfig::new(&self.config.account, &self.config.password)
                };
                let nonce = self.rng.u32();
                self.av = Some(AvClient::new(config, nonce, now, now + self.config.av_timeout));
            }
            DtlsEvent::Data(pkt) => {
                if let Some(av) = &mut self.av {
                    av.handle_packet(now, &pkt);
                }
            }
            DtlsEvent::Ended(e) => self.fail(e),
        }
    }

    fn on_av(&mut self, now: Instant, ev: AvEvent) {
        match ev {
            AvEvent::Ready => {
                let Some(peer) = self.iotc.peer() else { return };
                let info = LinkInfo {
                    peer: peer.addr,
                    local_port: self.socket.local_addr().map_or(0, |a| a.port()),
                    proto_version: peer.proto_version,
                    iotc_version: peer.iotc_version,
                    dtls_suite: self.dtls.as_ref().and_then(DtlsClient::negotiated_suite),
                    partial_encryption: peer.pe_capable,
                };
                log::info!("link up: {} via local port {}", info.peer, info.local_port);
                if let Some(done) = self.connect_done.take() {
                    done.complete(Ok(info));
                }
            }
            AvEvent::Control { typ, payload } => self.requests.on_control(now, typ, payload),
            AvEvent::Acked(id) => {
                if let Some(done) = self.sends.remove(&id) {
                    done.complete(Ok(()));
                }
            }
            AvEvent::Frame(frame) => {
                if self.media_on {
                    self.shared.media.push(frame);
                }
            }
            AvEvent::ResetDone { .. } => {
                for done in self.reset_waiters.drain(..) {
                    done.complete(Ok(()));
                }
            }
            AvEvent::Ended(e) => self.fail(e),
        }
        while let Some(push) = self.requests.poll_push() {
            self.shared.pushes.push_drop_oldest((push.typ, push.payload));
        }
    }

    fn on_request(&mut self, now: Instant, ev: RequestEvent) {
        match ev {
            RequestEvent::Send { id, typ, payload } => {
                let sent = match self.av.as_mut() {
                    Some(av) => av.send_ioctrl(now, typ, &payload).map(|_| ()),
                    None => Err(LinkError::Protocol("link not up")),
                };
                if let Err(e) = sent {
                    self.finish_waiter(id, Err(e));
                    self.requests.close(id);
                }
            }
            RequestEvent::Reply { id, payload } => match self.waiters.get(&id) {
                Some(Waiter::Single(done)) => {
                    done.complete(Ok(payload));
                    self.waiters.remove(&id);
                }
                Some(Waiter::Exchange(inbox)) => inbox.push_drop_oldest(payload),
                Some(Waiter::Key) => {
                    self.waiters.remove(&id);
                    self.on_key_reply(&payload);
                }
                None => {}
            },
            RequestEvent::Done { id, result } => self.finish_waiter(id, result),
        }
    }

    fn finish_waiter(&mut self, id: RequestId, result: Result<(), LinkError>) {
        match self.waiters.remove(&id) {
            Some(Waiter::Single(done)) => done.complete(result.map(|()| Vec::new())),
            Some(Waiter::Exchange(inbox)) => inbox.close(result),
            Some(Waiter::Key) => self.key_failed(result.err().unwrap_or(LinkError::Timeout)),
            None => {}
        }
    }

    fn on_server(&mut self, ev: ServerEvent) {
        let Some(talk) = &mut self.talk else { return };
        match ev {
            ServerEvent::Accepted => {
                for (_, done) in talk.accept.drain(..) {
                    done.complete(Ok(()));
                }
            }
            ServerEvent::Ended(e) => {
                for (_, done) in talk.accept.drain(..) {
                    done.complete(Err(e.clone()));
                }
                for done in talk.close.drain(..) {
                    done.complete(Ok(()));
                }
                let channel = talk.channel;
                self.iotc.close_channel(channel);
                self.shared.talk_in.clear();
                self.talk = None;
            }
        }
    }

    // ---- the live-view key ----

    fn need_key(&mut self, now: Instant, waiter: KeyWaiter) {
        match &self.key {
            Key::Ready(_) => self.key_ready(now, waiter),
            Key::Exchanging(_) => self.key_waiters.push(waiter),
            Key::None => {
                let mut secret = [0u8; 32];
                self.rng.fill(&mut secret);
                let kx = KeyExchange::new(secret);
                let id =
                    self.requests
                        .request(OP_KEY_HELLO, &kx.hello(), OP_KEY_REPLY, now + KEY_TIMEOUT, false);
                self.waiters.insert(id, Waiter::Key);
                self.key = Key::Exchanging(kx);
                self.key_waiters.push(waiter);
            }
        }
    }

    fn on_key_reply(&mut self, reply: &[u8]) {
        let Key::Exchanging(kx) = std::mem::replace(&mut self.key, Key::None) else {
            return;
        };
        match kx.master(reply) {
            Ok(master) => {
                let master = Arc::new(master);
                *self.shared.master.lock().unwrap_or_else(PoisonError::into_inner) =
                    Some(Arc::clone(&master));
                self.key = Key::Ready(master);
                let now = Instant::now();
                for w in std::mem::take(&mut self.key_waiters) {
                    self.key_ready(now, w);
                }
            }
            Err(e) => self.key_failed(e),
        }
    }

    fn key_failed(&mut self, e: LinkError) {
        self.key = Key::None;
        for w in self.key_waiters.drain(..) {
            match w {
                KeyWaiter::Media(done) => done.complete(Err(e.clone())),
                KeyWaiter::Talk(done) => done.complete(Err(e.clone())),
            }
        }
    }

    fn key_ready(&mut self, now: Instant, waiter: KeyWaiter) {
        match waiter {
            KeyWaiter::Media(done) => {
                self.media_on = true;
                done.complete(Ok(()));
            }
            KeyWaiter::Talk(done) => {
                if self.talk.is_some() {
                    return done.complete(Err(LinkError::Capacity));
                }
                match self.iotc.free_channel() {
                    Ok(channel) => {
                        let config = ServerConfig {
                            queue: 2,
                            ..ServerConfig::default()
                        };
                        let server = AvServer::new(config, now, now + TALK_LISTEN);
                        self.talk = Some(Talk {
                            channel,
                            server,
                            accept: Vec::new(),
                            close: Vec::new(),
                        });
                        done.complete(Ok(channel));
                    }
                    Err(e) => done.complete(Err(e)),
                }
            }
        }
    }

    // ---- commands ----

    fn on_command(&mut self, now: Instant, cmd: Command) {
        if self.finished {
            return;
        }
        let ready = self.av.as_ref().is_some_and(AvClient::is_ready);
        match cmd {
            Command::Close { done } => {
                self.close_waiters.extend(done);
                self.iotc.close();
                self.flush_socket();
                self.fail(LinkError::Closed(CloseReason::Local));
            }
            Command::Stats { done } => done.complete(Ok(self.stats())),
            _ if !ready => {
                let e = LinkError::Protocol("link not up");
                match cmd {
                    Command::Send { done, .. }
                    | Command::MediaStart { done }
                    | Command::MediaReset { done } => {
                        done.complete(Err(e));
                    }
                    Command::Request { done, .. } => done.complete(Err(e)),
                    Command::Exchange { started, .. } => started.complete(Err(e)),
                    Command::TalkOpen { done } => done.complete(Err(e)),
                    Command::TalkAccept { done, .. } | Command::TalkClose { done, .. } => {
                        done.complete(Err(e))
                    }
                    _ => {}
                }
            }
            Command::Send { typ, payload, done } => {
                let Some(av) = &mut self.av else { return };
                match av.send_ioctrl(now, typ, &payload) {
                    Ok(id) => {
                        self.sends.insert(id, done);
                    }
                    Err(e) => done.complete(Err(e)),
                }
            }
            Command::Request {
                typ,
                payload,
                reply,
                deadline,
                done,
            } => {
                let id = self.requests.request(typ, &payload, reply, deadline, false);
                self.waiters.insert(id, Waiter::Single(done));
            }
            Command::Exchange {
                typ,
                payload,
                reply,
                deadline,
                started,
                inbox,
            } => {
                let id = self.requests.request(typ, &payload, reply, deadline, true);
                self.waiters.insert(id, Waiter::Exchange(inbox));
                started.complete(Ok(id));
            }
            Command::EndExchange { id } => self.requests.close(id),
            Command::MediaStart { done } => self.need_key(now, KeyWaiter::Media(done)),
            Command::MediaStop => {
                self.media_on = false;
                self.shared.media.clear();
            }
            Command::MediaReset { done } => {
                self.shared.media.clear();
                let Some(av) = &mut self.av else { return };
                match av.reset_media(now) {
                    Ok(()) => self.reset_waiters.push(done),
                    Err(e) => done.complete(Err(e)),
                }
            }
            Command::TalkOpen { done } => self.need_key(now, KeyWaiter::Talk(done)),
            Command::TalkAccept { deadline, done } => match &mut self.talk {
                Some(t) if t.server.is_accepted() => done.complete(Ok(())),
                Some(t) => t.accept.push((deadline, done)),
                None => done.complete(Err(LinkError::Protocol("talk-back channel not open"))),
            },
            Command::TalkClose { drain, done } => match &mut self.talk {
                Some(t) => {
                    // Whatever the caller queued goes to the server first, if it is to be drained.
                    if drain {
                        while t.server.space() > 0 {
                            let Some(frame) = self.shared.talk_in.try_pop() else {
                                break;
                            };
                            let _ = t.server.push(now, frame);
                        }
                    }
                    t.close.push(done);
                    t.server.close(drain);
                }
                None => done.complete(Ok(())),
            },
        }
    }

    fn stats(&self) -> LinkStats {
        LinkStats {
            iotc: self.iotc.stats(),
            dtls: self.dtls.as_ref().map(DtlsClient::stats).unwrap_or_default(),
            av: self.av.as_ref().map(AvClient::stats),
            talk: self.talk.as_ref().map(|t| t.server.stats()),
            datagrams_in: self.datagrams_in,
            datagrams_out: self.datagrams_out,
            send_errors: self.send_errors,
            refused_sends: self.refused_sends,
            ..LinkStats::default()
        }
    }

    /// End the link: record why, wake everyone with the error, and stop the loop.
    fn fail(&mut self, e: LinkError) {
        if self.finished {
            return;
        }
        if e == LinkError::Closed(CloseReason::Local) {
            log::info!("link closed");
        } else {
            log::info!("link down: {e}");
        }
        self.finished = true;
        if !self.iotc.is_ended() {
            self.iotc.close();
            self.flush_socket();
        }
        *self.shared.closed.lock().unwrap_or_else(PoisonError::into_inner) = Some(e.clone());
        if let Some(done) = self.connect_done.take() {
            done.complete(Err(e.clone()));
        }
        for (_, done) in self.sends.drain() {
            done.complete(Err(e.clone()));
        }
        self.requests.fail_all(&e);
        while let Some(ev) = self.requests.poll_event() {
            if let RequestEvent::Done { id, result } = ev {
                match self.waiters.remove(&id) {
                    Some(Waiter::Single(done)) => done.complete(result.map(|()| Vec::new())),
                    Some(Waiter::Exchange(inbox)) => inbox.close(result),
                    Some(Waiter::Key) | None => {}
                }
            }
        }
        self.key_failed(e.clone());
        for done in self.reset_waiters.drain(..) {
            done.complete(Err(e.clone()));
        }
        if let Some(talk) = self.talk.take() {
            for (_, done) in talk.accept {
                done.complete(Err(e.clone()));
            }
            for done in talk.close {
                done.complete(Ok(()));
            }
        }
        self.shared.pushes.close(Err(e.clone()));
        self.shared.media.close(e.clone());
        self.shared.talk_in.close(Err(e));
        for done in self.close_waiters.drain(..) {
            done.complete(Ok(()));
        }
    }
}
