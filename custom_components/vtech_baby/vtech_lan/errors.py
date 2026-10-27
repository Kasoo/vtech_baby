"""The typed exceptions the integration acts on, and how `_vtech_link`'s errors map to them.

Authority: docs/vtech-lan-kit/docs/90-ha-runtime.md §4 (what each failure means and how an integration
should react).
"""
from __future__ import annotations


class VtechError(Exception):
    """Base class. `call` is the protocol step that failed, `code` the TUTK error code the module
    reports for it, if any."""

    def __init__(self, message: str, *, call: str | None = None, code: int | None = None):
        super().__init__(message)
        self.call = call
        self.code = code


class UnsupportedHost(VtechError):
    """The integration has no native module for this host's architecture and C library."""


class ModuleMismatch(VtechError):
    """The process already loaded the native module from a different path."""


class Unreachable(VtechError):
    """The camera can't be reached (off, off the network, or not on the same L2 segment). Retry with backoff."""


class AuthFailed(VtechError):
    """Wrong authKey or AV credentials. Do not retry in a loop — ask the user for new credentials."""


class CapacityExceeded(VtechError):
    """Session or channel capacity exhausted, locally or on the camera."""


class SessionGone(VtechError):
    """An established session is gone. Tear down and reconnect."""


class WorkerDied(SessionGone):
    """The worker process ended (a crash, or a restart), taking its sessions with it."""


class RequestTimeout(VtechError):
    """No reply to a request within its timeout."""


class ProtocolError(VtechError):
    """A reply was malformed, or the camera acknowledged a SET as failed."""


class MediaError(VtechError):
    """Media for talk-back can't be played: an unsupported URL, unreadable, or no audio in it."""


def from_link(err: Exception, call: str) -> VtechError:
    """The exception for a `_vtech_link` LinkError, by its `kind` and `detail`, carrying the TUTK error
    code the module reports."""
    kind, detail = getattr(err, "kind", "internal"), getattr(err, "detail", "")
    code = getattr(err, "code", None)
    cls: type[VtechError]
    if kind == "unreachable":
        cls = Unreachable
    elif kind == "iotc_rejected":
        cls = {"auth_key": AuthFailed, "max_sessions": CapacityExceeded}.get(detail, VtechError)
    elif kind == "av_auth_failed":
        cls = AuthFailed
    elif kind == "closed":
        cls = SessionGone
    elif kind == "timeout":
        cls = RequestTimeout
    elif kind == "protocol":
        cls = ProtocolError
    elif kind == "capacity":
        cls = CapacityExceeded
    else:                                       # invalid_argument, io, internal
        cls = VtechError
    text = f"{kind}: {detail}" if detail else kind
    return cls(f"{call} failed ({text})", call=call, code=code)
