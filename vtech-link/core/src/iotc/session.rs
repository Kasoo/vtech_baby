//! `IotcSession`: the sans-IO state machine for one LAN-direct IOTC session (search, connect,
//! channel data, keepalive, close). Design: `docs/vtech-link-design/20-iotc.md`, "The session state
//! machine"; reference `tutk-host/iotc/inbound.c`, `discovery.c`, `init.c`, `handshake.c`.

use std::collections::VecDeque;
use std::net::{Ipv4Addr, SocketAddrV4};
use std::time::{Duration, Instant};

use super::codec::{self, Drop, Ids, Inbound, LanReply};
use crate::error::{CloseReason, IotcReject, LinkError};

/// Channels per session, 0 to 31.
pub const CHANNELS: u8 = 32;

#[derive(Debug, Clone)]
pub struct IotcConfig {
    pub search_port: u16,
    pub search_interval: Duration,
    pub alive_interval: Duration,
    /// Silent keepalive ticks before the link is declared lost.
    pub silence_limit: u32,
    /// Silent ticks after which a keepalive goes out every tick rather than every other one.
    pub quiet_ticks: u32,
    pub sdk_version: u32,
}

impl Default for IotcConfig {
    fn default() -> Self {
        Self {
            search_port: 32762,
            search_interval: Duration::from_millis(100),
            alive_interval: Duration::from_secs(1),
            silence_limit: 60,
            quiet_ticks: 5,
            sdk_version: codec::SDK_VERSION,
        }
    }
}

/// A datagram to send, already scrambled where the wire needs it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Transmit {
    pub to: SocketAddrV4,
    pub data: Vec<u8>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PeerInfo {
    pub addr: SocketAddrV4,
    /// Header `+0x02` of the camera's reply.
    pub proto_version: u8,
    pub iotc_version: u32,
    /// Whether the camera allows partial encryption.
    pub pe_capable: bool,
}

#[derive(Debug, PartialEq, Eq)]
pub enum IotcEvent {
    Connected(PeerInfo),
    Data {
        channel: u8,
        datatype: u8,
        seq: u16,
        payload: Vec<u8>,
    },
    /// The session is over; nothing more will be sent or emitted.
    Ended(LinkError),
}

/// Datagrams dropped, by reason.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct IotcStats {
    pub bad_size: u64,
    pub bad_magic: u64,
    pub malformed: u64,
    /// From an address other than the peer, or naming another token.
    pub foreign: u64,
    /// For a channel that is not open.
    pub closed_channel: u64,
    /// Valid, but not expected in the current state.
    pub ignored: u64,
}

#[derive(Debug)]
enum State {
    Idle,
    Searching {
        deadline: Instant,
        next_search: Instant,
        targets: Vec<SocketAddrV4>,
    },
    Established {
        peer: PeerInfo,
        next_alive: Instant,
        silence: u32,
        alive_ticks: u32,
    },
    Ended,
}

pub struct IotcSession {
    config: IotcConfig,
    ids: Ids,
    state: State,
    pe_on: bool,
    send_seq: [u16; CHANNELS as usize],
    channel_open: u32,
    transmits: VecDeque<Transmit>,
    events: VecDeque<IotcEvent>,
    stats: IotcStats,
}

/// The UID as it goes on the wire: uppercased and NUL-padded to 20 bytes.
pub fn wire_uid(uid: &str) -> Result<[u8; 20], LinkError> {
    if uid.is_empty() || uid.len() > 20 || !uid.bytes().all(|b| b.is_ascii_alphanumeric()) {
        return Err(LinkError::InvalidArgument(
            "uid must be 1-20 ASCII letters and digits",
        ));
    }
    let mut out = [0u8; 20];
    for (o, b) in out.iter_mut().zip(uid.bytes()) {
        *o = b.to_ascii_uppercase();
    }
    Ok(out)
}

impl IotcSession {
    /// A session for `uid` and `authkey` (8 bytes), with this connect's random `token`.
    pub fn new(config: IotcConfig, uid: &str, authkey: &[u8], token: [u8; 8]) -> Result<Self, LinkError> {
        let authkey: [u8; 8] = authkey
            .try_into()
            .map_err(|_| LinkError::InvalidArgument("authkey must be 8 bytes"))?;
        let ids = Ids {
            uid: wire_uid(uid)?,
            authkey,
            token,
            sdk_version: config.sdk_version,
        };
        Ok(Self {
            config,
            ids,
            state: State::Idle,
            pe_on: false,
            send_seq: [0; CHANNELS as usize],
            channel_open: 1,
            transmits: VecDeque::new(),
            events: VecDeque::new(),
            stats: IotcStats::default(),
        })
    }

