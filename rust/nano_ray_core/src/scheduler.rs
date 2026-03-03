//! Task scheduler with lock-free ready queue and dependency tracking.
//!
//! The scheduler manages the lifecycle of tasks from submission to
//! execution readiness:
//!
//! 1. Task submitted with dependencies → pending (waiting)
//! 2. Dependencies resolved → task moves to ready queue
//! 3. Worker pops from ready queue → task executes
//!
//! In Ray, each node has a local scheduler that prioritizes data locality,
//! and a global scheduler handles cross-node placement. nano-ray uses a
//! single scheduler with a lock-free ready queue (crossbeam SegQueue).
//!
//! SIMPLIFICATION: No data locality awareness, no resource accounting.
//! Scheduling is pure FIFO on the ready queue.

use crossbeam::queue::SegQueue;
use dashmap::DashMap;
use parking_lot::Mutex;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::HashSet;

/// A task waiting for its dependencies to be resolved.
struct PendingTask {
    /// The Python task_spec dict, held until the task becomes ready.
    spec: PyObject,
    /// Set of object IDs this task is still waiting for.
    unresolved: Mutex<HashSet<u64>>,
}

/// Python-exposed task scheduler.
///
/// Uses crossbeam's SegQueue (lock-free) for the ready queue, providing
/// excellent throughput under contention. DashMap handles the dependency
/// tracking with fine-grained locking.
#[pyclass(name = "Scheduler")]
pub struct PyScheduler {
    /// Lock-free queue of tasks ready for execution.
    ready_queue: SegQueue<PyObject>,
    /// Tasks waiting for dependencies, keyed by task_id.
    pending_tasks: DashMap<u64, PendingTask>,
    /// Reverse index: object_id → list of task_ids waiting on it.
    object_to_waiting: DashMap<u64, Vec<u64>>,
}

#[pymethods]
impl PyScheduler {
    #[new]
    fn new() -> Self {
        Self {
            ready_queue: SegQueue::new(),
            pending_tasks: DashMap::new(),
            object_to_waiting: DashMap::new(),
        }
    }

    /// Submit a task. If all dependencies are met, enqueue immediately.
    ///
    /// `task_spec` is a Python dict with at least a "task_id" key.
    /// `dependencies` is an optional list of object IDs that must be
    /// available before the task can execute.
    #[pyo3(signature = (task_spec, dependencies=None))]
    fn submit(&self, py: Python<'_>, task_spec: PyObject, dependencies: Option<Vec<u64>>) -> PyResult<()> {
        let deps = dependencies.unwrap_or_default();
        if deps.is_empty() {
            self.ready_queue.push(task_spec);
        } else {
            // Extract task_id from the Python dict
            let bound = task_spec.bind(py);
            let dict = bound.downcast::<PyDict>()?;
            let task_id: u64 = dict
                .get_item("task_id")?
                .ok_or_else(|| {
                    pyo3::exceptions::PyKeyError::new_err("task_spec missing 'task_id' key")
                })?
                .extract()?;

            // Register dependency tracking
            let pending = PendingTask {
                spec: task_spec,
                unresolved: Mutex::new(deps.iter().copied().collect()),
            };
            self.pending_tasks.insert(task_id, pending);

            for dep_id in deps {
                self.object_to_waiting
                    .entry(dep_id)
                    .or_default()
                    .push(task_id);
            }
        }
        Ok(())
    }

    /// Notify that an object is available. Unblock waiting tasks.
    ///
    /// When all of a task's dependencies are resolved, it moves from
    /// pending to the ready queue.
    fn on_object_ready(&self, object_id: u64) {
        let waiting = self.object_to_waiting.remove(&object_id);
        if let Some((_, task_ids)) = waiting {
            for task_id in task_ids {
                let should_ready = {
                    if let Some(pending) = self.pending_tasks.get(&task_id) {
                        let mut unresolved = pending.unresolved.lock();
                        unresolved.remove(&object_id);
                        unresolved.is_empty()
                    } else {
                        false
                    }
                };

                if should_ready {
                    if let Some((_, task)) = self.pending_tasks.remove(&task_id) {
                        self.ready_queue.push(task.spec);
                    }
                }
            }
        }
    }

    /// Pop the next ready task. Returns None if queue is empty.
    ///
    /// The timeout parameter is accepted for interface compatibility
    /// with the Python fallback but only non-blocking pop is implemented.
    #[pyo3(signature = (timeout=None))]
    fn pop_ready_task(&self, #[allow(unused_variables)] timeout: Option<f64>) -> Option<PyObject> {
        self.ready_queue.pop()
    }

    /// Check if there are ready tasks.
    fn has_ready_tasks(&self) -> bool {
        !self.ready_queue.is_empty()
    }
}

#[cfg(test)]
mod tests {
    // Scheduler tests require PyObject, so they're tested via Python integration tests.
    // The dependency tracking logic is verified through the Phase 1 test suite
    // running against the Rust backend.
}
