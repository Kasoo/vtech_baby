//! AV tests: the codecs against tutk-host (T-AV-1, T-AV-2), the control stream, media receive and the
//! client's login and schedule (`docs/vtech-link-design/85-testing.md`), on a virtual clock.

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing,
    clippy::cast_possible_truncation
)]

mod common;

use std::time::{Duration, Instant};

use common::Fixtures;
use vtech_link_core::LinkError;
use vtech_link_core::av::control::{ControlEvent, ControlStream};
use vtech_link_core::av::media_rx::{MediaConfig, MediaRx, StreamKind};
use vtech_link_core::av::packet::{self, Ack};
use vtech_link_core::av::{AvClient, AvConfig, AvEvent};
use vtech_link_core::error::AvAuthStage;

const MS: Duration = Duration::from_millis(1);

fn stamped(mut pkt: Vec<u8>, counter: u16) -> Vec<u8> {
    packet::stamp(&mut pkt, counter);
    pkt
}

// ---- codecs against tutk-host ----

#[test]
fn codecs_match_tutk_host() {
    let f = Fixtures::load();
    let nonce = 0x1122_3344;
    assert_eq!(
        packet::login(0, nonce, "admin", "testpass", 1),
        f.get("av.login1")
    );
    assert_eq!(
        packet::login(1, nonce, "admin", "testpass", 1),
        f.get("av.login2")
    );
    let ack = Ack {
        pn_cum: 0x10,
        pn_high: 0x14,
        ctrl_in: 3,
        urgent: false,
        seq: 7,
        clock: 0x5678,
        naks: &[],
    };
    assert_eq!(stamped(packet::ack(&ack), 0x0200), f.get("av.ack"));
    let ack = Ack {
        seq: 8,
        urgent: true,
        naks: &[3, 5],
        ..ack
    };
    assert_eq!(stamped(packet::ack(&ack), 0x0201), f.get("av.ack.nak"));
    assert_eq!(
        stamped(packet::report(0x5678, 0x2a).to_vec(), 0x0300),
        f.get("av.report")
    );
    // RTT packets are not counter-stamped.
    assert_eq!(
        stamped(packet::rtt(0xbeef, 0x2a).to_vec(), 0x9999),
        f.get("av.rtt")
    );
    // tutk-host writes the frame number's low 16 bits as the packet number; we pass the same.
    let user = packet::ctrl_frame(
        packet::CTRL_USER,
        5,
        5,
        0x330,
        &[0xde, 0xad, 0xbe, 0xef, 0, 1, 2, 3],
    );
    assert_eq!(stamped(user, 0x0500), f.get("av.ctrl_frame"));
    let internal = packet::ctrl_frame(packet::CTRL_INTERNAL, 6, 6, 0x24, &[9, 0]);
    assert_eq!(stamped(internal, 0x0501), f.get("av.ctrl_frame.internal"));
}

#[test]
fn login_reply_and_fragment_parsing() {
    let mut reply = vec![0u8; 0x1c];
    reply[1] = packet::LOGIN_REPLY;
    reply[0x14..0x18].copy_from_slice(&0x1122_3345u32.to_le_bytes());
    assert_eq!(packet::login_reply(&reply, 0x1122_3344), Some(0));
    reply[0x18] = 3;
    assert_eq!(packet::login_reply(&reply, 0x1122_3344), Some(3));
    assert_eq!(packet::login_reply(&reply, 0x5555_5555), None);
    assert_eq!(packet::login_reply(&reply[..0x1b], 0x1122_3344), None);

    let frame = packet::ctrl_frame(packet::CTRL_USER, 9, 7, 0x331, b"hello");
    let env = packet::envelope(&frame).unwrap();
    let frag = packet::ctrl_fragment(env.frame).unwrap();
    assert_eq!(
        (
            frag.class,
            frag.packet_number,
            frag.count,
            frag.index,
            frag.frame_number
        ),
        (0x70, 9, 1, 0, 7)
    );
    assert_eq!(&frag.body[4..], b"hello");
    for n in 0..frame.len() {
        if let Some(env) = packet::envelope(&frame[..n]) {
            let _ = packet::ctrl_fragment(env.frame);
        }
    }
}

