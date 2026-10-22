//! Services: the key exchange and frame crypto against `vtech_lan/crypto.py` (vectors generated from
//! it), and request/reply matching (`docs/vtech-link-design/60-application.md`).

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)]

mod common;

use std::time::{Duration, Instant};

use common::unhex;
use vtech_link_core::LinkError;
use vtech_link_core::service::frame_crypto;
use vtech_link_core::service::media_key::{KeyExchange, MasterKey};
use vtech_link_core::service::requests::{RequestEvent, Requests};

// From crypto.py: our secret 01..20, the camera's secret 65..84, salt c8..e7.
const OUR_SECRET: &str = "0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20";
const HELLO: &str = "0100000007a37cbc142093c8b755dc1b10e86cb426374ad16aa853ed0bdfc0b2b86d1c7c";
const REPLY: &str = "0000000000000000000000005714769d116bf76436ae74bc793d2c30ad1903c59ac5273805c7e2698b410c36c8c9cacbcccdcecfd0d1d2d3d4d5d6d7d8d9dadbdcdddedfe0e1e2e3e4e5e6e7";
// encrypt_frame's layout with frame_key 07 x 32 and nonce 00..0b.
const PLAIN: &str = "0000000165000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f3031";
const FRAME: &str = "001dacb9787d0ff7764a94f881560734200c81fdb512cd68ce098bf80b4f923d000102030405060708090a0b22a4724675a91a92775c802d7e4179272c75f122099bae2e147204ccb812727feadae9ffe82ac16d1dc2a5e2567832f437fc1c6a8230f6e20aad20a066d65990fbc9c10e1e2a7e";

fn master() -> MasterKey {
    let secret: [u8; 32] = unhex(OUR_SECRET).try_into().unwrap();
    KeyExchange::new(secret).master(&unhex(REPLY)).unwrap()
}

#[test]
fn key_exchange_matches_crypto_py() {
    let secret: [u8; 32] = unhex(OUR_SECRET).try_into().unwrap();
    assert_eq!(KeyExchange::new(secret).hello().to_vec(), unhex(HELLO));
    // The master itself never leaves the core; decrypting crypto.py's frame proves it is the same.
    assert_eq!(
        frame_crypto::decrypt(&master(), &unhex(FRAME)),
        Some(unhex(PLAIN))
    );
    assert_eq!(format!("{:?}", master()), "MasterKey(..)");
}

#[test]
fn key_exchange_rejects_bad_replies() {
    let kx = || KeyExchange::new([1; 32]);
    let reply = unhex(REPLY);
    assert!(kx().master(&reply[..75]).is_err());
    let mut refused = reply.clone();
    refused[0] = 0xff;
    assert_eq!(
        kx().master(&refused).unwrap_err(),
        LinkError::Protocol("0x821 key exchange refused")
    );
    let mut no_key = reply.clone();
    no_key[12..44].fill(0);
    assert!(kx().master(&no_key).is_err());
    let mut no_salt = reply;
    no_salt[44..76].fill(0);
    assert!(kx().master(&no_salt).is_err());
}

#[test]
fn frame_crypto_matches_crypto_py() {
    let m = master();
    let nonce: [u8; 12] = std::array::from_fn(|i| u8::try_from(i).unwrap());
    assert_eq!(
        frame_crypto::encrypt(&m, [7; 32], nonce, &unhex(PLAIN)),
        unhex(FRAME)
    );
    // A damaged frame, a short one, or the wrong master: no plaintext.
    let mut bad = unhex(FRAME);
    *bad.last_mut().unwrap() ^= 1;
    assert_eq!(frame_crypto::decrypt(&m, &bad), None);
    assert_eq!(frame_crypto::decrypt(&m, &unhex(FRAME)[..59]), None);
    let other = KeyExchange::new([1; 32]).master(&unhex(REPLY)).unwrap();
    assert_eq!(frame_crypto::decrypt(&other, &unhex(FRAME)), None);
    // An empty body still round-trips.
    let empty = frame_crypto::encrypt(&m, [9; 32], [3; 12], b"");
    assert_eq!(frame_crypto::decrypt(&m, &empty), Some(vec![]));
}

fn events(r: &mut Requests) -> Vec<RequestEvent> {
    std::iter::from_fn(|| r.poll_event()).collect()
}

