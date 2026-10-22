#![no_main]
use libfuzzer_sys::fuzz_target;
use vtech_link_core::service::frame_crypto;
use vtech_link_core::service::media_key::KeyExchange;

/// A valid 76-byte 0x821 reply: status 0, nonzero key at 12 and salt at 44.
fn valid_reply() -> [u8; 76] {
    let mut r = [0u8; 76];
    r[12..44].fill(0x55);
    r[44..76].fill(0xc8);
    r
}

fuzz_target!(|data: &[u8]| {
    let _ = KeyExchange::new([1; 32]).master(data);
    // Fixed master, so the frame bytes are the only input.
    if let Ok(master) = KeyExchange::new([1; 32]).master(&valid_reply()) {
        if let Some(plain) = frame_crypto::decrypt(&master, data) {
            assert!(plain.len() + frame_crypto::HEADER_LEN == data.len());
        }
    }
});
