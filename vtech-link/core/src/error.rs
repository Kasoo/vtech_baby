//! The one error type every core call returns. Each variant maps onto one of the integration's
//! exception classes (`docs/vtech-link-design/75-python-api.md`).

/// Why the camera refused an IOTC connect (the `0x602` result byte).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IotcReject {
    /// Result 3: the camera has no free session.
    MaxSessions,
    /// Result 4.
    Port,
    /// Result 5: wrong authKey.
    AuthKey,
    /// Result 6, a short (no-auth) reply, or a camera too old for key authentication.
    NoKeyAuth,
    Other(u8),
}

impl IotcReject {
    pub fn from_result(result: u8) -> Self {
        match result {
            3 => Self::MaxSessions,
            4 => Self::Port,
            5 => Self::AuthKey,
            6 => Self::NoKeyAuth,
            other => Self::Other(other),
        }
    }
}

/// Where the AV login failed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AvAuthStage {
    /// The DTLS handshake ended with this alert, or went silent (`None`), as it does for a wrong
    /// AV password.
    Dtls(Option<u8>),
    /// The login reply carried this non-zero state.
    LoginRejected(u32),
}

/// Why an established link ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CloseReason {
    /// The camera sent an IOTC close.
    RemoteClose,
    /// Nothing heard from the camera for the silence limit.
    RemoteTimeout,
    /// A DTLS alert after the handshake.
    DtlsAlert(u8),
    /// Closed from our side.
    Local,
    /// The reactor thread panicked or could not start.
    ThreadFailed,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum LinkError {
    #[error("camera unreachable: no LAN search reply")]
    Unreachable,
    #[error("camera refused the IOTC connect: {0:?}")]
    IotcRejected(IotcReject),
    #[error("AV authentication failed: {0:?}")]
    AvAuthFailed(AvAuthStage),
    #[error("link closed: {0:?}")]
    Closed(CloseReason),
    #[error("timed out")]
    Timeout,
    #[error("protocol error: {0}")]
    Protocol(&'static str),
    #[error("no free channel or link slot")]
    Capacity,
    #[error("invalid argument: {0}")]
    InvalidArgument(&'static str),
    #[error("socket error: {0:?}")]
    Io(std::io::ErrorKind),
}
