//! Per-frame media crypto. Port of `vtech_lan/crypto.py`:
//!
//! ```text
//! frame     = wrapped_key[32] || nonce[12] || tag[16] || body
//! frame_key = wrapped_key XOR ChaCha20(master, nonce, counter 0)[0..32]
//! plaintext = ChaCha20-Poly1305(frame_key).decrypt(nonce, body, tag, no AAD)
//! ```

use chacha20::ChaCha20;
use chacha20::cipher::{KeyIvInit, StreamCipher};
use chacha20poly1305::aead::AeadInPlace;
use chacha20poly1305::{ChaCha20Poly1305, KeyInit, Nonce, Tag};
use zeroize::Zeroizing;

use super::media_key::MasterKey;

/// wrapped key 32 + nonce 12 + tag 16.
pub const HEADER_LEN: usize = 60;

/// XOR the frame key with the master's keystream for `nonce`: wraps and unwraps alike.
fn wrap(master: &MasterKey, nonce: &[u8; 12], key: &mut [u8; 32]) {
    let mut stream = ChaCha20::new(master.bytes().into(), nonce.into());
    stream.apply_keystream(key);
}

/// Decrypt one media frame; `None` if it is shorter than the header or fails its tag.
pub fn decrypt(master: &MasterKey, frame: &[u8]) -> Option<Vec<u8>> {
    let (header, body) = frame.split_at_checked(HEADER_LEN)?;
    let mut key = Zeroizing::new([0u8; 32]);
    key.copy_from_slice(header.get(..32)?);
    let nonce: [u8; 12] = header.get(32..44)?.try_into().ok()?;
    let tag = Tag::clone_from_slice(header.get(44..60)?);
    wrap(master, &nonce, &mut key);
    let mut plain = body.to_vec();
    ChaCha20Poly1305::new(key.as_ref().into())
        .decrypt_in_place_detached(Nonce::from_slice(&nonce), b"", &mut plain, &tag)
        .ok()?;
    Some(plain)
}

/// Encrypt one talk-back frame under a fresh `frame_key` and `nonce` (from the link's `Rng`).
pub fn encrypt(master: &MasterKey, frame_key: [u8; 32], nonce: [u8; 12], plain: &[u8]) -> Vec<u8> {
    let frame_key = Zeroizing::new(frame_key);
    let mut out = Vec::with_capacity(HEADER_LEN + plain.len());
    let mut body = plain.to_vec();
    // Encryption with a 32-byte key and a 12-byte nonce can't fail below 256 GiB.
    let tag = ChaCha20Poly1305::new(frame_key.as_ref().into())
        .encrypt_in_place_detached(Nonce::from_slice(&nonce), b"", &mut body)
        .unwrap_or_default();
    let mut wrapped = *frame_key;
    wrap(master, &nonce, &mut wrapped);
    out.extend_from_slice(&wrapped);
    out.extend_from_slice(&nonce);
    out.extend_from_slice(&tag);
    out.extend_from_slice(&body);
    out
}
