//! IOTC codec and session tests: T-IOTC-1..5 in `docs/vtech-link-design/85-testing.md`.

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)]

mod common;

use std::net::{Ipv4Addr, SocketAddrV4};
use std::time::{Duration, Instant};

use common::Fixtures;
use vtech_link_core::LinkError;
use vtech_link_core::error::{CloseReason, IotcReject};
use vtech_link_core::iotc::codec::{self, Ids, Inbound, LanReply, TokenRef};
use vtech_link_core::iotc::scramble::{KEY, scramble, unscramble};
use vtech_link_core::iotc::{IotcConfig, IotcEvent, IotcSession};

const UID: &str = "TESTUID0123456789ABC";
const AUTHKEY: &[u8; 8] = b"k3yAuth8";
const TOKEN: [u8; 8] = [1, 2, 3, 4, 5, 6, 7, 8];
const CAMERA: SocketAddrV4 = SocketAddrV4::new(Ipv4Addr::new(192, 168, 1, 220), 41234);

fn ids() -> Ids {
    let mut uid = [0u8; 20];
    uid.copy_from_slice(UID.as_bytes());
    Ids {
        uid,
        authkey: *AUTHKEY,
        token: TOKEN,
        sdk_version: codec::SDK_VERSION,
    }
}

fn sealed(mut pkt: Vec<u8>) -> Vec<u8> {
    codec::seal(&mut pkt);
    pkt
}

/// The connect ack as the kit specifies it. tutk-host leaves `+0x2c..0x30` uninitialised
/// (`handshake.c:57`), so its fixture carries 4 bytes of stack there; the kit has them zero.
fn ack_wire(f: &Fixtures) -> Vec<u8> {
    let mut p = f.get("iotc.connect_ack.plain").to_vec();
    p[0x2c..0x30].fill(0);
    p[3] &= !codec::FLAG_SCRAMBLED;
    sealed(p)
}

// ---- codec against tutk-host ----

#[test]
fn key_is_the_licence_key() {
    // T-IOTC-3.
    assert_eq!(KEY[..], *Fixtures::load().get("iotc.key"));
}

#[test]
fn scramble_matches_tutk_host() {
    let f = Fixtures::load();
    for n in [1, 2, 3, 4, 7, 8, 15, 16, 17, 33, 64, 100] {
        let mut b = f.get(&format!("iotc.scramble.len{n}.in")).to_vec();
        scramble(&mut b, n);
        assert_eq!(b, f.get(&format!("iotc.scramble.len{n}.out")), "len {n}");
        unscramble(&mut b, n);
        assert_eq!(b, f.get(&format!("iotc.scramble.len{n}.in")));
    }
    let mut b = f.get("iotc.scramble.len100.in").to_vec();
    scramble(&mut b, codec::PE_PREFIX);
    assert_eq!(b, f.get("iotc.scramble.pe.len100.out"));
}

#[test]
fn builders_match_tutk_host() {
    // T-IOTC-4. The fixtures' `.plain` forms already carry the scrambled flag.
    let f = Fixtures::load();
    let ids = ids();
    assert_eq!(codec::probe(&ids)[..], *f.get("iotc.probe.built"));
    assert_eq!(codec::record(&ids)[..], *f.get("iotc.record"));
    assert_eq!(sealed(codec::probe(&ids).to_vec()), f.get("iotc.probe.wire"));
    assert_eq!(sealed(codec::connect_ack(&ids).to_vec()), ack_wire(&f));
    let payload: Vec<u8> = (0..100).collect();
    assert_eq!(
        sealed(codec::data(&TOKEN, 0, 1, 5, false, &payload)),
        f.get("iotc.data.wire")
    );
    assert_eq!(
        sealed(codec::data(&TOKEN, 0, 1, 6, true, &payload)),
        f.get("iotc.data.pe.wire")
    );
    assert_eq!(
        sealed(codec::token_datagram(&TOKEN, codec::OP_ALIVE_OUT).to_vec()),
        f.get("iotc.keepalive.wire")
    );
    assert_eq!(
        sealed(codec::token_datagram(&TOKEN, codec::OP_CLOSE_OUT).to_vec()),
        f.get("iotc.close.wire")
    );
}

/// Turn one of our outbound datagrams into the camera's inbound form by swapping its opcode.
fn as_inbound(plain: &[u8], opcode: u16) -> Vec<u8> {
    let mut p = plain.to_vec();
    p[8..10].copy_from_slice(&opcode.to_le_bytes());
    p[3] &= !codec::FLAG_SCRAMBLED;
    sealed(p)
}

