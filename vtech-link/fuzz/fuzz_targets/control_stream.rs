#![no_main]
use libfuzzer_sys::fuzz_target;
use arbitrary::Unstructured;
use std::time::{Duration, Instant};
use vtech_link_core::av::control::ControlStream;
use vtech_link_core::av::packet::ctrl_fragment;

fuzz_target!(|data: &[u8]| {
    let mut u = Unstructured::new(data);
    let window: u8 = u.arbitrary().unwrap_or(1);
    let mut c = ControlStream::new(usize::from(window % 8) + 1, Duration::from_millis(60));
    let now = Instant::now();
    while !u.is_empty() {
        let len = u.int_in_range(0..=u.len().min(1200)).unwrap_or(0);
        let Ok(bytes) = u.bytes(len) else { break };
        // Mostly well-formed control frames: force the marker byte so ctrl_fragment parses them.
        let mut frame = bytes.to_vec();
        if let Some(b) = frame.first_mut() {
            *b = 0;
        }
        if let Some(frag) = ctrl_fragment(&frame) {
            c.on_fragment(frag);
        }
        let _ = c.take_ack_due();
        let _ = c.ctrl_in16();
        while c.poll_event().is_some() {}
        while c.poll_transmit().is_some() {}
        c.handle_timeout(now);
    }
});
