"""Example 03: Task DAG / pipeline.

Demonstrates:
- Passing ObjectRefs as arguments to create task dependencies
- Diamond DAG pattern (multiple inputs → single output)
- Deep task chains (sequential pipeline)

This is Ray's key insight: the user just passes ObjectRefs as arguments,
and the system automatically builds the dependency graph. No explicit
DAG construction is needed.
"""

import nano_ray as nanoray


@nanoray.remote
def square(x):
    return x * x


@nanoray.remote
def add(a, b):
    return a + b


@nanoray.remote
def multiply(a, b):
    return a * b


if __name__ == "__main__":
    nanoray.init(num_workers=2)

    # --- Diamond DAG ---
    # square(3) = 9  ─┐
    #                  ├─> add(9, 16) = 25
    # square(4) = 16 ─┘
    ref1 = square.remote(3)
    ref2 = square.remote(4)
    ref3 = add.remote(ref1, ref2)
    print(f"square(3) + square(4) = {nanoray.get(ref3)}")  # 25

    # --- Deep chain ---
    # 1 -> +1 -> +1 -> +1 -> +1 -> +1 = 6
    @nanoray.remote
    def increment(x):
        return x + 1

    ref = increment.remote(1)
    for _ in range(4):
        ref = increment.remote(ref)
    print(f"1 + 5 increments = {nanoray.get(ref)}")  # 6

    # --- Complex DAG ---
    #           square(2) = 4  ──┐
    #                            ├─> add(4, 9) = 13 ──┐
    #           square(3) = 9  ──┘                     ├─> multiply(13, 25) = 325
    # add(square(3), square(4)) = 25 ─────────────────┘
    a = square.remote(2)
    b = square.remote(3)
    c = square.remote(4)
    ab = add.remote(a, b)
    bc = add.remote(b, c)
    result = multiply.remote(ab, bc)
    print(f"Complex DAG result = {nanoray.get(result)}")  # (4+9) * (9+16) = 325

    # --- Verification ---
    assert nanoray.get(ref3) == 25
    assert nanoray.get(ref) == 6
    assert nanoray.get(result) == 325
    print("All checks passed!")

    nanoray.shutdown()