// ---- the control stream ----

/// An inbound control fragment as the camera sends it: `count` fragments of message `frame_number`.
fn ctrl_in(packet_number: u16, frame_number: u32, count: u16, index: u16, body: &[u8]) -> Vec<u8> {
    let mut f = vec![0u8; packet::SUBHEADER];
    f[1] = packet::CTRL_USER;
    f[2..4].copy_from_slice(&packet_number.to_le_bytes());
    f[4..6].copy_from_slice(&count.to_le_bytes());
    f[6..8].copy_from_slice(&index.to_le_bytes());
    f[8..10].copy_from_slice(&(body.len() as u16).to_le_bytes());
    f[0x0c..0x10].copy_from_slice(&frame_number.to_le_bytes());
    f.extend_from_slice(body);
    f
}

fn message(typ: u32, payload: &[u8]) -> Vec<u8> {
    let mut b = typ.to_le_bytes().to_vec();
    b.extend_from_slice(payload);
    b
}

fn feed(cs: &mut ControlStream, f: &[u8]) {
    cs.on_fragment(packet::ctrl_fragment(f).unwrap());
}

fn messages(cs: &mut ControlStream) -> Vec<(u32, Vec<u8>)> {
    std::iter::from_fn(|| cs.poll_event())
        .filter_map(|e| match e {
            ControlEvent::Message { typ, payload, .. } => Some((typ, payload)),
            ControlEvent::Acked(_) => None,
        })
        .collect()
}

#[test]
fn window_queue_ack_and_retransmit() {
    let t0 = Instant::now();
    let mut cs = ControlStream::new(8, 60 * MS);
    let ids: Vec<_> = (0..10)
        .map(|i| cs.send(t0, 0x300 + i, &[i as u8]).unwrap())
        .collect();
    let sent: Vec<_> = std::iter::from_fn(|| cs.poll_transmit()).collect();
    assert_eq!(sent.len(), 8, "the window holds 8");
    // Frame numbers 0..7, in order.
    for (i, p) in sent.iter().enumerate() {
        assert_eq!(packet::get32(p, 0x1c), Some(i as u32));
    }
    assert_eq!(cs.poll_timeout(), Some(t0 + 60 * MS));
    cs.handle_timeout(t0 + 60 * MS);
    assert_eq!(std::iter::from_fn(|| cs.poll_transmit()).count(), 8, "all resent");
    // The camera ACKs frames 0..2: three complete, two more go out.
    cs.on_ack(t0 + 70 * MS, 2);
    let acked: Vec<_> = std::iter::from_fn(|| cs.poll_event()).collect();
    assert_eq!(
        acked,
        ids[..3]
            .iter()
            .map(|&id| ControlEvent::Acked(id))
            .collect::<Vec<_>>()
    );
    assert_eq!(std::iter::from_fn(|| cs.poll_transmit()).count(), 2);
    // 0xffff is "nothing yet"; an old ACK doesn't move backwards.
    cs.on_ack(t0 + 80 * MS, 0xffff);
    cs.on_ack(t0 + 80 * MS, 1);
    assert!(cs.poll_event().is_none());
    cs.on_ack(t0 + 90 * MS, 9);
    assert_eq!(std::iter::from_fn(|| cs.poll_event()).count(), 7);
    assert_eq!(cs.poll_timeout(), None);
    let stats = cs.stats();
    assert_eq!((stats.frames_sent, stats.retransmits), (10, 8));
    assert_eq!(stats.ack_latency.iter().sum::<u64>(), 10);
    assert!(cs.send(t0, 1, &[0; 1021]).is_err());
}

