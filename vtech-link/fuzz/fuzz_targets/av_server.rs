#![no_main]
use std::time::{Duration, Instant};

use arbitrary::Unstructured;
use libfuzzer_sys::fuzz_target;
use vtech_link_core::av::packet;
use vtech_link_core::av::server::{AvServer, ServerConfig};

fuzz_target!(|data: &[u8]| {
    let mut u = Unstructured::new(data);
    let logged_in: bool = u.arbitrary().unwrap_or(false);
    let t0 = Instant::now();
    let mut s = AvServer::new(ServerConfig::default(), t0, t0 + Duration::from_secs(30));
    if logged_in {
        let l = packet::login(0, 0x1122_3344, "admin", "testpass", packet::LOGIN_WIDTH as u16);
        s.handle_datagram(t0, 1, &l);
    }
    let mut now = t0;
    while !u.is_empty() {
        let ms: u8 = u.arbitrary().unwrap_or(0);
        let datatype: u8 = u.arbitrary().unwrap_or(1);
        now += Duration::from_millis(u64::from(ms));
        let len = u.int_in_range(0..=u.len().min(1500)).unwrap_or(0);
        let Ok(bytes) = u.bytes(len) else { break };
        s.handle_datagram(now, datatype, bytes);
        while s.poll_transmit().is_some() {}
        while s.poll_event().is_some() {}
    }
});
