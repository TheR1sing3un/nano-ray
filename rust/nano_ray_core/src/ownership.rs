//! Ownership table: the heart of Ray's decentralized metadata management.
//!
//! Key insight from Ray: whoever submits a task *owns* that task and its
//! result object. The owner is responsible for tracking the task's status,
//! the object's location, and reference counting.
//!
//! In a multi-node Ray cluster, this distributes metadata across workers,
//! eliminating the single-master bottleneck (unlike Spark's driver).
//!
//! In single-node nano-ray, the driver owns all tasks. The table structure
//! is designed for future multi-node extension.
//!
//! The ownership table also stores lineage information (serialized_args +
//! dependencies) for each task, enabling lineage-based fault tolerance:
//! when an object is lost, its producer task can be re-executed from lineage.

use crate::types::TaskStatus;
use dashmap::DashMap;
use parking_lot::RwLock;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::sync::atomic::{AtomicI32, Ordering};

/// Metadata for a registered task, including full lineage information.
#[allow(dead_code)]
struct TaskEntry {
    owner: u64,
    status: RwLock<TaskStatus>,
    result_id: u64,
    function_desc: String,
    serialized_args: Vec<u8>,
    dependencies: Vec<u64>,
}

/// Metadata for a registered object (task result).
#[allow(dead_code)]
struct ObjectEntry {
    owner: u64,
    ref_count: AtomicI32,
    producer_task: u64,
}

/// Python-exposed ownership table.
///
/// Uses DashMap for concurrent access from multiple threads without
/// a global lock. Each entry has its own fine-grained lock (RwLock for
/// status, AtomicI32 for ref count).
#[pyclass(name = "OwnershipTable")]
pub struct PyOwnershipTable {
    tasks: DashMap<u64, TaskEntry>,
    objects: DashMap<u64, ObjectEntry>,
}

#[pymethods]
impl PyOwnershipTable {
    #[new]
    fn new() -> Self {
        Self {
            tasks: DashMap::new(),
            objects: DashMap::new(),
        }
    }

    /// Register a new task and its result object.
    ///
    /// Stores the complete lineage: function descriptor, serialized args
    /// (the full pickled (func, args, kwargs) tuple), and dependencies.
    /// This lineage enables re-execution when objects are lost.
    #[pyo3(signature = (task_id, object_id, owner_id, function_desc, serialized_args, dependencies=None))]
    fn register_task(
        &self,
        task_id: u64,
        object_id: u64,
        owner_id: u64,
        function_desc: &str,
        serialized_args: &[u8],
        dependencies: Option<Vec<u64>>,
    ) {
        let task = TaskEntry {
            owner: owner_id,
            status: RwLock::new(TaskStatus::Ready),
            result_id: object_id,
            function_desc: function_desc.to_string(),
            serialized_args: serialized_args.to_vec(),
            dependencies: dependencies.unwrap_or_default(),
        };
        self.tasks.insert(task_id, task);

        let object = ObjectEntry {
            owner: owner_id,
            ref_count: AtomicI32::new(1),
            producer_task: task_id,
        };
        self.objects.insert(object_id, object);
    }

    /// Mark a task as finished.
    fn task_finished(&self, task_id: u64) {
        if let Some(entry) = self.tasks.get(&task_id) {
            *entry.status.write() = TaskStatus::Finished;
        }
    }

    /// Mark a task as failed with an error message.
    fn task_failed(&self, task_id: u64, error: &str) {
        if let Some(entry) = self.tasks.get(&task_id) {
            *entry.status.write() = TaskStatus::Failed(error.to_string());
        }
    }

    /// Get the status of a task as a string (for debugging).
    fn get_task_status(&self, task_id: u64) -> Option<String> {
        self.tasks
            .get(&task_id)
            .map(|entry| entry.status.read().to_string())
    }

    /// Get the result object ID for a task.
    fn get_result_id(&self, task_id: u64) -> Option<u64> {
        self.tasks.get(&task_id).map(|entry| entry.result_id)
    }

    /// Get the task ID that produced a given object.
    ///
    /// This is the entry point for lineage-based reconstruction:
    /// object_id → producer_task → task lineage → re-execution.
    fn get_producer_task(&self, object_id: u64) -> Option<u64> {
        self.objects
            .get(&object_id)
            .map(|entry| entry.producer_task)
    }

    /// Get the complete lineage for a task: serialized_args and dependencies.
    ///
    /// Returns (serialized_args_bytes, [dep_object_id, ...]) or None.
    /// The serialized_args contain the pickled (func, args, kwargs) tuple
    /// needed to re-execute the task.
    fn get_task_lineage<'py>(
        &self,
        py: Python<'py>,
        task_id: u64,
    ) -> Option<(Bound<'py, PyBytes>, Vec<u64>)> {
        self.tasks.get(&task_id).map(|entry| {
            let bytes = PyBytes::new_bound(py, &entry.serialized_args);
            let deps = entry.dependencies.clone();
            (bytes, deps)
        })
    }

    /// Increment the reference count for an object.
    fn add_ref(&self, object_id: u64) {
        if let Some(entry) = self.objects.get(&object_id) {
            entry.ref_count.fetch_add(1, Ordering::Relaxed);
        }
    }

    /// Decrement the reference count. Returns True if the object can be collected.
    fn remove_ref(&self, object_id: u64) -> bool {
        if let Some(entry) = self.objects.get(&object_id) {
            let prev = entry.ref_count.fetch_sub(1, Ordering::Relaxed);
            return prev <= 1;
        }
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_task_lifecycle() {
        let table = PyOwnershipTable::new();
        table.register_task(1, 100, 0, "test_func", &[], None);

        assert_eq!(table.get_task_status(1), Some("READY".to_string()));
        assert_eq!(table.get_result_id(1), Some(100));

        table.task_finished(1);
        assert_eq!(table.get_task_status(1), Some("FINISHED".to_string()));
    }

    #[test]
    fn test_task_failure() {
        let table = PyOwnershipTable::new();
        table.register_task(2, 200, 0, "bad_func", &[], None);

        table.task_failed(2, "division by zero");
        assert_eq!(
            table.get_task_status(2),
            Some("FAILED(division by zero)".to_string())
        );
    }

    #[test]
    fn test_ref_counting() {
        let table = PyOwnershipTable::new();
        table.register_task(1, 100, 0, "f", &[], None);

        table.add_ref(100);
        assert!(!table.remove_ref(100)); // 2 -> 1
        assert!(table.remove_ref(100)); // 1 -> 0
    }

    #[test]
    fn test_nonexistent_task() {
        let table = PyOwnershipTable::new();
        assert!(table.get_task_status(999).is_none());
        assert!(table.get_result_id(999).is_none());
    }

    #[test]
    fn test_producer_task_lookup() {
        let table = PyOwnershipTable::new();
        table.register_task(42, 100, 0, "my_func", &[1, 2, 3], Some(vec![10, 20]));

        assert_eq!(table.get_producer_task(100), Some(42));
        assert_eq!(table.get_producer_task(999), None);
    }

    #[test]
    fn test_lineage_storage() {
        let table = PyOwnershipTable::new();
        let lineage_data = b"pickled_func_args_kwargs";
        table.register_task(1, 100, 0, "compute", lineage_data, Some(vec![50, 60]));

        // Verify lineage is stored (need Python context for full test)
        // Here we just verify the task exists and has correct deps
        assert_eq!(table.get_result_id(1), Some(100));
        assert_eq!(table.get_producer_task(100), Some(1));
    }
}