#[test]
fn decode_data_both_scramble_modes() {
    let f = Fixtures::load();
    let payload: Vec<u8> = (0..100).collect();
    for name in ["iotc.data.plain", "iotc.data.pe.plain"] {
        let mut pkt = as_inbound(f.get(name), codec::OP_DATA_IN);
        match codec::decode(&mut pkt).unwrap() {
            Inbound::Data {
                token,
                channel,
                datatype,
                payload: got,
                ..
            } => {
                assert_eq!(token, TokenRef::Full(TOKEN));
                assert_eq!((channel, datatype), (0, 1));
                assert_eq!(got, &payload[..]);
            }
            other => panic!("{name}: {other:?}"),
        }
    }
}

#[test]
fn decode_rejects_bad_input() {
    use codec::Drop;
    assert_eq!(codec::decode(&mut [0u8; 15]), Err(Drop::Size));
    assert_eq!(codec::decode(&mut vec![0u8; 2049]), Err(Drop::Size));
    assert_eq!(codec::decode(&mut [0u8; 16]), Err(Drop::Magic));
    // A data header whose length runs past the datagram.
    let mut p = codec::data(&TOKEN, 0, 1, 0, false, &[0; 10]);
    p[4..6].copy_from_slice(&200u16.to_le_bytes());
    assert_eq!(
        codec::decode(&mut as_inbound(&p, codec::OP_DATA_IN)),
        Err(Drop::Malformed)
    );
    // Every truncation of a valid datagram decodes or drops, never panics.
    let full = as_inbound(&codec::data(&TOKEN, 0, 1, 0, false, &[7; 40]), codec::OP_DATA_IN);
    for n in 0..full.len() {
        let _ = codec::decode(&mut full[..n].to_vec());
    }
}

/// The camera's long `0x602` reply to our probe.
fn long_reply(token: [u8; 8], result: u8, version: u8) -> Vec<u8> {
    let mut p = vec![0u8; 0xc8];
    p[0..2].copy_from_slice(&codec::MAGIC.to_le_bytes());
    p[2] = version;
    p[4..6].copy_from_slice(&0xb8u16.to_le_bytes());
    p[8..10].copy_from_slice(&codec::OP_LAN_REPLY.to_le_bytes());
    p[0x30..0x34].copy_from_slice(&0x0102_0304u32.to_le_bytes());
    p[0xbc..0xc4].copy_from_slice(&token);
    p[0xc4] = result;
    sealed(p)
}

fn short_reply(uid: &str) -> Vec<u8> {
    let mut p = vec![0u8; 0x34];
    p[0..2].copy_from_slice(&codec::MAGIC.to_le_bytes());
    p[2] = 0x1b;
    p[4..6].copy_from_slice(&0x24u16.to_le_bytes());
    p[8..10].copy_from_slice(&codec::OP_LAN_REPLY.to_le_bytes());
    p[0x10..0x10 + uid.len()].copy_from_slice(uid.as_bytes());
    sealed(p)
}

#[test]
fn decode_lan_replies() {
    // T-IOTC-5.
    match codec::decode(&mut long_reply(TOKEN, 7, 0x1b)).unwrap() {
        Inbound::LanReply(LanReply::Long {
            token,
            result,
            proto_version,
            iotc_version,
            pe_capable,
        }) => {
            assert_eq!(
                (token, result, proto_version, iotc_version),
                (TOKEN, 7, 0x1b, 0x0102_0304)
            );
            assert!(pe_capable);
        }
        other => panic!("{other:?}"),
    }
    let mut short = short_reply(UID);
    assert!(matches!(
        codec::decode(&mut short),
        Ok(Inbound::LanReply(LanReply::Short { .. }))
    ));
    // A long-form length on a datagram too short for the result falls back to nothing usable.
    let mut cut = long_reply(TOKEN, 1, 0x1b);
    unscramble(&mut cut, usize::MAX);
    cut.truncate(0xc4);
    let mut cut = sealed({
        cut[3] &= !codec::FLAG_SCRAMBLED;
        cut
    });
    assert!(matches!(
        codec::decode(&mut cut),
        Ok(Inbound::LanReply(LanReply::Short { .. })) | Err(codec::Drop::Malformed)
    ));
}

// ---- the session ----

fn session() -> (IotcSession, Instant) {
    let s = IotcSession::new(IotcConfig::default(), "testuid0123456789abc", AUTHKEY, TOKEN).unwrap();
    (s, Instant::now())
}

