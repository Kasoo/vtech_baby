//! The vtech-link protocol core: sans-IO state machines for IOTC, DTLS and AV, and the reactor that
//! drives them. Design: `docs/vtech-link-design/`.

#![forbid(unsafe_code)]

pub mod iotc;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
