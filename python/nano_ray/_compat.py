"""Cross-platform compatibility utilities for nano-ray.

Handles platform differences such as multiprocessing start methods:
- Unix (Linux/macOS): prefers "fork" for fast child process creation
- Windows: only supports "spawn" (fork is not available)
- macOS Python 3.14+: "fork" is deprecated, will default to "spawn"
"""

from __future__ import annotations

import multiprocessing
import sys
import warnings


def mp_context() -> multiprocessing.context.BaseContext:
    """Return a multiprocessing context appropriate for the current platform.

    On Unix, uses "fork" for performance (child inherits parent memory).
    On Windows, uses "spawn" (the only available option).

    Ray also uses fork on Linux. On macOS Python 3.12+ this emits a
    DeprecationWarning about fork in multi-threaded processes, which
    we suppress since nano-ray carefully manages thread/process lifetimes.
    """
    if sys.platform == "win32":
        return multiprocessing.get_context("spawn")

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        return multiprocessing.get_context("fork")
