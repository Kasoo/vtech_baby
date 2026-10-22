#![no_main]
use std::net::{Ipv4Addr, SocketAddrV4};
use std::time::{Duration, Instant};

use arbitrary::Unstructured;
use libfuzzer_sys::fuzz_target;
use vtech_link_core::iotc::codec;
use vtech_link_core::iotc::{IotcConfig, IotcSession};

const TOKEN: [u8; 8] = [1, 2, 3, 4, 5, 6, 7, 8];
const CAMERA: SocketAddrV4 = SocketAddrV4::new(Ipv4Addr::new(192, 168, 1, 220), 41234);

/// A valid long 0x602 reply carrying our token, so the session reaches the connected state.
fn long_reply() -> Vec<u8> {
    let mut p = vec![0u8; 0xc8];
    p[0..2].copy_from_slice(&codec::MAGIC.to_le_bytes());
    p[2] = 0x1b;
    p[4..6].copy_from_slice(&0xb8u16.to_le_bytes());
    p[8..10].copy_from_slice(&codec::OP_LAN_REPLY.to_le_bytes());
    p[0x30..0x34].copy_from_slice(&0x0102_0304u32.to_le_bytes());
    p[0xbc..0xc4].copy_from_slice(&TOKEN);
    p[0xc4] = 1;
    codec::seal(&mut p);
    p
}

fuzz_target!(|data: &[u8]| {
    let mut u = Unstructured::new(data);
    let established: bool = u.arbitrary().unwrap_or(false);
    let Ok(mut s) = IotcSession::new(IotcConfig::default(), "TESTUID0123456789ABC", b"k3yAuth8", TOKEN) else {
        return;
    };
    let t0 = Instant::now();
    s.connect(t0, t0 + Duration::from_secs(15), &[Ipv4Addr::new(192, 168, 1, 255)]);
    if established {
        s.handle_datagram(t0, CAMERA, &mut long_reply());
    }
    let mut now = t0;
    while !u.is_empty() {
        let ms: u8 = u.arbitrary().unwrap_or(0);
        now += Duration::from_millis(u64::from(ms));
        let len = u.int_in_range(0..=u.len().min(2100)).unwrap_or(0);
        let Ok(bytes) = u.bytes(len) else { break };
        let mut pkt = bytes.to_vec();
        // Half the inputs get a valid checksum-free seal so they get past the magic check.
        if ms & 1 == 1 && pkt.len() >= codec::HEADER_LEN {
            pkt[0..2].copy_from_slice(&codec::MAGIC.to_le_bytes());
            pkt[3] &= !codec::FLAG_SCRAMBLED;
            codec::seal(&mut pkt);
        }
        s.handle_datagram(now, CAMERA, &mut pkt);
        while s.poll_transmit().is_some() {}
        while s.poll_event().is_some() {}
        s.handle_timeout(now);
    }
});
