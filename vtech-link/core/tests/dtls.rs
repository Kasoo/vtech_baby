//! DTLS client tests against dimpl's own PSK server (T-DTLS in `docs/vtech-link-design/85-testing.md`),
//! on a virtual clock.

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)]

use std::sync::Arc;
use std::time::{Duration, Instant};

use dimpl::crypto::Dtls12CipherSuite;
use dimpl::{Config, Dtls, Output, PskResolver};
use sha2::{Digest, Sha256};
use vtech_link_core::LinkError;
use vtech_link_core::dtls::{DtlsClient, DtlsConfig, DtlsEvent, Suite};
use vtech_link_core::error::{AvAuthStage, CloseReason};

struct Key(Vec<u8>);

impl PskResolver for Key {
    fn resolve(&self, identity: &[u8]) -> Option<Vec<u8>> {
        // The camera keys on the identity; check we send the SDK's.
        assert_eq!(identity, b"AUTHPWD_admin");
        Some(self.0.clone())
    }
}

/// The camera's side: a dimpl PSK server for `password`, offering `suites`.
struct Camera {
    dtls: Dtls,
    buf: Vec<u8>,
    out: Vec<Vec<u8>>,
    app: Vec<Vec<u8>>,
    timeout: Option<Instant>,
    error: Option<dimpl::Error>,
}

impl Camera {
    /// dimpl's server sends a HelloVerifyRequest by default, which the camera doesn't.
    fn new(password: &str, suites: &[Dtls12CipherSuite], now: Instant) -> Self {
        Self::with_cookie(password, suites, now, true)
    }

    fn with_cookie(password: &str, suites: &[Dtls12CipherSuite], now: Instant, cookie: bool) -> Self {
        let key = Sha256::digest(password.as_bytes()).to_vec();
        let config = Config::builder()
            .with_psk_server(None, Arc::new(Key(key)))
            .dtls12_cipher_suites(suites)
            .dtls13_cipher_suites(&[])
            .dangerously_set_rng_seed(7)
            .use_server_cookie(cookie)
            .build()
            .unwrap();
        let mut c = Self {
            dtls: Dtls::new_12_psk(Arc::new(config), now),
            buf: vec![0; 2048],
            out: vec![],
            app: vec![],
            timeout: None,
            error: None,
        };
        c.dtls.handle_timeout(now).unwrap();
        c.pump();
        c
    }

    fn pump(&mut self) {
        loop {
            match self.dtls.poll_output(&mut self.buf) {
                Output::Packet(p) => self.out.push(p.to_vec()),
                Output::Timeout(t) => {
                    self.timeout = Some(t);
                    return;
                }
                Output::ApplicationData(d) => self.app.push(d.to_vec()),
                _ => {}
            }
        }
    }

    fn receive(&mut self, datagram: &[u8]) {
        if let Err(e) = self.dtls.handle_packet(datagram) {
            self.error = Some(e);
        }
        self.pump();
    }
}

fn config(suite: Suite) -> DtlsConfig {
    DtlsConfig {
        suite,
        test_rng_seed: Some(1),
        ..DtlsConfig::default()
    }
}

fn dimpl_suite(suite: Suite) -> Dtls12CipherSuite {
    match suite {
        Suite::EcdhePskChaCha20 => Dtls12CipherSuite::ECDHE_PSK_CHACHA20_POLY1305_SHA256,
        Suite::PskAes128Gcm => Dtls12CipherSuite::PSK_AES128_GCM_SHA256,
    }
}

/// Exchange datagrams until neither side has anything to send. `drop` decides, per datagram index
/// and direction (true = to the camera), whether the network loses it.
fn run(
    client: &mut DtlsClient,
    camera: &mut Camera,
    now: Instant,
    drop: &mut dyn FnMut(usize, bool) -> bool,
) -> usize {
    let mut n = 0;
    loop {
        let mut moved = false;
        while let Some(d) = client.poll_transmit() {
            moved = true;
            if !drop(n, true) {
                camera.receive(&d);
            }
            n += 1;
        }
        for d in std::mem::take(&mut camera.out) {
            moved = true;
            if !drop(n, false) {
                client.handle_datagram(now, &d);
            }
            n += 1;
        }
        if !moved {
            return n;
        }
    }
}

fn events(client: &mut DtlsClient) -> Vec<DtlsEvent> {
    std::iter::from_fn(|| client.poll_event()).collect()
}

