//! The `_vtech_link` Python module: a thin PyO3 layer over `vtech-link-core`.

use pyo3::prelude::*;

/// The core library version.
#[pyfunction]
fn version() -> &'static str {
    vtech_link_core::VERSION
}

/// Return `data` unchanged, with the GIL released while it is copied.
#[pyfunction]
fn echo(py: Python<'_>, data: Vec<u8>) -> Vec<u8> {
    py.detach(move || data)
}

/// Panic inside Rust, to prove a panic unwinds into a Python exception rather than aborting.
#[pyfunction]
#[allow(clippy::panic)]
fn _panic() {
    panic!("test panic");
}

#[pymodule]
fn _vtech_link(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(echo, m)?)?;
    m.add_function(wrap_pyfunction!(_panic, m)?)?;
    Ok(())
}
