"""vtech_lan — LAN-local protocol library for the VTech RM5754HD (ThroughTek/TUTK Kalay).

No Home Assistant imports: usable and testable standalone. Specification: docs/vtech-lan-kit/docs/.

    from vtech_lan import sdk, Session, ops, provision
    c = provision.provision(qr_text)        # the pairing QR -> uid, av_password, authkey
    s = Session(sdk.load(libdir), c.uid, c.authkey, c.av_password, on_push=print)
    s.connect(); s.get_params(); s.close()
"""
from . import ops, provision
from .errors import (
    AuthFailed,
    CapacityExceeded,
    MediaError,
    NotLanDirect,
    ProtocolError,
    RequestTimeout,
    SdkMismatch,
    SessionGone,
    Unlicensed,
    Unreachable,
    UnsupportedHost,
    VtechError,
    WorkerDied,
)
from .session import PARAMS_UPDATE, LinkSession, Push, SdkSession, Session, SessionBase

__all__ = [
    "AuthFailed", "CapacityExceeded", "MediaError", "NotLanDirect", "PARAMS_UPDATE", "ProtocolError", "Push", "RequestTimeout",
    "LinkSession", "SdkMismatch", "SdkSession", "Session", "SessionBase", "SessionGone", "Unlicensed", "Unreachable", "UnsupportedHost",
    "VtechError", "WorkerDied", "ops", "provision",
]
