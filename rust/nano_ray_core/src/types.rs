//! Shared type definitions for nano-ray's Rust core.
//!
//! These types are used across all Rust modules and form the common
//! vocabulary for the system. In Ray, similar types live in
//! `src/ray/common/id.h`.

// These type aliases are used across modules. Some are not yet used in Phase 2
// but are reserved for Phase 4 (multi-node) and beyond.
#![allow(dead_code)]

/// Unique identifier for a task.
pub type TaskId = u64;

/// Unique identifier for an object (task result).
/// In Ray, ObjectId and TaskId have a 1:1 relationship for return values.
pub type ObjectId = u64;

/// Unique identifier for a worker process.
pub type WorkerId = u64;

/// Status of a task in the system.
///
/// SIMPLIFICATION: Ray has more granular states (e.g., separate states for
/// "submitted to raylet" vs "scheduled on worker"). We collapse these into
/// five states that capture the essential lifecycle.
#[derive(Clone, Debug, PartialEq)]
pub enum TaskStatus {
    WaitingForDependencies,
    Ready,
    Running,
    Finished,
    Failed(String),
}

impl std::fmt::Display for TaskStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TaskStatus::WaitingForDependencies => write!(f, "WAITING_FOR_DEPENDENCIES"),
            TaskStatus::Ready => write!(f, "READY"),
            TaskStatus::Running => write!(f, "RUNNING"),
            TaskStatus::Finished => write!(f, "FINISHED"),
            TaskStatus::Failed(e) => write!(f, "FAILED({e})"),
        }
    }
}
