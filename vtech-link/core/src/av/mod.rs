//! The AV layer on IOTC channel 0: login, the control stream, media receive and parity recovery.

pub mod client;
pub mod control;
pub mod fec;
pub mod media_rx;
pub mod packet;
pub mod server;

pub use client::{AvClient, AvConfig, AvEvent, AvStats};
pub use control::SendId;
pub use media_rx::{EncryptedFrame, StreamKind};
