#![no_main]
use std::time::{Duration, Instant};

use arbitrary::Unstructured;
use libfuzzer_sys::fuzz_target;
use vtech_link_core::dtls::{DtlsClient, DtlsConfig};

fuzz_target!(|data: &[u8]| {
    let config = DtlsConfig {
        test_rng_seed: Some(1),
        ..Default::default()
    };
    let t0 = Instant::now();
    let Ok(mut c) = DtlsClient::new(config, "admin", "testpass", t0) else {
        return;
    };
    let mut u = Unstructured::new(data);
    let mut now = t0;
    while !u.is_empty() {
        let ms: u8 = u.arbitrary().unwrap_or(0);
        now += Duration::from_millis(u64::from(ms));
        let len = u.int_in_range(0..=u.len().min(1500)).unwrap_or(0);
        let Ok(bytes) = u.bytes(len) else { break };
        c.handle_datagram(now, bytes);
        while c.poll_transmit().is_some() {}
        while c.poll_event().is_some() {}
        c.handle_timeout(now);
    }
});
