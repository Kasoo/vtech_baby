"""SDK return codes and the typed exceptions they map to.

Authority: vtech-lan-kit/docs/90-ha-runtime.md §4 (what each code means and how an integration
should react). Every exception carries the originating SDK call and return code.
"""
from __future__ import annotations

# ── IOTC layer ───────────────────────────────────────────────────────────────
IOTC_ER_ALREADY_INITIALIZED = -3
IOTC_ER_UNLICENSE = -10
IOTC_ER_TIMEOUT = -13
IOTC_ER_INVALID_SID = -14
IOTC_ER_EXCEED_MAX_SESSION = -18
IOTC_ER_CAN_NOT_FIND_DEVICE = -19
IOTC_ER_SESSION_CLOSE_BY_REMOTE = -22
IOTC_ER_REMOTE_TIMEOUT_DISCONNECT = -23
IOTC_ER_DEVICE_NOT_LISTENING = -24
IOTC_ER_NETWORK_UNREACHABLE = -41
IOTC_ER_DEVICE_EXCEED_MAX_SESSION = -48
IOTC_ER_DEVICE_REJECT_BY_WRONG_AUTH_KEY = -68
IOTC_ER_DEVICE_OFFLINE = -90

# ── AV layer ─────────────────────────────────────────────────────────────────
AV_ER_INVALID_ARG = -20000
AV_ER_EXCEED_MAX_CHANNEL = -20002
AV_ER_EXCEED_MAX_SIZE = -20006
AV_ER_WRONG_VIEWACCorPWD = -20009
AV_ER_INVALID_SID = -20010
AV_ER_TIMEOUT = -20011
AV_ER_DATA_NOREADY = -20012
AV_ER_INCOMPLETE_FRAME = -20013
AV_ER_LOSED_THIS_FRAME = -20014
AV_ER_SESSION_CLOSE_BY_REMOTE = -20015
AV_ER_REMOTE_TIMEOUT_DISCONNECT = -20016
AV_ER_SENDIOCTRL_ALREADY_CALLED = -20021   # another avSendIOCtrl is in progress on this channel
AV_ER_CLEANBUF_ALREADY_CALLED = -20029     # a buffer clean is in progress on this channel (name unconfirmed)
AV_ER_ALREADY_INITIALIZED = -20031
AV_ER_NOT_SUPPORT = -20033

#: "Nothing yet" on a receive — keep looping.
NO_DATA = frozenset({AV_ER_TIMEOUT, AV_ER_DATA_NOREADY})
#: A damaged media frame — skip it.
BAD_FRAME = frozenset({AV_ER_INCOMPLETE_FRAME, AV_ER_LOSED_THIS_FRAME})
#: The channel is momentarily busy — retry the send (these are the transient ones).
SEND_BUSY = frozenset({AV_ER_SENDIOCTRL_ALREADY_CALLED, AV_ER_CLEANBUF_ALREADY_CALLED})


class VtechError(Exception):
    """Base class. `call` is the SDK function (or protocol step), `code` its return code if any."""

    def __init__(self, message: str, *, call: str | None = None, code: int | None = None):
        super().__init__(message)
        self.call = call
        self.code = code


class UnsupportedHost(VtechError):
    """The host or library set can't run the SDK (arch, page size, arch mismatch)."""


class SdkMismatch(VtechError):
    """The process already loaded the SDK from a different directory or with a different license."""


class Unlicensed(VtechError):
    """License key missing or invalid."""


class Unreachable(VtechError):
    """The camera can't be reached (off, off the network, or not on the same L2 segment). Retry with backoff."""


class NotLanDirect(Unreachable):
    """The SDK reached the camera some way other than LAN-direct (P2P or ThroughTek relay). The session is
    refused: this integration is LAN-only. The companion's egress policy should make this impossible, so
    seeing it means that policy has a gap (vtech-lan-kit/docs/00-prerequisites.md, "LAN-only")."""


class AuthFailed(VtechError):
    """Wrong authKey or AV credentials. Do not retry in a loop — ask the user for new credentials."""


class CapacityExceeded(VtechError):
    """Session or channel capacity exhausted, locally or on the camera."""


class SessionGone(VtechError):
    """An established session is gone. Tear down and reconnect."""


class RequestTimeout(VtechError):
    """No reply to a request within its timeout."""


class ProtocolError(VtechError):
    """A reply was malformed, or the camera acknowledged a SET as failed."""


_CLASSES: dict[int, type[VtechError]] = {
    IOTC_ER_UNLICENSE: Unlicensed,
    IOTC_ER_DEVICE_OFFLINE: Unreachable,
    IOTC_ER_TIMEOUT: Unreachable,
    IOTC_ER_CAN_NOT_FIND_DEVICE: Unreachable,
    IOTC_ER_NETWORK_UNREACHABLE: Unreachable,
    IOTC_ER_DEVICE_NOT_LISTENING: Unreachable,
    IOTC_ER_DEVICE_REJECT_BY_WRONG_AUTH_KEY: AuthFailed,
    AV_ER_WRONG_VIEWACCorPWD: AuthFailed,
    IOTC_ER_EXCEED_MAX_SESSION: CapacityExceeded,
    IOTC_ER_DEVICE_EXCEED_MAX_SESSION: CapacityExceeded,
    AV_ER_EXCEED_MAX_CHANNEL: CapacityExceeded,
    AV_ER_SESSION_CLOSE_BY_REMOTE: SessionGone,
    AV_ER_REMOTE_TIMEOUT_DISCONNECT: SessionGone,
    IOTC_ER_SESSION_CLOSE_BY_REMOTE: SessionGone,
    IOTC_ER_REMOTE_TIMEOUT_DISCONNECT: SessionGone,
    AV_ER_INVALID_SID: SessionGone,
    IOTC_ER_INVALID_SID: SessionGone,
    AV_ER_INVALID_ARG: SessionGone,      # on a receive: the channel index is no longer valid
}


def error_for(code: int, call: str) -> VtechError:
    """The exception for a negative SDK return code. Unlisted codes give a plain VtechError."""
    cls = _CLASSES.get(code, VtechError)
    return cls(f"{call} failed rc={code}", call=call, code=code)


def check(code: int, call: str) -> int:
    """Return `code` if non-negative, else raise the mapped exception."""
    if code < 0:
        raise error_for(code, call)
    return code
