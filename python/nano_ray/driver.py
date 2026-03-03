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
        # Try Rust backend first, fall back to pure Python.
        # The Rust backend provides DashMap (lock-free concurrent map),
        # SegQueue (lock-free ready queue), and GIL-free blocking wait.
        try:
            from nano_ray._core import ObjectStore, OwnershipTable, Scheduler
        except ImportError:
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

        If args contain ObjectRefs (references to other tasks' results),
        those are extracted as dependencies. The scheduler holds the task
        until all dependencies are resolved, forming an implicit Task DAG.

        This is Ray's key insight: users just pass ObjectRefs as arguments,
        and the system automatically builds and resolves the dependency graph.

        Returns the object_id for the result.
        """
        from nano_ray.dag import extract_dependencies

        task_id = self._next_unique_id()
        object_id = self._next_unique_id()

        # Extract ObjectRef dependencies from args
        dep_refs = extract_dependencies(args, kwargs)
        dep_ids = [ref.object_id for ref in dep_refs]

        # Register in ownership table (tracks owner + lineage)
        func_desc = getattr(func, "__qualname__", str(func))
        lineage = cloudpickle.dumps((func, args, kwargs))
        self.ownership.register_task(
            task_id=task_id,
            object_id=object_id,
            owner_id=os.getpid(),
            function_desc=func_desc,
            serialized_args=lineage,
            dependencies=dep_ids if dep_ids else None,
        )

        # Store raw func/args/kwargs in the task_spec. Serialization is
        # deferred to _flush_ready_tasks() so ObjectRefs can be resolved
        # to actual values before the task is sent to a worker.
        task_spec = {
            "task_id": task_id,
            "object_id": object_id,
            "func": func,
            "args": args,
            "kwargs": kwargs,
        }
        self.scheduler.submit(
            task_spec,
            dependencies=dep_ids if dep_ids else None,
        )

        # Handle race condition: dependencies may already be resolved.
        # This happens when a task depends on results from previously
        # completed tasks (e.g., actor method chains where get() is called
        # between submissions). The scheduler's on_object_ready for those
        # objects already fired before this task was submitted, so we
        # re-trigger for any already-available deps.
        if dep_ids:
            for did in dep_ids:
                if self.object_store.contains(did):
                    self.scheduler.on_object_ready(did)

        # Flush ready tasks into the multiprocessing task_queue
        self._flush_ready_tasks()

        return object_id

    def get_object(self, object_id: int, timeout: float | None = None) -> Any:
        """Blocking get for a task result. Re-raises task errors.

        If the object has been evicted from the store but its lineage exists
        in the ownership table, it is automatically reconstructed by
        re-executing the producer task chain.
        """
        # Auto-reconstruct: if object is missing but lineage exists, rebuild it
        if not self.object_store.contains(object_id):
            producer = self.ownership.get_producer_task(object_id)
            if producer is not None:
                self.reconstruct_object(object_id)

        value = self.object_store.get_or_wait(object_id, timeout=timeout)
        if isinstance(value, _TaskError):
            raise RuntimeError(
                f"Task {value.task_id} failed with error:\n{value.error_msg}"
            )
        return value

    def reconstruct_object(self, object_id: int) -> None:
        """Reconstruct a lost object by re-executing its producer task from lineage.

        This is the core of lineage-based fault tolerance:
        1. Look up the producer task for the lost object
        2. Recursively reconstruct any missing dependencies
        3. Deserialize the original (func, args, kwargs) from lineage
        4. Re-submit the task to workers for execution

        The serialized_args in the ownership table contain the *original*
        arguments including unresolved ObjectRefs. During re-execution,
        ObjectRefs are resolved to actual values (which may themselves have
        been reconstructed in the recursive step).
        """
        if self.object_store.contains(object_id):
            return  # Already available, nothing to do

        task_id = self.ownership.get_producer_task(object_id)
        if task_id is None:
            raise RuntimeError(
                f"Cannot reconstruct object {object_id}: no lineage found"
            )

        lineage = self.ownership.get_task_lineage(task_id)
        if lineage is None:
            raise RuntimeError(
                f"Cannot reconstruct object {object_id}: "
                f"lineage for task {task_id} not found"
            )

        serialized_args, dep_ids = lineage

        # Step 1: Recursively reconstruct any missing dependencies
        for dep_id in dep_ids:
            if not self.object_store.contains(dep_id):
                self.reconstruct_object(dep_id)

        # Step 2: Deserialize the original (func, args, kwargs) from lineage
        func, args, kwargs = cloudpickle.loads(bytes(serialized_args))

        # Step 3: Resolve ObjectRefs in args to actual values, then re-execute
        from nano_ray.dag import resolve_args

        resolved_args, resolved_kwargs = resolve_args(
            args,
            kwargs,
            lambda ref: self.object_store.get_or_wait(ref.object_id),
        )

        payload = cloudpickle.dumps((func, resolved_args, resolved_kwargs))
        self._task_queue.put((task_id, object_id, payload))

        # Step 4: Block until the result is available
        self.object_store.get_or_wait(object_id)

    def evict_object(self, object_id: int) -> bool:
        """Evict an object from the store (simulate data loss).

        The lineage in the ownership table is preserved, so the object
        can be reconstructed later via reconstruct_object().

        Returns True if the object was evicted, False if it didn't exist.
        """
        return self.object_store.delete(object_id)

    def _flush_ready_tasks(self) -> None:
        """Move all ready tasks from the scheduler to the worker task_queue.

        For tasks that had dependencies (Phase 3 DAG), this is where
        ObjectRefs in arguments are resolved to actual values. By the time
        a task reaches the ready queue, all its dependencies are guaranteed
        to be in the ObjectStore, so get_or_wait returns immediately.
        """
        from nano_ray.dag import resolve_args

        while True:
            spec = self.scheduler.pop_ready_task(timeout=0)
            if spec is None:
                break

            func = spec["func"]
            args = spec["args"]
            kwargs = spec["kwargs"]

            # Resolve any ObjectRefs in args to their actual values
            resolved_args, resolved_kwargs = resolve_args(
                args,
                kwargs,
                lambda ref: self.object_store.get_or_wait(ref.object_id),
            )

            # Serialize the fully resolved task and send to a worker
            payload = cloudpickle.dumps((func, resolved_args, resolved_kwargs))
            self._task_queue.put(
                (spec["task_id"], spec["object_id"], payload)
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
