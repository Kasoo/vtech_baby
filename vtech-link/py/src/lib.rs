//! The `_vtech_link` Python module: a thin PyO3 layer over `vtech-link-core`
//! (`docs/vtech-link-design/75-python-api.md`). No method calls back into Python from a Rust thread.

mod error;
mod link;
mod logs;

use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::error::secs;

/// Connect to a camera and block until the link is up. Raises `LinkError`.
#[pyfunction]
#[pyo3(signature = (uid, authkey, password, account = "admin", *, connect_timeout = 15.0,
    av_timeout = 30.0, search_port = 32762, peer_hint = None, interface = None))]
#[allow(clippy::too_many_arguments)]
fn connect(
    py: Python<'_>,
    uid: &str,
    authkey: &str,
    password: &str,
    account: &str,
    connect_timeout: f64,
    av_timeout: f64,
    search_port: u16,
    peer_hint: Option<&str>,
    interface: Option<String>,
) -> PyResult<link::Link> {
    link::connect(
        py,
        uid,
        authkey,
        password,
        account,
        connect_timeout,
        av_timeout,
        search_port,
        peer_hint,
        interface,
    )
}

/// Process-wide LAN-only counters.
#[pyfunction]
fn lan_only_stats(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let (refused_sends, refused_lookups) = vtech_link_core::runtime::egress::stats();
    let d = PyDict::new(py);
    d.set_item("refused_sends", refused_sends)?;
    d.set_item("refused_lookups", refused_lookups)?;
    Ok(d)
}

/// The queued log records as `(level, target, message)`, waiting up to `timeout` for the first;
/// `[]` on timeout.
#[pyfunction]
fn next_logs(py: Python<'_>, timeout: f64) -> PyResult<Vec<logs::Record>> {
    let timeout = secs(py, Some(timeout))?;
    Ok(py.detach(|| logs::take(timeout)))
}

/// Log records dropped because the ring was full.
#[pyfunction]
fn logs_dropped() -> u64 {
    logs::dropped()
}

/// Record logs at `level` (a Python `logging` level) and above. The default is INFO.
#[pyfunction]
fn set_log_level(level: i32) {
    logs::set_level(level);
}

/// `info()` of every live link.
#[pyfunction]
fn links(py: Python<'_>) -> PyResult<Vec<Bound<'_, PyDict>>> {
    link::links(py)
}

/// Panic inside Rust, to prove a panic unwinds into a Python exception rather than aborting.
#[pyfunction]
#[allow(clippy::panic)]
fn _panic() {
    panic!("test panic");
}

#[pymodule]
fn _vtech_link(m: &Bound<'_, PyModule>) -> PyResult<()> {
    logs::install();
    let version = match option_env!("VTECH_LINK_REV") {
        Some(rev) => format!("{}+{rev}", vtech_link_core::VERSION),
        None => vtech_link_core::VERSION.to_string(),
    };
    m.add("__version__", version)?;
    m.add("SDK_VERSION", vtech_link_core::iotc::codec::SDK_VERSION)?;
    m.add("LinkError", m.py().get_type::<error::LinkError>())?;
    m.add_class::<link::Link>()?;
    m.add_class::<link::Exchange>()?;
    m.add_class::<link::MediaReceiver>()?;
    m.add_class::<link::Frame>()?;
    m.add_class::<link::TalkBack>()?;
    m.add_function(wrap_pyfunction!(connect, m)?)?;
    m.add_function(wrap_pyfunction!(lan_only_stats, m)?)?;
    m.add_function(wrap_pyfunction!(next_logs, m)?)?;
    m.add_function(wrap_pyfunction!(logs_dropped, m)?)?;
    m.add_function(wrap_pyfunction!(set_log_level, m)?)?;
    m.add_function(wrap_pyfunction!(links, m)?)?;
    m.add_function(wrap_pyfunction!(_panic, m)?)?;
    Ok(())
}