fn drain(s: &mut IotcSession) -> Vec<(SocketAddrV4, Vec<u8>)> {
    std::iter::from_fn(|| s.poll_transmit())
        .map(|t| (t.to, t.data))
        .collect()
}

fn connected() -> (IotcSession, Instant) {
    let (mut s, t0) = session();
    s.connect(
        t0,
        t0 + Duration::from_secs(15),
        &[Ipv4Addr::new(192, 168, 1, 255)],
    );
    drain(&mut s);
    s.handle_datagram(t0, CAMERA, &mut long_reply(TOKEN, 1, 0x1b));
    assert!(matches!(s.poll_event(), Some(IotcEvent::Connected(_))));
    drain(&mut s);
    (s, t0)
}

#[test]
fn search_then_connect() {
    let f = Fixtures::load();
    let (mut s, t0) = session();
    let bcast = Ipv4Addr::new(192, 168, 1, 255);
    let hint = Ipv4Addr::new(192, 168, 1, 220);
    s.connect(t0, t0 + Duration::from_secs(15), &[bcast, hint]);
    let round = drain(&mut s);
    let port = 32762;
    assert_eq!(
        round,
        vec![
            (SocketAddrV4::new(bcast, port), f.get("iotc.record").to_vec()),
            (SocketAddrV4::new(bcast, port), f.get("iotc.probe.wire").to_vec()),
            (SocketAddrV4::new(hint, port), f.get("iotc.record").to_vec()),
            (SocketAddrV4::new(hint, port), f.get("iotc.probe.wire").to_vec()),
        ]
    );
    // Nothing more until the next search interval.
    s.handle_timeout(t0 + Duration::from_millis(50));
    assert!(drain(&mut s).is_empty());
    assert_eq!(s.poll_timeout(), Some(t0 + Duration::from_millis(100)));
    s.handle_timeout(t0 + Duration::from_millis(100));
    assert_eq!(drain(&mut s).len(), 4);

    // Another session's reply is ignored; ours establishes and is answered unicast.
    s.handle_datagram(t0, CAMERA, &mut long_reply([9; 8], 1, 0x1b));
    assert_eq!(s.poll_event(), None);
    s.handle_datagram(t0, CAMERA, &mut long_reply(TOKEN, 1, 0x1b));
    let Some(IotcEvent::Connected(peer)) = s.poll_event() else {
        panic!("not connected")
    };
    assert_eq!(peer.addr, CAMERA);
    assert!(peer.pe_capable);
    assert_eq!(
        drain(&mut s),
        vec![
            (CAMERA, f.get("iotc.probe.wire").to_vec()),
            (CAMERA, ack_wire(&f))
        ]
    );
}

#[test]
fn search_failures() {
    let reject = |reply: Vec<u8>| {
        let (mut s, t0) = session();
        s.connect(t0, t0 + Duration::from_secs(1), &[Ipv4Addr::BROADCAST]);
        s.handle_datagram(t0, CAMERA, &mut reply.clone());
        s.poll_event()
    };
    for (result, why) in [
        (3, IotcReject::MaxSessions),
        (4, IotcReject::Port),
        (5, IotcReject::AuthKey),
        (6, IotcReject::NoKeyAuth),
        (9, IotcReject::Other(9)),
    ] {
        assert_eq!(
            reject(long_reply(TOKEN, result, 0x1b)),
            Some(IotcEvent::Ended(LinkError::IotcRejected(why)))
        );
    }
    let no_key_auth = Some(IotcEvent::Ended(LinkError::IotcRejected(IotcReject::NoKeyAuth)));
    assert_eq!(reject(long_reply(TOKEN, 1, 0x16)), no_key_auth);
    assert_eq!(reject(short_reply(UID)), no_key_auth);
    assert_eq!(reject(short_reply("OTHERUID")), None);

    let (mut s, t0) = session();
    s.connect(t0, t0 + Duration::from_secs(1), &[Ipv4Addr::BROADCAST]);
    s.handle_timeout(t0 + Duration::from_secs(1));
    assert_eq!(s.poll_event(), Some(IotcEvent::Ended(LinkError::Unreachable)));
    assert!(s.poll_timeout().is_none());
}