#[test]
fn inbound_in_order_with_duplicates_and_reordering() {
    let mut cs = ControlStream::new(8, 60 * MS);
    assert_eq!(cs.ctrl_in16(), 0xffff);
    feed(&mut cs, &ctrl_in(0, 1, 1, 0, &message(0x331, b"b")));
    assert!(messages(&mut cs).is_empty(), "frame 1 waits for frame 0");
    assert!(cs.take_ack_due());
    feed(&mut cs, &ctrl_in(1, 0, 1, 0, &message(0x330, b"a")));
    assert_eq!(
        messages(&mut cs),
        [(0x330, b"a".to_vec()), (0x331, b"b".to_vec())]
    );
    assert_eq!(cs.ctrl_in16(), 1);
    // A resend of something delivered is stale, and still makes an ACK due.
    cs.take_ack_due();
    feed(&mut cs, &ctrl_in(0, 0, 1, 0, &message(0x330, b"a")));
    assert!(messages(&mut cs).is_empty() && cs.take_ack_due());
    assert_eq!(cs.stats().fragments_stale, 1);
}

#[test]
fn multi_fragment_then_singles() {
    // The case tutk-host gets wrong: after a multi-fragment message, packet numbers run ahead of
    // frame numbers, and the stream must keep going.
    let mut cs = ControlStream::new(8, 60 * MS);
    let big: Vec<u8> = (0..2500u32).map(|i| i as u8).collect();
    let body = message(0x501, &big);
    let chunks: Vec<&[u8]> = body.chunks(1024).collect();
    assert_eq!(chunks.len(), 3);
    // Fragments out of order, one duplicated.
    feed(&mut cs, &ctrl_in(2, 0, 3, 2, chunks[2]));
    feed(&mut cs, &ctrl_in(0, 0, 3, 0, chunks[0]));
    feed(&mut cs, &ctrl_in(0, 0, 3, 0, chunks[0]));
    assert!(messages(&mut cs).is_empty());
    feed(&mut cs, &ctrl_in(1, 0, 3, 1, chunks[1]));
    assert_eq!(messages(&mut cs), [(0x501, big)]);
    for frame in 1..5u32 {
        feed(
            &mut cs,
            &ctrl_in(frame as u16 + 2, frame, 1, 0, &message(0x331, &[frame as u8])),
        );
    }
    assert_eq!(messages(&mut cs).len(), 4);
    assert_eq!(cs.ctrl_in16(), 4);
    let s = cs.stats();
    assert_eq!(
        (s.messages_in, s.multi_fragment_in, s.fragments_duplicate),
        (5, 1, 1)
    );
}

#[test]
fn inbound_limits() {
    let mut cs = ControlStream::new(8, 60 * MS);
    // Too far ahead, or claiming too many fragments: dropped.
    feed(&mut cs, &ctrl_in(0, 64, 1, 0, &message(1, &[])));
    feed(&mut cs, &ctrl_in(0, 0, 65, 0, &message(1, &[])));
    // Fragment count changing mid-message: dropped.
    feed(&mut cs, &ctrl_in(0, 1, 2, 0, &message(1, &[])));
    feed(&mut cs, &ctrl_in(0, 1, 3, 1, &[0]));
    assert_eq!(cs.stats().fragments_dropped, 3);
    // Holding is capped, but the next expected message still gets in.
    for frame in 2..40u32 {
        feed(&mut cs, &ctrl_in(0, frame, 1, 0, &message(1, &[])));
    }
    feed(&mut cs, &ctrl_in(0, 0, 1, 0, &message(1, &[])));
    assert_eq!(
        messages(&mut cs).len(),
        1,
        "frame 0 delivered; frame 1 is incomplete"
    );
}

// ---- media receive ----

