"""vtech_lan — LAN-local protocol library for the VTech RM5754HD (ThroughTek/TUTK Kalay).

No Home Assistant imports: usable and testable standalone. Specification: ../vtech-lan-kit/docs/.

    from vtech_lan import sdk, Session, ops, provision
    c = provision.provision(qr_text)        # the pairing QR -> uid, av_password, authkey
    s = Session(sdk.load(libdir), c.uid, c.authkey, c.av_password, on_push=print)
    s.connect(); s.get_params(); s.close()
"""
from . import ops, provision
from .errors import (
    AuthFailed,
    CapacityExceeded,
    NotLanDirect,
    ProtocolError,
    RequestTimeout,
    SdkMismatch,
    SessionGone,
    Unlicensed,
    Unreachable,
    UnsupportedHost,
    VtechError,
)
from .session import PARAMS_UPDATE, Push, Session

__all__ = [
    "AuthFailed", "CapacityExceeded", "NotLanDirect", "PARAMS_UPDATE", "ProtocolError", "Push", "RequestTimeout",
    "SdkMismatch", "Session", "SessionGone", "Unlicensed", "Unreachable", "UnsupportedHost",
    "VtechError", "ops", "provision",
]
