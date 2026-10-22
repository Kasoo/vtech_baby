//! Talk-back server tests (`docs/vtech-link-design/50-talkback.md`), on a virtual clock. What it sends
//! is received by our own `MediaRx` playing the camera's side, and decrypted.

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing,
    clippy::cast_possible_truncation
)]

mod common;

use std::time::{Duration, Instant};

use common::unhex;
use vtech_link_core::LinkError;
use vtech_link_core::av::media_rx::{MediaConfig, MediaRx, StreamKind};
use vtech_link_core::av::packet;
use vtech_link_core::av::server::{AvServer, REFUSE_DTLS, Sealer, ServerConfig, ServerEvent, frame_info};
use vtech_link_core::error::CloseReason;
use vtech_link_core::service::frame_crypto;
use vtech_link_core::service::media_key::{KeyExchange, MasterKey};

const MS: Duration = Duration::from_millis(1);
const REPLY: &str = "0000000000000000000000005714769d116bf76436ae74bc793d2c30ad1903c59ac5273805c7e2698b410c36c8c9cacbcccdcecfd0d1d2d3d4d5d6d7d8d9dadbdcdddedfe0e1e2e3e4e5e6e7";

fn master() -> MasterKey {
    KeyExchange::new([1; 32]).master(&unhex(REPLY)).unwrap()
}

struct TestSealer {
    master: MasterKey,
    n: u8,
}

impl Sealer for TestSealer {
    fn seal(&mut self, plain: &[u8]) -> Vec<u8> {
        self.n += 1;
        frame_crypto::encrypt(&self.master, [self.n; 32], [self.n; 12], plain)
    }
    fn wall_ms(&mut self) -> u32 {
        0x1234_5678
    }
}

fn sealer() -> TestSealer {
    TestSealer {
        master: master(),
        n: 0,
    }
}

fn drain(s: &mut AvServer) -> Vec<(u8, Vec<u8>)> {
    std::iter::from_fn(|| s.poll_transmit()).collect()
}

/// The camera's login, as a client sends it: sub-type 0 with `+0x12` zero, or sub-type 0x20.
fn camera_login(n: u8) -> Vec<u8> {
    packet::login(n, 0xabcd_0000, "", "", u16::from(n))
}

fn accepted() -> (AvServer, Instant) {
    let t0 = Instant::now();
    let mut s = AvServer::new(ServerConfig::default(), t0, t0 + Duration::from_secs(10));
    s.handle_datagram(t0, 0, &camera_login(0));
    assert_eq!(s.poll_event(), Some(ServerEvent::Accepted));
    drain(&mut s);
    (s, t0)
}

#[test]
fn dtls_refused_then_login_accepted() {
    let t0 = Instant::now();
    let mut s = AvServer::new(ServerConfig::default(), t0, t0 + Duration::from_secs(10));
    s.handle_datagram(t0, 1, &[22, 0xfe, 0xff, 0, 0]);
    assert_eq!(drain(&mut s), [(0, REFUSE_DTLS.to_vec())]);
    // Leftovers before a login are dropped; so is a sub-type 0 login with +0x12 set.
    s.handle_datagram(t0, 0, &packet::report(1, 2));
    s.handle_datagram(t0, 0, &packet::login(0, 1, "", "", 1));
    assert!(drain(&mut s).is_empty());
    assert_eq!(s.stats().packets_ignored, 2);

    s.handle_datagram(t0, 0, &camera_login(0));
    s.handle_datagram(t0, 0, &camera_login(1));
    assert_eq!(s.poll_event(), Some(ServerEvent::Accepted));
    assert_eq!(s.poll_event(), None);
    let out = drain(&mut s);
    // A reply to each login, and the internal HELLO after the first.
    assert_eq!(out.iter().map(|(_, p)| p[0]).collect::<Vec<_>>(), [0, 0x0c, 0]);
    let reply = &out[0].1;
    assert_eq!(reply.len(), 0x3c);
    assert_eq!(
        (reply[1], packet::get16(reply, 2), packet::get16(reply, 0x10)),
        (0x21, Some(0x0c), Some(0x24))
    );
    assert_eq!(packet::get32(reply, 0x14), Some(0xabcd_0000), "nonce echoed");
    assert_eq!(
        (packet::get32(reply, 0x18), reply[0x1d], reply[0x1f], reply[0x20]),
        (Some(0), 1, 1, 1)
    );
    assert_eq!(reply[0x24..0x38], packet::OPCODE_SUPPORT);
    let hello = &out[1].1;
    assert_eq!(
        (hello[0x11], packet::get32(hello, 0x24), &hello[0x28..]),
        (0x10, Some(0x40), &[0, 1, 0, 0][..])
    );
    assert_eq!(packet::get32(&out[2].1, 0x14), Some(0xabcd_0001));
}

