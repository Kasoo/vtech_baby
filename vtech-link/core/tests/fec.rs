//! The FEC rebuild checked against what tutk-host's `av_fec_on_packet` hands to reassembly.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)]

mod common;

use common::Fixtures;
use vtech_link_core::av::fec::{Fec, GroupHeader};

fn check(k: u8) {
    let fx = Fixtures::load();
    let missing = usize::from(fx.get(&format!("av.fec.k{k}.missing"))[0]);
    let mut fec = Fec::new();
    let mut fed = 0;
    for i in 0..k {
        if usize::from(i) == missing {
            continue;
        }
        let shard = fx.get(&format!("av.fec.k{k}.shard{i}"));
        let header = GroupHeader {
            group: 0x1234,
            index: i,
            k,
        };
        assert!(fec.on_packet(header, shard).is_none());
        fed += 1;
    }
    assert_eq!(fed, usize::from(k) - 1);
    let header = GroupHeader {
        group: 0x1234,
        index: k,
        k,
    };
    let rebuilt = fec.on_packet(header, fx.get(&format!("av.fec.k{k}.parity")));
    assert_eq!(rebuilt.as_deref(), Some(fx.get(&format!("av.fec.k{k}.rebuilt"))));
}

#[test]
fn k20_matches_tutk_host() {
    check(20);
}

#[test]
fn k25_matches_tutk_host() {
    check(25);
}
