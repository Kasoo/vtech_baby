//! The public API: `Link` and the objects it hands out. Every call that waits takes a timeout, and
//! blocks only on a `Completion` or a `Queue`; the protocol runs on the link's reactor thread
//! (`driver.rs`). Design: `docs/vtech-link-design/10-architecture.md`, `75-python-api.md`.

use std::collections::VecDeque;
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4, UdpSocket};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use socket2::{Domain, Protocol, Socket, Type};

use super::driver::{Command, Reactor};
use super::sync::{Completion, Queue};
use crate::av::media_rx::{EncryptedFrame, StreamKind};
use crate::av::packet::CTRL_MAX_PAYLOAD;
use crate::dtls::Suite;
use crate::error::{CloseReason, LinkError};
use crate::service::frame_crypto;
use crate::service::media_key::MasterKey;
use crate::service::requests::RequestId;

/// Links alive in this process, and the most allowed.
static LIVE_LINKS: AtomicUsize = AtomicUsize::new(0);
pub const MAX_LINKS: usize = 32;
const SOCKET_BUFFER: usize = 1_024_000;

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(PoisonError::into_inner)
}

#[derive(Debug, Clone)]
pub struct LinkConfig {
    pub uid: String,
    /// The 8-character IOTC authKey.
    pub authkey: String,
    pub password: String,
    pub account: String,
    pub connect_timeout: Duration,
    pub av_timeout: Duration,
    pub search_port: u16,
    /// The camera's last known address: searched by unicast as well as broadcast.
    pub peer_hint: Option<Ipv4Addr>,
    /// Search only on this interface.
    pub interface: Option<String>,
    pub suite: Suite,
    pub ctrl_window: usize,
    pub push_queue: usize,
    pub video_queue: usize,
    pub audio_queue: usize,
    pub talk_queue: usize,
    pub sdk_version: u32,
}

