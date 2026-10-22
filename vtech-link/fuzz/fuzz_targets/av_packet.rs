#![no_main]
use libfuzzer_sys::fuzz_target;
use vtech_link_core::av::packet::{ctrl_fragment, envelope, login_reply};

fuzz_target!(|data: &[u8]| {
    let nonce = data
        .get(..4)
        .and_then(|b| <[u8; 4]>::try_from(b).ok())
        .map_or(0x1122_3344, u32::from_le_bytes);
    let _ = login_reply(data, nonce);
    let _ = login_reply(data, 0x1122_3344);
    if let Some(env) = envelope(data) {
        assert!(env.group.len() == 8 && env.frame.len() <= data.len());
        let _ = ctrl_fragment(env.frame);
    }
    if let Some(f) = ctrl_fragment(data) {
        assert!(f.index < f.count && f.body.len() <= data.len());
    }
});
