"""Tests for multi-node cluster mode (Phase 4).

Tests the head node, worker node, and remote runtime communication
over TCP. All tests run on localhost to verify the protocol without
requiring a real network.
"""

import threading
import time

import nano_ray as nanoray
from nano_ray.head import HeadService
from nano_ray.node import WorkerNodeService
from nano_ray.remote_runtime import RemoteRuntime


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestHeadWithLocalWorkers:
    """Test head node with local workers (no remote workers)."""

    def setup_method(self):
        self.port = _find_free_port()
        self.head = HeadService(
            host="127.0.0.1", port=self.port, num_workers=2
        )
        self.head.start()
        time.sleep(0.2)  # Let server start

        # Client connects via RemoteRuntime
        nanoray.init(address=f"127.0.0.1:{self.port}")

    def teardown_method(self):
        nanoray.shutdown()
        self.head.shutdown()

    def test_basic_remote_call(self):
        """Submit a simple task via TCP and get the result."""

        @nanoray.remote
        def add(a, b):
            return a + b

        ref = add.remote(1, 2)
        assert nanoray.get(ref) == 3

    def test_parallel_tasks(self):
        """Submit multiple tasks in parallel."""

        @nanoray.remote
        def square(x):
            return x * x

        refs = [square.remote(i) for i in range(8)]
        results = nanoray.get(refs)
        assert results == [i * i for i in range(8)]

    def test_task_dag(self):
        """Task DAG with dependencies over TCP."""

        @nanoray.remote
        def double(x):
            return x * 2

        @nanoray.remote
        def add(a, b):
            return a + b

        a = double.remote(3)   # 6
        b = double.remote(4)   # 8
        c = add.remote(a, b)   # 14
        assert nanoray.get(c) == 14

    def test_error_propagation(self):
        """Task errors should be propagated to the client."""

        @nanoray.remote
        def fail():
            raise ValueError("intentional error")

        ref = fail.remote()
        try:
            nanoray.get(ref)
            assert False, "Should have raised"
        except RuntimeError as e:
            assert "intentional error" in str(e)


class TestHeadWithRemoteWorkers:
    """Test head node with a remote worker node (no local workers on head)."""

    def setup_method(self):
        self.port = _find_free_port()
        # Head with NO local workers — tasks go to remote workers only
        self.head = HeadService(
            host="127.0.0.1", port=self.port, num_workers=0
        )
        self.head.start()
        time.sleep(0.2)

        # Start a worker node connecting to head
        self.worker = WorkerNodeService(
            head_address=f"127.0.0.1:{self.port}",
            num_workers=2,
            poll_interval=0.02,
        )
        self.worker.start()
        time.sleep(0.2)

        # Client connects via RemoteRuntime
        nanoray.init(address=f"127.0.0.1:{self.port}")

    def teardown_method(self):
        nanoray.shutdown()
        self.worker.shutdown()
        self.head.shutdown()

    def test_remote_worker_execution(self):
        """Tasks should be executed by the remote worker node."""

        @nanoray.remote
        def multiply(a, b):
            return a * b

        ref = multiply.remote(6, 7)
        assert nanoray.get(ref) == 42

    def test_remote_worker_parallel(self):
        """Multiple tasks on remote workers."""

        @nanoray.remote
        def cube(x):
            return x ** 3

        refs = [cube.remote(i) for i in range(6)]
        results = nanoray.get(refs)
        assert results == [i ** 3 for i in range(6)]

    def test_remote_worker_dag(self):
        """Task DAG with remote workers."""

        @nanoray.remote
        def inc(x):
            return x + 1

        ref = inc.remote(0)
        for _ in range(4):
            ref = inc.remote(ref)
        assert nanoray.get(ref) == 5


class TestTransportProtocol:
    """Test the TCP transport protocol directly."""

    def test_send_recv(self):
        """Basic send/recv message roundtrip."""
        import socket

        from nano_ray.transport import recv_msg, send_msg

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.listen(1)

        received = []

        def server_fn():
            conn, _ = server.accept()
            msg = recv_msg(conn)
            received.append(msg)
            send_msg(conn, {"reply": msg})
            conn.close()
            server.close()

        t = threading.Thread(target=server_fn, daemon=True)
        t.start()

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.connect(("127.0.0.1", port))
        send_msg(client, {"hello": "world", "num": 42})
        reply = recv_msg(client)
        client.close()
        t.join(timeout=2)

        assert received == [{"hello": "world", "num": 42}]
        assert reply == {"reply": {"hello": "world", "num": 42}}

    def test_large_message(self):
        """Test sending a large message."""
        import socket

        from nano_ray.transport import recv_msg, send_msg

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.listen(1)

        large_data = list(range(100_000))
        received = []

        def server_fn():
            conn, _ = server.accept()
            msg = recv_msg(conn)
            received.append(msg)
            send_msg(conn, "ok")
            conn.close()
            server.close()

        t = threading.Thread(target=server_fn, daemon=True)
        t.start()

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.connect(("127.0.0.1", port))
        send_msg(client, large_data)
        reply = recv_msg(client)
        client.close()
        t.join(timeout=5)

        assert received == [large_data]
        assert reply == "ok"
