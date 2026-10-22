#![no_main]
use libfuzzer_sys::fuzz_target;
use vtech_link_core::iotc::codec::{self, Inbound};

fuzz_target!(|data: &[u8]| {
    let mut pkt = data.to_vec();
    if let Ok(inbound) = codec::decode(&mut pkt) {
        if let Inbound::Data { payload, .. } = inbound {
            assert!(payload.len() <= data.len());
        }
    }
});
