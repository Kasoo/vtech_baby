//! The camera's side of talk-back: after `SPEAKERSTART` names a channel, the camera connects to the
//! client's AV server there. It first offers DTLS (a ClientHello as datatype 1), and on the server's
//! "recv dtls data!" logs in plainly with empty credentials. From then on it is an ordinary AV client,
//! so the core's `AvClient` plays it: it ACKs the server's control frames and receives the audio.

use std::time::{Duration, Instant};

use vtech_link_core::av::server::REFUSE_DTLS;
use vtech_link_core::av::{AvClient, AvConfig, AvEvent};
use vtech_link_core::rng::{OsRng, Rng};

/// IOTC datatypes on the channel.
const DATATYPE_PLAIN: u8 = 0;
const DATATYPE_DTLS: u8 = 1;
/// How often the DTLS offer repeats until the server answers.
const HELLO_EVERY: Duration = Duration::from_millis(100);
const LOGIN_TIMEOUT: Duration = Duration::from_secs(10);
/// Enough of a DTLS 1.2 ClientHello record for the server to see datatype 1 starting with 0x16.
const FAKE_HELLO: [u8; 16] = [0x16, 0xfe, 0xff, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3, 1, 0, 0];

enum State {
    Offering { next: Instant },
    Av(Box<AvClient>),
}

pub(crate) struct TalkClient {
    pub channel: u8,
    state: State,
}

/// What the talk-back client did on one call.
#[derive(Default)]
pub(crate) struct TalkOutput {
    /// `(datatype, payload)` for the channel.
    pub transmits: Vec<(u8, Vec<u8>)>,
    /// Complete audio frames, still encrypted.
    pub frames: Vec<Vec<u8>>,
    pub logged_in: bool,
}

impl TalkClient {
    pub fn new(channel: u8, now: Instant) -> Self {
        Self {
            channel,
            state: State::Offering { next: now },
        }
    }

    pub fn handle_datagram(&mut self, now: Instant, datatype: u8, payload: &[u8], out: &mut TalkOutput) {
        match &mut self.state {
            State::Offering { .. } if datatype == DATATYPE_PLAIN && payload == REFUSE_DTLS => {
                let config = AvConfig {
                    ctrl_window: 1,
                    ..AvConfig::new("", "")
                };
                let nonce = OsRng.u32();
                self.state = State::Av(Box::new(AvClient::new(config, nonce, now, now + LOGIN_TIMEOUT)));
            }
            State::Offering { .. } => {}
            State::Av(av) if datatype == DATATYPE_PLAIN => av.handle_packet(now, payload),
            State::Av(_) => {}
        }
        self.drain(out);
    }

    pub fn handle_timeout(&mut self, now: Instant, out: &mut TalkOutput) {
        match &mut self.state {
            State::Offering { next } => {
                if now >= *next {
                    *next = now + HELLO_EVERY;
                    out.transmits.push((DATATYPE_DTLS, FAKE_HELLO.to_vec()));
                }
            }
            State::Av(av) => {
                if av.poll_timeout().is_some_and(|t| now >= t) {
                    av.handle_timeout(now);
                }
            }
        }
        self.drain(out);
    }

    fn drain(&mut self, out: &mut TalkOutput) {
        let State::Av(av) = &mut self.state else { return };
        while let Some(ev) = av.poll_event() {
            match ev {
                AvEvent::Ready => out.logged_in = true,
                AvEvent::Frame(f) => out.frames.push(f.data),
                _ => {}
            }
        }
        while let Some(pkt) = av.poll_transmit() {
            out.transmits.push((DATATYPE_PLAIN, pkt));
        }
    }
}
