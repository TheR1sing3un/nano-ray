"""Benchmark: Task submission throughput.

Measures how many tasks per second nano-ray can submit and complete,
comparing the Rust backend vs Python fallback.

Usage:
    python benchmarks/bench_throughput.py
"""

from __future__ import annotations

import time

import nano_ray as nanoray


@nanoray.remote
def noop():
    """Minimal task — measures scheduling overhead, not computation."""
    return 1


@nanoray.remote
def compute(n: int) -> float:
    """Light compute task — sum of squares."""
    return sum(i * i for i in range(n))


def bench_submit_and_get(num_tasks: int, label: str) -> dict:
    """Benchmark: submit num_tasks noop tasks, then get all results."""
    # Warm up
    ref = noop.remote()
    nanoray.get(ref)

    # Submit phase
    t0 = time.perf_counter()
    refs = [noop.remote() for _ in range(num_tasks)]
    t_submit = time.perf_counter() - t0

    # Get phase
    t1 = time.perf_counter()
    results = nanoray.get(refs)
    t_get = time.perf_counter() - t1

    t_total = t_submit + t_get

    return {
        "label": label,
        "num_tasks": num_tasks,
        "submit_time": t_submit,
        "get_time": t_get,
        "total_time": t_total,
        "submit_throughput": num_tasks / t_submit,
        "total_throughput": num_tasks / t_total,
    }


def bench_compute_tasks(num_tasks: int, work_size: int, label: str) -> dict:
    """Benchmark: submit compute tasks with actual work."""
    refs = [compute.remote(work_size) for _ in range(num_tasks)]
    t0 = time.perf_counter()
    results = nanoray.get(refs)
    t_total = time.perf_counter() - t0

    return {
        "label": label,
        "num_tasks": num_tasks,
        "work_size": work_size,
        "total_time": t_total,
        "throughput": num_tasks / t_total,
    }


def print_result(r: dict) -> None:
    """Pretty-print a benchmark result."""
    print(f"\n  {r['label']}:")
    print(f"    Tasks: {r['num_tasks']}")
    if "submit_throughput" in r:
        print(f"    Submit: {r['submit_time']:.3f}s ({r['submit_throughput']:.0f} tasks/s)")
        print(f"    Get:    {r['get_time']:.3f}s")
        print(f"    Total:  {r['total_time']:.3f}s ({r['total_throughput']:.0f} tasks/s)")
    else:
        print(f"    Work size: {r['work_size']}")
        print(f"    Total: {r['total_time']:.3f}s ({r['throughput']:.0f} tasks/s)")


if __name__ == "__main__":
    print("=" * 60)
    print("nano-ray Task Throughput Benchmark")
    print("=" * 60)

    # Check backend
    backend = "Rust" if nanoray._RUST_AVAILABLE else "Python fallback"
    print(f"\nBackend: {backend}")

    nanoray.init(num_workers=4)

    # Noop tasks (measures scheduling overhead)
    print("\n--- Noop tasks (scheduling overhead) ---")
    for n in [100, 500, 1000]:
        r = bench_submit_and_get(n, f"{n} noop tasks")
        print_result(r)

    # Compute tasks (measures parallelism benefit)
    print("\n--- Compute tasks (parallelism) ---")
    for n in [10, 50, 100]:
        r = bench_compute_tasks(n, 10000, f"{n} compute tasks (work=10K)")
        print_result(r)

    nanoray.shutdown()
    print("\n" + "=" * 60)
