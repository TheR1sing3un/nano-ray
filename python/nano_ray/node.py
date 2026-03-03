"""Worker node service for nano-ray multi-node mode.

A worker node connects to the head node and executes tasks assigned to it.
Each worker node runs multiple worker processes locally, similar to the
single-node Runtime but with remote task dispatch.

Architecture:

    WorkerNodeService
    +-- TCP Connection to Head (persistent)
    +-- Poller Thread: polls head for tasks → local task_queue
    +-- Result Reporter Thread: local result_queue → head
    +-- Worker Process x N
        +-- Loop: read task_queue → execute → write result_queue

Compared to Ray: In Ray, each node runs a "raylet" (local scheduler +
object store + worker management). Our WorkerNodeService is a simplified
raylet without local scheduling — all scheduling decisions are made by
the head node.

SIMPLIFICATION: Ray workers can submit tasks to their local raylet,
which may execute them locally or forward to the global scheduler.
Our workers only execute tasks, all submission goes through the head.
"""

from __future__ import annotations

import multiprocessing
import socket
import threading
import time
import traceback
from typing import Any

import cloudpickle

from nano_ray.transport import recv_msg, send_msg


def _worker_process_loop(
    task_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
    worker_id: int,
) -> None:
    """Worker process main loop (same as driver._worker_loop)."""
    while True:
        msg = task_queue.get()
        if msg is None:
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


class WorkerNodeService:
    """A worker node that connects to a head node and executes tasks."""

    def __init__(
        self,
        head_address: str,
        num_workers: int = 4,
        poll_interval: float = 0.05,
    ) -> None:
        self._head_host, port_str = head_address.rsplit(":", 1)
        self._head_port = int(port_str)
        self._num_workers = num_workers
        self._poll_interval = poll_interval

        # TCP connection to head
        self._conn: socket.socket | None = None
        self._conn_lock = threading.Lock()

        # Local worker processes
        self._task_queue: multiprocessing.Queue | None = None
        self._result_queue: multiprocessing.Queue | None = None
        self._workers: list[multiprocessing.Process] = []
        self._running = False

    def start(self) -> None:
        """Connect to head and start local worker processes."""
        if self._running:
            return
        self._running = True

        # Connect to head node
        self._conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._conn.connect((self._head_host, self._head_port))

        # Register as a worker node
        send_msg(self._conn, ("register_worker",))
        response = recv_msg(self._conn)
        if response is None or response[0] != "ok":
            raise RuntimeError(f"Failed to register with head node: {response}")

        # Start local worker processes
        from nano_ray._compat import mp_context

        self._task_queue = multiprocessing.Queue()
        self._result_queue = multiprocessing.Queue()

        ctx = mp_context()

        for i in range(self._num_workers):
            p = ctx.Process(
                target=_worker_process_loop,
                args=(self._task_queue, self._result_queue, i),
                daemon=True,
            )
            p.start()
            self._workers.append(p)

        # Start poller thread (receives tasks from head)
        self._poller_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poller_thread.start()

        # Start result reporter thread
        self._reporter_thread = threading.Thread(target=self._report_loop, daemon=True)
        self._reporter_thread.start()

    def _send_to_head(self, msg: Any) -> Any:
        """Send a message to head and receive the response (thread-safe)."""
        with self._conn_lock:
            send_msg(self._conn, msg)
            return recv_msg(self._conn)

    def _poll_loop(self) -> None:
        """Poll head node for ready tasks and dispatch to local workers."""
        while self._running:
            try:
                response = self._send_to_head(("poll_task",))
                if response is None:
                    break
                if response[0] == "task":
                    _, task_id, object_id, payload = response
                    self._task_queue.put((task_id, object_id, payload))
                else:
                    # No task available, wait before polling again
                    time.sleep(self._poll_interval)
            except (ConnectionResetError, BrokenPipeError, OSError):
                break
            except Exception:
                time.sleep(self._poll_interval)

    def _report_loop(self) -> None:
        """Report completed task results back to the head node."""
        while self._running:
            try:
                result = self._result_queue.get(timeout=0.1)
            except Exception:
                continue

            task_id, object_id, success, data = result
            try:
                self._send_to_head(("task_result", task_id, object_id, success, data))
            except (ConnectionResetError, BrokenPipeError, OSError):
                break

    def shutdown(self) -> None:
        """Shut down this worker node."""
        if not self._running:
            return
        self._running = False

        # Shut down local worker processes
        for _ in self._workers:
            if self._task_queue is not None:
                self._task_queue.put(None)
        for w in self._workers:
            w.join(timeout=5)
            if w.is_alive():
                w.terminate()
        self._workers.clear()

        # Close connection to head
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass

    def serve_forever(self) -> None:
        """Block until shutdown (for CLI usage)."""
        self.start()
        print(
            f"Worker node started with {self._num_workers} workers, "
            f"connected to {self._head_host}:{self._head_port}"
        )
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.shutdown()
