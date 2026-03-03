"""Actor abstraction for nano-ray.

An Actor is a stateful remote object. Unlike stateless tasks, actor
method calls execute sequentially on a dedicated worker, preserving
state between calls.

In Ray, actors are bound to a worker process and their methods are
serialized through a single-threaded executor. nano-ray ensures
sequential execution by chaining method calls through ObjectRef
dependencies: each call depends on the previous call's result.

SIMPLIFICATION: Ray binds actors to specific workers and pins them.
nano-ray doesn't pin actors — instead, the actor state flows through
the task system as a serialized _ActorState. Each method call receives
the previous state, mutates it, and returns the updated state.
This means the state is serialized/deserialized on every call (slower
than Ray's pinned approach) but is simpler and works with the existing
task infrastructure.
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
    the method call as a task, ensuring sequential execution via
    ObjectRef dependency chaining.
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
        # Lock ensures method calls are chained in submission order
        self._call_lock = threading.Lock()
        # Chain of ObjectRefs: each method call depends on the previous one.
        # Points to a task that returns _ActorState (init) or
        # (_ActorState, return_value) tuple (method call).
        self._last_ref: ObjectRef | None = None

    @classmethod
    def _create(
        cls, actor_cls: type, args: tuple, kwargs: dict, runtime: Runtime
    ) -> ActorHandle:
        """Create a new actor instance on a worker.

        The actor's __init__ runs as a task that returns an _ActorState.
        Subsequent method calls are chained to ensure sequential execution.
        """
        actor_id = runtime._next_unique_id()

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

    The state flows through the task system: each method call receives
    the previous state, calls the method, and returns updated state +
    method return value as a tuple.
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
        """Submit this actor method call as a sequenced task.

        Creates two tasks:
        1. The actual method call (returns state + value tuple, for chaining)
        2. A value extractor (returns just the value, for the user)

        This separation ensures the actor state flows through the chain
        while the user only sees the return value.
        """
        handle = self._handle
        method_name = self._method_name

        with handle._call_lock:
            prev_ref = handle._last_ref

            # Task 1: call the actor method (returns (state, value) tuple)
            def _actor_method_task(
                prev_state_or_tuple: Any, *a: Any, **kw: Any
            ) -> tuple[Any, Any]:
                if isinstance(prev_state_or_tuple, _ActorState):
                    state = prev_state_or_tuple
                else:
                    state = prev_state_or_tuple[0]
                return state.call_method(method_name, a, kw)

            runtime = handle._runtime
            chain_object_id = runtime.submit_task(
                _actor_method_task, (prev_ref, *args), kwargs
            )
            chain_ref = ObjectRef(chain_object_id)
            handle._last_ref = chain_ref  # Next call depends on this

            # Task 2: extract just the return value for the user
            def _extract_return_value(state_and_value: tuple) -> Any:
                return state_and_value[1]

            user_object_id = runtime.submit_task(
                _extract_return_value, (chain_ref,), {}
            )
            return ObjectRef(user_object_id)