/// A media fragment (the bytes at f): `index` is the fragment index, or on the last fragment ignored
/// in favour of the info length.
fn media(
    kind: u8,
    pn: u16,
    count: u16,
    index: u16,
    number: u32,
    payload: &[u8],
    info: Option<&[u8]>,
) -> Vec<u8> {
    let mut f = vec![0u8; packet::SUBHEADER];
    f[0] = kind;
    f[1] = u8::from(info.is_some());
    f[2..4].copy_from_slice(&pn.to_le_bytes());
    f[4..6].copy_from_slice(&count.to_le_bytes());
    let w6 = info.map_or(index, |i| i.len() as u16);
    f[6..8].copy_from_slice(&w6.to_le_bytes());
    let plen = payload.len() + info.map_or(0, <[u8]>::len);
    f[8..10].copy_from_slice(&(plen as u16).to_le_bytes());
    f[0x0c..0x10].copy_from_slice(&number.to_le_bytes());
    f.extend_from_slice(payload);
    f.extend_from_slice(info.unwrap_or_default());
    f
}

const INFO: &[u8] = &[0xaa; 16];

#[test]
fn frames_assemble_in_order() {
    let t0 = Instant::now();
    let mut rx = MediaRx::new(MediaConfig::default());
    // Video frame 10 in three fragments, out of order, with audio frame 11 between.
    rx.on_fragment(t0, &media(5, 0, 3, 1, 10, &[2; 1024], None));
    rx.on_fragment(t0, &media(3, 1, 1, 0, 11, &[9; 160], Some(INFO)));
    rx.on_fragment(t0, &media(5, 2, 3, 2, 10, &[3; 100], Some(INFO)));
    assert_eq!(
        rx.poll_frame().unwrap().stream,
        StreamKind::Audio,
        "audio isn't held by video"
    );
    assert!(rx.poll_frame().is_none());
    rx.on_fragment(t0, &media(5, 3, 3, 0, 10, &[1; 1024], None));
    let f = rx.poll_frame().unwrap();
    assert_eq!(
        (f.stream, f.kind, f.number, f.data.len()),
        (StreamKind::Video, 5, 10, 2148)
    );
    assert_eq!((f.data[0], f.data[1024], f.data[2048]), (1, 2, 3));
    assert_eq!(f.info, INFO);
    assert!(!f.late && f.lost_before == 0);
    // Kind 4 is audio too (the kit's fold); kind 9 isn't media.
    rx.on_fragment(t0, &media(4, 4, 1, 0, 12, &[0; 10], Some(INFO)));
    assert_eq!(rx.poll_frame().unwrap().stream, StreamKind::Audio);
    rx.on_fragment(t0, &media(9, 5, 1, 0, 13, &[0; 10], Some(INFO)));
    assert_eq!(rx.stats().fragments_dropped, 1);
}

#[test]
fn loss_nak_late_and_given_up() {
    let t0 = Instant::now();
    let mut rx = MediaRx::new(MediaConfig::default());
    // pn 0 arrives, pn 1 (frame 20's second half) is lost, pn 2 (frame 21) arrives.
    rx.on_fragment(t0, &media(5, 0, 2, 0, 20, &[0; 1024], None));
    rx.on_fragment(t0, &media(7, 2, 1, 0, 21, &[0; 50], Some(INFO)));
    let ack = rx.ack_fields(t0);
    assert_eq!((ack.pn_cum, ack.pn_high, ack.naks), (0, 2, vec![1]));
    // Not repeated within 50 ms.
    assert!(rx.ack_fields(t0 + 10 * MS).naks.is_empty());
    assert_eq!(rx.ack_fields(t0 + 60 * MS).naks, [1]);
    assert!(rx.poll_frame().is_none(), "frame 21 waits behind 20");
    // The resend arrives under a new packet number: frame 20 completes, late.
    rx.on_fragment(t0 + 70 * MS, &media(5, 3, 2, 0, 20, &[0; 10], Some(INFO)));
    let f = rx.poll_frame().unwrap();
    assert_eq!((f.number, f.late), (20, true));
    assert_eq!(rx.poll_frame().unwrap().number, 21);

    // A frame that never completes is given up after 500 ms, and the next one says so.
    let t1 = t0 + 100 * MS;
    rx.on_fragment(t1, &media(5, 4, 2, 0, 30, &[0; 1024], None));
    rx.on_fragment(t1, &media(5, 5, 1, 0, 31, &[0; 10], Some(INFO)));
    assert_eq!(rx.poll_timeout(), Some(t1 + 500 * MS));
    rx.handle_timeout(t1 + 500 * MS);
    let f = rx.poll_frame().unwrap();
    assert_eq!((f.number, f.lost_before), (31, 1));
    let s = rx.stats();
    // Frames 20 and 21 both waited while the NAK was out.
    assert_eq!((s.video.given_up, s.video.late, s.video.frames), (1, 2, 3));
}

