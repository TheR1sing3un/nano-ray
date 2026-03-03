"""Example 02: Parallel Monte Carlo estimation of Pi.

Demonstrates:
- Embarrassingly parallel workload
- Aggregating results from multiple tasks
"""

import random

import nano_ray as nanoray


@nanoray.remote
def sample_points(num_samples: int) -> int:
    """Count how many random points fall inside the unit circle."""
    count = 0
    for _ in range(num_samples):
        x = random.random()
        y = random.random()
        if x * x + y * y <= 1.0:
            count += 1
    return count


if __name__ == "__main__":
    nanoray.init(num_workers=4)

    NUM_SAMPLES = 1_000_000
    NUM_TASKS = 10
    SAMPLES_PER_TASK = NUM_SAMPLES // NUM_TASKS

    # Launch parallel sampling tasks
    refs = [sample_points.remote(SAMPLES_PER_TASK) for _ in range(NUM_TASKS)]

    # Collect results
    counts = nanoray.get(refs)
    total_inside = sum(counts)

    pi_estimate = 4.0 * total_inside / NUM_SAMPLES
    print(
        f"Pi estimate: {pi_estimate:.6f} "
        f"(from {NUM_SAMPLES:,} samples across {NUM_TASKS} tasks)"
    )
    print(f"Error: {abs(pi_estimate - 3.141592653589793):.6f}")

    nanoray.shutdown()
