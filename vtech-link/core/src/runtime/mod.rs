//! The runtime: one reactor thread per link, the caller-facing handles, the LAN-only rule.

mod driver;
pub mod egress;
pub mod link;
pub mod sync;

pub use link::{Exchange, Frame, Link, LinkConfig, LinkInfo, LinkStats, MediaReceiver, TalkBack, Timing};