#[test]
fn window_duplicates_stale_and_gap_skip() {
    let t0 = Instant::now();
    let mut rx = MediaRx::new(MediaConfig::default());
    rx.on_fragment(t0, &media(3, 0, 1, 0, 1, &[0; 8], Some(INFO)));
    rx.on_fragment(t0, &media(3, 0, 1, 0, 1, &[0; 8], Some(INFO)));
    assert_eq!(rx.stats().packets_stale, 1, "at or behind pn_cum");
    rx.on_fragment(t0, &media(3, 2, 1, 0, 2, &[0; 8], Some(INFO)));
    rx.on_fragment(t0, &media(3, 2, 1, 0, 2, &[0; 8], Some(INFO)));
    assert_eq!(rx.stats().packets_duplicate, 1);
    // pn 1 never arrives: after 300 ms the next packet moves pn_cum past it.
    rx.on_fragment(t0 + 299 * MS, &media(3, 3, 1, 0, 3, &[0; 8], Some(INFO)));
    assert_eq!(rx.ack_fields(t0 + 299 * MS).pn_cum, 0);
    rx.on_fragment(t0 + 300 * MS, &media(3, 4, 1, 0, 4, &[0; 8], Some(INFO)));
    let ack = rx.ack_fields(t0 + 400 * MS);
    assert_eq!((ack.pn_cum, ack.pn_high, ack.naks), (4, 4, vec![]));
    assert_eq!(rx.stats().gaps_skipped, 1);
    // Packets wrap the 16-bit space and the 4096-packet window.
    for pn in 5..20_000u32 {
        rx.on_fragment(t0 + 400 * MS, &media(3, pn as u16, 1, 0, pn, &[0; 8], Some(INFO)));
    }
    assert_eq!(rx.ack_fields(t0 + 500 * MS).pn_cum, (19_999u32 as u16));
    assert_eq!(rx.stats().packets_duplicate, 1);
}

#[test]
fn reset_moves_the_numbering() {
    let t0 = Instant::now();
    let mut rx = MediaRx::new(MediaConfig::default());
    rx.on_fragment(t0, &media(5, 0, 2, 0, 1, &[0; 1024], None));
    let (seq, payload) = rx.reset();
    assert_eq!(seq, 1);
    assert_eq!(
        payload,
        [1, 0, 0x00, 0x40, 0, 0, 0, 0],
        "seq 1, pn_cum 0 + 0x4000"
    );
    // The rest of the old frame is stale now.
    rx.on_fragment(t0, &media(5, 1, 2, 1, 1, &[0; 10], Some(INFO)));
    assert!(rx.poll_frame().is_none());
    assert_eq!(rx.stats().packets_stale, 1);
    rx.on_fragment(t0, &media(5, 0x4001, 1, 0, 2, &[0; 10], Some(INFO)));
    assert_eq!(rx.poll_frame().unwrap().number, 2);
}

#[test]
fn parity_rebuilds_through_media_receive() {
    let fx = Fixtures::load();
    let t0 = Instant::now();
    let mut rx = MediaRx::new(MediaConfig::default());
    let missing = fx.get("av.fec.k20.missing")[0];
    let group = |index: u8| {
        let mut g = [0u8; 8];
        g[..2].copy_from_slice(&0x1234u16.to_le_bytes());
        g[2] = index;
        g[3] = 20;
        g[4] = 1;
        g
    };
    for i in (0..20u8).filter(|&i| i != missing) {
        rx.on_envelope(t0, &group(i), fx.get(&format!("av.fec.k20.shard{i}")));
    }
    let before = rx.ack_fields(t0);
    assert!(!before.naks.is_empty(), "the missing packet is NAKed");
    rx.on_envelope(t0, &group(20), fx.get("av.fec.k20.parity"));
    assert_eq!(rx.stats().packets_recovered, 1);
    let rebuilt_pn = packet::get16(fx.get("av.fec.k20.rebuilt"), 2).unwrap();
    let after = rx.ack_fields(t0 + 100 * MS);
    assert!(
        after
            .naks
            .iter()
            .all(|&n| after.pn_cum.wrapping_add(n) != rebuilt_pn)
    );
}

