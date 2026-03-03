"""Benchmark: Object Store read/write latency.

Measures put/get/get_or_wait performance for the ObjectStore,
comparing Rust backend vs Python fallback.

Usage:
    python benchmarks/bench_object_store.py
"""

from __future__ import annotations

import threading
import time


def bench_backend(name: str, ObjectStore: type) -> None:
    """Run benchmarks on a specific ObjectStore implementation."""
    store = ObjectStore()

    print(f"\n--- {name} ---")

    # Benchmark: sequential put
    n = 10000
    t0 = time.perf_counter()
    for i in range(n):
        store.put(i, f"value-{i}")
    t_put = time.perf_counter() - t0
    print(f"  put() x {n}: {t_put:.4f}s ({n / t_put:.0f} ops/s)")

    # Benchmark: sequential get_or_wait (already available — non-blocking path)
    t0 = time.perf_counter()
    for i in range(n):
        store.get_or_wait(i)
    t_get = time.perf_counter() - t0
    print(f"  get_or_wait() (ready) x {n}: {t_get:.4f}s ({n / t_get:.0f} ops/s)")

    # Benchmark: contains
    t0 = time.perf_counter()
    for i in range(n):
        store.contains(i)
    t_contains = time.perf_counter() - t0
    print(f"  contains() x {n}: {t_contains:.4f}s ({n / t_contains:.0f} ops/s)")

    # Benchmark: get_or_wait with delayed put (measures notification latency)
    store2 = ObjectStore()
    delays = 100
    latencies = []

    def delayed_put(store, obj_id, delay):
        time.sleep(delay)
        store.put(obj_id, "delayed-value")

    for i in range(delays):
        obj_id = n + i
        t = threading.Thread(target=delayed_put, args=(store2, obj_id, 0.001))
        t0 = time.perf_counter()
        t.start()
        store2.get_or_wait(obj_id)
        latency = time.perf_counter() - t0
        latencies.append(latency)
        t.join()

    avg_latency = sum(latencies) / len(latencies) * 1000  # ms
    p50 = sorted(latencies)[len(latencies) // 2] * 1000
    p99 = sorted(latencies)[int(len(latencies) * 0.99)] * 1000
    print(f"  get_or_wait() (delayed) x {delays}: avg={avg_latency:.2f}ms p50={p50:.2f}ms p99={p99:.2f}ms")


if __name__ == "__main__":
    print("=" * 60)
    print("nano-ray Object Store Benchmark")
    print("=" * 60)

    # Always benchmark Python fallback
    from nano_ray._fallback.object_store import ObjectStore as PyObjectStore

    bench_backend("Python fallback (dict + threading.Event)", PyObjectStore)

    # Try Rust backend
    try:
        from nano_ray._core import ObjectStore as RustObjectStore

        bench_backend("Rust backend (DashMap + Condvar)", RustObjectStore)
    except ImportError:
        print("\n  Rust backend not available, skipping.")

    print("\n" + "=" * 60)
