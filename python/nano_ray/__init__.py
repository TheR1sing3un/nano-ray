"""nano-ray: A minimal distributed computing framework inspired by Ray."""

from nano_ray.api import ObjectRef, evict, get, init, reconstruct, remote, shutdown, wait

try:
    from nano_ray._core import hello as _hello

    _RUST_AVAILABLE = True
except ImportError:
    _RUST_AVAILABLE = False


def hello() -> str:
    """Verify that the nano-ray runtime is available."""
    if _RUST_AVAILABLE:
        return _hello()
    return "nano-ray is alive! (pure Python fallback)"


__all__ = [
    "init", "shutdown", "remote", "get", "wait",
    "evict", "reconstruct",
    "ObjectRef", "hello",
]
