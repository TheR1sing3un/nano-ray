"""Pure Python fallback: Ownership Table.

The Ownership Table is the heart of Ray's decentralized metadata management.
Key insight: whoever submits a task *owns* that task and its result object.
This distributes metadata tracking across all workers — no single master
bottleneck.

In single-node nano-ray, all tasks are owned by the driver process, so the
decentralization benefit doesn't apply yet. But the table structure matches
Ray's model so multi-node support (Phase 4) can extend it naturally.

SIMPLIFICATION: No lineage-based fault tolerance. We store lineage info
(function_desc + serialized_args) for future use but don't implement
re-execution on failure.
"""

from __future__ import annotations

import threading
from enum import Enum, auto
from typing import Any


class TaskStatus(Enum):
    WAITING_FOR_DEPENDENCIES = auto()
    READY = auto()
    RUNNING = auto()
    FINISHED = auto()
    FAILED = auto()


class OwnershipTable:
    """Tracks ownership and status of tasks and objects."""

    def __init__(self) -> None:
        self._tasks: dict[int, dict[str, Any]] = {}
        self._objects: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def register_task(
        self,
        task_id: int,
        object_id: int,
        owner_id: int,
        function_desc: str,
        serialized_args: bytes,
        dependencies: list[int] | None = None,
    ) -> None:
        """Register a new task and its result object in the ownership table."""
        with self._lock:
            self._tasks[task_id] = {
                "owner": owner_id,
                "status": TaskStatus.READY,
                "result_id": object_id,
                "function_desc": function_desc,
                "serialized_args": serialized_args,
                "dependencies": dependencies or [],
                "error": None,
            }
            self._objects[object_id] = {
                "owner": owner_id,
                "ref_count": 1,
                "producer_task": task_id,
                "location": None,
            }

    def task_finished(self, task_id: int) -> None:
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["status"] = TaskStatus.FINISHED

    def task_failed(self, task_id: int, error: str) -> None:
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["status"] = TaskStatus.FAILED
                self._tasks[task_id]["error"] = error

    def get_task_status(self, task_id: int) -> TaskStatus | None:
        with self._lock:
            entry = self._tasks.get(task_id)
            return entry["status"] if entry else None

    def get_result_id(self, task_id: int) -> int | None:
        with self._lock:
            entry = self._tasks.get(task_id)
            return entry["result_id"] if entry else None

    def add_ref(self, object_id: int) -> None:
        with self._lock:
            if object_id in self._objects:
                self._objects[object_id]["ref_count"] += 1

    def remove_ref(self, object_id: int) -> bool:
        """Decrement ref count. Returns True if the object can be collected."""
        with self._lock:
            if object_id in self._objects:
                self._objects[object_id]["ref_count"] -= 1
                return self._objects[object_id]["ref_count"] <= 0
            return False
