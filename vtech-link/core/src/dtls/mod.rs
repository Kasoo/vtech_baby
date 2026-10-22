//! `DtlsClient`: the DTLS 1.2 PSK client on the AV channel, a sans-IO wrapper around `dimpl`.
//! Design: `docs/vtech-link-design/30-dtls.md`; reference `tutk-host/iotc/dtls.c`, `av/init.c:112–150`.
//!
//! Records travel as IOTC data with datatype 1. The PSK identity is `"AUTHPWD_" + account` and the key
//! `SHA-256(password)`.

use std::collections::VecDeque;
use std::sync::Arc;
use std::time::{Duration, Instant};

use dimpl::crypto::Dtls12CipherSuite;
use dimpl::{Config, Dtls, Output, PskResolver};
use sha2::{Digest, Sha256};
use zeroize::Zeroizing;

use crate::error::{AvAuthStage, CloseReason, LinkError};

/// The IOTC datatype that carries DTLS records.
pub const DATATYPE_DTLS: u8 = 1;
/// The largest application plaintext one record carries: an IOTC payload (1388 bytes) less the
/// record header (13) and the AEAD tag (16).
pub const MAX_PLAINTEXT: usize = 1359;

const RECORD_HEADER_LEN: usize = 13;
const CONTENT_CHANGE_CIPHER_SPEC: u8 = 20;
const CONTENT_HANDSHAKE: u8 = 22;
const HANDSHAKE_SERVER_HELLO: u8 = 2;
const IDENTITY_PREFIX: &str = "AUTHPWD_";

/// The suite offered (the only one).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Suite {
    /// `TLS_ECDHE_PSK_WITH_CHACHA20_POLY1305_SHA256` (`0xCCAC`): what the camera picks today, with
    /// forward secrecy.
    EcdhePskChaCha20,
    /// `TLS_PSK_WITH_AES_128_GCM_SHA256` (`0x00A8`): accepted too, without forward secrecy.
    PskAes128Gcm,
}

impl Suite {
    fn dimpl(self) -> Dtls12CipherSuite {
        match self {
            Self::EcdhePskChaCha20 => Dtls12CipherSuite::ECDHE_PSK_CHACHA20_POLY1305_SHA256,
            Self::PskAes128Gcm => Dtls12CipherSuite::PSK_AES128_GCM_SHA256,
        }
    }
}

#[derive(Debug, Clone)]
pub struct DtlsConfig {
    pub suite: Suite,
    /// The first handshake retransmission; it doubles each time (`iotc/dtls.c:97`).
    pub flight_start_rto: Duration,
    /// The whole handshake, the link's `av_timeout`.
    pub handshake_timeout: Duration,
    /// How long to wait for the server's Finished after ours. The camera answers within 40 ms, and
    /// never answers at all when the AV password is wrong.
    pub finished_wait: Duration,
    /// A fixed seed for dimpl's randomness. Tests only: 64 bits is too little entropy for keys.
    pub test_rng_seed: Option<u64>,
}

