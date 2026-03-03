"""Runtime driver for nano-ray.

The driver manages the lifecycle of the nano-ray runtime:
- Spawns worker processes
- Distributes tasks to workers via a task queue
- Collects results from workers via a result queue
- Stores results in the local ObjectStore

Architecture (Phase 1, single-node):

    Driver Process
    +-- Main Thread: runs user code, calls submit_task() and get()
    +-- Result Collector Thread: reads result_queue, stores in ObjectStore
    +-- ObjectStore: thread-safe dict for task results
    +-- OwnershipTable: tracks task/object metadata
    +-- Scheduler: manages dependency tracking -> ready queue

    Worker Process x N
    +-- Loop: read task_queue -> execute -> write result_queue

Compared to Ray: Ray's driver is the initial Python process that calls
ray.init(). Each worker has its own raylet for local scheduling. We
simplify to a single scheduler in the driver process.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import traceback
from typing import Any

import cloudpickle


class _TaskError:
    """Wrapper for task execution errors stored in the ObjectStore.

    When get() retrieves this, it re-raises as a RuntimeError.
    """

    def __init__(self, task_id: int, error_msg: str) -> None:
        self.task_id = task_id
        self.error_msg = error_msg

    def __repr__(self) -> str:
        return f"_TaskError(task_id={self.task_id})"


def _worker_loop(
    task_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
    worker_id: int,
) -> None:
    """Main loop for a worker process.

    Each worker pulls tasks from the shared task_queue, executes them,
    and reports results via the result_queue. This mirrors Ray's worker
    process, but without a local raylet.
    """
    while True:
        msg = task_queue.get()
        if msg is None:  # Shutdown sentinel
            break

        task_id, object_id, payload = msg
        try:
            func, args, kwargs = cloudpickle.loads(payload)
            result = func(*args, **kwargs)
            result_bytes = cloudpickle.dumps(result)
            result_queue.put((task_id, object_id, True, result_bytes))
        except Exception:
            error_info = traceback.format_exc()
            result_queue.put((task_id, object_id, False, error_info))


class Runtime:
    """The nano-ray runtime, managing workers and task execution."""

    def __init__(self, num_workers: int = 4) -> None:
        from nano_ray._fallback.object_store import ObjectStore
        from nano_ray._fallback.ownership import OwnershipTable
        from nano_ray._fallback.scheduler import Scheduler

        self.num_workers = num_workers
        self.object_store = ObjectStore()
        self.ownership = OwnershipTable()
        self.scheduler = Scheduler()

        # IPC queues
        self._task_queue: multiprocessing.Queue = multiprocessing.Queue()
        self._result_queue: multiprocessing.Queue = multiprocessing.Queue()

        # ID generator (monotonically increasing)
        self._next_id = 0
        self._id_lock = threading.Lock()

        # Worker processes
        self._workers: list[multiprocessing.Process] = []
        self._running = False
        self._collector_thread: threading.Thread | None = None

    def start(self) -> None:
        """Start worker processes and the result collector thread."""
        if self._running:
            return
        self._running = True

        # Spawn worker processes.
        # Use "fork" context: child processes inherit the parent's memory,
        # avoiding the "spawn" re-import issue where __main__ gets re-executed.
        # Ray also uses fork on Linux. On macOS Python 3.12+ this emits a
        # DeprecationWarning which we suppress.
        import warnings

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            ctx = multiprocessing.get_context("fork")
        for i in range(self.num_workers):
            p = ctx.Process(
                target=_worker_loop,
                args=(self._task_queue, self._result_queue, i),
                daemon=True,
            )
            p.start()
            self._workers.append(p)

        # Start result collector thread
        self._collector_thread = threading.Thread(
            target=self._collect_results, daemon=True
        )
        self._collector_thread.start()

    def shutdown(self) -> None:
        """Gracefully shut down all workers and the collector thread."""
        if not self._running:
            return
        self._running = False

        # Send shutdown sentinels to all workers
        for _ in self._workers:
            self._task_queue.put(None)

        # Wait for workers to finish
        for w in self._workers:
            w.join(timeout=5)
            if w.is_alive():
                w.terminate()

        # Wait for collector to drain remaining results
        if self._collector_thread is not None:
            self._collector_thread.join(timeout=2)

        self._workers.clear()

    def _next_unique_id(self) -> int:
        """Generate a monotonically increasing unique ID."""
        with self._id_lock:
            id_ = self._next_id
            self._next_id += 1
            return id_

    def submit_task(self, func: Any, args: tuple, kwargs: dict) -> int:
        """Submit a task for execution.

        Returns the object_id for the result. The caller wraps this
        in an ObjectRef.
        """
        task_id = self._next_unique_id()
        object_id = self._next_unique_id()

        # Serialize function + arguments with cloudpickle
        payload = cloudpickle.dumps((func, args, kwargs))

        # Register in ownership table (tracks owner + lineage)
        func_desc = getattr(func, "__qualname__", str(func))
        self.ownership.register_task(
            task_id=task_id,
            object_id=object_id,
            owner_id=os.getpid(),
            function_desc=func_desc,
            serialized_args=payload,
        )

        # Submit to scheduler (no dependencies in Phase 1)
        task_spec = {
            "task_id": task_id,
            "object_id": object_id,
            "payload": payload,
        }
        self.scheduler.submit(task_spec)

        # Flush ready tasks into the multiprocessing task_queue
        self._flush_ready_tasks()

        return object_id

    def get_object(self, object_id: int, timeout: float | None = None) -> Any:
        """Blocking get for a task result. Re-raises task errors."""
        value = self.object_store.get_or_wait(object_id, timeout=timeout)
        if isinstance(value, _TaskError):
            raise RuntimeError(
                f"Task {value.task_id} failed with error:\n{value.error_msg}"
            )
        return value

    def _flush_ready_tasks(self) -> None:
        """Move all ready tasks from the scheduler to the worker task_queue."""
        while True:
            spec = self.scheduler.pop_ready_task(timeout=0)
            if spec is None:
                break
            self._task_queue.put(
                (spec["task_id"], spec["object_id"], spec["payload"])
            )

    def _collect_results(self) -> None:
        """Background thread: reads results from workers and stores them."""
        while self._running or not self._result_queue.empty():
            try:
                msg = self._result_queue.get(timeout=0.1)
            except Exception:
                continue

            task_id, object_id, success, data = msg
            if success:
                value = cloudpickle.loads(data)
                self.object_store.put(object_id, value)
                self.ownership.task_finished(task_id)
                # Notify scheduler that this object is ready (for Phase 3 DAG)
                self.scheduler.on_object_ready(object_id)
                # Flush any newly unblocked tasks to workers
                self._flush_ready_tasks()
            else:
                error = _TaskError(task_id, data)
                self.object_store.put(object_id, error)
                self.ownership.task_failed(task_id, data)