    /// Start searching: every `search_interval` until `deadline`, send the record and the probe to
    /// each of `targets` (directed broadcast addresses, and the camera's last known address if any)
    /// on the search port.
    pub fn connect(&mut self, now: Instant, deadline: Instant, targets: &[Ipv4Addr]) {
        if !matches!(self.state, State::Idle) {
            return;
        }
        let targets = targets
            .iter()
            .map(|&ip| SocketAddrV4::new(ip, self.config.search_port))
            .collect();
        self.state = State::Searching {
            deadline,
            next_search: now,
            targets,
        };
        self.handle_timeout(now);
    }

    pub fn peer(&self) -> Option<PeerInfo> {
        match self.state {
            State::Established { peer, .. } => Some(peer),
            _ => None,
        }
    }

    pub fn is_ended(&self) -> bool {
        matches!(self.state, State::Ended)
    }

    pub fn stats(&self) -> IotcStats {
        self.stats
    }

    pub fn poll_transmit(&mut self) -> Option<Transmit> {
        self.transmits.pop_front()
    }

    pub fn poll_event(&mut self) -> Option<IotcEvent> {
        self.events.pop_front()
    }

    /// When `handle_timeout` next needs calling.
    pub fn poll_timeout(&self) -> Option<Instant> {
        match self.state {
            State::Searching {
                deadline,
                next_search,
                ..
            } => Some(deadline.min(next_search)),
            State::Established { next_alive, .. } => Some(next_alive),
            State::Idle | State::Ended => None,
        }
    }

    pub fn handle_timeout(&mut self, now: Instant) {
        match &mut self.state {
            State::Searching {
                deadline,
                next_search,
                targets,
            } => {
                if now >= *deadline {
                    self.end(LinkError::Unreachable);
                    return;
                }
                if now >= *next_search {
                    *next_search = now + self.config.search_interval;
                    let record = codec::record(&self.ids);
                    let mut probe = codec::probe(&self.ids);
                    codec::seal(&mut probe);
                    for &to in targets.iter() {
                        self.transmits.push_back(Transmit {
                            to,
                            data: record.to_vec(),
                        });
                        self.transmits.push_back(Transmit {
                            to,
                            data: probe.to_vec(),
                        });
                    }
                }
            }
            State::Established {
                peer,
                next_alive,
                silence,
                alive_ticks,
            } => {
                let to = peer.addr;
                let mut keepalive = false;
                while now >= *next_alive {
                    *next_alive += self.config.alive_interval;
                    *silence += 1;
                    if *silence > self.config.silence_limit {
                        self.end(LinkError::Closed(CloseReason::RemoteTimeout));
                        return;
                    }
                    // Every tick once the camera has been quiet, else every other tick (`init.c:89`).
                    if *silence >= self.config.quiet_ticks || {
                        *alive_ticks = alive_ticks.wrapping_add(1);
                        *alive_ticks % 2 == 0
                    } {
                        keepalive = true;
                    }
                }
                if keepalive {
                    self.push_sealed(
                        to,
                        codec::token_datagram(&self.ids.token, codec::OP_ALIVE_OUT).to_vec(),
                    );
                }
            }
            State::Idle | State::Ended => {}
        }
    }

    /// Handle one received datagram (scrambled, as read from the socket). `buf` is unscrambled in
    /// place.
    pub fn handle_datagram(&mut self, now: Instant, from: SocketAddrV4, buf: &mut [u8]) {
        let inbound = match codec::decode(buf) {
            Ok(inbound) => inbound,
            Err(Drop::Size) => return self.stats.bad_size += 1,
            Err(Drop::Magic) => return self.stats.bad_magic += 1,
            Err(Drop::Malformed) => return self.stats.malformed += 1,
        };
        match &mut self.state {
            // Any address may answer the search; the token or UID is the filter.
            State::Searching { .. } => match inbound {
                Inbound::LanReply(LanReply::Long {
                    token,
                    result,
                    proto_version,
                    iotc_version,
                    pe_capable,
                }) => {
                    if token != self.ids.token {
                        return self.stats.foreign += 1;
                    }
                    let peer = PeerInfo {
                        addr: from,
                        proto_version,
                        iotc_version,
                        pe_capable,
                    };
                    self.on_long_reply(now, peer, result);
                }
                Inbound::LanReply(LanReply::Short { uid, .. }) if uid == self.ids.uid => {
                    // We always have an authKey, and this camera answered without key authentication.
                    self.end(LinkError::IotcRejected(IotcReject::NoKeyAuth));
                }
                _ => self.stats.ignored += 1,
            },
            State::Established { peer, silence, .. } => {
                if from != peer.addr {
                    return self.stats.foreign += 1;
                }
                match inbound {
                    Inbound::Data {
                        token,
                        channel,
                        datatype,
                        seq,
                        payload,
                    } => {
                        if !token.matches(&self.ids.token) {
                            return self.stats.foreign += 1;
                        }
                        *silence = 0;
                        if channel >= CHANNELS || self.channel_open & 1 << channel == 0 {
                            return self.stats.closed_channel += 1;
                        }
                        self.events.push_back(IotcEvent::Data {
                            channel,
                            datatype,
                            seq,
                            payload: payload.to_vec(),
                        });
                    }
                    Inbound::Alive(token) if token.matches(&self.ids.token) => *silence = 0,
                    Inbound::Close(token) if token.matches(&self.ids.token) => {
                        self.end(LinkError::Closed(CloseReason::RemoteClose));
                    }
                    Inbound::Alive(_) | Inbound::Close(_) => self.stats.foreign += 1,
                    Inbound::LanReply(_) | Inbound::Other(_) => self.stats.ignored += 1,
                }
            }
            State::Idle | State::Ended => self.stats.ignored += 1,
        }
    }