impl LinkConfig {
    pub fn new(uid: &str, authkey: &str, password: &str) -> Self {
        Self {
            uid: uid.to_string(),
            authkey: authkey.to_string(),
            password: password.to_string(),
            account: "admin".to_string(),
            connect_timeout: Duration::from_secs(15),
            av_timeout: Duration::from_secs(30),
            search_port: 32762,
            peer_hint: None,
            interface: None,
            suite: Suite::EcdhePskChaCha20,
            ctrl_window: 8,
            push_queue: 256,
            video_queue: 60,
            audio_queue: 200,
            talk_queue: 10,
            sdk_version: crate::iotc::codec::SDK_VERSION,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LinkInfo {
    pub peer: SocketAddrV4,
    pub local_port: u16,
    pub proto_version: u8,
    pub iotc_version: u32,
    pub dtls_suite: Option<u16>,
    pub partial_encryption: bool,
}

/// A decrypted media frame.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub stream: StreamKind,
    /// Video kinds 5 and 6.
    pub key: bool,
    pub number: u32,
    pub arrived: Instant,
    /// Frames of this stream lost just before this one: given up, overflowed or undecryptable.
    pub lost_before: u32,
    pub data: Vec<u8>,
}

/// Encrypted frames waiting for the consumer, both streams in arrival order, each stream capped.
pub(crate) struct MediaQueue {
    state: Mutex<MediaQState>,
    cv: Condvar,
    caps: [usize; 2],
}

struct MediaQState {
    items: VecDeque<EncryptedFrame>,
    counts: [usize; 2],
    overflow: [u64; 2],
    /// Frames dropped per stream since the last one taken, to add to its `lost_before`.
    lost: [u32; 2],
    closed: Option<LinkError>,
}

fn slot(stream: StreamKind) -> usize {
    match stream {
        StreamKind::Video => 0,
        StreamKind::Audio => 1,
    }
}

impl MediaQueue {
    fn new(video: usize, audio: usize) -> Self {
        Self {
            state: Mutex::new(MediaQState {
                items: VecDeque::new(),
                counts: [0; 2],
                overflow: [0; 2],
                lost: [0; 2],
                closed: None,
            }),
            cv: Condvar::new(),
            caps: [video.max(1), audio.max(1)],
        }
    }

    /// Add a frame; when its stream is full, drop that stream's oldest (`media_recv.c:160`).
    pub(crate) fn push(&self, frame: EncryptedFrame) {
        let mut s = lock(&self.state);
        let i = slot(frame.stream);
        if s.counts.get(i).copied().unwrap_or(0) >= self.caps.get(i).copied().unwrap_or(1) {
            if let Some(pos) = s.items.iter().position(|f| slot(f.stream) == i) {
                s.items.remove(pos);
                if let Some(c) = s.counts.get_mut(i) {
                    *c -= 1;
                }
                if let Some(o) = s.overflow.get_mut(i) {
                    *o += 1;
                }
                if let Some(l) = s.lost.get_mut(i) {
                    *l += 1;
                }
            }
        }
        if let Some(c) = s.counts.get_mut(i) {
            *c += 1;
        }
        s.items.push_back(frame);
        self.cv.notify_all();
    }

    fn pop_wait(&self, timeout: Duration) -> Result<Option<(EncryptedFrame, u32)>, LinkError> {
        let deadline = Instant::now() + timeout;
        let mut s = lock(&self.state);
        loop {
            if let Some(f) = s.items.pop_front() {
                let i = slot(f.stream);
                if let Some(c) = s.counts.get_mut(i) {
                    *c -= 1;
                }
                let dropped = s.lost.get_mut(i).map(std::mem::take).unwrap_or(0);
                return Ok(Some((f, dropped)));
            }
            if let Some(e) = &s.closed {
                return Err(e.clone());
            }
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Ok(None);
            }
            s = self
                .cv
                .wait_timeout(s, left)
                .unwrap_or_else(PoisonError::into_inner)
                .0;
        }
    }

    pub(crate) fn clear(&self) {
        let mut s = lock(&self.state);
        s.items.clear();
        s.counts = [0; 2];
        s.lost = [0; 2];
    }

    pub(crate) fn close(&self, e: LinkError) {
        let mut s = lock(&self.state);
        s.closed.get_or_insert(e);
        self.cv.notify_all();
    }

    fn overflow(&self) -> [u64; 2] {
        lock(&self.state).overflow
    }
}

/// What the reactor and the caller-side handles share.
pub(crate) struct Shared {
    pub(crate) commands: Mutex<Sender<Command>>,
    waker: UdpSocket,
    wake_to: SocketAddr,
    pub(crate) pushes: Queue<(u32, Vec<u8>)>,
    pub(crate) media: MediaQueue,
    pub(crate) talk_in: Queue<Vec<u8>>,
    pub(crate) master: Mutex<Option<Arc<MasterKey>>>,
    pub(crate) closed: Mutex<Option<LinkError>>,
    pub(crate) info: Mutex<Option<LinkInfo>>,
    thread: Mutex<Option<JoinHandle<()>>>,
    decrypt_failed: [AtomicU64; 2],
}

impl Shared {
    /// Hand the reactor a command and wake it.
    fn command(&self, cmd: Command) -> Result<(), LinkError> {
        if let Some(e) = lock(&self.closed).clone() {
            return Err(e);
        }
        lock(&self.commands)
            .send(cmd)
            .map_err(|_| LinkError::Closed(CloseReason::ThreadFailed))?;
        self.wake();
        Ok(())
    }

    pub(crate) fn wake(&self) {
        // A lost wake-up only delays the reactor to its next deadline.
        let _ = self.waker.send_to(&[0], self.wake_to);
    }

    pub(crate) fn link_ended(&self) {
        LIVE_LINKS.fetch_sub(1, Ordering::Relaxed);
    }
}

/// The caller-side owner. When the last handle goes, the link closes.
struct Handle {
    shared: Arc<Shared>,
}

impl Drop for Handle {
    fn drop(&mut self) {
        let _ = self.shared.command(Command::Close { done: None });
    }
}

/// One LAN-direct connection to a camera.
#[derive(Clone)]
pub struct Link {
    handle: Arc<Handle>,
}

fn timeout_deadline(timeout: Duration) -> Instant {
    Instant::now() + timeout
}

impl Link {
    /// Connect, and block until the link is up or has failed.
    pub fn connect(config: LinkConfig) -> Result<Self, LinkError> {
        if config.authkey.len() != 8 {
            return Err(LinkError::InvalidArgument("authkey must be 8 characters"));
        }
        crate::iotc::session::wire_uid(&config.uid)?;
        if LIVE_LINKS.fetch_add(1, Ordering::Relaxed) >= MAX_LINKS {
            LIVE_LINKS.fetch_sub(1, Ordering::Relaxed);
            return Err(LinkError::Capacity);
        }
        Self::start(config)
    }

