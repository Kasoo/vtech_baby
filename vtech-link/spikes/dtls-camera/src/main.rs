//! Phase 0 spike: connect to the camera over a minimal IOTC session, run dimpl's DTLS 1.2 PSK
//! client on channel 0, log in to the AV channel and ask for the device info.
//!
//!   VTECH_UID=... VTECH_AUTHKEY=... VTECH_PWD=... dtls-camera <suite> [camera-ip] [options]
//!
//! <suite> is one of ecdhe-psk-chacha, psk-gcm, psk-ccm8. Options:
//!   --wrong-pwd          log in with a wrong AV password
//!   --sdk-version 0xNN   the SDK version advertised (default 0x04030864, 4.3.8.100)
//!   --window N           after DEVINFO, send N read-only GETs back to back, unacknowledged
//!   --ack-ms N           send an ACK every N ms (default 100)
//!   --hold S             keep the link up S seconds (keepalives and ACKs), then DEVINFO again
//! Read-only: it sends DEVINFO and GETs only.
use std::collections::HashMap;
use std::env;
use std::net::{SocketAddr, UdpSocket};
use std::process::exit;
use std::sync::Arc;
use std::time::{Duration, Instant};

use dimpl::crypto::Dtls12CipherSuite;
use dimpl::{Config, Dtls, Output, PskResolver};
use hmac::{Hmac, Mac};
use sha1::Sha1;

const KEY: &[u8; 16] = b"Charlie is the d";
const SEARCH_PORT: u16 = 32762;
const ROT: [u32; 4] = [1, 5, 9, 13];

// ---- the scramble (tutk-host/iotc/wire.c) ----

fn permute(b: [u32; 4]) -> [u32; 4] {
    let (b0, b1, b2, b3) = (b[0], b[1], b[2], b[3]);
    let t = (b0 >> 16 & 0xff) | (b0 & 0xff00) | b0 << 24;
    let u = (b1 >> 16 & 0xff) | (b1 >> 8 & 0xff0000) | (b1 << 8 & 0xffff) | (b0 & 0xff000000);
    [
        (b2 >> 24) << 29 | ((b2 & 0xff00 | (b2 & 0xff) << 16 | b2 >> 24 | b3 & 0xff000000) >> 3),
        (b3 >> 8) << 25
            | ((b3 >> 8 & 0xff | b2 >> 8 & 0xff00 | (b3 & 0xff) << 16 | (b3 & 0xff0000) << 8) >> 7),
        t << 21 | ((t | b1 << 8 & 0xff0000) >> 11),
        u << 17 | u >> 15,
    ]
}

fn inverse_map() -> [u8; 128] {
    let mut map = [0u8; 128];
    for i in 0..128 {
        let mut b = [0u32; 4];
        b[i / 32] = 1 << (i % 32);
        let y = permute(b);
        for j in 0..128 {
            if y[j / 32] >> (j % 32) & 1 == 1 {
                map[j] = i as u8;
            }
        }
    }
    map
}

fn tail_order(n: usize) -> Option<&'static [usize]> {
    match n {
        2 => Some(&[1, 0]),
        4 => Some(&[2, 3, 0, 1]),
        8 => Some(&[7, 4, 3, 2, 1, 6, 5, 0]),
        _ => None,
    }
}

fn word(b: &[u8], i: usize) -> u32 {
    u32::from_le_bytes([b[i], b[i + 1], b[i + 2], b[i + 3]])
}

fn scramble(buf: &mut [u8], prefix: usize) {
    let n = prefix.min(buf.len());
    let (nblk, tail) = (n / 16, n % 16);
    for k in 0..nblk {
        let o = 16 * k;
        let mut b = [0u32; 4];
        for i in 0..4 {
            b[i] = word(buf, o + 4 * i).rotate_right(ROT[i]) ^ word(KEY, 4 * i);
        }
        for (i, y) in permute(b).iter().enumerate() {
            buf[o + 4 * i..o + 4 * i + 4].copy_from_slice(&y.to_le_bytes());
        }
    }
    let o = 16 * nblk;
    let tmp: Vec<u8> = (0..tail).map(|i| buf[o + i] ^ KEY[i]).collect();
    for i in 0..tail {
        buf[o + i] = tmp[tail_order(tail).map_or(i, |r| r[i])];
    }
}