#[test]
fn accept_deadline() {
    let t0 = Instant::now();
    let mut s = AvServer::new(ServerConfig::default(), t0, t0 + Duration::from_secs(10));
    assert_eq!(s.poll_timeout(), Some(t0 + Duration::from_secs(10)));
    s.handle_timeout(t0 + Duration::from_secs(10), &mut sealer());
    assert_eq!(s.poll_event(), Some(ServerEvent::Ended(LinkError::Timeout)));
    assert!(s.push(t0, vec![0; 160]).is_err());
}

/// Run the server until `until`, returning (ms since t0, packet) for each media packet sent.
fn run(s: &mut AvServer, t0: Instant, until: Instant, sl: &mut TestSealer) -> Vec<(u128, Vec<u8>)> {
    let mut sent = Vec::new();
    while let Some(t) = s.poll_timeout() {
        if t > until {
            break;
        }
        s.handle_timeout(t, sl);
        for (_, p) in drain(s) {
            if p[0] == 0x0c && p[1] == 0x01 {
                sent.push(((t - t0).as_millis(), p));
            }
        }
    }
    sent
}

#[test]
fn frames_are_paced_and_decrypt_on_the_camera_side() {
    let (mut s, t0) = accepted();
    let mut sl = sealer();
    let frames: Vec<Vec<u8>> = (0..3u8).map(|i| vec![i; 160]).collect();
    for f in &frames {
        s.push(t0, f.clone()).unwrap();
    }
    let sent = run(&mut s, t0, t0 + 100 * MS, &mut sl);
    assert_eq!(sent.iter().map(|(t, _)| *t).collect::<Vec<_>>(), [0, 20, 40]);

    // The camera's side: a MediaRx receives the packets; each frame decrypts to what was pushed.
    let mut rx = MediaRx::new(MediaConfig::default());
    for (_, p) in &sent {
        let env = packet::envelope(p).unwrap();
        assert_eq!(env.group[3], 20, "k in the group header");
        rx.on_envelope(t0, env.group, env.frame);
    }
    let m = master();
    for (i, want) in frames.iter().enumerate() {
        let f = rx.poll_frame().unwrap();
        assert_eq!((f.stream, f.kind, f.number), (StreamKind::Audio, 3, i as u32));
        assert_eq!(f.info, frame_info(0x1234_5678));
        assert_eq!(frame_crypto::decrypt(&m, &f.data).as_ref(), Some(want));
    }
    // Sub-header details: pn, count 1, info length on the last, plen 220 + 16, checksum.
    let p = &sent[1].1;
    let sub = &p[0x10..0x24];
    assert_eq!(packet::get16(sub, 2), Some(1));
    assert_eq!(
        (
            packet::get16(sub, 4),
            packet::get16(sub, 6),
            packet::get16(sub, 8)
        ),
        (Some(1), Some(16), Some(236))
    );
    let mut zeroed = sub.to_vec();
    zeroed[0x0a..0x0c].fill(0);
    let sum = zeroed
        .iter()
        .chain(&p[0x24..])
        .fold(0u16, |a, &b| a.wrapping_add(u16::from(b)));
    assert_eq!(packet::get16(sub, 0x0a), Some(sum));
    assert_eq!(packet::get32(sub, 0x10), Some(2), "audio frame count from 1");

    // After a pause, the clock restarts from the next frame instead of bursting.
    s.push(t0 + 300 * MS, vec![9; 160]).unwrap();
    s.push(t0 + 300 * MS, vec![9; 160]).unwrap();
    let sent = run(&mut s, t0, t0 + 400 * MS, &mut sl);
    assert_eq!(sent.iter().map(|(t, _)| *t).collect::<Vec<_>>(), [300, 320]);
    assert_eq!(s.stats().frames_sent, 5);
}

