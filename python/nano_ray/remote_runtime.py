"""Remote runtime for nano-ray client mode.

RemoteRuntime provides the same interface as driver.Runtime but delegates
all operations to a remote head node via TCP. This allows clients to
submit tasks and get results without running local worker processes.

Compared to Ray: Ray's client mode (ray.init(address=...)) connects to
an existing cluster. The driver process communicates with the GCS (Global
Control Store) for metadata and directly with workers for data transfer.
nano-ray simplifies this to a single TCP connection to the head node.

SIMPLIFICATION: Ray's client communicates with multiple components (GCS,
raylets, object store). Our client only talks to the head node, which
is simpler but means all data flows through the head.
"""

from __future__ import annotations

import socket
import threading
from typing import Any

import cloudpickle

from nano_ray.driver import _TaskError
from nano_ray.transport import recv_msg, send_msg


class _RemoteObjectStoreProxy:
    """Proxy for the head node's ObjectStore, used by wait()."""

    def __init__(self, runtime: RemoteRuntime) -> None:
        self._runtime = runtime

    def contains(self, object_id: int) -> bool:
        return self._runtime._contains_object(object_id)


class RemoteRuntime:
    """Runtime that connects to a remote head node.

    Provides the same interface as driver.Runtime (submit_task, get_object)
    so that api.py can use it transparently.
    """

    def __init__(self, address: str) -> None:
        host, port_str = address.rsplit(":", 1)
        self._host = host
        self._port = int(port_str)
        self._conn: socket.socket | None = None
        self._conn_lock = threading.Lock()
        self.object_store = _RemoteObjectStoreProxy(self)

        # Local ID generator for actor_id (client-side only, not task/object IDs)
        self._next_id = 0
        self._id_lock = threading.Lock()

    def _next_unique_id(self) -> int:
        """Generate a locally unique ID (for actor handles, not task IDs)."""
        with self._id_lock:
            id_ = self._next_id
            self._next_id += 1
            return id_

    def start(self) -> None:
        """Connect to the head node."""
        self._conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._conn.connect((self._host, self._port))

    def shutdown(self) -> None:
        """Disconnect from the head node."""
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None

    def _send_recv(self, msg: Any) -> Any:
        """Send a message and receive the response (thread-safe)."""
        with self._conn_lock:
            send_msg(self._conn, msg)
            return recv_msg(self._conn)

    def submit_task(self, func: Any, args: tuple, kwargs: dict) -> int:
        """Submit a task to the head node for execution.

        Args/kwargs are serialized with cloudpickle and sent to the head.
        The head handles dependency resolution and scheduling.
        Returns the object_id for the result.
        """
        func_bytes = cloudpickle.dumps(func)
        args_bytes = cloudpickle.dumps(args)
        kwargs_bytes = cloudpickle.dumps(kwargs)

        response = self._send_recv(("submit_task", func_bytes, args_bytes, kwargs_bytes))
        if response[0] == "object_id":
            return response[1]
        raise RuntimeError(f"submit_task failed: {response}")

    def get_object(self, object_id: int, timeout: float | None = None) -> Any:
        """Blocking get for a task result from the head node."""
        # TODO: timeout is not forwarded to head (simplified)
        response = self._send_recv(("get_object", object_id))
        if response[0] == "value":
            value = cloudpickle.loads(response[1])
            if isinstance(value, _TaskError):
                raise RuntimeError(f"Task {value.task_id} failed with error:\n{value.error_msg}")
            return value
        raise RuntimeError(f"get_object failed: {response}")

    def _contains_object(self, object_id: int) -> bool:
        """Check if an object is available on the head node."""
        response = self._send_recv(("contains_object", object_id))
        if response[0] == "contains":
            return response[1]
        return False