// ---- the client ----

const NONCE: u32 = 0x1122_3344;

fn login_reply(state: u8) -> Vec<u8> {
    let mut r = vec![0u8; 0x1c];
    r[1] = packet::LOGIN_REPLY;
    r[0x14..0x18].copy_from_slice(&NONCE.to_le_bytes());
    r[0x18] = state;
    r
}

fn drain(c: &mut AvClient) -> Vec<Vec<u8>> {
    std::iter::from_fn(|| c.poll_transmit()).collect()
}

fn classes(pkts: &[Vec<u8>]) -> Vec<u8> {
    pkts.iter().map(|p| p[0]).collect()
}

fn events(c: &mut AvClient) -> Vec<AvEvent> {
    std::iter::from_fn(|| c.poll_event()).collect()
}

fn ready() -> (AvClient, Instant) {
    let t0 = Instant::now();
    let mut c = AvClient::new(
        AvConfig::new("admin", "testpass"),
        NONCE,
        t0,
        t0 + Duration::from_secs(30),
    );
    drain(&mut c);
    c.handle_packet(t0, &login_reply(0));
    assert_eq!(events(&mut c), [AvEvent::Ready]);
    (c, t0)
}

#[test]
fn login_resends_until_reply() {
    let f = Fixtures::load();
    let t0 = Instant::now();
    let mut c = AvClient::new(
        AvConfig::new("admin", "testpass"),
        NONCE,
        t0,
        t0 + Duration::from_secs(30),
    );
    assert_eq!(
        drain(&mut c),
        [f.get("av.login1").to_vec(), f.get("av.login2").to_vec()]
    );
    assert_eq!(c.poll_timeout(), Some(t0 + 100 * MS));
    c.handle_timeout(t0 + 100 * MS);
    assert_eq!(drain(&mut c).len(), 2);
    assert!(c.send_ioctrl(t0, 0x330, &[]).is_err(), "not before login");
    c.handle_packet(t0 + 120 * MS, &login_reply(0));
    assert_eq!(events(&mut c), [AvEvent::Ready]);
    // The schedule starts with a report.
    assert_eq!(classes(&drain(&mut c)), [packet::CLASS_REPORT]);
}

#[test]
fn login_failures() {
    let t0 = Instant::now();
    let mut c = AvClient::new(
        AvConfig::new("admin", "bad"),
        NONCE,
        t0,
        t0 + Duration::from_secs(30),
    );
    c.handle_packet(t0, &login_reply(3));
    assert_eq!(
        events(&mut c),
        [AvEvent::Ended(LinkError::AvAuthFailed(
            AvAuthStage::LoginRejected(3)
        ))]
    );
    assert_eq!(c.poll_timeout(), None);

    let mut c = AvClient::new(
        AvConfig::new("admin", "x"),
        NONCE,
        t0,
        t0 + Duration::from_secs(1),
    );
    while let Some(t) = c.poll_timeout() {
        c.handle_timeout(t);
    }
    assert_eq!(events(&mut c), [AvEvent::Ended(LinkError::Unreachable)]);
}