#[test]
fn queue_limit_and_close() {
    let (mut s, t0) = accepted();
    let mut sl = sealer();
    for _ in 0..10 {
        s.push(t0, vec![0; 160]).unwrap();
    }
    assert_eq!(s.space(), 0);
    assert!(s.push(t0, vec![0; 160]).is_err(), "full: the caller waits");
    // Draining sends the rest at pace, then ends.
    s.close(true);
    let sent = run(&mut s, t0, t0 + Duration::from_secs(1), &mut sl);
    assert_eq!(sent.len(), 10);
    assert_eq!(sent.last().unwrap().0, 180);
    assert_eq!(
        s.poll_event(),
        Some(ServerEvent::Ended(LinkError::Closed(CloseReason::Local)))
    );

    let (mut s, t0) = accepted();
    for _ in 0..4 {
        s.push(t0, vec![0; 160]).unwrap();
    }
    s.close(false);
    assert_eq!(
        s.poll_event(),
        Some(ServerEvent::Ended(LinkError::Closed(CloseReason::Local)))
    );
    assert_eq!(s.stats().frames_dropped_queue, 4);
    assert_eq!(s.poll_timeout(), None);
}

#[test]
fn large_frames_fragment() {
    let (mut s, t0) = accepted();
    let mut sl = sealer();
    s.push(t0, vec![5; 2000]).unwrap();
    let sent = run(&mut s, t0, t0 + 10 * MS, &mut sl);
    assert_eq!(sent.len(), 3, "2060 sealed bytes: 1024 + 1024 + 12");
    let mut rx = MediaRx::new(MediaConfig::default());
    for (_, p) in &sent {
        let env = packet::envelope(p).unwrap();
        rx.on_envelope(t0, env.group, env.frame);
    }
    let f = rx.poll_frame().unwrap();
    assert_eq!(frame_crypto::decrypt(&master(), &f.data), Some(vec![5; 2000]));
}

#[test]
fn schedule_and_camera_acks() {
    let (mut s, t0) = accepted();
    let mut sl = sealer();
    // The HELLO is resent until the camera ACKs it.
    s.handle_timeout(t0 + 60 * MS, &mut sl);
    assert!(drain(&mut s).iter().any(|(_, p)| p[0] == 0x0c && p[0x11] == 0x10));
    let ack = packet::ack(&packet::Ack {
        pn_cum: 0xffff,
        pn_high: 0xffff,
        ctrl_in: 0,
        urgent: false,
        seq: 0,
        clock: 0,
        naks: &[],
    });
    s.handle_datagram(t0 + 70 * MS, 0, &ack);
    s.handle_timeout(t0 + 130 * MS, &mut sl);
    assert!(
        !drain(&mut s).iter().any(|(_, p)| p[0] == 0x0c),
        "no more HELLO resends"
    );
    // Reports are answered.
    s.handle_datagram(t0 + 140 * MS, 0, &packet::report(0x777, 0));
    let out = drain(&mut s);
    assert_eq!(
        (out[0].1[0], packet::get16(&out[0].1, 8)),
        (packet::CLASS_RTT, Some(0x777))
    );
}