#[test]
fn one_request_per_reply_type_at_a_time() {
    let t0 = Instant::now();
    let later = t0 + Duration::from_secs(5);
    let mut r = Requests::new(256);
    let a = r.request(0x330, b"a", 0x331, later, false);
    let b = r.request(0x330, b"b", 0x331, later, false);
    let c = r.request(0x732, b"c", 0x733, later, false);
    // b waits behind a; c has its own reply type and goes at once.
    assert_eq!(
        events(&mut r),
        [
            RequestEvent::Send {
                id: a,
                typ: 0x330,
                payload: b"a".to_vec()
            },
            RequestEvent::Send {
                id: c,
                typ: 0x732,
                payload: b"c".to_vec()
            },
        ]
    );
    r.on_control(t0, 0x331, b"reply-a".to_vec());
    assert_eq!(
        events(&mut r),
        [
            RequestEvent::Reply {
                id: a,
                payload: b"reply-a".to_vec()
            },
            RequestEvent::Send {
                id: b,
                typ: 0x330,
                payload: b"b".to_vec()
            },
        ]
    );
    // Nobody asked for 0x351: a push.
    r.on_control(t0, 0x351, b"talk".to_vec());
    assert_eq!(r.poll_push().unwrap().typ, 0x351);
}

#[test]
fn timeouts_and_late_replies() {
    let t0 = Instant::now();
    let mut r = Requests::new(256);
    let a = r.request(0x330, b"", 0x331, t0 + Duration::from_secs(1), false);
    let b = r.request(0x330, b"", 0x331, t0 + Duration::from_secs(3), false);
    let q = r.request(0x330, b"", 0x331, t0 + Duration::from_millis(500), false);
    events(&mut r);
    // The queued one with the earliest deadline goes first, without ever being sent.
    assert_eq!(r.poll_timeout(), Some(t0 + Duration::from_millis(500)));
    r.handle_timeout(t0 + Duration::from_millis(500));
    assert_eq!(
        events(&mut r),
        [RequestEvent::Done {
            id: q,
            result: Err(LinkError::Timeout)
        }]
    );
    r.handle_timeout(t0 + Duration::from_secs(1));
    assert_eq!(
        events(&mut r),
        [
            RequestEvent::Send {
                id: b,
                typ: 0x330,
                payload: vec![]
            },
            RequestEvent::Done {
                id: a,
                result: Err(LinkError::Timeout)
            },
        ]
    );
    r.on_control(t0, 0x331, b"for b".to_vec());
    assert_eq!(
        events(&mut r),
        [RequestEvent::Reply {
            id: b,
            payload: b"for b".to_vec()
        }]
    );
    // A reply nobody waits for any more is a push, as session.py logs it today.
    r.on_control(t0, 0x331, b"late".to_vec());
    assert_eq!(r.poll_push().unwrap().payload, b"late");
}

#[test]
fn exchanges_collect_until_closed() {
    let t0 = Instant::now();
    let mut r = Requests::new(256);
    let x = r.request(0x318, b"", 0x319, t0 + Duration::from_secs(5), true);
    let next = r.request(0x318, b"", 0x319, t0 + Duration::from_secs(5), false);
    events(&mut r);
    r.on_control(t0, 0x319, b"1".to_vec());
    r.on_control(t0, 0x319, b"2".to_vec());
    r.close(x);
    assert_eq!(
        events(&mut r),
        [
            RequestEvent::Reply {
                id: x,
                payload: b"1".to_vec()
            },
            RequestEvent::Reply {
                id: x,
                payload: b"2".to_vec()
            },
            RequestEvent::Send {
                id: next,
                typ: 0x318,
                payload: vec![]
            },
            RequestEvent::Done {
                id: x,
                result: Ok(())
            },
        ]
    );
    // An exchange that reaches its deadline ends normally.
    let y = r.request(0x31a, b"", 0x31b, t0 + Duration::from_secs(1), true);
    events(&mut r);
    r.handle_timeout(t0 + Duration::from_secs(1));
    assert_eq!(
        events(&mut r),
        [RequestEvent::Done {
            id: y,
            result: Ok(())
        }]
    );
}

#[test]
fn push_queue_drops_oldest_and_link_loss_fails_all() {
    let t0 = Instant::now();
    let mut r = Requests::new(2);
    for typ in 1..=3 {
        r.on_control(t0, typ, vec![]);
    }
    assert_eq!((r.poll_push().unwrap().typ, r.pushes_dropped()), (2, 1));
    let a = r.request(0x330, b"", 0x331, t0 + Duration::from_secs(5), false);
    let b = r.request(0x330, b"", 0x331, t0 + Duration::from_secs(5), false);
    events(&mut r);
    let lost = LinkError::Closed(vtech_link_core::error::CloseReason::RemoteTimeout);
    r.fail_all(&lost);
    let mut done = events(&mut r);
    done.sort_by_key(|e| match e {
        RequestEvent::Done { id, .. } => *id,
        _ => 0,
    });
    assert_eq!(
        done,
        [
            RequestEvent::Done {
                id: a,
                result: Err(lost.clone())
            },
            RequestEvent::Done {
                id: b,
                result: Err(lost)
            }
        ]
    );
    assert_eq!(r.poll_timeout(), None);
}