fn connect(suite: Suite) -> (DtlsClient, Camera, Instant) {
    let t0 = Instant::now();
    let mut camera = Camera::new("testpass", &[dimpl_suite(suite)], t0);
    let mut client = DtlsClient::new(config(suite), "admin", "testpass", t0).unwrap();
    run(&mut client, &mut camera, t0, &mut |_, _| false);
    assert_eq!(events(&mut client), [DtlsEvent::Connected]);
    (client, camera, t0)
}

#[test]
fn without_cookie_like_the_camera() {
    let t0 = Instant::now();
    let mut camera = Camera::with_cookie("testpass", &[dimpl_suite(Suite::EcdhePskChaCha20)], t0, false);
    let mut client = DtlsClient::new(config(Suite::EcdhePskChaCha20), "admin", "testpass", t0).unwrap();
    assert_eq!(
        run(&mut client, &mut camera, t0, &mut |_, _| false),
        4,
        "two flights each way"
    );
    assert_eq!(events(&mut client), [DtlsEvent::Connected]);
}

#[test]
fn handshake_and_data_both_suites() {
    // With a HelloVerifyRequest, which the camera doesn't send but a conformant server may.
    for (suite, id) in [(Suite::EcdhePskChaCha20, 0xccac), (Suite::PskAes128Gcm, 0x00a8)] {
        let (mut client, mut camera, t0) = connect(suite);
        assert_eq!(client.negotiated_suite(), Some(id));
        client.send(t0, b"login").unwrap();
        run(&mut client, &mut camera, t0, &mut |_, _| false);
        assert_eq!(camera.app, [b"login".to_vec()]);
        camera.dtls.send_application_data(&[0x5a; 1359]).unwrap();
        camera.pump();
        run(&mut client, &mut camera, t0, &mut |_, _| false);
        assert_eq!(events(&mut client), [DtlsEvent::Data(vec![0x5a; 1359])]);
    }
}

#[test]
fn hello_is_one_suite_no_cookie() {
    let t0 = Instant::now();
    let mut client = DtlsClient::new(config(Suite::EcdhePskChaCha20), "admin", "testpass", t0).unwrap();
    let hello = client.poll_transmit().unwrap();
    assert_eq!(client.poll_transmit(), None);
    // Record header 13, handshake header 12, version 2, random 32, session id 1, cookie 1.
    let body = &hello[13 + 12..];
    assert_eq!(&body[..2], &[0xfe, 0xfd]);
    assert_eq!(body[34], 0, "session id");
    assert_eq!(body[35], 0, "cookie");
    assert_eq!(&body[36..40], &[0x00, 0x02, 0xcc, 0xac], "one suite, 0xccac");
}

#[test]
fn records_fit_one_iotc_payload() {
    let (mut client, _, t0) = connect(Suite::EcdhePskChaCha20);
    client.send(t0, &[0; 1359]).unwrap();
    assert!(client.poll_transmit().unwrap().len() <= 1388);
    assert!(client.send(t0, &[0; 1360]).is_err());
}

#[test]
fn lost_flights_are_retransmitted() {
    for lose in 0..4 {
        let t0 = Instant::now();
        let mut camera = Camera::new("testpass", &[dimpl_suite(Suite::EcdhePskChaCha20)], t0);
        let mut client = DtlsClient::new(config(Suite::EcdhePskChaCha20), "admin", "testpass", t0).unwrap();
        let mut sent = 0;
        let mut now = t0;
        for _ in 0..20 {
            let n = run(&mut client, &mut camera, now, &mut |i, _| sent + i == lose);
            sent += n;
            if client.is_connected() {
                break;
            }
            now = client.poll_timeout().unwrap();
            client.handle_timeout(now);
            if camera.timeout.is_some_and(|t| t <= now) {
                let _ = camera.dtls.handle_timeout(now);
                camera.pump();
            }
        }
        assert!(client.is_connected(), "datagram {lose} lost");
        assert!(
            now - t0 < Duration::from_secs(3),
            "datagram {lose} lost: {:?}",
            now - t0
        );
    }
}