fn unscramble(buf: &mut [u8], prefix: usize, inv: &[u8; 128]) {
    let n = prefix.min(buf.len());
    let (nblk, tail) = (n / 16, n % 16);
    for k in 0..nblk {
        let o = 16 * k;
        let y = [word(buf, o), word(buf, o + 4), word(buf, o + 8), word(buf, o + 12)];
        let mut b = [0u32; 4];
        for j in 0..128 {
            if y[j / 32] >> (j % 32) & 1 == 1 {
                let s = inv[j] as usize;
                b[s / 32] |= 1 << (s % 32);
            }
        }
        for i in 0..4 {
            let x = (b[i] ^ word(KEY, 4 * i)).rotate_left(ROT[i]);
            buf[o + 4 * i..o + 4 * i + 4].copy_from_slice(&x.to_le_bytes());
        }
    }
    let o = 16 * nblk;
    let mut tmp = vec![0u8; tail];
    for i in 0..tail {
        tmp[tail_order(tail).map_or(i, |r| r[i])] = buf[o + i];
    }
    for i in 0..tail {
        buf[o + i] = tmp[i] ^ KEY[i];
    }
}

// ---- IOTC datagrams (tutk-host/iotc/handshake.c, data.c) ----

fn header(pkt: &mut [u8], flags: u8, length: u16, opcode: u16, field_a: u16) {
    pkt[..16].fill(0);
    pkt[0] = 0x04;
    pkt[1] = 0x02;
    pkt[2] = 0x1d;
    pkt[3] = flags;
    pkt[4..6].copy_from_slice(&length.to_le_bytes());
    pkt[8..10].copy_from_slice(&opcode.to_le_bytes());
    pkt[10..12].copy_from_slice(&field_a.to_le_bytes());
}

struct Iotc {
    sock: UdpSocket,
    uid: [u8; 20],
    authkey: [u8; 8],
    token: [u8; 8],
    peer: Option<SocketAddr>,
    pe: bool,
    seq: [u16; 32],
    inv: [u8; 128],
    sdk_version: u32,
}

impl Iotc {
    fn send(&self, to: SocketAddr, mut pkt: Vec<u8>, scrambled: bool) {
        if scrambled {
            pkt[3] |= 0x02;
            let prefix = if pkt[3] & 0x01 != 0 { 0x40 } else { pkt.len() };
            scramble(&mut pkt, prefix);
        }
        if let Err(e) = self.sock.send_to(&pkt, to) {
            eprintln!("send to {to}: {e}");
        }
    }

    fn probe(&self) -> Vec<u8> {
        let mut p = vec![0u8; 0x58];
        header(&mut p, 0, 0x48, 0x0601, 0x0021);
        p[0x0f] = 1;
        p[0x10..0x24].copy_from_slice(&self.uid);
        p[0x34..0x38].copy_from_slice(&self.sdk_version.to_le_bytes());
        p[0x38..0x40].copy_from_slice(&self.token);
        p[0x40..0x44].copy_from_slice(&1u32.to_le_bytes());
        p[0x4a..0x52].copy_from_slice(&self.authkey);
        p
    }

    fn record(&self) -> Vec<u8> {
        let mut r = vec![0u8; 0x34];
        r[0..2].copy_from_slice(&0xcc51u16.to_le_bytes());
        r[4] = 2;
        r[5] = 0x10;
        r[6..8].copy_from_slice(&0x28u16.to_le_bytes());
        r[0x10..0x18].copy_from_slice(&self.token);
        r[0x18..0x1c].copy_from_slice(&self.sdk_version.to_le_bytes());
        r[0x1c] = 0x1d;
        let mut key = [0u8; 28];
        key[..20].copy_from_slice(&self.uid);
        key[20..].copy_from_slice(&self.authkey);
        let mut mac = Hmac::<Sha1>::new_from_slice(&key).unwrap();
        mac.update(&r[..0x20]);
        r[0x20..0x34].copy_from_slice(&mac.finalize().into_bytes());
        r
    }

    fn connect_ack(&self) -> Vec<u8> {
        let mut a = vec![0u8; 0x34];
        header(&mut a, 0, 0x24, 0x0402, 0x0033);
        a[0x10..0x24].copy_from_slice(&self.uid);
        a[0x24..0x2c].copy_from_slice(&self.token);
        a[0x30..0x34].copy_from_slice(&self.sdk_version.to_le_bytes());
        a
    }

