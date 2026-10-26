//! T3: our DTLS client against `openssl s_server` over loopback UDP (`docs/vtech-link-design/85-testing.md`).
//! There is no IOTC here: each UDP datagram carries one DTLS datagram. Application data goes both ways:
//! ours to the server's stdout, the server's stdin back to us. Without `openssl` on the PATH the tests
//! pass with a note.

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)]

use std::fmt::Write as _;
use std::io::{BufRead, BufReader, Write};
use std::net::UdpSocket;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};
use vtech_link_core::dtls::{DtlsClient, DtlsConfig, DtlsEvent, Suite};

const PASSWORD: &str = "interop-pass";
const WAIT: Duration = Duration::from_secs(10);

fn openssl_missing() -> bool {
    let found = Command::new("openssl")
        .arg("version")
        .output()
        .is_ok_and(|o| o.status.success());
    if !found {
        eprintln!("openssl not on the PATH: skipped");
    }
    !found
}

/// A running `openssl s_server` and everything it has printed.
struct Server {
    child: Child,
    stdin: ChildStdin,
    port: u16,
    output: Arc<Mutex<String>>,
}

impl Server {
    fn start(cipher: &str, extra: &[&str]) -> Self {
        let port = UdpSocket::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        let psk = Sha256::digest(PASSWORD.as_bytes())
            .iter()
            .fold(String::new(), |mut s, b| {
                let _ = write!(s, "{b:02x}");
                s
            });
        let accept = format!("127.0.0.1:{port}");
        let mut child = Command::new("openssl")
            .args(["s_server", "-dtls1_2", "-nocert", "-accept", &accept])
            .args(["-psk", &psk, "-psk_identity", "AUTHPWD_admin", "-cipher", cipher])
            .args(extra)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let output = Arc::new(Mutex::new(String::new()));
        for stream in [
            Box::new(child.stdout.take().unwrap()) as Box<dyn std::io::Read + Send>,
            Box::new(child.stderr.take().unwrap()),
        ] {
            let output = Arc::clone(&output);
            std::thread::spawn(move || {
                for line in BufReader::new(stream).lines().map_while(Result::ok) {
                    let mut o = output.lock().unwrap();
                    o.push_str(&line);
                    o.push('\n');
                }
            });
        }
        let stdin = child.stdin.take().unwrap();
        let server = Self {
            child,
            stdin,
            port,
            output,
        };
        assert!(
            server.wait_for("ACCEPT"),
            "s_server didn't start:\n{}",
            server.text()
        );
        server
    }

    fn text(&self) -> String {
        self.output.lock().unwrap().clone()
    }

    fn wait_for(&self, needle: &str) -> bool {
        let deadline = Instant::now() + WAIT;
        while Instant::now() < deadline {
            if self.text().contains(needle) {
                return true;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        false
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Whether a datagram starts with a ClientHello: a handshake record (22) whose message type is 1.
fn is_client_hello(d: &[u8]) -> bool {
    d.first() == Some(&22) && d.get(13) == Some(&1)
}

/// Drive the client over `socket` until `done` says so or `WAIT` passes; returns whether it did.
/// Counts the ClientHellos sent and the HelloVerifyRequests received.
fn drive(
    client: &mut DtlsClient,
    socket: &UdpSocket,
    events: &mut Vec<DtlsEvent>,
    hellos: &mut usize,
    verify_requests: &mut usize,
    mut done: impl FnMut(&mut DtlsClient, &[DtlsEvent]) -> bool,
) -> bool {
    let deadline = Instant::now() + WAIT;
    let mut buf = [0u8; 2048];
    while Instant::now() < deadline {
        while let Some(d) = client.poll_transmit() {
            *hellos += usize::from(is_client_hello(&d));
            socket.send(&d).unwrap();
        }
        if let Ok(n) = socket.recv(&mut buf) {
            *verify_requests += usize::from(buf[0] == 22 && buf[13] == 3);
            client.handle_datagram(Instant::now(), &buf[..n]);
        }
        let now = Instant::now();
        if client.poll_timeout().is_some_and(|t| now >= t) {
            client.handle_timeout(now);
        }
        while let Some(e) = client.poll_event() {
            events.push(e);
        }
        if done(client, events) {
            return true;
        }
    }
    false
}

/// `s_server` asks for a cookie on every DTLS connection (`-listen` only makes it stateless), so each
/// run sees one HelloVerifyRequest and sends two ClientHellos. The no-cookie handshake the camera does
/// is covered by `dtls.rs` and the live runs.
fn interop(suite: Suite, cipher: &str, extra: &[&str]) {
    if openssl_missing() {
        return;
    }
    let mut server = Server::start(cipher, extra);
    let socket = UdpSocket::bind("127.0.0.1:0").unwrap();
    socket.connect(("127.0.0.1", server.port)).unwrap();
    socket.set_read_timeout(Some(Duration::from_millis(5))).unwrap();
    let config = DtlsConfig {
        suite,
        ..DtlsConfig::default()
    };
    let mut client = DtlsClient::new(config, "admin", PASSWORD, Instant::now()).unwrap();
    let mut events = Vec::new();
    let mut sent_hellos = 0;
    let mut verify_requests = 0;

    let up = drive(
        &mut client,
        &socket,
        &mut events,
        &mut sent_hellos,
        &mut verify_requests,
        |c, _| c.is_connected(),
    );
    assert!(up, "no handshake: {events:?}\n{}", server.text());
    assert_eq!(
        (verify_requests, sent_hellos),
        (1, 2),
        "HelloVerifyRequests, ClientHellos"
    );
    let expected = match suite {
        Suite::EcdhePskChaCha20 => 0xccac,
    };
    assert_eq!(client.negotiated_suite(), Some(expected));

    client.send(Instant::now(), b"hello from vtech-link\n").unwrap();
    while let Some(d) = client.poll_transmit() {
        socket.send(&d).unwrap();
    }
    assert!(
        server.wait_for("hello from vtech-link"),
        "the server didn't print our data:\n{}",
        server.text()
    );

    server.stdin.write_all(b"hello from openssl\n").unwrap();
    server.stdin.flush().unwrap();
    let got = drive(
        &mut client,
        &socket,
        &mut events,
        &mut sent_hellos,
        &mut verify_requests,
        |_, ev| {
            ev.iter()
                .any(|e| matches!(e, DtlsEvent::Data(d) if d.starts_with(b"hello from openssl")))
        },
    );
    assert!(got, "no data from the server: {events:?}\n{}", server.text());
    assert_eq!(client.stats().rejected, 0);
}

#[test]
fn ecdhe_psk_chacha20() {
    interop(
        Suite::EcdhePskChaCha20,
        "ECDHE-PSK-CHACHA20-POLY1305",
        &["-groups", "X25519"],
    );
}

#[test]
fn ecdhe_psk_chacha20_no_ticket() {
    interop(
        Suite::EcdhePskChaCha20,
        "ECDHE-PSK-CHACHA20-POLY1305",
        &["-groups", "X25519", "-no_ticket"],
    );
}

#[test]
fn ecdhe_psk_chacha20_cookie_exchange() {
    // `-listen` answers the first ClientHello with a HelloVerifyRequest.
    interop(
        Suite::EcdhePskChaCha20,
        "ECDHE-PSK-CHACHA20-POLY1305",
        &["-groups", "X25519", "-listen"],
    );
}
