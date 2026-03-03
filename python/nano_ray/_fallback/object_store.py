"""Pure Python fallback: Object Store.

The Object Store holds immutable task results and supports blocking waits
for objects that haven't been created yet.

In Ray, every node has a Plasma-based shared-memory object store enabling
zero-copy reads across workers on the same node. nano-ray simplifies this
to a thread-safe dict in the driver process.

SIMPLIFICATION: No shared memory, no memory limit enforcement, no spilling
to disk. Objects are plain Python values protected by a lock.
"""

from __future__ import annotations

import threading
from typing import Any


class ObjectStore:
    """Thread-safe in-memory object store with blocking get support."""

    def __init__(self) -> None:
        self._store: dict[int, Any] = {}
        self._events: dict[int, threading.Event] = {}
        self._lock = threading.Lock()

    def put(self, object_id: int, value: Any) -> None:
        """Store an immutable object and wake up any waiters."""
        with self._lock:
            self._store[object_id] = value
            event = self._events.get(object_id)
        # Signal outside the lock to avoid holding it while waking waiters
        if event is not None:
            event.set()

    def get(self, object_id: int) -> tuple[bool, Any]:
        """Non-blocking get. Returns (found, value)."""
        with self._lock:
            if object_id in self._store:
                return True, self._store[object_id]
        return False, None

    def get_or_wait(self, object_id: int, timeout: float | None = None) -> Any:
        """Blocking get. Waits until the object is available or timeout expires."""
        # Fast path: already available
        with self._lock:
            if object_id in self._store:
                return self._store[object_id]
            if object_id not in self._events:
                self._events[object_id] = threading.Event()
            event = self._events[object_id]

        if not event.wait(timeout=timeout):
            raise TimeoutError(f"Timed out waiting for object {object_id}")

        # Object must be available now
        return self._store[object_id]

    def contains(self, object_id: int) -> bool:
        """Check if an object exists (non-blocking)."""
        return object_id in self._store

    def delete(self, object_id: int) -> bool:
        """Remove an object. Returns True if it existed."""
        with self._lock:
            removed = self._store.pop(object_id, None) is not None
            self._events.pop(object_id, None)
            return removed

    def size(self) -> int:
        """Number of stored objects."""
        return len(self._store)