impl Default for DtlsConfig {
    fn default() -> Self {
        Self {
            suite: Suite::EcdhePskChaCha20,
            flight_start_rto: Duration::from_millis(500),
            handshake_timeout: Duration::from_secs(30),
            finished_wait: Duration::from_secs(3),
            test_rng_seed: None,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum DtlsEvent {
    Connected,
    /// One application-data record's plaintext.
    Data(Vec<u8>),
    /// The DTLS session is over.
    Ended(LinkError),
}

/// Dropped or rejected inputs, by reason.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct DtlsStats {
    /// Records dimpl refused once connected (bad MAC, replay, unexpected). Not fatal.
    pub rejected: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Phase {
    /// Our ClientHello is out (and maybe retransmitted).
    Hello,
    /// Our Finished is out; the server's must arrive by the deadline.
    Finished(Instant),
    Connected,
    Ended,
}

struct Psk(Zeroizing<Vec<u8>>);

impl PskResolver for Psk {
    fn resolve(&self, _identity: &[u8]) -> Option<Vec<u8>> {
        Some(self.0.to_vec())
    }
}

pub struct DtlsClient {
    dtls: Dtls,
    config: DtlsConfig,
    phase: Phase,
    /// When dimpl next needs `handle_timeout`.
    dimpl_timeout: Option<Instant>,
    negotiated: Option<u16>,
    buf: Vec<u8>,
    transmits: VecDeque<Vec<u8>>,
    events: VecDeque<DtlsEvent>,
    stats: DtlsStats,
}

impl DtlsClient {
    /// A client for `account` and `password`; its ClientHello is ready to poll at once.
    pub fn new(config: DtlsConfig, account: &str, password: &str, now: Instant) -> Result<Self, LinkError> {
        let identity = format!("{IDENTITY_PREFIX}{account}").into_bytes();
        // The identity goes in a u16-length field; the SDK caps it at 0x77 bytes (`av/init.c:124`).
        if identity.len() > 0x77 {
            return Err(LinkError::InvalidArgument("account name too long"));
        }
        let psk = Psk(Zeroizing::new(Sha256::digest(password.as_bytes()).to_vec()));
        let mut builder = Config::builder()
            .with_psk_client(identity, Arc::new(psk))
            .dtls12_cipher_suites(&[config.suite.dimpl()])
            .dtls13_cipher_suites(&[])
            .mtu(crate::iotc::codec::MAX_PAYLOAD)
            .flight_start_rto(config.flight_start_rto)
            .flight_retries(16)
            .handshake_timeout(config.handshake_timeout);
        if let Some(seed) = config.test_rng_seed {
            builder = builder.dangerously_set_rng_seed(seed);
        }
        let dimpl_config = builder
            .build()
            .map_err(|_| LinkError::Protocol("dtls configuration refused"))?;
        let mut dtls = Dtls::new_12_psk(Arc::new(dimpl_config), now);
        dtls.set_active(true);
        let mut client = Self {
            dtls,
            config,
            phase: Phase::Hello,
            dimpl_timeout: None,
            negotiated: None,
            buf: vec![0u8; 2048],
            transmits: VecDeque::new(),
            events: VecDeque::new(),
            stats: DtlsStats::default(),
        };
        client.handle_timeout(now);
        Ok(client)
    }

    pub fn is_connected(&self) -> bool {
        self.phase == Phase::Connected
    }

    pub fn is_ended(&self) -> bool {
        self.phase == Phase::Ended
    }

    /// The suite id in the server's ServerHello, once seen.
    pub fn negotiated_suite(&self) -> Option<u16> {
        self.negotiated
    }

    pub fn stats(&self) -> DtlsStats {
        self.stats
    }

    /// The next DTLS datagram to send as one IOTC payload with datatype 1.
    pub fn poll_transmit(&mut self) -> Option<Vec<u8>> {
        self.transmits.pop_front()
    }

    pub fn poll_event(&mut self) -> Option<DtlsEvent> {
        self.events.pop_front()
    }

    pub fn poll_timeout(&self) -> Option<Instant> {
        let finished = match self.phase {
            Phase::Finished(deadline) => Some(deadline),
            Phase::Ended => return None,
            _ => None,
        };
        match (self.dimpl_timeout, finished) {
            (Some(a), Some(b)) => Some(a.min(b)),
            (a, b) => a.or(b),
        }
    }

    pub fn handle_timeout(&mut self, now: Instant) {
        if let Phase::Finished(deadline) = self.phase {
            if now >= deadline {
                return self.end(LinkError::AvAuthFailed(AvAuthStage::Dtls(None)));
            }
        }
        if self.is_ended() {
            return;
        }
        if self.dimpl_timeout.is_none_or(|t| now >= t) {
            if let Err(e) = self.dtls.handle_timeout(now) {
                return self.on_error(e);
            }
        }
        self.pump(now);
    }

    /// Handle one inbound IOTC payload of datatype 1.
    pub fn handle_datagram(&mut self, now: Instant, datagram: &[u8]) {
        if self.is_ended() {
            return;
        }
        if self.negotiated.is_none() {
            self.negotiated = server_hello_suite(datagram);
        }
        if let Err(e) = self.dtls.handle_packet(datagram) {
            if self.phase == Phase::Connected && !is_fatal(&e) {
                self.stats.rejected += 1;
            } else {
                return self.on_error(e);
            }
        }
        self.pump(now);
    }

    /// Queue one application-data record.
    pub fn send(&mut self, now: Instant, plaintext: &[u8]) -> Result<(), LinkError> {
        match self.phase {
            Phase::Connected => {}
            Phase::Ended => return Err(LinkError::Protocol("dtls session ended")),
            _ => return Err(LinkError::Protocol("dtls handshake not finished")),
        }
        if plaintext.len() > MAX_PLAINTEXT {
            return Err(LinkError::InvalidArgument("dtls plaintext over 1359 bytes"));
        }
        self.dtls
            .send_application_data(plaintext)
            .map_err(|_| LinkError::Protocol("dtls send refused"))?;
        self.pump(now);
        Ok(())
    }

    /// Stop without sending an alert, as today's client does (quiet shutdown, `iotc/dtls.c`).
    pub fn close(&mut self) {
        if !self.is_ended() {
            self.end(LinkError::Closed(CloseReason::Local));
        }
    }

    /// Collect everything dimpl has to say, up to its next timeout.
    fn pump(&mut self, now: Instant) {
        loop {
            match self.dtls.poll_output(&mut self.buf) {
                Output::Packet(p) => {
                    if self.phase == Phase::Hello && has_record(p, CONTENT_CHANGE_CIPHER_SPEC) {
                        self.phase = Phase::Finished(now + self.config.finished_wait);
                    }
                    self.transmits.push_back(p.to_vec());
                }
                Output::BufferTooSmall { needed } => self.buf.resize(needed, 0),
                Output::Timeout(t) => {
                    self.dimpl_timeout = Some(t);
                    return;
                }
                Output::Connected => {
                    self.phase = Phase::Connected;
                    self.events.push_back(DtlsEvent::Connected);
                }
                Output::ApplicationData(d) => self.events.push_back(DtlsEvent::Data(d.to_vec())),
                Output::CloseNotify => return self.end(LinkError::Closed(CloseReason::DtlsAlert(0))),
                _ => {}
            }
        }
    }

    fn on_error(&mut self, e: dimpl::Error) {
        log::debug!("dtls: {e}");
        let alert = match &e {
            dimpl::Error::SecurityError(dimpl::SecurityError::FatalAlert { description }) => {
                Some(*description)
            }
            _ => None,
        };
        let error = match self.phase {
            Phase::Connected => LinkError::Closed(CloseReason::DtlsAlert(alert.unwrap_or(0))),
            // Anything going wrong after our Finished means the server didn't accept our key.
            Phase::Finished(_) => LinkError::AvAuthFailed(AvAuthStage::Dtls(alert)),
            Phase::Hello if matches!(e, dimpl::Error::Timeout(_)) => LinkError::Unreachable,
            Phase::Hello if alert.is_some() => LinkError::Protocol("dtls handshake refused"),
            Phase::Hello => LinkError::Protocol("dtls handshake failed"),
            Phase::Ended => return,
        };
        self.end(error);
    }

    fn end(&mut self, error: LinkError) {
        self.phase = Phase::Ended;
        self.dimpl_timeout = None;
        self.events.push_back(DtlsEvent::Ended(error));
    }
}

fn is_fatal(e: &dimpl::Error) -> bool {
    matches!(
        e,
        dimpl::Error::SecurityError(dimpl::SecurityError::FatalAlert { .. }) | dimpl::Error::ConnectionClosed
    )
}

/// The records in a DTLS datagram: `(content type, fragment)`.
fn records(datagram: &[u8]) -> impl Iterator<Item = (u8, &[u8])> {
    let mut rest = datagram;
    std::iter::from_fn(move || {
        let header = rest.get(..RECORD_HEADER_LEN)?;
        let len = usize::from(u16::from_be_bytes([*header.get(11)?, *header.get(12)?]));
        let fragment = rest.get(RECORD_HEADER_LEN..RECORD_HEADER_LEN + len)?;
        let content = *header.first()?;
        rest = rest.get(RECORD_HEADER_LEN + len..)?;
        Some((content, fragment))
    })
}

fn has_record(datagram: &[u8], content: u8) -> bool {
    records(datagram).any(|(c, _)| c == content)
}

/// The cipher suite in a plaintext ServerHello, if the datagram carries one.
fn server_hello_suite(datagram: &[u8]) -> Option<u16> {
    records(datagram).find_map(|(content, fragment)| {
        if content != CONTENT_HANDSHAKE || fragment.first() != Some(&HANDSHAKE_SERVER_HELLO) {
            return None;
        }
        // Handshake header 12, then version 2, random 32, session id (u8 length).
        let body = fragment.get(12..)?;
        let at = 35 + usize::from(*body.get(34)?);
        Some(u16::from_be_bytes([*body.get(at)?, *body.get(at + 1)?]))
    })
}
