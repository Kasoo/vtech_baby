"""Media crypto: known-answer frame-key unwraps from a live capture, plus round-trips.

The vectors come from one ephemeral live-view session captured 2026-09-20. They are session keys, not
credentials.
"""
import struct

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import camera_crypto as crypto
from vtech_lan.errors import ProtocolError

MASTER = bytes.fromhex("93d03230faf4c28a612638a0e345f862ec5c0598d7f1230e8db370b3beaadbdb")
VECTORS = [   # (wrapped_key, nonce, frame_key)
    ("2ea4656d895e2f5a8d154ad4a8cd7f3538baaa4edd9f548420b9199febc7aae3", "8edf439ee2b4beb3472c0702",
     "27e9bf70e9cfc60bfdafb7c492fa8cf0542e8123690bd19ba64290a04463a946"),
    ("a945a5a345344f4860c5c8f16562c4d45862393723eaef7954e4d737452a7c47", "2e446fb96bd63bb241861485",
     "952ae1e50cb4944554c288e0f098e889a85429f1de241439c5fa59f748a421c5"),
    ("6fb568d0ccd9034a21f38a7c1a6a83c97f8fb5f1ebffc4668db45db534686ddc", "067119ab99cf6d33327a36bc",
     "a5dd1e243d1d534d9ca774f214d01d300c19946cf55add792f006551061f430e"),
]


@pytest.mark.parametrize("wrapped,nonce,frame_key", VECTORS)
def test_unwrap_known_answers(wrapped, nonce, frame_key):
    assert crypto.unwrap_frame_key(bytes.fromhex(wrapped), bytes.fromhex(nonce), MASTER).hex() == frame_key


def test_encrypt_decrypt_roundtrip():
    body = bytes(range(160))
    frame = crypto.encrypt_frame(body, MASTER)
    assert len(frame) == crypto.HEADER_LEN + len(body)
    assert crypto.decrypt_frame(frame, MASTER) == body
    tampered = frame[:-1] + bytes([frame[-1] ^ 1])
    with pytest.raises(InvalidTag):
        crypto.decrypt_frame(tampered, MASTER)
    with pytest.raises(InvalidTag):
        crypto.decrypt_frame(frame, bytes(32))
    with pytest.raises(ProtocolError):
        crypto.decrypt_frame(frame[:59], MASTER)


def fake_camera_reply(client_hello: bytes, status: int = 0):
    """Play the camera's side of 0x820/0x821: returns (reply, camera's view of the master)."""
    assert len(client_hello) == 36 and struct.unpack_from("<I", client_hello)[0] == 1
    cam = X25519PrivateKey.generate()
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    shared = cam.exchange(X25519PublicKey.from_public_bytes(client_hello[4:]))
    salt = bytes(range(1, 33))
    reply = (struct.pack("<ii", status, 1) + bytes(4)
             + cam.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw) + salt)
    return reply, crypto.session_master(salt, shared)


def test_key_exchange_agrees_with_camera():
    kx = crypto.KeyExchange.new()
    reply, cam_master = fake_camera_reply(kx.hello())
    assert kx.master(reply) == cam_master


def test_key_reply_rejections():
    kx = crypto.KeyExchange.new()
    reply, _ = fake_camera_reply(kx.hello(), status=-1)
    with pytest.raises(ProtocolError):
        kx.master(reply)
    with pytest.raises(ProtocolError):
        crypto.parse_key_reply(bytes(40))
    with pytest.raises(ProtocolError):
        crypto.parse_key_reply(bytes(76))            # GO but no key material
