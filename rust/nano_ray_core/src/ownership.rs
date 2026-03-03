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
//! is designed for future multi-node extension (Phase 4).
//!
//! SIMPLIFICATION: No lineage-based fault tolerance. We store function
//! descriptors but don't implement re-execution on failure.

use crate::types::TaskStatus;
use dashmap::DashMap;
use parking_lot::RwLock;
use pyo3::prelude::*;
use std::sync::atomic::{AtomicI32, Ordering};

/// Metadata for a registered task.
/// Some fields are reserved for Phase 4 (multi-node, lineage recovery).
#[allow(dead_code)]
struct TaskEntry {
    owner: u64,
    status: RwLock<TaskStatus>,
    result_id: u64,
    function_desc: String,
    dependencies: Vec<u64>,
}

/// Metadata for a registered object (task result).
/// Some fields are reserved for Phase 4 (multi-node).
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
    /// `serialized_args` is accepted for interface compatibility with the
    /// Python fallback but not stored in Rust (lineage re-execution is
    /// not implemented). The function descriptor is stored for debugging.
    #[pyo3(signature = (task_id, object_id, owner_id, function_desc, serialized_args, dependencies=None))]
    fn register_task(
        &self,
        task_id: u64,
        object_id: u64,
        owner_id: u64,
        function_desc: &str,
        #[allow(unused_variables)]
        serialized_args: &[u8],
        dependencies: Option<Vec<u64>>,
    ) {
        let task = TaskEntry {
            owner: owner_id,
            status: RwLock::new(TaskStatus::Ready),
            result_id: object_id,
            function_desc: function_desc.to_string(),
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

        assert_eq!(
            table.get_task_status(1),
            Some("READY".to_string())
        );
        assert_eq!(table.get_result_id(1), Some(100));

        table.task_finished(1);
        assert_eq!(
            table.get_task_status(1),
            Some("FINISHED".to_string())
        );
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

        // Initial ref count is 1
        table.add_ref(100);
        // Now ref count is 2
        assert!(!table.remove_ref(100)); // 2 -> 1, not collectable
        assert!(table.remove_ref(100)); // 1 -> 0, collectable
    }

    #[test]
    fn test_nonexistent_task() {
        let table = PyOwnershipTable::new();
        assert!(table.get_task_status(999).is_none());
        assert!(table.get_result_id(999).is_none());
    }
}