    fn send_data(&mut self, channel: u8, datatype: u8, payload: &[u8]) {
        let mut p = vec![0u8; 28 + payload.len()];
        header(&mut p, 0x08 | if self.pe { 1 } else { 0 }, 12 + payload.len() as u16, 0x0407, 0x0021);
        p[6..8].copy_from_slice(&self.seq[channel as usize].to_le_bytes());
        self.seq[channel as usize] = self.seq[channel as usize].wrapping_add(1);
        p[0x0c..0x0e].copy_from_slice(&self.token[..2]);
        p[0x0e] = channel;
        p[0x0f] = datatype;
        p[16..20].copy_from_slice(&12u32.to_le_bytes());
        p[20..28].copy_from_slice(&self.token);
        p[28..].copy_from_slice(payload);
        self.send(self.peer.unwrap(), p, true);
    }

    fn token_datagram(&self, opcode: u16) -> Vec<u8> {
        let mut p = vec![0u8; 24];
        header(&mut p, 0x08, 8, opcode, 0x0021);
        p[16..24].copy_from_slice(&self.token);
        p
    }
}

enum Inbound {
    Reply { result: u8, version: u8, pe: bool, from: SocketAddr },
    Data { channel: u8, datatype: u8, payload: Vec<u8> },
    Other(u16),
}

fn parse(iotc: &Iotc, raw: &[u8], from: SocketAddr) -> Option<Inbound> {
    if raw.len() < 16 || raw.len() > 2048 {
        return None;
    }
    let mut hdr = raw[..16].to_vec();
    unscramble(&mut hdr, 16, &iotc.inv);
    if hdr[0] != 0x04 || hdr[1] != 0x02 {
        return None;
    }
    let mut p = raw.to_vec();
    let prefix = if hdr[3] & 1 != 0 { 0x40 } else { p.len() };
    unscramble(&mut p, prefix, &iotc.inv);
    let length = u16::from_le_bytes([p[4], p[5]]) as usize;
    match u16::from_le_bytes([p[8], p[9]]) {
        0x0602 if length >= 0xb8 && p.len() >= 0xc5 => {
            (p[0xbc..0xc4] == iotc.token).then(|| Inbound::Reply {
                result: p[0xc4],
                version: p[2],
                pe: p[3] & 0x02 != 0,
                from,
            })
        }
        0x0408 => {
            let skip = if p[3] & 0x08 != 0 { 12 } else { 0 };
            if skip != 0 && (p.len() < 28 || p[0x14..0x1c] != iotc.token) {
                return None;
            }
            let n = length.checked_sub(skip)?;
            let start = 16 + skip;
            (start + n <= p.len()).then(|| Inbound::Data {
                channel: p[0x0e],
                datatype: p[0x0f] & 3,
                payload: p[start..start + n].to_vec(),
            })
        }
        op => Some(Inbound::Other(op)),
    }
}

// ---- AV (tutk-host/av/session.c, ioctrl.c, transport.c) ----

const OPCODE_SUPPORT: [u8; 20] = [
    0x04, 0, 0, 0, 0xfb, 0x07, 0x1f, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x03, 0,
];

fn login(n: usize, nonce: u32, account: &str, password: &str) -> Vec<u8> {
    let w = 0x101;
    let mut p = vec![0u8; 2 * w + 0x38 + 2 * n];
    p[1] = if n == 1 { 0x20 } else { 0 };
    p[2..4].copy_from_slice(&0x0cu16.to_le_bytes());
    p[0x10..0x12].copy_from_slice(&((2 * w + 0x20 + 2 * n) as u16).to_le_bytes());
    p[0x12] = 1;
    p[0x14..0x18].copy_from_slice(&nonce.wrapping_add(n as u32).to_le_bytes());
    p[0x18..0x18 + account.len()].copy_from_slice(account.as_bytes());
    p[0x18 + w..0x18 + w + password.len()].copy_from_slice(password.as_bytes());
    p[0x18 + 2 * w] = 1;
    p[0x1c + 2 * w..0x30 + 2 * w].copy_from_slice(&OPCODE_SUPPORT);
    p[0x30 + 2 * w] = 0;
    p[0x34 + 2 * w] = 1;
    p
}