#[test]
fn wrong_password_is_silence_after_finished() {
    // The camera sends its ServerHello flight, then nothing once it can't verify our Finished.
    let t0 = Instant::now();
    let mut camera = Camera::with_cookie("otherpass", &[dimpl_suite(Suite::EcdhePskChaCha20)], t0, false);
    let mut client = DtlsClient::new(config(Suite::EcdhePskChaCha20), "admin", "testpass", t0).unwrap();
    let mut flights = 0;
    run(&mut client, &mut camera, t0, &mut |_, to_camera| {
        flights += usize::from(to_camera);
        !to_camera && flights >= 2
    });
    assert!(events(&mut client).is_empty());
    let mut now = t0;
    while !client.is_ended() {
        now = client.poll_timeout().unwrap();
        client.handle_timeout(now);
        while client.poll_transmit().is_some() {}
    }
    assert_eq!(
        events(&mut client),
        [DtlsEvent::Ended(LinkError::AvAuthFailed(AvAuthStage::Dtls(None)))]
    );
    assert_eq!(now - t0, Duration::from_secs(3));
}

#[test]
fn wrong_password_with_an_alert() {
    // A server that does answer a bad Finished, with an alert or a bad Finished of its own.
    let t0 = Instant::now();
    let mut camera = Camera::new("otherpass", &[dimpl_suite(Suite::EcdhePskChaCha20)], t0);
    let mut client = DtlsClient::new(config(Suite::EcdhePskChaCha20), "admin", "testpass", t0).unwrap();
    run(&mut client, &mut camera, t0, &mut |_, _| false);
    let got = events(&mut client);
    assert!(
        got.is_empty()
            || matches!(
                got[..],
                [DtlsEvent::Ended(LinkError::AvAuthFailed(AvAuthStage::Dtls(_)))]
            ),
        "{got:?}"
    );
}

#[test]
fn refused_suite() {
    // The camera answers dimpl's hello for a suite it won't serve with a fatal handshake_failure
    // (live, 2026-10-21): epoch 0, level 2, description 40.
    let t0 = Instant::now();
    let mut client = DtlsClient::new(config(Suite::EcdhePskChaCha20), "admin", "testpass", t0).unwrap();
    client.poll_transmit().unwrap();
    client.handle_datagram(t0, &[21, 0xfe, 0xfd, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 2, 40]);
    assert_eq!(
        events(&mut client),
        [DtlsEvent::Ended(LinkError::Protocol("dtls handshake refused"))]
    );

    // dimpl's own server says nothing to a hello it can't serve, as the camera does to OpenSSL's.
    let mut camera = Camera::new("testpass", &[Dtls12CipherSuite::PSK_AES128_CCM_8], t0);
    let mut client = DtlsClient::new(config(Suite::EcdhePskChaCha20), "admin", "testpass", t0).unwrap();
    run(&mut client, &mut camera, t0, &mut |_, _| false);
    assert!(events(&mut client).is_empty() && !client.is_ended());
}

#[test]
fn no_answer_is_unreachable() {
    let t0 = Instant::now();
    let mut client = DtlsClient::new(config(Suite::EcdhePskChaCha20), "admin", "testpass", t0).unwrap();
    let mut hellos = 0;
    let mut now = t0;
    while !client.is_ended() {
        while client.poll_transmit().is_some() {
            hellos += 1;
        }
        now = client.poll_timeout().unwrap();
        client.handle_timeout(now);
    }
    assert_eq!(events(&mut client), [DtlsEvent::Ended(LinkError::Unreachable)]);
    assert!(
        now - t0 >= Duration::from_secs(30) && now - t0 < Duration::from_secs(31),
        "{:?}",
        now - t0
    );
    // 0.5 s doubling: sent at 0, 0.5, 1.5, 3.5, 7.5, 15.5 (with jitter).
    assert!((5..=7).contains(&hellos), "{hellos} hellos");
}

#[test]
fn close_notify_ends_the_link() {
    let (mut client, mut camera, t0) = connect(Suite::EcdhePskChaCha20);
    camera.dtls.close().unwrap();
    camera.pump();
    run(&mut client, &mut camera, t0, &mut |_, _| false);
    assert_eq!(
        events(&mut client),
        [DtlsEvent::Ended(LinkError::Closed(CloseReason::DtlsAlert(0)))]
    );
}

#[test]
fn garbage_once_connected_is_counted_not_fatal() {
    let (mut client, _, t0) = connect(Suite::EcdhePskChaCha20);
    let mut fake = vec![23, 0xfe, 0xfd, 0, 1, 0, 0, 0, 0, 0, 9, 0, 20];
    fake.extend_from_slice(&[0xaa; 20]);
    client.handle_datagram(t0, &fake);
    client.handle_datagram(t0, &[1, 2, 3]);
    assert!(client.is_connected());
    assert!(events(&mut client).is_empty());
}
