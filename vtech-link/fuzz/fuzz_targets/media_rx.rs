#![no_main]
use std::time::{Duration, Instant};

use arbitrary::Unstructured;
use libfuzzer_sys::fuzz_target;
use vtech_link_core::av::media_rx::{MediaConfig, MediaRx};

fuzz_target!(|data: &[u8]| {
    let mut u = Unstructured::new(data);
    let mut rx = MediaRx::new(MediaConfig::default());
    let t0 = Instant::now();
    let mut now = t0;
    while !u.is_empty() {
        let ms: u8 = u.arbitrary().unwrap_or(0);
        let envelope: bool = u.arbitrary().unwrap_or(false);
        now += Duration::from_millis(u64::from(ms) * 4);
        let len = u.int_in_range(0..=u.len().min(1500)).unwrap_or(0);
        let Ok(bytes) = u.bytes(len) else { break };
        if envelope {
            let (group, frame) = bytes.split_at(bytes.len().min(8));
            rx.on_envelope(now, group, frame);
        } else {
            rx.on_fragment(now, bytes);
        }
        rx.handle_timeout(now);
        let _ = rx.ack_fields(now);
        while let Some(f) = rx.poll_frame() {
            // A frame is assembled from fragments of the input, so it can't outgrow what was fed in.
            assert!(f.data.len() <= data.len() * 2 + 65536);
            assert!(f.info.len() <= 16);
        }
    }
    let _ = rx.reset();
});
