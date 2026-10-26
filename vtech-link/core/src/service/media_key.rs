//! The live-view key exchange (`0x820`/`0x821`) and the session master it yields. Port of
//! `vtech_lan/crypto.py` `KeyExchange`; kit `docs/vtech-lan-kit/docs/50-media-decryption.md`.

use hmac::{Hmac, KeyInit, Mac};
use sha2::Sha256;
use x25519_dalek::{PublicKey, StaticSecret};
use zeroize::Zeroizing;

use crate::error::LinkError;

pub const OP_KEY_HELLO: u32 = 0x820;
pub const OP_KEY_REPLY: u32 = 0x821;
const REPLY_LEN: usize = 76;

/// The per-link media master key. It never leaves the core and is never formatted.
pub struct MasterKey(Zeroizing<[u8; 32]>);

impl MasterKey {
    pub(crate) fn bytes(&self) -> &[u8; 32] {
        &self.0
    }
}

impl std::fmt::Debug for MasterKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("MasterKey(..)")
    }
}

/// Our side of one exchange: an ephemeral X25519 secret.
pub struct KeyExchange {
    secret: StaticSecret,
}

impl KeyExchange {
    /// `secret` comes from the link's `Rng`.
    pub fn new(secret: [u8; 32]) -> Self {
        Self {
            secret: StaticSecret::from(secret),
        }
    }

    /// The `0x820` payload: u32 1, then our public key (36 bytes).
    pub fn hello(&self) -> [u8; 36] {
        let mut out = [0u8; 36];
        out[..4].copy_from_slice(&1u32.to_le_bytes());
        out[4..].copy_from_slice(PublicKey::from(&self.secret).as_bytes());
        out
    }

    /// The master from the camera's `0x821`: `HMAC-SHA256(key = salt, X25519(secret, camera key))`.
    /// The reply needs status 0, the camera's key at `+12` and the salt at `+44`, neither all zero.
    pub fn master(self, reply: &[u8]) -> Result<MasterKey, LinkError> {
        let Some(reply) = reply
            .get(..REPLY_LEN)
            .and_then(|r| <&[u8; REPLY_LEN]>::try_from(r).ok())
        else {
            return Err(LinkError::Protocol("0x821 reply too short"));
        };
        let status = i32::from_le_bytes([reply[0], reply[1], reply[2], reply[3]]);
        if status != 0 {
            log::warn!("live-view key exchange refused: 0x821 status {status}");
            return Err(LinkError::Protocol("0x821 key exchange refused"));
        }
        let mut camera = [0u8; 32];
        camera.copy_from_slice(&reply[12..44]);
        let salt = &reply[44..76];
        if camera == [0; 32] || salt.iter().all(|&b| b == 0) {
            return Err(LinkError::Protocol("0x821 carries no key material"));
        }
        let shared = Zeroizing::new(self.secret.diffie_hellman(&PublicKey::from(camera)).to_bytes());
        let mut mac = Hmac::<Sha256>::new_from_slice(salt).map_err(|_| LinkError::Protocol("0x821 salt"))?;
        mac.update(shared.as_ref());
        let mut master = Zeroizing::new([0u8; 32]);
        master.copy_from_slice(&mac.finalize().into_bytes());
        Ok(MasterKey(master))
    }
}
