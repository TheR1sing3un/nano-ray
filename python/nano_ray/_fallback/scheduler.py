"""Pure Python fallback: Task Scheduler.

The scheduler manages a queue of ready-to-execute tasks and tracks
dependency resolution. In Phase 1, all tasks have no dependencies and
are immediately ready. Phase 3 adds DAG support where tasks may wait
for their ObjectRef dependencies to be resolved.

SIMPLIFICATION: No two-level scheduling (local + global). In single-node
mode, all tasks go through one ready queue. Ray's local scheduler
prioritizes data locality; we skip that in Phase 1-3.
"""

from __future__ import annotations

import threading
from queue import Empty, Queue
from typing import Any


class Scheduler:
    """Task scheduler with ready queue and dependency tracking."""

    def __init__(self) -> None:
        self._ready_queue: Queue[dict[str, Any]] = Queue()
        # Pending tasks waiting for dependencies (Phase 3 DAG support)
        self._pending_tasks: dict[int, dict[str, Any]] = {}
        # Reverse index: object_id -> list of task_ids waiting on it
        self._object_to_waiting: dict[int, list[int]] = {}
        self._lock = threading.Lock()

    def submit(self, task_spec: dict[str, Any], dependencies: list[int] | None = None) -> None:
        """Submit a task. If all dependencies are met, enqueue immediately."""
        deps = dependencies or []
        if not deps:
            self._ready_queue.put(task_spec)
        else:
            task_id = task_spec["task_id"]
            with self._lock:
                self._pending_tasks[task_id] = {
                    "spec": task_spec,
                    "unresolved": set(deps),
                }
                for dep_id in deps:
                    self._object_to_waiting.setdefault(dep_id, []).append(task_id)

    def on_object_ready(self, object_id: int) -> None:
        """Notify that an object is available. Unblock waiting tasks."""
        with self._lock:
            waiting = self._object_to_waiting.pop(object_id, [])
            for task_id in waiting:
                pending = self._pending_tasks.get(task_id)
                if pending is None:
                    continue
                pending["unresolved"].discard(object_id)
                if not pending["unresolved"]:
                    spec = self._pending_tasks.pop(task_id)["spec"]
                    self._ready_queue.put(spec)

    def pop_ready_task(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Get the next ready task. Returns None if queue is empty after timeout."""
        try:
            return self._ready_queue.get(timeout=timeout)
        except Empty:
            return None

    def has_ready_tasks(self) -> bool:
        return not self._ready_queue.empty()
