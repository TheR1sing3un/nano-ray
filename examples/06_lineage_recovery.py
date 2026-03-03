"""Example 06: Lineage-Based Fault Tolerance.

Demonstrates:
- Object eviction (simulating data loss)
- Automatic reconstruction via lineage re-execution
- Recursive reconstruction of dependency chains
- The core insight: storing "how to recompute" instead of checkpointing data

In real Ray, objects are evicted automatically under memory pressure or
when a node fails. nano-ray provides manual evict() for educational
purposes to observe the reconstruction process.
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

    # === Part 1: Basic eviction and reconstruction ===
    print("=== Part 1: Basic eviction and auto-reconstruction ===")

    ref = square.remote(7)
    print(f"square(7) = {nanoray.get(ref)}")  # 49

    # Simulate data loss by evicting the object from the store
    nanoray.evict(ref)
    print("Object evicted from store!")

    # get() automatically detects the missing object and reconstructs
    # it by re-executing the producer task from its stored lineage
    result = nanoray.get(ref)
    print(f"After reconstruction: square(7) = {result}")
    assert result == 49
    print()

    # === Part 2: Explicit reconstruction ===
    print("=== Part 2: Explicit reconstruction ===")

    ref = square.remote(5)
    print(f"square(5) = {nanoray.get(ref)}")

    nanoray.evict(ref)
    result = nanoray.reconstruct(ref)
    print(f"Explicitly reconstructed: square(5) = {result}")
    assert result == 25
    print()

    # === Part 3: Recursive chain reconstruction ===
    print("=== Part 3: Reconstructing a dependency chain ===")
    print("Building: add(square(3), square(4)) = add(9, 16) = 25")

    ref_a = square.remote(3)   # → 9
    ref_b = square.remote(4)   # → 16
    ref_c = add.remote(ref_a, ref_b)  # → 25
    print(f"Result: {nanoray.get(ref_c)}")

    # Evict ALL objects in the chain
    nanoray.evict(ref_a)
    nanoray.evict(ref_b)
    nanoray.evict(ref_c)
    print("Evicted all 3 objects from the chain!")

    # get() recursively reconstructs: first ref_a and ref_b, then ref_c
    result = nanoray.get(ref_c)
    print(f"After full chain reconstruction: {result}")
    assert result == 25
    print()

    # === Part 4: Repeated eviction and reconstruction ===
    print("=== Part 4: Repeated eviction (lineage is permanent) ===")

    ref = square.remote(10)
    for i in range(3):
        val = nanoray.get(ref)
        print(f"  Round {i + 1}: get() = {val}")
        assert val == 100
        nanoray.evict(ref)

    print()
    print("All checks passed!")
    print()
    print("Key insight: nano-ray stores 'how to recompute' (lineage) rather")
    print("than checkpointing data. This is Ray's approach to fault tolerance:")
    print("  object lost → find producer task → re-execute from lineage")

    nanoray.shutdown()
