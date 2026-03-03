"""Example 01: Basic @remote usage.

Demonstrates:
- Initializing nano-ray
- Decorating a function with @remote
- Submitting tasks and retrieving results
- Parallel execution
"""

import nano_ray as nanoray


@nanoray.remote
def square(x):
    return x * x


@nanoray.remote
def add(a, b):
    return a + b


if __name__ == "__main__":
    nanoray.init(num_workers=2)

    # --- Single remote call ---
    ref = square.remote(5)
    print(f"square(5) = {nanoray.get(ref)}")  # 25

    # --- Parallel calls ---
    refs = [add.remote(i, i) for i in range(10)]
    results = nanoray.get(refs)
    print(f"add(i, i) for i in range(10) = {results}")
    # [0, 2, 4, 6, 8, 10, 12, 14, 16, 18]

    # --- Verification ---
    assert results == [0, 2, 4, 6, 8, 10, 12, 14, 16, 18]
    print("All checks passed!")

    nanoray.shutdown()