#[test]
fn ack_coalescing_idle_and_report_schedule() {
    let (mut c, t0) = ready();
    drain(&mut c);
    // Idle: an ACK every 110 ms, a report every 200 ms.
    let mut seen = Vec::new();
    let now = loop {
        let now = c.poll_timeout().unwrap();
        if now > t0 + 450 * MS {
            break now;
        }
        c.handle_timeout(now);
        for p in drain(&mut c) {
            seen.push(((now - t0).as_millis(), p[0]));
        }
    };
    assert_eq!(
        seen,
        [(110, 9), (200, 0x0a), (220, 9), (330, 9), (400, 0x0a), (440, 9)]
    );
    // The idle ACK due at this instant.
    c.handle_timeout(now);
    drain(&mut c);
    // A control frame from the camera is ACKed within 10 ms, carrying its frame number.
    let mut pkt = vec![0x0c, 0, 0x0c, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
    pkt.extend_from_slice(&ctrl_in(0, 0, 1, 0, &message(0x331, b"x")));
    c.handle_packet(now, &pkt);
    assert_eq!(
        events(&mut c),
        [AvEvent::Control {
            typ: 0x331,
            payload: b"x".to_vec()
        }]
    );
    assert_eq!(c.poll_timeout(), Some(now + 10 * MS));
    c.handle_timeout(now + 10 * MS);
    let ack = drain(&mut c).pop().unwrap();
    assert_eq!((ack[0], packet::get16(&ack, 0x0c)), (9, Some(0)));
}

#[test]
fn ioctrl_round_trip_and_report_answer() {
    let (mut c, t0) = ready();
    drain(&mut c);
    let id = c.send_ioctrl(t0, 0x330, b"devinfo").unwrap();
    let sent = drain(&mut c);
    assert_eq!(sent.len(), 1);
    assert_eq!(packet::get32(&sent[0], 0x24), Some(0x330));
    let mut ack = packet::ack(&Ack {
        pn_cum: 0xffff,
        pn_high: 0xffff,
        ctrl_in: 0,
        urgent: false,
        seq: 0,
        clock: 0,
        naks: &[],
    });
    packet::stamp(&mut ack, 1);
    c.handle_packet(t0 + 30 * MS, &ack);
    assert_eq!(events(&mut c), [AvEvent::Acked(id)]);
    // A camera report is answered with an RTT packet echoing its timestamp.
    let mut report = packet::report(0x4321, 0).to_vec();
    packet::stamp(&mut report, 2);
    c.handle_packet(t0 + 40 * MS, &report);
    let rtt = drain(&mut c);
    assert_eq!(
        (rtt[0][0], packet::get16(&rtt[0], 8)),
        (packet::CLASS_RTT, Some(0x4321))
    );
}

#[test]
fn media_reset_confirmed_and_timed_out() {
    let (mut c, t0) = ready();
    drain(&mut c);
    c.reset_media(t0).unwrap();
    let out = drain(&mut c);
    // The internal 0x24 frame, then an urgent ACK at once.
    assert_eq!(classes(&out), [packet::CLASS_DATA, packet::CLASS_ACK]);
    assert_eq!(
        (out[0][0x11], packet::get32(&out[0], 0x24)),
        (packet::CTRL_INTERNAL, Some(0x24))
    );
    assert_eq!(packet::get16(&out[1], 0x10), Some(1), "urgent");
    // The camera's 0x25 with our reset sequence.
    let mut confirm = ctrl_in(0, 0, 1, 0, &message(0x25, &1u16.to_le_bytes()));
    confirm[1] = packet::CTRL_INTERNAL;
    let mut pkt = vec![0x0c, 0, 0x0c, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
    pkt.extend_from_slice(&confirm);
    c.handle_packet(t0 + 20 * MS, &pkt);
    assert_eq!(events(&mut c), [AvEvent::ResetDone { confirmed: true }]);

    c.reset_media(t0 + 50 * MS).unwrap();
    let mut now = t0 + 50 * MS;
    while now < t0 + 1050 * MS {
        now = c.poll_timeout().unwrap();
        c.handle_timeout(now);
    }
    assert_eq!(events(&mut c), [AvEvent::ResetDone { confirmed: false }]);
    assert_eq!((c.stats().resets, c.stats().reset_timeouts), (2, 1));
}
