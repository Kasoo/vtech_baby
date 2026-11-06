"""vtech_lan — LAN-local protocol library for the VTech RM5754HD (ThroughTek/TUTK Kalay).

No Home Assistant imports: usable and testable standalone. Specification: docs/vtech-lan-kit/docs/.

    from vtech_lan import link, LinkSession, ops, provision
    c = provision.provision(qr_text)        # the pairing QR -> uid, av_password, authkey
    s = LinkSession(link.load(), c.uid, c.authkey, c.av_password, on_push=print)
    s.connect(); s.get_params(); s.close()
"""
from . import ops, provision
from .errors import (
    AuthFailed,
    CapacityExceeded,
    MediaError,
    ProtocolError,
    RequestTimeout,
    SessionGone,
    Unreachable,
    UnsupportedHost,
    VtechError,
)
from .session import PARAMS_UPDATE, LinkSession, Push, SessionBase

__all__ = [
    "AuthFailed", "CapacityExceeded", "LinkSession", "MediaError", "PARAMS_UPDATE",
    "ProtocolError", "Push", "RequestTimeout", "SessionBase", "SessionGone", "Unreachable", "UnsupportedHost",
    "VtechError", "ops", "provision",
]
