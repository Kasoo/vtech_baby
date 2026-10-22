//! `LinkError` on the Python side: one exception class carrying `kind`, `detail` and the equivalent
//! legacy SDK `code` (`docs/vtech-link-design/75-python-api.md`, "Mapping errors").

use std::time::Duration;

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use vtech_link_core::LinkError as CoreError;
use vtech_link_core::error::{AvAuthStage, CloseReason, IotcReject};

pyo3::create_exception!(
    _vtech_link,
    LinkError,
    PyException,
    "A link failed or a call on it did not complete. Attributes: kind, detail, code."
);

/// The longest wait a call accepts; longer timeouts are cut to this.
const MAX_WAIT: Duration = Duration::from_secs(86_400);

/// `(kind, detail, legacy code)` for an error.
pub fn describe(e: &CoreError) -> (&'static str, String, Option<i32>) {
    match e {
        CoreError::Unreachable => ("unreachable", String::new(), Some(-13)),
        CoreError::IotcRejected(r) => {
            let (detail, code) = match r {
                IotcReject::AuthKey => ("auth_key".to_string(), Some(-68)),
                IotcReject::MaxSessions => ("max_sessions".to_string(), Some(-48)),
                IotcReject::NoKeyAuth => ("no_key_auth".to_string(), Some(-69)),
                IotcReject::Port => ("port".to_string(), Some(-67)),
                IotcReject::Other(n) => (format!("other:{n}"), None),
            };
            ("iotc_rejected", detail, code)
        }
        CoreError::AvAuthFailed(stage) => {
            let detail = match stage {
                AvAuthStage::Dtls(Some(alert)) => format!("dtls_alert:{alert}"),
                AvAuthStage::Dtls(None) => "dtls_silent".to_string(),
                AvAuthStage::LoginRejected(state) => format!("login_rejected:{state}"),
            };
            ("av_auth_failed", detail, Some(-20009))
        }
        CoreError::Closed(reason) => {
            let (detail, code) = match reason {
                CloseReason::RemoteClose => ("remote_close".to_string(), Some(-22)),
                CloseReason::RemoteTimeout => ("remote_timeout".to_string(), Some(-23)),
                CloseReason::DtlsAlert(alert) => (format!("dtls_alert:{alert}"), None),
                CloseReason::Local => ("local".to_string(), None),
                CloseReason::ThreadFailed => ("thread_failed".to_string(), None),
            };
            ("closed", detail, code)
        }
        CoreError::Timeout => ("timeout", String::new(), Some(-20011)),
        CoreError::Protocol(what) => ("protocol", (*what).to_string(), None),
        CoreError::Capacity => ("capacity", String::new(), None),
        CoreError::InvalidArgument(what) => ("invalid_argument", (*what).to_string(), None),
        CoreError::Io(kind) => ("io", format!("{kind:?}").to_lowercase(), None),
    }
}

/// The Python exception for a core error.
pub fn err(py: Python<'_>, e: &CoreError) -> PyErr {
    let (kind, detail, code) = describe(e);
    let exc = LinkError::new_err(e.to_string());
    let value = exc.value(py);
    // Setting attributes on a fresh exception instance only fails on memory exhaustion.
    let _ = value.setattr("kind", kind);
    let _ = value.setattr("detail", detail);
    let _ = value.setattr("code", code);
    exc
}

pub fn invalid(py: Python<'_>, what: &'static str) -> PyErr {
    err(py, &CoreError::InvalidArgument(what))
}

/// A timeout in seconds as a `Duration`; `None` waits as long as allowed.
pub fn secs(py: Python<'_>, timeout: Option<f64>) -> PyResult<Duration> {
    let Some(t) = timeout else {
        return Ok(MAX_WAIT);
    };
    if t.is_nan() || t < 0.0 {
        return Err(invalid(py, "timeout must be a non-negative number of seconds"));
    }
    Ok(Duration::try_from_secs_f64(t).map_or(MAX_WAIT, |d| d.min(MAX_WAIT)))
}

/// Run `f` with the GIL released, and raise its error as `LinkError`.
pub fn detached<T: Send>(py: Python<'_>, f: impl FnOnce() -> Result<T, CoreError> + Send) -> PyResult<T> {
    py.detach(f).map_err(|e| err(py, &e))
}