    fn start(config: LinkConfig) -> Result<Self, LinkError> {
        // Until the reactor thread runs, a failure here gives the link slot back; after that the thread
        // does when it exits.
        let io = |e: std::io::Error| {
            LIVE_LINKS.fetch_sub(1, Ordering::Relaxed);
            LinkError::Io(e.kind())
        };
        let socket = Socket::new(Domain::IPV4, Type::DGRAM, Some(Protocol::UDP)).map_err(io)?;
        socket.set_broadcast(true).map_err(io)?;
        // Best effort: a 1080p keyframe arrives as a burst of ~200 datagrams.
        let _ = socket.set_recv_buffer_size(SOCKET_BUFFER);
        let _ = socket.set_send_buffer_size(SOCKET_BUFFER);
        socket
            .bind(&SocketAddr::from((Ipv4Addr::UNSPECIFIED, 0)).into())
            .map_err(io)?;
        let socket: UdpSocket = socket.into();
        let port = socket.local_addr().map_err(io)?.port();
        let waker = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).map_err(io)?;
        let waker_addr = waker.local_addr().map_err(io)?;

        let (tx, rx) = mpsc::channel();
        let shared = Arc::new(Shared {
            commands: Mutex::new(tx),
            waker,
            wake_to: SocketAddr::from((Ipv4Addr::LOCALHOST, port)),
            pushes: Queue::new(config.push_queue),
            media: MediaQueue::new(config.video_queue, config.audio_queue),
            talk_in: Queue::new(config.talk_queue),
            master: Mutex::new(None),
            closed: Mutex::new(None),
            info: Mutex::new(None),
            thread: Mutex::new(None),
            decrypt_failed: [AtomicU64::new(0), AtomicU64::new(0)],
        });
        let done = Arc::new(Completion::default());
        let wait = config.connect_timeout + config.av_timeout + Duration::from_secs(1);
        let name = format!("vtech-link-{}", &config.uid[config.uid.len().saturating_sub(6)..]);
        let reactor = Reactor::new(
            config,
            Arc::clone(&shared),
            socket,
            rx,
            waker_addr,
            Arc::clone(&done),
        )
        .inspect_err(|_| {
            LIVE_LINKS.fetch_sub(1, Ordering::Relaxed);
        })?;
        let thread = std::thread::Builder::new()
            .name(name)
            .spawn(move || reactor.run())
            .map_err(io)?;
        *lock(&shared.thread) = Some(thread);
        let link = Self {
            handle: Arc::new(Handle { shared }),
        };
        match done.wait(wait) {
            Ok(info) => {
                *lock(&link.shared().info) = Some(info);
                Ok(link)
            }
            Err(e) => {
                link.close();
                Err(e)
            }
        }
    }

    fn shared(&self) -> &Arc<Shared> {
        &self.handle.shared
    }

    /// Send an IOCtrl; returns once the camera has acknowledged it. Concurrent callers queue.
    pub fn send(&self, typ: u32, payload: &[u8], timeout: Duration) -> Result<(), LinkError> {
        check_payload(payload)?;
        let done = Arc::new(Completion::default());
        self.shared().command(Command::Send {
            typ,
            payload: payload.to_vec(),
            done: Arc::clone(&done),
        })?;
        done.wait(timeout)
    }

    /// Send an IOCtrl and wait for the reply of type `reply`.
    pub fn request(
        &self,
        typ: u32,
        payload: &[u8],
        reply: u32,
        timeout: Duration,
    ) -> Result<Vec<u8>, LinkError> {
        check_payload(payload)?;
        let done = Arc::new(Completion::default());
        self.shared().command(Command::Request {
            typ,
            payload: payload.to_vec(),
            reply,
            deadline: timeout_deadline(timeout),
            done: Arc::clone(&done),
        })?;
        // The reactor times the request out; the margin covers its wake-up.
        done.wait(timeout + Duration::from_millis(500))
    }

