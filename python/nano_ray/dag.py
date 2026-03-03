"""DAG dependency resolution utilities.

When a task's arguments include ObjectRefs (references to results of
other tasks), those ObjectRefs represent data dependencies. The task
cannot execute until all its dependencies are resolved.

This is how Ray builds implicit task DAGs: the user just passes ObjectRefs
as arguments, and the system automatically tracks and resolves dependencies.
No explicit DAG construction needed.

This module provides utilities to:
1. Extract ObjectRef dependencies from task arguments
2. Resolve arguments by replacing ObjectRefs with actual values
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from nano_ray.api import ObjectRef


def extract_dependencies(args: tuple, kwargs: dict) -> list[ObjectRef]:
    """Recursively extract all ObjectRef instances from args and kwargs.

    This determines the set of objects that must be available before
    the task can execute.
    """
    from nano_ray.api import ObjectRef

    refs: list[ObjectRef] = []
    _collect_refs(args, refs, ObjectRef)
    _collect_refs(tuple(kwargs.values()), refs, ObjectRef)
    return refs


def _collect_refs(items: tuple, refs: list, ref_type: type) -> None:
    """Recursively collect ObjectRef instances from nested structures."""
    for item in items:
        if isinstance(item, ref_type):
            refs.append(item)
        elif isinstance(item, (list, tuple)):
            _collect_refs(tuple(item), refs, ref_type)
        elif isinstance(item, dict):
            _collect_refs(tuple(item.values()), refs, ref_type)


def resolve_args(
    args: tuple, kwargs: dict, get_fn: Callable[[ObjectRef], Any]
) -> tuple[tuple, dict]:
    """Replace ObjectRefs in args/kwargs with their actual values.

    Args:
        args: Positional arguments, may contain ObjectRefs.
        kwargs: Keyword arguments, may contain ObjectRefs.
        get_fn: Callable that takes an ObjectRef and returns its value.

    Returns:
        Tuple of (resolved_args, resolved_kwargs).
    """
    from nano_ray.api import ObjectRef

    def _resolve(v: Any) -> Any:
        if isinstance(v, ObjectRef):
            return get_fn(v)
        if isinstance(v, list):
            return [_resolve(x) for x in v]
        if isinstance(v, tuple):
            return tuple(_resolve(x) for x in v)
        if isinstance(v, dict):
            return {k: _resolve(val) for k, val in v.items()}
        return v

    resolved_args = tuple(_resolve(a) for a in args)
    resolved_kwargs = {k: _resolve(v) for k, v in kwargs.items()}
    return resolved_args, resolved_kwargs
