//! Concurrent object store backed by DashMap.
//!
//! The object store holds immutable task results and supports blocking
//! waits for objects that haven't been created yet.
//!
//! In Ray, each node has a Plasma shared-memory object store. Objects are
//! stored as serialized bytes in shared memory, enabling zero-copy reads
//! across workers on the same node.
//!
//! nano-ray stores Python objects (`PyObject`) directly. The key optimization
//! is the blocking wait: `get_or_wait` releases the GIL via `py.allow_threads`
//! while waiting on a parking_lot Condvar, allowing other Python threads to
//! make progress.
//!
//! Thread safety: DashMap provides concurrent read/write without a global
//! lock. Each object_id has its own notification (Mutex + Condvar) so
//! waiters don't interfere with each other.

use dashmap::DashMap;
use parking_lot::{Condvar, Mutex};
use pyo3::prelude::*;
use std::sync::Arc;
use std::time::Duration;

/// Notification primitive: a flag + condvar pair.
/// Waiters block on the condvar until `ready` is set to true.
struct Notify {
    ready: Mutex<bool>,
    condvar: Condvar,
}

impl Notify {
    fn new() -> Self {
        Self {
            ready: Mutex::new(false),
            condvar: Condvar::new(),
        }
    }
}

/// Python-exposed concurrent object store.
///
/// Stores arbitrary Python objects keyed by integer IDs.
/// Provides blocking get with GIL release for efficient waiting.
#[pyclass(name = "ObjectStore")]
pub struct PyObjectStore {
    objects: DashMap<u64, PyObject>,
    notifications: DashMap<u64, Arc<Notify>>,
}

#[pymethods]
impl PyObjectStore {
    #[new]
    fn new() -> Self {
        Self {
            objects: DashMap::new(),
            notifications: DashMap::new(),
        }
    }

    /// Store an immutable object and wake up any waiters.
    fn put(&self, object_id: u64, value: PyObject) {
        self.objects.insert(object_id, value);
        // Wake up any threads waiting for this object
        if let Some(notify) = self.notifications.get(&object_id) {
            let mut ready = notify.ready.lock();
            *ready = true;
            notify.condvar.notify_all();
        }
    }

    /// Blocking get. Waits until the object is available or timeout expires.
    ///
    /// The GIL is released during the wait so other Python threads can run.
    /// This is a key performance advantage over the pure Python fallback.
    #[pyo3(signature = (object_id, timeout=None))]
    fn get_or_wait(
        &self,
        py: Python<'_>,
        object_id: u64,
        timeout: Option<f64>,
    ) -> PyResult<PyObject> {
        // Fast path: object already available
        if let Some(entry) = self.objects.get(&object_id) {
            return Ok(entry.value().clone_ref(py));
        }

        // Register interest (get-or-create notification entry)
        let notify = self
            .notifications
            .entry(object_id)
            .or_insert_with(|| Arc::new(Notify::new()))
            .clone();

        // Double-check after registering to avoid missed notification
        if let Some(entry) = self.objects.get(&object_id) {
            return Ok(entry.value().clone_ref(py));
        }

        // Release GIL and wait on condvar
        let timed_out = py.allow_threads(|| {
            let mut ready = notify.ready.lock();
            if *ready {
                return false;
            }
            match timeout {
                Some(secs) if secs > 0.0 => {
                    let duration = Duration::from_secs_f64(secs);
                    notify.condvar.wait_for(&mut ready, duration).timed_out()
                }
                Some(_) => !*ready, // timeout <= 0: non-blocking check
                None => {
                    // Wait forever
                    while !*ready {
                        notify.condvar.wait(&mut ready);
                    }
                    false
                }
            }
        });

        if timed_out {
            return Err(pyo3::exceptions::PyTimeoutError::new_err(format!(
                "Timed out waiting for object {object_id}"
            )));
        }

        self.objects
            .get(&object_id)
            .map(|entry| entry.value().clone_ref(py))
            .ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(
                    "Object disappeared after notification (internal error)",
                )
            })
    }

    /// Check if an object exists (non-blocking).
    fn contains(&self, object_id: u64) -> bool {
        self.objects.contains_key(&object_id)
    }

    /// Remove an object. Returns True if it existed.
    fn delete(&self, object_id: u64) -> bool {
        let removed = self.objects.remove(&object_id).is_some();
        self.notifications.remove(&object_id);
        removed
    }

    /// Number of stored objects.
    fn size(&self) -> usize {
        self.objects.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread;

    // Note: PyObject-based tests require a Python interpreter.
    // We test the notification logic separately.

    #[test]
    fn test_notify_signal() {
        let notify = Arc::new(Notify::new());
        let n2 = notify.clone();

        let handle = thread::spawn(move || {
            thread::sleep(Duration::from_millis(50));
            let mut ready = n2.ready.lock();
            *ready = true;
            n2.condvar.notify_all();
        });

        let mut ready = notify.ready.lock();
        while !*ready {
            notify.condvar.wait(&mut ready);
        }
        assert!(*ready);
        handle.join().unwrap();
    }

    #[test]
    fn test_notify_timeout() {
        let notify = Notify::new();
        let mut ready = notify.ready.lock();
        let result = notify
            .condvar
            .wait_for(&mut ready, Duration::from_millis(10));
        assert!(result.timed_out());
    }
}