    /// Send an IOCtrl and collect every reply of type `reply` until closed or `timeout`.
    pub fn exchange(
        &self,
        typ: u32,
        payload: &[u8],
        reply: u32,
        timeout: Duration,
    ) -> Result<Exchange, LinkError> {
        check_payload(payload)?;
        let started = Arc::new(Completion::default());
        let inbox = Arc::new(Queue::new(64));
        self.shared().command(Command::Exchange {
            typ,
            payload: payload.to_vec(),
            reply,
            deadline: timeout_deadline(timeout),
            started: Arc::clone(&started),
            inbox: Arc::clone(&inbox),
        })?;
        let id = started.wait(Duration::from_secs(1))?;
        Ok(Exchange {
            link: self.clone(),
            id,
            inbox,
        })
    }

    /// The next IOCtrl no request claimed; `None` on timeout.
    pub fn next_push(&self, timeout: Duration) -> Result<Option<(u32, Vec<u8>)>, LinkError> {
        self.shared().pushes.pop_wait(timeout)
    }

    /// The media receiver. Nothing is queued until `start`.
    pub fn media(&self) -> MediaReceiver {
        MediaReceiver {
            link: self.clone(),
            lost: [0; 2],
        }
    }

    /// Open the talk-back channel (running the key exchange if needed); announce `channel` to the
    /// camera, then `accept`.
    pub fn talkback(&self, timeout: Duration) -> Result<TalkBack, LinkError> {
        let done = Arc::new(Completion::default());
        self.shared().command(Command::TalkOpen {
            done: Arc::clone(&done),
        })?;
        let channel = done.wait(timeout)?;
        Ok(TalkBack {
            link: self.clone(),
            channel,
        })
    }

    pub fn info(&self) -> Option<LinkInfo> {
        lock(&self.shared().info).clone()
    }

    pub fn stats(&self, timeout: Duration) -> Result<LinkStats, LinkError> {
        let done = Arc::new(Completion::default());
        self.shared().command(Command::Stats {
            done: Arc::clone(&done),
        })?;
        let mut stats = done.wait(timeout)?;
        stats.video_overflow = self.shared().media.overflow()[0];
        stats.audio_overflow = self.shared().media.overflow()[1];
        stats.decrypt_failed_video = self.shared().decrypt_failed[0].load(Ordering::Relaxed);
        stats.decrypt_failed_audio = self.shared().decrypt_failed[1].load(Ordering::Relaxed);
        stats.pushes_dropped = self.shared().pushes.dropped();
        Ok(stats)
    }

    /// Close the link, waiting briefly for the close datagrams to go out. Idempotent.
    pub fn close(&self) {
        let done = Arc::new(Completion::default());
        if self
            .shared()
            .command(Command::Close {
                done: Some(Arc::clone(&done)),
            })
            .is_ok()
        {
            let _ = done.wait(Duration::from_secs(1));
        }
        // Join the thread if it has already gone; otherwise it finishes on its own.
        let mut thread = lock(&self.shared().thread);
        if thread.as_ref().is_some_and(JoinHandle::is_finished) {
            if let Some(t) = thread.take() {
                let _ = t.join();
            }
        }
    }

    /// Why the link ended, once it has.
    pub fn closed(&self) -> Option<LinkError> {
        lock(&self.shared().closed).clone()
    }
}

fn check_payload(payload: &[u8]) -> Result<(), LinkError> {
    if payload.len() > CTRL_MAX_PAYLOAD {
        return Err(LinkError::InvalidArgument("IOCtrl payload over 1020 bytes"));
    }
    Ok(())
}

/// Replies to one exchange.
pub struct Exchange {
    link: Link,
    id: RequestId,
    inbox: Arc<Queue<Vec<u8>>>,
}

impl Exchange {
    /// The next reply; `None` on timeout or once the exchange has ended.
    pub fn next(&self, timeout: Duration) -> Result<Option<Vec<u8>>, LinkError> {
        self.inbox.pop_wait(timeout)
    }

    /// Free the reply type for the next request.
    pub fn close(&self) {
        let _ = self.link.shared().command(Command::EndExchange { id: self.id });
    }
}

