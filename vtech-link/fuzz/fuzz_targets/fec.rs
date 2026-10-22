#![no_main]
use libfuzzer_sys::fuzz_target;
use vtech_link_core::av::fec::{Fec, GroupHeader};
use arbitrary::Unstructured;

fuzz_target!(|data: &[u8]| {
    let mut u = Unstructured::new(data);
    let mut fec = Fec::new();
    while !u.is_empty() {
        let len = u.int_in_range(0..=u.len().min(1500)).unwrap_or(0);
        let Ok(bytes) = u.bytes(len) else { break };
        // The header comes from the first bytes, the shard is the rest.
        if let Some(header) = GroupHeader::parse(bytes) {
            let _ = fec.on_packet(header, bytes.get(8..).unwrap_or(&[]));
        }
    }
    fec.reset();
});
