"""User-facing API for nano-ray.

This module provides the core API that users interact with:
- init() / shutdown(): runtime lifecycle
- @remote: decorator to make functions/classes remotely callable
- get(): blocking retrieval of task results
- wait(): wait for a subset of results

The API mirrors Ray's interface for familiarity.
"""

from __future__ import annotations

import time
from typing import Any

from nano_ray.driver import Runtime

# Global runtime instance — set by init(), cleared by shutdown()
_runtime: Runtime | None = None


class ObjectRef:
    """Reference to a remote object (task result).

    Analogous to Ray's ObjectRef. This is a future-like handle that
    represents the result of a remote task. The actual value lives in
    the ObjectStore and can be retrieved with nanoray.get().

    ObjectRefs can be passed as arguments to other remote functions,
    creating implicit task dependencies (DAG).
    """

    def __init__(self, object_id: int) -> None:
        self._object_id = object_id

    @property
    def object_id(self) -> int:
        return self._object_id

    def __repr__(self) -> str:
        return f"ObjectRef({self._object_id})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ObjectRef) and self._object_id == other._object_id

    def __hash__(self) -> int:
        return hash(self._object_id)


class RemoteFunction:
    """Wrapper for a function decorated with @remote.

    Provides a .remote() method that submits the function as a
    distributed task and returns an ObjectRef.
    """

    def __init__(self, func: Any) -> None:
        self._func = func
        self.__name__ = func.__name__
        self.__qualname__ = func.__qualname__
        self.__module__ = getattr(func, "__module__", "")

    def remote(self, *args: Any, **kwargs: Any) -> ObjectRef:
        """Submit this function as a remote task."""
        if _runtime is None:
            raise RuntimeError("nano-ray is not initialized. Call nanoray.init() first.")
        object_id = _runtime.submit_task(self._func, args, kwargs)
        return ObjectRef(object_id)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Direct local call (useful for testing/debugging)."""
        return self._func(*args, **kwargs)


class RemoteClass:
    """Wrapper for a class decorated with @remote.

    Provides a .remote() method that creates a remote actor instance.
    Actor support is implemented in Phase 3.
    """

    def __init__(self, cls: type) -> None:
        self._cls = cls
        self.__name__ = cls.__name__
        self.__qualname__ = cls.__qualname__
        self.__module__ = getattr(cls, "__module__", "")

    def remote(self, *args: Any, **kwargs: Any) -> Any:
        """Create a remote actor instance."""
        if _runtime is None:
            raise RuntimeError("nano-ray is not initialized. Call nanoray.init() first.")
        from nano_ray.actor import ActorHandle

        return ActorHandle._create(self._cls, args, kwargs, _runtime)


def remote(func_or_class: Any) -> RemoteFunction | RemoteClass:
    """Decorator to make a function or class remotely callable.

    Usage:
        @nanoray.remote
        def f(x):
            return x + 1

        @nanoray.remote
        class Counter:
            def __init__(self):
                self.n = 0
            def increment(self):
                self.n += 1
                return self.n
    """
    if isinstance(func_or_class, type):
        return RemoteClass(func_or_class)
    return RemoteFunction(func_or_class)


def init(num_workers: int = 4, address: str | None = None) -> None:
    """Initialize the nano-ray runtime.

    Args:
        num_workers: Number of local worker processes to spawn.
        address: Remote head node address (Phase 4+, not yet supported).
    """
    global _runtime
    if _runtime is not None:
        raise RuntimeError("nano-ray is already initialized. Call shutdown() first.")
    if address is not None:
        raise NotImplementedError("Multi-node mode is not yet supported (Phase 4).")
    _runtime = Runtime(num_workers=num_workers)
    _runtime.start()


def shutdown() -> None:
    """Shut down the nano-ray runtime and clean up all resources."""
    global _runtime
    if _runtime is None:
        return
    _runtime.shutdown()
    _runtime = None


def get(
    refs: ObjectRef | list[ObjectRef], timeout: float | None = None
) -> Any | list[Any]:
    """Blocking retrieval of one or more ObjectRefs.

    Args:
        refs: A single ObjectRef or a list of ObjectRefs.
        timeout: Maximum seconds to wait per object (None = wait forever).

    Returns:
        The value for a single ObjectRef, or a list of values for a list.
    """
    if _runtime is None:
        raise RuntimeError("nano-ray is not initialized. Call nanoray.init() first.")

    if isinstance(refs, list):
        return [_runtime.get_object(ref.object_id, timeout=timeout) for ref in refs]

    return _runtime.get_object(refs.object_id, timeout=timeout)


def wait(
    refs: list[ObjectRef],
    num_returns: int = 1,
    timeout: float | None = None,
) -> tuple[list[ObjectRef], list[ObjectRef]]:
    """Wait for at least num_returns results to be ready.

    Args:
        refs: List of ObjectRefs to wait on.
        num_returns: Minimum number of results to wait for.
        timeout: Maximum seconds to wait.

    Returns:
        Tuple of (ready_refs, remaining_refs).
    """
    if _runtime is None:
        raise RuntimeError("nano-ray is not initialized. Call nanoray.init() first.")

    ready: list[ObjectRef] = []
    remaining = list(refs)
    deadline = time.monotonic() + timeout if timeout is not None else None

    while len(ready) < num_returns:
        still_remaining: list[ObjectRef] = []
        for ref in remaining:
            if _runtime.object_store.contains(ref.object_id):
                ready.append(ref)
            else:
                still_remaining.append(ref)
        remaining = still_remaining

        if len(ready) >= num_returns:
            break
        if deadline is not None and time.monotonic() >= deadline:
            break

        time.sleep(0.01)  # Avoid busy-waiting

    return ready, remaining
