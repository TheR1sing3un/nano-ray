"""Head node service for nano-ray multi-node mode.

The head node is the central coordinator of a nano-ray cluster:
- Accepts TCP connections from worker nodes and clients
- Manages the global task queue (scheduler)
- Stores task results in the object store
- Resolves dependencies before dispatching tasks to workers

Architecture:

    Head Node
    +-- TCP Server Thread: accepts new connections
    +-- Client Handler Threads: one per connected client
    +-- Worker Handler Threads: one per connected worker node
    +-- ObjectStore: stores task results
    +-- OwnershipTable: tracks task/object metadata
    +-- Scheduler: manages dependency tracking -> ready queue

    Worker Node x M (each with N local worker processes)
    +-- Connects to head via TCP
    +-- Polls for ready tasks
    +-- Executes tasks locally
    +-- Reports results back to head

Compared to Ray: Ray's Global Control Store (GCS) is the equivalent of
our head node. However, Ray's GCS is primarily a metadata store, while
actual scheduling happens at the per-node raylet level. nano-ray
simplifies to a single head node that does both scheduling and metadata
management.

SIMPLIFICATION: Ray uses a two-layer scheduler (local raylet + global).
We use a single centralized scheduler on the head node. This is simpler
but creates a potential bottleneck for very large clusters.
"""

from __future__ import annotations

import os
import socket
import threading
import traceback
from typing import Any

import cloudpickle

from nano_ray.transport import recv_msg, send_msg