fn control_frame(counter: u16, frame: u32, typ: u32, payload: &[u8]) -> Vec<u8> {
    let mut p = vec![0u8; 0x28 + payload.len()];
    p[0] = 0x0c;
    p[2..4].copy_from_slice(&0x0cu16.to_le_bytes());
    p[4..6].copy_from_slice(&counter.to_le_bytes());
    let f = 0x10;
    p[f + 1] = 0x70;
    p[f + 2..f + 4].copy_from_slice(&(frame as u16).to_le_bytes());
    p[f + 4..f + 8].copy_from_slice(&1u32.to_le_bytes());
    p[f + 8..f + 12].copy_from_slice(&(payload.len() as u32 + 4).to_le_bytes());
    p[f + 12..f + 16].copy_from_slice(&frame.to_le_bytes());
    p[f + 20..f + 24].copy_from_slice(&typ.to_le_bytes());
    p[f + 24..].copy_from_slice(payload);
    p
}

fn ack(counter: u16, ctrl_in: u16, seq: u16, ms: u16) -> Vec<u8> {
    let mut p = vec![0u8; 0x18];
    p[0] = 0x09;
    p[2..4].copy_from_slice(&0x0cu16.to_le_bytes());
    p[4..6].copy_from_slice(&counter.to_le_bytes());
    p[8..10].copy_from_slice(&0xffffu16.to_le_bytes());
    p[10..12].copy_from_slice(&0xffffu16.to_le_bytes());
    p[12..14].copy_from_slice(&ctrl_in.to_le_bytes());
    p[0x12..0x14].copy_from_slice(&seq.to_le_bytes());
    p[0x14..0x16].copy_from_slice(&ms.to_le_bytes());
    p
}

struct Resolver(Vec<u8>);

impl PskResolver for Resolver {
    fn resolve(&self, _identity: &[u8]) -> Option<Vec<u8>> {
        Some(self.0.clone())
    }
}

fn sha256(data: &[u8]) -> Vec<u8> {
    use dimpl_sha::Digest;
    dimpl_sha::Sha256::digest(data).to_vec()
}

mod dimpl_sha {
    pub use sha2::{Digest, Sha256};
}

