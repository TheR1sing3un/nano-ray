// PyO3 #[pymethods] macro generates `impl From<PyErr> for PyErr` conversions
// that trigger clippy::useless_conversion. This is a known false positive.
#![allow(clippy::useless_conversion)]

//! nano_ray_core: Rust backend for nano-ray.
//!
//! This crate provides the performance-critical internals:
//! - ObjectStore: concurrent object storage with GIL-free blocking wait
//! - OwnershipTable: decentralized task/object metadata tracking
//! - Scheduler: lock-free ready queue with dependency resolution
//! - Serializer: byte-level serialization utilities (placeholder)
//!
//! Exposed to Python as `nano_ray._core` via PyO3.

use pyo3::prelude::*;

mod object_store;
mod ownership;
mod scheduler;
mod serializer;
mod types;

/// Returns a greeting string to verify the Rust-Python bridge works.
#[pyfunction]
fn hello() -> &'static str {
    "nano-ray is alive!"
}

/// The native extension module exposed to Python as `nano_ray._core`.
#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(hello, m)?)?;
    m.add_class::<object_store::PyObjectStore>()?;
    m.add_class::<ownership::PyOwnershipTable>()?;
    m.add_class::<scheduler::PyScheduler>()?;
    Ok(())
}