class HeadService:
    """Central coordinator for a nano-ray cluster."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 6379,
        num_workers: int = 0,
        dashboard_port: int | None = 8265,
    ) -> None:
        # Use Python fallback or Rust backend for core data structures
        try:
            from nano_ray._core import ObjectStore, OwnershipTable, Scheduler
        except ImportError:
            from nano_ray._fallback.object_store import ObjectStore
            from nano_ray._fallback.ownership import OwnershipTable
            from nano_ray._fallback.scheduler import Scheduler

        self.host = host
        self.port = port
        self._dashboard_port = dashboard_port
        self.object_store = ObjectStore()
        self.ownership = OwnershipTable()
        self.scheduler = Scheduler()

        # ID generator
        self._next_id = 0
        self._id_lock = threading.Lock()

        # Server socket
        self._server_socket: socket.socket | None = None
        self._running = False

        # Connected worker node sockets (for pulling tasks)
        self._worker_conns: list[socket.socket] = []
        self._worker_lock = threading.Lock()

        # Local worker processes (if num_workers > 0, head also runs workers)
        self._local_workers: list[Any] = []
        self._num_local_workers = num_workers

        # Task queue for local workers (if any)
        self._local_task_queue: Any = None
        self._local_result_queue: Any = None

        # Dashboard metrics
        self._metrics: Any = None
        self._dashboard_server: Any = None

    def _next_unique_id(self) -> int:
        with self._id_lock:
            id_ = self._next_id
            self._next_id += 1
            return id_

    def start(self) -> None:
        """Start the head node TCP server."""
        if self._running:
            return
        self._running = True

        # Start dashboard if port is specified
        if self._dashboard_port is not None:
            from nano_ray.dashboard import DashboardMetrics, start_dashboard

            self._metrics = DashboardMetrics()
            self._metrics.set_worker_count(self._num_local_workers)
            try:
                self._dashboard_server = start_dashboard(
                    self._metrics,
                    host="127.0.0.1",
                    port=self._dashboard_port,
                )
            except OSError:
                pass  # Dashboard port in use, skip silently

        # Start local workers if requested
        if self._num_local_workers > 0:
            self._start_local_workers()

        # Start TCP server
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, self.port))
        self._server_socket.listen(64)
        self._server_socket.settimeout(1.0)  # Allow periodic shutdown check

        self._accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True
        )
        self._accept_thread.start()

    def _start_local_workers(self) -> None:
        """Start local worker processes on the head node."""
        import multiprocessing
        import warnings

        from nano_ray.driver import _worker_loop

        self._local_task_queue = multiprocessing.Queue()
        self._local_result_queue = multiprocessing.Queue()

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            ctx = multiprocessing.get_context("fork")

        for i in range(self._num_local_workers):
            p = ctx.Process(
                target=_worker_loop,
                args=(self._local_task_queue, self._local_result_queue, i),
                daemon=True,
            )
            p.start()
            self._local_workers.append(p)

        # Start result collector for local workers
        self._local_collector = threading.Thread(
            target=self._collect_local_results, daemon=True
        )
        self._local_collector.start()

    def _collect_local_results(self) -> None:
        """Collect results from local worker processes."""
        while self._running:
            try:
                msg = self._local_result_queue.get(timeout=0.1)
            except Exception:
                continue
            self._handle_task_result(msg)

    def _accept_loop(self) -> None:
        """Accept incoming TCP connections."""
        while self._running:
            try:
                conn, addr = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            handler = threading.Thread(
                target=self._handle_connection,
                args=(conn, addr),
                daemon=True,
            )
            handler.start()

    def _handle_connection(self, conn: socket.socket, addr: tuple) -> None:
        """Handle a single TCP connection (client or worker node)."""
        try:
            while self._running:
                msg = recv_msg(conn)
                if msg is None:
                    break
                response = self._dispatch(msg, conn)
                if response is not None:
                    send_msg(conn, response)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            # Remove from worker connections if registered
            with self._worker_lock:
                if conn in self._worker_conns:
                    self._worker_conns.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, msg: tuple, conn: socket.socket) -> Any:
        """Dispatch an incoming message to the appropriate handler."""
        cmd = msg[0]

        if cmd == "register_worker":
            with self._worker_lock:
                self._worker_conns.append(conn)
            if self._metrics is not None:
                self._metrics.set_worker_count(
                    self._num_local_workers + len(self._worker_conns)
                )
            return ("ok",)

        elif cmd == "submit_task":
            _, func_bytes, args_bytes, kwargs_bytes = msg
            func = cloudpickle.loads(func_bytes)
            args = cloudpickle.loads(args_bytes)
            kwargs = cloudpickle.loads(kwargs_bytes)
            object_id = self._submit_task(func, args, kwargs)
            return ("object_id", object_id)

        elif cmd == "get_object":
            _, object_id = msg
            value = self.object_store.get_or_wait(object_id)
            value_bytes = cloudpickle.dumps(value)
            return ("value", value_bytes)

        elif cmd == "contains_object":
            _, object_id = msg
            return ("contains", self.object_store.contains(object_id))

        elif cmd == "poll_task":
            # Worker node polling for a ready task
            spec = self.scheduler.pop_ready_task(timeout=0)
            if spec is None:
                return ("no_task",)
            # Resolve ObjectRefs and serialize the task
            payload = self._prepare_task_payload(spec)
            return ("task", spec["task_id"], spec["object_id"], payload)

        elif cmd == "task_result":
            _, task_id, object_id, success, data = msg
            self._handle_task_result((task_id, object_id, success, data))
            return ("ack",)

        else:
            return ("error", f"Unknown command: {cmd}")

    def _submit_task(self, func: Any, args: tuple, kwargs: dict) -> int:
        """Submit a task for execution (called from client connection)."""
        from nano_ray.api import ObjectRef
        from nano_ray.dag import extract_dependencies

        task_id = self._next_unique_id()
        object_id = self._next_unique_id()

        dep_refs = extract_dependencies(args, kwargs)
        dep_ids = [ref.object_id for ref in dep_refs]

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

        # Re-trigger for already-resolved dependencies (race condition fix)
        if dep_ids:
            for did in dep_ids:
                if self.object_store.contains(did):
                    self.scheduler.on_object_ready(did)

        self._flush_ready_tasks()

        if self._metrics is not None:
            self._metrics.on_task_submitted(task_id)

        return object_id

    def _prepare_task_payload(self, spec: dict) -> bytes:
        """Resolve ObjectRefs in a task spec and serialize for execution."""
        from nano_ray.dag import resolve_args

        func = spec["func"]
        args = spec["args"]
        kwargs = spec["kwargs"]

        resolved_args, resolved_kwargs = resolve_args(
            args,
            kwargs,
            lambda ref: self.object_store.get_or_wait(ref.object_id),
        )
        return cloudpickle.dumps((func, resolved_args, resolved_kwargs))

    def _flush_ready_tasks(self) -> None:
        """Move ready tasks to local workers.

        Remote workers get tasks via poll_task, which pops directly from
        the scheduler's ready queue. This means local workers get priority
        for tasks that become ready during _flush_ready_tasks(), while
        remote workers pick up tasks between flushes.

        SIMPLIFICATION: Ray uses resource-aware scheduling with data
        locality hints. We use a simple "local first, remote polls" model.
        """
        if self._local_task_queue is None:
            return  # No local workers; tasks stay in ready queue for polling

        while True:
            spec = self.scheduler.pop_ready_task(timeout=0)
            if spec is None:
                break

            payload = self._prepare_task_payload(spec)
            self._local_task_queue.put(
                (spec["task_id"], spec["object_id"], payload)
            )

    def _handle_task_result(self, result: tuple) -> None:
        """Process a completed task result."""
        task_id, object_id, success, data = result
        if success:
            value = cloudpickle.loads(data)
            self.object_store.put(object_id, value)
            self.ownership.task_finished(task_id)
            self.scheduler.on_object_ready(object_id)
            self._flush_ready_tasks()
            if self._metrics is not None:
                self._metrics.on_task_completed(task_id)
                self._metrics.set_objects_stored(self.object_store.size())
        else:
            from nano_ray.driver import _TaskError
            error = _TaskError(task_id, data)
            self.object_store.put(object_id, error)
            self.ownership.task_failed(task_id, data)
            if self._metrics is not None:
                self._metrics.on_task_failed(task_id)

    def shutdown(self) -> None:
        """Shut down the head node."""
        if not self._running:
            return
        self._running = False

        # Shut down local workers
        for _ in self._local_workers:
            if self._local_task_queue is not None:
                self._local_task_queue.put(None)
        for w in self._local_workers:
            w.join(timeout=5)
            if w.is_alive():
                w.terminate()
        self._local_workers.clear()

        # Close worker connections
        with self._worker_lock:
            for conn in self._worker_conns:
                try:
                    conn.close()
                except OSError:
                    pass
            self._worker_conns.clear()

        # Close server socket
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass

        # Shut down dashboard
        if self._dashboard_server is not None:
            self._dashboard_server.shutdown()

    def serve_forever(self) -> None:
        """Block until shutdown is called (for CLI usage)."""
        self.start()
        try:
            self._accept_thread.join()
        except KeyboardInterrupt:
            self.shutdown()
