use pyo3::prelude::*;

/// Returns a greeting string to verify the Rust ↔ Python bridge works.
#[pyfunction]
fn hello() -> &'static str {
    "nano-ray is alive!"
}

/// The native extension module exposed to Python as `nano_ray._core`.
#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(hello, m)?)?;
    Ok(())
}
