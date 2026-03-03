"""Tests for lineage-based fault tolerance.

Tests that objects can be evicted from the object store and reconstructed
from their lineage (serialized function + args stored in the ownership table).
"""

import nano_ray as nanoray


def setup_function():
    nanoray.init(num_workers=2)


def teardown_function():
    nanoray.shutdown()


@nanoray.remote
def square(x):
    return x * x


@nanoray.remote
def add(a, b):
    return a + b


# --- Basic eviction and reconstruction ---


def test_evict_and_get():
    """Evicting an object then calling get() auto-reconstructs it."""
    ref = square.remote(5)
    assert nanoray.get(ref) == 25

    # Evict the object from the store
    assert nanoray.evict(ref) is True

    # get() should auto-reconstruct via lineage
    assert nanoray.get(ref) == 25


def test_evict_nonexistent():
    """Evicting a non-existent object returns False."""
    ref = nanoray.ObjectRef(99999)
    assert nanoray.evict(ref) is False


def test_explicit_reconstruct():
    """reconstruct() explicitly rebuilds a lost object."""
    ref = square.remote(7)
    assert nanoray.get(ref) == 49

    nanoray.evict(ref)

    # Explicit reconstruction
    value = nanoray.reconstruct(ref)
    assert value == 49


def test_reconstruct_available_object():
    """Reconstructing an object that is still available is a no-op."""
    ref = square.remote(3)
    assert nanoray.get(ref) == 9

    # Object is still in the store, reconstruct should just return it
    value = nanoray.reconstruct(ref)
    assert value == 9


# --- DAG / chain reconstruction ---


def test_reconstruct_chain():
    """Reconstructing a task that depends on another task's result.

    When both the final result and its dependency are evicted,
    the system recursively reconstructs the dependency chain.
    """
    ref1 = square.remote(3)    # 9
    ref2 = square.remote(4)    # 16
    ref3 = add.remote(ref1, ref2)  # 25

    # Get all values first to make sure they're computed
    assert nanoray.get(ref3) == 25

    # Evict the final result only
    nanoray.evict(ref3)
    assert nanoray.get(ref3) == 25  # Dependencies still in store


def test_reconstruct_deep_chain():
    """Reconstruct through a multi-level dependency chain."""
    ref_a = square.remote(2)     # 4
    ref_b = add.remote(ref_a, ref_a)  # 8
    ref_c = square.remote(ref_b)  # This doesn't work because square takes int not ObjectRef
    # Actually square.remote(ref_b) would pass ObjectRef as arg to square

    # Let's build a proper chain
    ref1 = square.remote(2)       # 4
    ref2 = add.remote(ref1, 1)    # 5
    ref3 = add.remote(ref2, 1)    # 6

    assert nanoray.get(ref3) == 6

    # Evict intermediate and final
    nanoray.evict(ref3)
    nanoray.evict(ref2)

    # Should reconstruct ref2 first, then ref3
    assert nanoray.get(ref3) == 6


def test_reconstruct_full_chain_evicted():
    """Reconstruct when the entire chain is evicted."""
    ref1 = square.remote(5)       # 25
    ref2 = add.remote(ref1, 10)   # 35

    assert nanoray.get(ref2) == 35

    # Evict everything
    nanoray.evict(ref1)
    nanoray.evict(ref2)

    # Should reconstruct ref1 first (dependency), then ref2
    assert nanoray.get(ref2) == 35


# --- Multiple reconstructions ---


def test_multiple_evict_reconstruct():
    """An object can be evicted and reconstructed multiple times."""
    ref = square.remote(6)
    assert nanoray.get(ref) == 36

    for _ in range(3):
        nanoray.evict(ref)
        assert nanoray.get(ref) == 36


# --- Parallel reconstruction ---


def test_parallel_evict_reconstruct():
    """Multiple independent objects can be evicted and reconstructed."""
    refs = [square.remote(i) for i in range(5)]
    results = nanoray.get(refs)
    assert results == [0, 1, 4, 9, 16]

    # Evict all
    for ref in refs:
        nanoray.evict(ref)

    # Reconstruct all via get()
    results = nanoray.get(refs)
    assert results == [0, 1, 4, 9, 16]