/// The suite the server picked, from a ServerHello record in a datagram.
fn server_hello_suite(d: &[u8]) -> Option<u16> {
    let mut i = 0;
    while i + 13 <= d.len() {
        let len = u16::from_be_bytes([d[i + 11], d[i + 12]]) as usize;
        let body = d.get(i + 13..i + 13 + len)?;
        if d[i] == 22 && body.first() == Some(&2) && body.len() > 12 + 35 {
            let b = &body[12..];
            let o = 35 + b[34] as usize;
            return Some(u16::from_be_bytes([*b.get(o)?, *b.get(o + 1)?]));
        }
        i += 13 + len;
    }
    None
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let suite = match args.get(1).map(String::as_str) {
        Some("ecdhe-psk-chacha") => Dtls12CipherSuite::ECDHE_PSK_CHACHA20_POLY1305_SHA256,
        Some("psk-gcm") => Dtls12CipherSuite::PSK_AES128_GCM_SHA256,
        Some("psk-ccm8") => Dtls12CipherSuite::PSK_AES128_CCM_8,
        _ => {
            eprintln!("usage: dtls-camera ecdhe-psk-chacha|psk-gcm|psk-ccm8 [camera-ip] [--wrong-pwd]");
            exit(2);
        }
    };
    let camera_ip = args.get(2).filter(|a| !a.starts_with("--")).cloned();
    let wrong_pwd = args.iter().any(|a| a == "--wrong-pwd");
    let opt = |name: &str| args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned();
    let sdk_version = opt("--sdk-version")
        .map(|v| u32::from_str_radix(v.trim_start_matches("0x"), 16).expect("--sdk-version hex"))
        .unwrap_or(0x0403_0864);
    let window: usize = opt("--window").map(|v| v.parse().expect("--window N")).unwrap_or(0);
    let ack_ms: u64 = opt("--ack-ms").map(|v| v.parse().expect("--ack-ms N")).unwrap_or(100);
    let hold: u64 = opt("--hold").map(|v| v.parse().expect("--hold S")).unwrap_or(0);
    let var = |k: &str| env::var(k).unwrap_or_else(|_| panic!("{k} not set"));
    let (uid_s, authkey_s, mut password) = (var("VTECH_UID"), var("VTECH_AUTHKEY"), var("VTECH_PWD"));
    if wrong_pwd {
        password.push('x');
    }
    let account = env::var("VTECH_ACCOUNT").unwrap_or_else(|_| "admin".into());

    let mut uid = [0u8; 20];
    uid[..uid_s.len()].copy_from_slice(uid_s.to_uppercase().as_bytes());
    let mut authkey = [0u8; 8];
    authkey.copy_from_slice(authkey_s.as_bytes());
    let mut token = [0u8; 8];
    getrandom::getrandom(&mut token).unwrap();

    let sock = UdpSocket::bind("0.0.0.0:0").unwrap();
    sock.set_broadcast(true).unwrap();
    sock.set_read_timeout(Some(Duration::from_millis(10))).unwrap();
    let mut iotc = Iotc { sock, uid, authkey, token, peer: None, pe: false, seq: [0; 32], inv: inverse_map(), sdk_version };

    // Discovery and IOTC connect.
    let t0 = Instant::now();
    let mut targets: Vec<SocketAddr> = vec![format!("255.255.255.255:{SEARCH_PORT}").parse().unwrap()];
    if let Some(ip) = &camera_ip {
        targets.push(format!("{ip}:{SEARCH_PORT}").parse().unwrap());
    }
    let mut buf = [0u8; 2048];
    let mut next_search = Instant::now();
    let (version, pe_capable) = loop {
        if t0.elapsed() > Duration::from_secs(15) {
            eprintln!("FAIL iotc: no connect reply in 15 s");
            exit(1);
        }
        if Instant::now() >= next_search {
            let mut probe = iotc.probe();
            probe[3] |= 0x02;
            scramble(&mut probe, 0x58);
            for t in &targets {
                iotc.send(*t, iotc.record(), false);
                let _ = iotc.sock.send_to(&probe, t);
            }
            next_search = Instant::now() + Duration::from_millis(100);
        }
        if let Ok((n, from)) = iotc.sock.recv_from(&mut buf) {
            if let Some(Inbound::Reply { result, version, pe, from }) = parse(&iotc, &buf[..n], from) {
                if result != 1 && result != 7 {
                    eprintln!("FAIL iotc: camera rejected the connect, result {result}");
                    exit(1);
                }
                iotc.peer = Some(from);
                break (version, pe);
            }
        }
    };
    let peer = iotc.peer.unwrap();
    iotc.send(peer, iotc.probe(), true);
    iotc.send(peer, iotc.connect_ack(), true);
    println!("iotc: established with {peer} from local port {} in {} ms, camera version 0x{version:x}, \
              pe_capable {pe_capable}, advertised 0x{sdk_version:08x}",
             iotc.sock.local_addr().unwrap().port(), t0.elapsed().as_millis());
    iotc.pe = pe_capable;

    // DTLS over channel 0, datatype 1.
    let config = Arc::new(
        Config::builder()
            .with_psk_client(format!("AUTHPWD_{account}").into_bytes(), Arc::new(Resolver(sha256(password.as_bytes()))))
            .dtls12_cipher_suites(&[suite])
            .dtls13_cipher_suites(&[])
            .mtu(1300)
            .build()
            .expect("dimpl config"),
    );
    let mut dtls = Dtls::new_12_psk(config, Instant::now());
    dtls.set_active(true);
    let t1 = Instant::now();
    let mut connected = false;
    let mut out = vec![0u8; 4096];
    let mut negotiated = None;
    let mut timer = Instant::now();
    let mut app: Vec<Vec<u8>> = Vec::new();
    let mut ignored: HashMap<u16, u32> = HashMap::new();

    // Drive dimpl: feed it inbound channel-0 payloads, send what it emits.
    let mut pump = |iotc: &mut Iotc, dtls: &mut Dtls, timer: &mut Instant, connected: &mut bool,
                    app: &mut Vec<Vec<u8>>| {
        loop {
            match dtls.poll_output(&mut out) {
                Output::Packet(p) => {
                    let p = p.to_vec();
                    iotc.send_data(0, 1, &p);
                }
                Output::Timeout(t) => {
                    *timer = t;
                    break;
                }
                Output::Connected => *connected = true,
                Output::ApplicationData(d) => app.push(d.to_vec()),
                Output::CloseNotify => {
                    eprintln!("dtls: close_notify from camera");
                }
                Output::BufferTooSmall { needed } => panic!("buffer too small: {needed}"),
                _ => {}
            }
        }
    };

    let deadline = Instant::now() + Duration::from_secs(20);
    dtls.handle_timeout(Instant::now()).unwrap();
    pump(&mut iotc, &mut dtls, &mut timer, &mut connected, &mut app);
    while !connected {
        if Instant::now() > deadline {
            eprintln!("FAIL dtls: no handshake in 20 s (suite {suite:?}, negotiated {negotiated:x?})");
            close(&iotc);
            exit(1);
        }
        if let Ok((n, from)) = iotc.sock.recv_from(&mut buf) {
            if Some(from) == iotc.peer {
                match parse(&iotc, &buf[..n], from) {
                    Some(Inbound::Data { channel: 0, payload, .. }) => {
                        negotiated = negotiated.or(server_hello_suite(&payload));
                        if let Err(e) = dtls.handle_packet(&payload) {
                            eprintln!("FAIL dtls: {e:?} (negotiated {negotiated:x?}) after {} ms", t1.elapsed().as_millis());
                            close(&iotc);
                            exit(1);
                        }
                    }
                    Some(Inbound::Other(op)) => *ignored.entry(op).or_default() += 1,
                    _ => {}
                }
            }
        }
        if Instant::now() >= timer {
            if let Err(e) = dtls.handle_timeout(Instant::now()) {
                eprintln!("FAIL dtls timeout: {e:?}");
                close(&iotc);
                exit(1);
            }
        }
        pump(&mut iotc, &mut dtls, &mut timer, &mut connected, &mut app);
    }
    println!("dtls: connected in {} ms, suite 0x{:04x}", t1.elapsed().as_millis(), negotiated.unwrap_or(0));

    // AV login, then DEVINFO, the window of GETs, the hold, and DEVINFO again.
    let mut nonce = [0u8; 4];
    getrandom::getrandom(&mut nonce).unwrap();
    let nonce = u32::from_le_bytes(nonce) | 1;
    let mut av = Av::default();
    let t2 = Instant::now();
    let mut stage = 0; // 0 login, 1 DEVINFO, 2 window, 3 hold, 4 DEVINFO again, 5 done
    let mut stage_deadline = Instant::now() + Duration::from_secs(15);
    let mut next_login = Instant::now();
    let mut next_ack = Instant::now();
    let mut next_alive = Instant::now() + Duration::from_secs(1);
    let mut hold_until: Option<Instant> = None;
    let mut waiting: Vec<(u32, Instant, &'static str)> = Vec::new();
    let gets: [(u32, &[u8], u32, &str); 8] = [
        (0x733, &[0; 4], 0x734, "GET_PARAMS"),
        (0x7a0, &[0; 4], 0x7a1, "GET_NIGHT_LIGHT"),
        (0x709, &[0; 8], 0x710, "GET_STATUS_LED"),
        (0x713, &[0; 4], 0x714, "GET_NIGHT_VISION"),
        (0x74b, &[0; 4], 0x74c, "GET_TEMP_ALERT"),
        (0x7a4, &[0; 4], 0x7a5, "GET_MOTION"),
        (0x7b0, &[0; 4], 0x7b1, "GET_SOUND"),
        (0x322, &[0; 8], 0x323, "GET_STREAM_CTRL"),
    ];
    let mut devinfo = vec![0u8, 1, 0x12, 0x34];
    devinfo.extend_from_slice(&[0u8; 16]);
    loop {
        let now = Instant::now();
        if now > stage_deadline {
            let names: Vec<_> = waiting.iter().map(|w| w.2).collect();
            eprintln!("FAIL av: stage {stage} timed out, still waiting for {names:?}, {} frames unacknowledged \
                       (camera acked up to {})", av.out.len(), av.acked);
            close(&iotc);
            exit(1);
        }
        if stage == 0 && now >= next_login {
            for n in 0..2 {
                dtls.send_application_data(&login(n, nonce, &account, &password)).unwrap();
            }
            next_login = now + Duration::from_millis(100);
        }
        if stage > 0 && waiting.is_empty() {
            match stage {
                1 => {
                    av.send(&mut dtls, 0x330, &devinfo, now);
                    waiting.push((0x331, now, "DEVINFO"));
                    stage = 2;
                    stage_deadline = now + Duration::from_secs(5);
                }
                2 => {
                    for (typ, payload, reply, name) in gets.iter().take(window) {
                        av.send(&mut dtls, *typ, payload, now);
                        waiting.push((*reply, now, name));
                    }
                    if window > 0 {
                        println!("av: sent {window} GETs back to back, {} control frames unacknowledged", av.out.len());
                    }
                    stage = 3;
                    stage_deadline = now + Duration::from_secs(10);
                }
                3 => {
                    if hold_until.is_none() {
                        hold_until = Some(now + Duration::from_secs(hold));
                        stage_deadline = now + Duration::from_secs(hold + 10);
                        if hold > 0 {
                            println!("av: holding the link for {hold} s");
                        }
                    }
                    if hold_until.is_some_and(|t| now >= t) {
                        av.send(&mut dtls, 0x330, &devinfo, now);
                        waiting.push((0x331, now, "DEVINFO again"));
                        stage = 4;
                        stage_deadline = now + Duration::from_secs(5);
                    }
                }
                _ => {
                    println!("av: {} control frames sent, {} retransmitted, at most {} unacknowledged at once",
                             av.next_frame, av.retransmits, av.max_in_flight);
                    close(&iotc);
                    println!("PASS suite 0x{:04x}", negotiated.unwrap_or(0));
                    return;
                }
            }
        }
        if stage > 0 && now >= next_ack {
            av.send_ack(&mut dtls, t0.elapsed().as_millis() as u16);
            next_ack = now + Duration::from_millis(ack_ms);
        }
        if now >= next_alive {
            iotc.send(peer, iotc.token_datagram(0x0427), true);
            next_alive = now + Duration::from_secs(1);
        }
        av.retransmit(&mut dtls, now);
        pump(&mut iotc, &mut dtls, &mut timer, &mut connected, &mut app);
        if let Ok((n, from)) = iotc.sock.recv_from(&mut buf) {
            if Some(from) == iotc.peer {
                if let Some(Inbound::Data { channel: 0, payload, .. }) = parse(&iotc, &buf[..n], from) {
                    if let Err(e) = dtls.handle_packet(&payload) {
                        eprintln!("dtls error after connect: {e:?}");
                    }
                }
            }
        }
        if Instant::now() >= timer {
            let _ = dtls.handle_timeout(Instant::now());
        }
        pump(&mut iotc, &mut dtls, &mut timer, &mut connected, &mut app);
        for p in app.drain(..) {
            if p.len() >= 0x1c && p[0] == 0 && p[1] == 0x21 {
                let got = u32::from_le_bytes([p[0x14], p[0x15], p[0x16], p[0x17]]);
                if stage == 0 && (got == nonce || got == nonce.wrapping_add(1)) {
                    if p[0x18] != 0 {
                        eprintln!("FAIL av: login rejected, state {}", p[0x18]);
                        close(&iotc);
                        exit(1);
                    }
                    println!("av: logged in {} ms after DTLS", t2.elapsed().as_millis());
                    stage = 1;
                }
            } else if p.len() >= 0x0e && p[0] == 0x09 {
                av.on_ack(u16::from_le_bytes([p[0x0c], p[0x0d]]));
            } else if p.len() >= 0x10 && p[0] == 0x0c {
                let b = if p[1] & 0x08 != 0 { 0x10 } else { 0x08 };
                let Some(f) = p.get(b + 8..) else { continue };
                if f.len() < 0x18 || f[0] != 0 {
                    continue;
                }
                let frame = u16::from_le_bytes([f[2], f[3]]);
                if frame != av.ctrl_in.wrapping_add(1) {
                    continue;
                }
                av.ctrl_in = frame;
                let typ = u32::from_le_bytes([f[0x14], f[0x15], f[0x16], f[0x17]]);
                let body = u32::from_le_bytes([f[8], f[9], f[10], f[11]]) as usize;
                let data = &f[0x18..(0x18 + body.saturating_sub(4)).min(f.len())];
                if let Some(i) = waiting.iter().position(|w| w.0 == typ) {
                    let (_, sent, name) = waiting.remove(i);
                    if name.starts_with("DEVINFO") {
                        let text: String = data.iter().take(24)
                            .map(|&c| if c.is_ascii_graphic() { c as char } else { '.' }).collect();
                        println!("av: {name} reply after {} ms: {text}", sent.elapsed().as_millis());
                    } else {
                        println!("av: {name} reply 0x{typ:x} after {} ms, {} bytes", sent.elapsed().as_millis(), data.len());
                    }
                } else {
                    println!("av: push 0x{typ:x}, {} bytes", data.len());
                }
            }
        }
    }
}

/// The client side of the AV control stream: our frames until the camera acknowledges them, and
/// the camera's frames we have taken in order.
struct Av {
    counter: u16,
    next_frame: u32,
    ctrl_in: u16,
    ack_seq: u16,
    acked: u16,
    out: Vec<(u32, Vec<u8>, Instant)>,
    retransmits: u32,
    max_in_flight: usize,
}

impl Default for Av {
    fn default() -> Self {
        Av { counter: 0, next_frame: 0, ctrl_in: 0xffff, ack_seq: 0, acked: 0xffff, out: Vec::new(),
             retransmits: 0, max_in_flight: 0 }
    }
}

impl Av {
    fn send(&mut self, dtls: &mut Dtls, typ: u32, payload: &[u8], now: Instant) {
        let pkt = control_frame(self.counter, self.next_frame, typ, payload);
        self.counter = self.counter.wrapping_add(1);
        dtls.send_application_data(&pkt).unwrap();
        self.out.push((self.next_frame, pkt, now));
        self.max_in_flight = self.max_in_flight.max(self.out.len());
        self.next_frame += 1;
    }

    fn retransmit(&mut self, dtls: &mut Dtls, now: Instant) {
        for (_, pkt, sent) in self.out.iter_mut() {
            if now.duration_since(*sent) >= Duration::from_millis(60) {
                pkt[4..6].copy_from_slice(&self.counter.to_le_bytes());
                self.counter = self.counter.wrapping_add(1);
                dtls.send_application_data(pkt).unwrap();
                *sent = now;
                self.retransmits += 1;
            }
        }
    }

    fn on_ack(&mut self, acked: u16) {
        if acked == 0xffff {
            return;
        }
        if self.acked == 0xffff || (acked.wrapping_sub(self.acked) as i16) > 0 {
            self.acked = acked;
        }
        let a = self.acked;
        self.out.retain(|(frame, _, _)| ((*frame as u16).wrapping_sub(a) as i16) > 0);
    }

    fn send_ack(&mut self, dtls: &mut Dtls, ms: u16) {
        dtls.send_application_data(&ack(self.counter, self.ctrl_in, self.ack_seq, ms)).unwrap();
        self.counter = self.counter.wrapping_add(1);
        self.ack_seq = self.ack_seq.wrapping_add(1);
    }
}

fn close(iotc: &Iotc) {
    if let Some(peer) = iotc.peer {
        for _ in 0..3 {
            iotc.send(peer, iotc.token_datagram(0x0417), true);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kit_worked_example() {
        // tutk-kit/docs/iotc/15-wire-format.md: one block under a test key.
        let key: [u8; 16] = [0x44, 0x33, 0x22, 0x11, 0x88, 0x77, 0x66, 0x55, 0xcc, 0xbb, 0xaa, 0x99, 0x00, 0xff, 0xee, 0xdd];
        let plain: [u8; 16] = [4, 3, 2, 1, 8, 7, 6, 5, 0xc, 0xb, 0xa, 9, 0x10, 0xf, 0xe, 0xd];
        let mut b = [0u32; 4];
        for i in 0..4 {
            b[i] = word(&plain, 4 * i).rotate_right(ROT[i]) ^ word(&key, 4 * i);
        }
        let mut out = Vec::new();
        for y in permute(b) {
            out.extend_from_slice(&y.to_le_bytes());
        }
        assert_eq!(out, [0xc3, 0x27, 0xb9, 0xf4, 0x5d, 0xe1, 0xdc, 0x2e, 0xe6, 0xc8, 0x78, 0x54, 0x2b, 0x22, 0x9c, 0x60]);
    }

    #[test]
    fn round_trip() {
        let inv = inverse_map();
        for len in 0..200 {
            let orig: Vec<u8> = (0..len).map(|i| (i * 7 + 3) as u8).collect();
            for prefix in [len, 0x40] {
                let mut b = orig.clone();
                scramble(&mut b, prefix);
                unscramble(&mut b, prefix, &inv);
                assert_eq!(b, orig, "len {len} prefix {prefix}");
            }
        }
    }
}
