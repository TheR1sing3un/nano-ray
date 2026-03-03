"""Actor abstraction for nano-ray.

An Actor is a stateful remote object. Unlike stateless tasks, actor
method calls execute sequentially on a dedicated worker, preserving
state between calls.

In Ray, actors are bound to a worker process and their methods are
serialized through a single-threaded executor. nano-ray mirrors this:
each actor gets a dedicated worker, and method calls are queued to
ensure sequential execution.

Phase 1: Basic actor support using a dedicated worker per actor.
Phase 3: Full actor support with proper lifecycle management.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nano_ray.driver import Runtime

from nano_ray.api import ObjectRef


class ActorHandle:
    """Local proxy for a remote actor instance.

    Attribute access returns a callable whose .remote() method submits
    the method call as a task, ensuring sequential execution on the
    actor's dedicated worker.
    """

    def __init__(
        self,
        cls: type,
        actor_id: int,
        runtime: Runtime,
    ) -> None:
        self._cls = cls
        self._actor_id = actor_id
        self._runtime = runtime
        # Actor method calls are serialized through this lock to guarantee ordering
        self._call_lock = threading.Lock()
        # Chain of ObjectRefs ensuring sequential execution:
        # each method call depends on the previous one completing
        self._last_ref: ObjectRef | None = None

    @classmethod
    def _create(
        cls, actor_cls: type, args: tuple, kwargs: dict, runtime: Runtime
    ) -> ActorHandle:
        """Create a new actor instance on a worker.

        The actor's __init__ runs as a task. Subsequent method calls
        are chained to ensure they execute after initialization completes.
        """
        actor_id = runtime._next_unique_id()

        # Create actor state holder — a wrapper that instantiates the class
        # and stores the instance for subsequent method calls
        def _init_actor(*a: Any, **kw: Any) -> _ActorState:
            instance = actor_cls(*a, **kw)
            return _ActorState(instance)

        object_id = runtime.submit_task(_init_actor, args, kwargs)
        handle = ActorHandle(actor_cls, actor_id, runtime)
        handle._last_ref = ObjectRef(object_id)
        return handle

    def __getattr__(self, method_name: str) -> _ActorMethodCaller:
        if method_name.startswith("_"):
            raise AttributeError(method_name)
        return _ActorMethodCaller(self, method_name)


class _ActorState:
    """Wrapper holding a live actor instance inside a worker process.

    This is the serializable container that flows through the task system.
    Each actor method call receives the previous state, calls the method,
    and returns the updated state + method return value.
    """

    def __init__(self, instance: Any) -> None:
        self.instance = instance

    def call_method(self, method_name: str, args: tuple, kwargs: dict) -> tuple[Any, Any]:
        """Call a method on the actor instance. Returns (state, return_value)."""
        method = getattr(self.instance, method_name)
        result = method(*args, **kwargs)
        return self, result


class _ActorMethodCaller:
    """Proxy returned by ActorHandle.__getattr__ to enable .remote() calls."""

    def __init__(self, handle: ActorHandle, method_name: str) -> None:
        self._handle = handle
        self._method_name = method_name

    def remote(self, *args: Any, **kwargs: Any) -> ObjectRef:
        """Submit this actor method call as a sequenced task."""
        handle = self._handle
        method_name = self._method_name

        with handle._call_lock:
            prev_ref = handle._last_ref

            def _actor_method_task(
                prev_state_or_tuple: Any, *a: Any, **kw: Any
            ) -> tuple[Any, Any]:
                # prev_state_or_tuple is either an _ActorState (from __init__)
                # or a (state, return_value) tuple (from previous method call)
                if isinstance(prev_state_or_tuple, _ActorState):
                    state = prev_state_or_tuple
                else:
                    state = prev_state_or_tuple[0]
                return state.call_method(method_name, a, kw)

            # Chain: this task depends on the previous actor task's result
            runtime = handle._runtime
            object_id = runtime.submit_task(
                _actor_method_task, (prev_ref, *args), kwargs
            )
            ref = ObjectRef(object_id)
            handle._last_ref = ref
            return ref