impl Drop for Exchange {
    fn drop(&mut self) {
        if !self.inbox.is_closed() {
            self.close();
        }
    }
}

/// The live view's frames, decrypted on the caller's thread.
pub struct MediaReceiver {
    link: Link,
    lost: [u32; 2],
}

impl MediaReceiver {
    /// Run the key exchange if needed, and start queueing frames.
    pub fn start(&self, timeout: Duration) -> Result<(), LinkError> {
        let done = Arc::new(Completion::default());
        self.link.shared().command(Command::MediaStart {
            done: Arc::clone(&done),
        })?;
        done.wait(timeout)
    }

    /// Drop what is queued and in progress, and restart the packet numbering with the camera.
    pub fn reset(&self, timeout: Duration) -> Result<(), LinkError> {
        let done = Arc::new(Completion::default());
        self.link.shared().command(Command::MediaReset {
            done: Arc::clone(&done),
        })?;
        done.wait(timeout)
    }

    /// The next frame, decrypted; `None` on timeout. Frames that fail to decrypt are skipped and
    /// counted in the next one's `lost_before`.
    pub fn next(&mut self, timeout: Duration) -> Result<Option<Frame>, LinkError> {
        let deadline = Instant::now() + timeout;
        loop {
            let left = deadline.saturating_duration_since(Instant::now());
            let Some((f, dropped)) = self.link.shared().media.pop_wait(left)? else {
                return Ok(None);
            };
            let i = slot(f.stream);
            let master = lock(&self.link.shared().master).clone();
            let plain = master.and_then(|m| frame_crypto::decrypt(&m, &f.data));
            let lost = self.lost.get_mut(i);
            let Some(plain) = plain else {
                if let Some(counter) = self.link.shared().decrypt_failed.get(i) {
                    counter.fetch_add(1, Ordering::Relaxed);
                }
                if let Some(l) = lost {
                    *l += 1 + dropped;
                }
                continue;
            };
            let carried = lost.map(std::mem::take).unwrap_or(0);
            return Ok(Some(Frame {
                stream: f.stream,
                key: matches!(f.kind, 5 | 6),
                number: f.number,
                arrived: f.arrived,
                lost_before: f.lost_before + dropped + carried,
                data: plain,
            }));
        }
    }

    /// Stop queueing frames and drop what is queued.
    pub fn close(&self) {
        let _ = self.link.shared().command(Command::MediaStop);
        self.link.shared().media.clear();
    }
}

/// The talk-back channel.
pub struct TalkBack {
    link: Link,
    pub channel: u8,
}

impl TalkBack {
    /// Wait for the camera to log in to the channel.
    pub fn accept(&self, timeout: Duration) -> Result<(), LinkError> {
        let done = Arc::new(Completion::default());
        self.link.shared().command(Command::TalkAccept {
            deadline: timeout_deadline(timeout),
            done: Arc::clone(&done),
        })?;
        done.wait(timeout + Duration::from_millis(500))
    }

    /// Queue one A-law frame (160 bytes, 20 ms); blocks while the queue is full.
    pub fn send(&self, alaw: &[u8], timeout: Duration) -> Result<(), LinkError> {
        if let Some(e) = self.link.closed() {
            return Err(e);
        }
        self.link.shared().talk_in.push_wait(alaw.to_vec(), timeout)?;
        self.link.shared().wake();
        Ok(())
    }

    /// Close the channel: after sending what is queued (`drain`), or at once.
    pub fn close(&self, drain: bool, timeout: Duration) -> Result<(), LinkError> {
        let done = Arc::new(Completion::default());
        self.link.shared().command(Command::TalkClose {
            drain,
            done: Arc::clone(&done),
        })?;
        done.wait(timeout)
    }
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct LinkStats {
    pub iotc: crate::iotc::IotcStats,
    pub dtls: crate::dtls::DtlsStats,
    pub av: Option<crate::av::AvStats>,
    pub talk: Option<crate::av::server::ServerStats>,
    pub datagrams_in: u64,
    pub datagrams_out: u64,
    pub send_errors: u64,
    pub refused_sends: u64,
    pub pushes_dropped: u64,
    pub video_overflow: u64,
    pub audio_overflow: u64,
    pub decrypt_failed_video: u64,
    pub decrypt_failed_audio: u64,
}
