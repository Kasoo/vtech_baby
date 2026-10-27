"""Live-view key exchange and per-frame media crypto, for the fake camera (`fake_camera.FakeCamera`) and
the fake module (`fake_link`). The real module does this in Rust.

Authority: docs/vtech-lan-kit/docs/50-media-decryption.md. Scheme:
    S         = X25519(n, camera_pubkey)                  # n = our ephemeral scalar
    master    = HMAC-SHA256(key = salt, msg = S)          # per session
    frame     = [wrapped_key:32][nonce:12][tag:16][body]
    frame_key = wrapped_key XOR ChaCha20-IETF-keystream(master, nonce, counter 0)
    plaintext = ChaCha20-Poly1305-IETF-decrypt(frame_key, nonce, body || tag, no AAD)
The uplink (talk-back) uses the same layout with a fresh random frame_key and nonce per frame.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from vtech_lan.errors import ProtocolError

HEADER_LEN = 60          # wrapped_key 32 + nonce 12 + tag 16


@dataclass
class KeyExchange:
    """Our side of one 0x820/0x821 exchange: an ephemeral X25519 keypair."""
    private: X25519PrivateKey
    public: bytes

    @classmethod
    def new(cls) -> "KeyExchange":
        k = X25519PrivateKey.generate()
        return cls(k, k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))

    def hello(self) -> bytes:
        """Round-2 0x820 payload: word0 = 1, then our 32-byte public key (36 bytes)."""
        return struct.pack("<I", 1) + self.public

    def master(self, reply: bytes) -> bytes:
        """Derive the session master from a 0x821 GO reply."""
        cam_pub, salt = parse_key_reply(reply)
        return session_master(salt, self.private.exchange(X25519PublicKey.from_public_bytes(cam_pub)))


def parse_key_reply(reply: bytes) -> tuple[bytes, bytes]:
    """0x821 → (camera public key @12, salt @44). Raises ProtocolError unless it is a valid GO."""
    if len(reply) < 76:
        raise ProtocolError(f"0x821 too short ({len(reply)} < 76 bytes)")
    (status,) = struct.unpack_from("<i", reply, 0)
    if status != 0:
        raise ProtocolError(f"0x821 status {status}: key exchange not accepted", code=status)
    cam_pub, salt = reply[12:44], reply[44:76]
    if cam_pub == bytes(32) or salt == bytes(32):
        raise ProtocolError("0x821 carries no key material")
    return cam_pub, salt


def session_master(salt: bytes, shared: bytes) -> bytes:
    return hmac.new(salt, shared, hashlib.sha256).digest()


def _keystream(key: bytes, nonce12: bytes, n: int) -> bytes:
    # cryptography's ChaCha20 takes a 16-byte nonce: 4-byte LE block counter (0) || 12-byte IETF nonce.
    cipher = Cipher(algorithms.ChaCha20(key, bytes(4) + nonce12), mode=None)
    return cipher.encryptor().update(bytes(n))


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def unwrap_frame_key(wrapped_key: bytes, nonce: bytes, master: bytes) -> bytes:
    return _xor(wrapped_key, _keystream(master, nonce, len(wrapped_key)))


def decrypt_frame(frame: bytes, master: bytes) -> bytes:
    """One wire frame → plaintext. Raises cryptography's InvalidTag on a bad key or damaged frame,
    and ProtocolError if the frame is too short to carry the header."""
    if len(frame) < HEADER_LEN:
        raise ProtocolError(f"frame too short ({len(frame)} < {HEADER_LEN} bytes)")
    wrapped_key, nonce, tag, body = frame[:32], frame[32:44], frame[44:60], frame[60:]
    return ChaCha20Poly1305(unwrap_frame_key(wrapped_key, nonce, master)).decrypt(nonce, body + tag, None)


def encrypt_frame(plain: bytes, master: bytes) -> bytes:
    """Talk-back uplink: plaintext → [wrapped_key][nonce][tag][body] with a fresh frame_key and nonce."""
    frame_key, nonce = os.urandom(32), os.urandom(12)
    ct = ChaCha20Poly1305(frame_key).encrypt(nonce, plain, None)        # body || tag
    return _xor(frame_key, _keystream(master, nonce, 32)) + nonce + ct[-16:] + ct[:-16]