#[test]
fn data_both_ways() {
    let f = Fixtures::load();
    let (mut s, t0) = connected();
    let payload: Vec<u8> = (0..100).collect();
    for _ in 0..5 {
        s.send(0, 1, &payload).unwrap();
    }
    drain(&mut s);
    s.send(0, 1, &payload).unwrap();
    assert_eq!(drain(&mut s), vec![(CAMERA, f.get("iotc.data.wire").to_vec())]);
    s.set_partial_encryption(true).unwrap();
    s.send(0, 1, &payload).unwrap();
    assert_eq!(drain(&mut s), vec![(CAMERA, f.get("iotc.data.pe.wire").to_vec())]);

    let inbound = || as_inbound(f.get("iotc.data.pe.plain"), codec::OP_DATA_IN);
    s.handle_datagram(t0, CAMERA, &mut inbound());
    assert_eq!(
        s.poll_event(),
        Some(IotcEvent::Data {
            channel: 0,
            datatype: 1,
            seq: 6,
            payload: payload.clone()
        })
    );
    // From another address, or another token: dropped.
    s.handle_datagram(
        t0,
        SocketAddrV4::new(Ipv4Addr::new(192, 168, 1, 9), 41234),
        &mut inbound(),
    );
    let mut other = codec::data(&[9; 8], 0, 1, 0, false, &payload);
    s.handle_datagram(t0, CAMERA, &mut as_inbound(&other, codec::OP_DATA_IN));
    // On a channel that isn't open: dropped.
    other = codec::data(&TOKEN, 3, 0, 0, false, &payload);
    s.handle_datagram(t0, CAMERA, &mut as_inbound(&other, codec::OP_DATA_IN));
    assert_eq!(s.poll_event(), None);
    assert_eq!((s.stats().foreign, s.stats().closed_channel), (2, 1));

    assert!(s.send(3, 0, &payload).is_err());
    assert!(s.send(0, 0, &[0; 1389]).is_err());
}

#[test]
fn keepalive_cadence_and_timeout() {
    let f = Fixtures::load();
    let (mut s, t0) = connected();
    let tick = |s: &mut IotcSession, n: u64| {
        s.handle_timeout(t0 + Duration::from_secs(n));
        drain(s)
    };
    let alive = vec![(CAMERA, f.get("iotc.keepalive.wire").to_vec())];
    // Every other tick while the camera talks, then every tick from the fifth silent one.
    let sent: Vec<bool> = (1..=7).map(|n| tick(&mut s, n) == alive).collect();
    assert_eq!(sent, [false, true, false, true, true, true, true]);
    // Inbound traffic resets the silence count.
    s.handle_datagram(
        t0,
        CAMERA,
        &mut as_inbound(f.get("iotc.keepalive.plain"), codec::OP_ALIVE_IN),
    );
    for n in 8..=67 {
        tick(&mut s, n);
        assert_eq!(s.poll_event(), None, "tick {n}");
    }
    tick(&mut s, 68);
    assert_eq!(
        s.poll_event(),
        Some(IotcEvent::Ended(LinkError::Closed(CloseReason::RemoteTimeout)))
    );
}

#[test]
fn close_both_ways() {
    let f = Fixtures::load();
    let (mut s, t0) = connected();
    s.handle_datagram(
        t0,
        CAMERA,
        &mut as_inbound(f.get("iotc.close.plain"), codec::OP_CLOSE_IN),
    );
    assert_eq!(
        s.poll_event(),
        Some(IotcEvent::Ended(LinkError::Closed(CloseReason::RemoteClose)))
    );

    let (mut s, _) = connected();
    s.close();
    let close = (CAMERA, f.get("iotc.close.wire").to_vec());
    assert_eq!(drain(&mut s), vec![close.clone(), close.clone(), close]);
    assert_eq!(
        s.poll_event(),
        Some(IotcEvent::Ended(LinkError::Closed(CloseReason::Local)))
    );
    assert!(s.send(0, 0, b"x").is_err());
}

#[test]
fn channels() {
    let (mut s, _) = connected();
    assert_eq!(s.free_channel().unwrap(), 1);
    assert_eq!(s.free_channel().unwrap(), 2);
    s.close_channel(1);
    assert_eq!(s.free_channel().unwrap(), 1);
    for _ in 3..32 {
        s.free_channel().unwrap();
    }
    assert_eq!(s.free_channel(), Err(LinkError::Capacity));
    assert!(IotcSession::new(IotcConfig::default(), "bad uid!", AUTHKEY, TOKEN).is_err());
    assert!(IotcSession::new(IotcConfig::default(), UID, b"short", TOKEN).is_err());
}
