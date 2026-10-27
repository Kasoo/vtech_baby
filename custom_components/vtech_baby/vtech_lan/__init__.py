"""vtech_lan — LAN-local protocol library for the VTech RM5754HD (ThroughTek/TUTK Kalay).

No Home Assistant imports: usable and testable standalone. Specification: docs/vtech-lan-kit/docs/.

    from vtech_lan import link, LinkSession, ops, provision
    c = provision.provision(qr_text)        # the pairing QR -> uid, av_password, authkey
    s = LinkSession(link.load(module_path), c.uid, c.authkey, c.av_password, on_push=print)
    s.connect(); s.get_params(); s.close()
"""
from . import ops, provision
from .errors import (
    AuthFailed,
    CapacityExceeded,
    MediaError,
    ModuleMismatch,
    ProtocolError,
    RequestTimeout,
    SessionGone,
    Unreachable,
    UnsupportedHost,
    VtechError,
    WorkerDied,
)
from .session import PARAMS_UPDATE, LinkSession, Push, SessionBase

__all__ = [
    "AuthFailed", "CapacityExceeded", "LinkSession", "MediaError", "ModuleMismatch", "PARAMS_UPDATE",
    "ProtocolError", "Push", "RequestTimeout", "SessionBase", "SessionGone", "Unreachable", "UnsupportedHost",
    "VtechError", "WorkerDied", "ops", "provision",
]
