#![no_main]
use std::time::{Duration, Instant};

use arbitrary::Unstructured;
use libfuzzer_sys::fuzz_target;
use vtech_link_core::av::packet;
use vtech_link_core::av::{AvClient, AvConfig};

const NONCE: u32 = 0x1122_3344;

fuzz_target!(|data: &[u8]| {
    let mut u = Unstructured::new(data);
    let logged_in: bool = u.arbitrary().unwrap_or(false);
    let t0 = Instant::now();
    let mut c = AvClient::new(AvConfig::new("admin", "testpass"), NONCE, t0, t0 + Duration::from_secs(30));
    if logged_in {
        let mut r = vec![0u8; 0x1c];
        r[1] = packet::LOGIN_REPLY;
        r[0x14..0x18].copy_from_slice(&NONCE.to_le_bytes());
        c.handle_packet(t0, &r);
    }
    let mut now = t0;
    while !u.is_empty() {
        let ms: u8 = u.arbitrary().unwrap_or(0);
        now += Duration::from_millis(u64::from(ms) * 4);
        let len = u.int_in_range(0..=u.len().min(1500)).unwrap_or(0);
        let Ok(bytes) = u.bytes(len) else { break };
        c.handle_packet(now, bytes);
        while c.poll_transmit().is_some() {}
        while c.poll_event().is_some() {}
        c.handle_timeout(now);
    }
    let _ = c.stats();
});