    fn on_long_reply(&mut self, now: Instant, peer: PeerInfo, result: u8) {
        if result != 1 && result != 7 {
            return self.end(LinkError::IotcRejected(IotcReject::from_result(result)));
        }
        if peer.proto_version < codec::KEY_AUTH_MIN_VERSION {
            return self.end(LinkError::IotcRejected(IotcReject::NoKeyAuth));
        }
        // Answer with the connect-path probe and then the ack, both unicast (`handshake.c:54`).
        self.push_sealed(peer.addr, codec::probe(&self.ids).to_vec());
        self.push_sealed(peer.addr, codec::connect_ack(&self.ids).to_vec());
        self.state = State::Established {
            peer,
            next_alive: now + self.config.alive_interval,
            silence: 0,
            alive_ticks: 0,
        };
        self.events.push_back(IotcEvent::Connected(peer));
    }

    /// Queue `payload` on `channel` with `datatype` (0 data, 1 DTLS record).
    pub fn send(&mut self, channel: u8, datatype: u8, payload: &[u8]) -> Result<(), LinkError> {
        let Some(peer) = self.peer() else {
            return Err(LinkError::Protocol(
                "send on an IOTC session that is not established",
            ));
        };
        if payload.len() > codec::MAX_PAYLOAD {
            return Err(LinkError::InvalidArgument("IOTC payload over 1388 bytes"));
        }
        let Some(seq) = self.send_seq.get_mut(usize::from(channel)) else {
            return Err(LinkError::InvalidArgument("channel over 31"));
        };
        if self.channel_open & 1 << channel == 0 {
            return Err(LinkError::InvalidArgument("channel not open"));
        }
        let pkt = codec::data(&self.ids.token, channel, datatype, *seq, self.pe_on, payload);
        *seq = seq.wrapping_add(1);
        self.push_sealed(peer.addr, pkt);
        Ok(())
    }

    /// Scramble only the first 64 bytes of later data datagrams, as `IOTC_sCHL_new` does once DTLS
    /// starts. Refused if the camera didn't offer it.
    pub fn set_partial_encryption(&mut self, on: bool) -> Result<(), LinkError> {
        match self.peer() {
            Some(peer) if peer.pe_capable || !on => {
                self.pe_on = on;
                Ok(())
            }
            Some(_) => Err(LinkError::Protocol("camera does not allow partial encryption")),
            None => Err(LinkError::Protocol("IOTC session not established")),
        }
    }

    pub fn open_channel(&mut self, channel: u8) -> Result<(), LinkError> {
        if channel >= CHANNELS {
            return Err(LinkError::InvalidArgument("channel over 31"));
        }
        self.channel_open |= 1 << channel;
        Ok(())
    }

    /// Open the lowest free channel from 1 to 31.
    pub fn free_channel(&mut self) -> Result<u8, LinkError> {
        let channel = (1..CHANNELS)
            .find(|&c| self.channel_open & 1 << c == 0)
            .ok_or(LinkError::Capacity)?;
        self.channel_open |= 1 << channel;
        Ok(channel)
    }

    pub fn close_channel(&mut self, channel: u8) {
        if channel == 0 || channel >= CHANNELS {
            return;
        }
        self.channel_open &= !(1 << channel);
        if let Some(seq) = self.send_seq.get_mut(usize::from(channel)) {
            *seq = 0;
        }
    }

    /// Close from our side: three close datagrams if established, then `Ended(Closed(Local))`.
    pub fn close(&mut self) {
        if let Some(peer) = self.peer() {
            let pkt = codec::token_datagram(&self.ids.token, codec::OP_CLOSE_OUT);
            for _ in 0..3 {
                self.push_sealed(peer.addr, pkt.to_vec());
            }
        }
        if !self.is_ended() {
            self.end(LinkError::Closed(CloseReason::Local));
        }
    }

    fn push_sealed(&mut self, to: SocketAddrV4, mut data: Vec<u8>) {
        codec::seal(&mut data);
        self.transmits.push_back(Transmit { to, data });
    }

    fn end(&mut self, error: LinkError) {
        self.state = State::Ended;
        self.events.push_back(IotcEvent::Ended(error));
    }
}
